"""子 Agent 节点：基于 bind_tools 的函数调用（ReAct）范式。

每个子 Agent 在「自己的工具命名空间」内：
  1. 用 llm.bind_tools(本命名空间工具) 让模型自主决定调用哪些工具；
  2. 执行工具（ToolMessage 回填）；
  3. 若涉及敏感动作（锁号/结算/转诊/120），触发 interrupt() 人工审核门；
  4. 工具结果写入 state["tool_result"]，交给 final_answer 流式汇总。

fake 模式下 FakeLLM 会确定性地返回该命名空间的 tool_calls，因此无需任何 API 即可演示
「LLM 自主选工具 → 执行 → 敏感动作人工确认」的完整链路。
"""

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.types import interrupt

from .context import patient_ctx, thread_ctx
from .db import clear_pending, is_db_enabled, pop_pending, set_pending
from .llm import FakeLLM, acompose, get_llm
from .memory import append_note
from .state_utils import last_human
from .tools import NAMESPACES

# 每个子 Agent 的系统提示（告诉模型它的职责与可选工具）
SYSTEM_PROMPTS = {
    "triage": "你是分诊助手。根据患者主诉，调用 search_department / dept_map_rag 给出建议科室。",
    "booking": "你是挂号助手。当用户要挂号并办理医保结算时，必须在【同一条】回复里同时调用 lock_appointment 和 medicare_settle（两者都是敏感动作，会一起等待人工批准）；不要分两次、也不要只调其中一个。可先调用 query_availability 查号源（非敏感，会立即执行）。\n关键：不要向患者反问确认——是否放行由医护端人工审核门决定。若用户未指定具体时段/号源，直接锁定该科室当天第一个可用号源（上午优先）并立即办理医保结算，具体号源细节由人工审核时确认。直接执行工具调用。",
    "intake": "你是诊前问诊助手。调用 read_lab_report / clinical_kb 解读报告与临床指引。",
    "followup": "你是慢病随访助手。调用 read_vitals / plan_reminder / memory_append 完成随访动作。",
    "emergency": "你是应急转诊助手。命中红线后需转接急诊，handoff / call_120 必须人工确认。",
}

# 敏感工具：执行前必须走 interrupt 人工审核门（AI 不直接放行）
SENSITIVE_TOOLS = {"lock_appointment", "medicare_settle", "handoff", "call_120"}

# 人工审核门在审批存储里的 action 标识（也用于测试断言 / 前端展示）
APPROVAL_ACTION = {
    "booking": "lock_and_settle",
    "emergency": "emergency_handoff",
    "intake": "tool_approval",
    "triage": "tool_approval",
    "followup": "tool_approval",
}


# 待人工审批的敏感工具调用缓存（按「患者+意图」稳定 key）。
# 为什么需要：LangGraph 的 interrupt() 在 resume 时把节点从头重跑；若重跑时真实 LLM
# 不再生成敏感工具调用，会导致「已批准却没执行」。故把待审批的 tool_calls 缓存，
# resume 重跑直接执行，保证落库确定性。
# 持久化：有 DATABASE_URL 时落 pending_calls 表（跨进程/重启不丢），否则内存兜底。
class _PendingStore:
    def __init__(self) -> None:
        self._mem: dict[str, list] = {}

    def pop(self, key, default=None):
        if is_db_enabled():
            try:
                v = pop_pending(key)
                return v if v is not None else default
            except Exception:
                return self._mem.pop(key, default)
        return self._mem.pop(key, default)

    def __setitem__(self, key, value):
        if is_db_enabled():
            try:
                set_pending(key, value)
                return
            except Exception:
                pass
        self._mem[key] = value

    def clear(self):
        if is_db_enabled():
            try:
                clear_pending()
                return
            except Exception:
                pass
        self._mem.clear()


_PENDING = _PendingStore()


