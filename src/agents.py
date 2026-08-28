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
from .llm import FakeLLM, acompose, get_llm
from .memory import append_note
from .state_utils import last_human
from .tools import NAMESPACES

# 每个子 Agent 的系统提示（告诉模型它的职责与可选工具）
SYSTEM_PROMPTS = {
    "triage": "你是分诊助手。根据患者主诉，调用 search_department / dept_map_rag 给出建议科室。",
    "booking": "你是挂号助手。先 query_availability 查号源；确认锁号与医保结算必须等待人工批准。",
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


async def run_agent_with_tools(llm, tools, state, system, sensitive_tools, approval_action):
    """通用 ReAct：bind_tools → 执行 → 敏感动作 interrupt。

    返回 (messages_extend, tool_result)，由调用方写回 state。
    """
    llm_bound = llm.bind_tools(tools)
    # 给 fake 模型提供上下文 hint，使其能确定性地选出本命名空间工具
    if isinstance(llm_bound, FakeLLM):
        llm_bound.intent_hint = state.get("intent")
        llm_bound.patient_id_hint = state.get("patient_id")
        llm_bound.human_hint = last_human(state)

    msgs = [SystemMessage(content=system), *state["messages"]]
    collected = []
    tool_result_parts = []
    tool_map = {t.name: t for t in tools}
    MAX_STEPS = 3

    for _ in range(MAX_STEPS):
        ai = await llm_bound.ainvoke(msgs + collected)
        collected.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            # 模型认为无需再调工具 → 结束工具轮
            break

        # 敏感动作：执行前必须人工确认
        sensitive = [c for c in calls if c["name"] in sensitive_tools]
        if sensitive:
            decision = interrupt(
                {
                    "action": approval_action,
                    "intent": state.get("intent"),
                    "tools": [c["name"] for c in sensitive],
                }
            )
            if not (isinstance(decision, dict) and decision.get("approved")):
                collected.append(AIMessage(content="操作已被人工拒绝。"))
                return collected, "操作未获人工批准，已取消。"

        # 执行工具（等价于图内的 ToolNode）
        tool_msgs = []
        for c in calls:
            fn = tool_map.get(c["name"])
            if not fn:
                continue
            out = fn.invoke(c.get("args", {}) or {})
            tool_msgs.append(ToolMessage(content=str(out), tool_call_id=c["id"]))
            tool_result_parts.append(str(out))
        collected += tool_msgs

    return collected, "\n".join(tool_result_parts)


def _make_agent_node(intent: str):
    """工厂：为某个意图生成一个「独立子 Agent 节点」。"""

    async def node(state):
        tools = NAMESPACES[intent]
        # 把请求级上下文写入 contextvars，供 DbHub 关联真实患者（不改工具签名）
        patient_ctx.set(state.get("patient_id") or "anonymous")
        thread_ctx.set(state.get("thread_id", ""))
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