async def run_agent_with_tools(llm, tools, state, system, sensitive_tools, approval_action):
    """通用 ReAct：bind_tools → 执行 → 敏感动作 interrupt（人工审核门）。

    HITL 设计：非敏感工具（如查号源）即时执行；敏感工具（锁号/结算/转诊/120）
    先「累积」不立即执行，等 LLM 本轮工具调用结束后再「统一一次」interrupt 审批，
    resume 后一次性执行全部敏感动作 —— 医生只需批准一次（lock+settle 一起批），
    也避免 LLM 分步调用敏感工具时被提前挂起导致后续动作丢失。
    """
    llm_bound = llm.bind_tools(tools)
    # 给 fake 模型提供上下文 hint，使其能确定性地选出本命名空间工具
    if isinstance(llm_bound, FakeLLM):
        llm_bound.intent_hint = state.get("intent")
        llm_bound.patient_id_hint = state.get("patient_id")
        llm_bound.human_hint = last_human(state)

    tool_map = {t.name: t for t in tools}
    msgs = [SystemMessage(content=system), *state["messages"]]
    # 稳定 key：用会话线程 ID（resume 重跑同一线程，故稳定）；缺失时回退患者+意图
    cache_key = thread_ctx.get() or f"{patient_ctx.get()}:{state.get('intent')}"

    # —— resume 重跑：直接执行之前缓存的敏感工具调用（不依赖 LLM 重新生成） ——
    pending = _PENDING.pop(cache_key, None)
    if pending is not None:
        decision = interrupt(
            {
                "action": approval_action,
                "intent": state.get("intent"),
                "tools": [c["name"] for c in pending if c["name"] in sensitive_tools],
            }
        )
        if not (isinstance(decision, dict) and decision.get("approved")):
            return [AIMessage(content="操作已被人工拒绝。")], "操作未获人工批准，已取消。"
        ai_msg = AIMessage(content="", tool_calls=pending)
        tool_msgs = []
        parts = []
        for c in pending:
            fn = tool_map.get(c["name"])
            if not fn:
                continue
            out = fn.invoke(c.get("args", {}) or {})
            tool_msgs.append(ToolMessage(content=str(out), tool_call_id=c["id"]))
            parts.append(str(out))
        return [ai_msg, *tool_msgs], "\n".join(parts)

    # —— 首次运行：LLM 选工具；敏感动作累积，工具轮结束后再统一审批 ——
    collected = []
    tool_result_parts = []
    pending_sensitive: list = []
    MAX_STEPS = 4

    for _ in range(MAX_STEPS):
        ai = await llm_bound.ainvoke(msgs + collected)
        collected.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            # 模型认为无需再调工具 → 结束工具轮
            break

        # 敏感动作：先累积，留到审批后统一执行（本轮不执行、不挂起）
        for c in calls:
            if c["name"] in sensitive_tools:
                pending_sensitive.append(c)
                # 补占位 ToolMessage，保证「带 tool_calls 的助手消息必须有对应回复」，
                # 否则下一轮把孤儿 tool_call 发给模型会报 400；真实执行结果在 resume 后产出
                collected.append(
                    ToolMessage(
                        content="[工具调用已记录，尚未执行，等待人工审批；请继续调用本意图所需的其它工具]",
                        tool_call_id=c["id"],
                    )
                )

        # 非敏感工具：即时执行（等价于图内的 ToolNode）
        for c in calls:
            if c["name"] in sensitive_tools:
                continue
            fn = tool_map.get(c["name"])
            if not fn:
                continue
            out = fn.invoke(c.get("args", {}) or {})
            collected.append(ToolMessage(content=str(out), tool_call_id=c["id"]))
            tool_result_parts.append(str(out))

    # 工具轮结束：若有敏感动作，统一挂起等待一次人工审批
    if pending_sensitive:
        _PENDING[cache_key] = pending_sensitive
        decision = interrupt(
            {
                "action": approval_action,
                "intent": state.get("intent"),
                "tools": [c["name"] for c in pending_sensitive],
            }
        )
        if not (isinstance(decision, dict) and decision.get("approved")):
            collected.append(AIMessage(content="操作已被人工拒绝。"))
            return collected, "操作未获人工批准，已取消。"

    return collected, "\n".join(tool_result_parts)


def _make_agent_node(intent: str):
    """工厂：为某个意图生成一个「独立子 Agent 节点」。"""

    async def node(state, config=None):
        tools = NAMESPACES[intent]
        # 把请求级上下文写入 contextvars，供 DbHub 关联真实患者（不改工具签名）
        patient_ctx.set(state.get("patient_id") or "anonymous")
        # 真实会话线程 ID（LangGraph 通过 config 传入），用于 HITL 待审批缓存的 key
        tid = ""
        if config:
            tid = (config.get("configurable") or {}).get("thread_id", "")
        thread_ctx.set(tid)
        collected, tool_result = await run_agent_with_tools(
            get_llm(),
            tools,
            state,
            SYSTEM_PROMPTS[intent],
            SENSITIVE_TOOLS,
            APPROVAL_ACTION.get(intent, "tool_approval"),
        )
        # 随访动作落库到长期记忆
        if intent == "followup":
            append_note(state["patient_id"], last_human(state))
        return {"messages": collected, "tool_result": tool_result}

    node.__name__ = f"agent_{intent}"
    return node


async def final_answer(state):
    """汇总节点：所有 LLM 模式下都走流式 (llm.astream)。

    fake 模式由 FakeLLM(BaseChatModel) 产出与 compose_answer 一致的内容；
    真实模型直接调用 ChatOpenAI/ChatOllama。网关用 graph.astream_events 捕获
    on_chat_model_stream 事件，把 token 实时推给 SSE 客户端。
    """
    intent = state.get("intent", "triage")
    tool_result = state.get("tool_result", "")
    redline = state.get("redline_reason", "")
    pid = state["patient_id"]
    llm = get_llm()
    msgs = [
        SystemMessage(
            content="你是医疗预约诊疗助手的回复生成模块，基于工具结果用中文简洁回复，不超过 120 字。"
        ),
        HumanMessage(content=f"意图:{intent}\n工具结果:{tool_result}\n红线:{redline}\n患者:{pid}"),
    ]
    text = await acompose(llm, msgs)
    return {"messages": [AIMessage(content=text)]}
