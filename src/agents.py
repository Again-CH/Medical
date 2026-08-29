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

from .compose import try_format_knowledge_reply
from .config import LLM_MODEL_NAME
from .context import patient_ctx, tenant_ctx, thread_ctx
from .cost import record_llm_tokens
from .db import clear_pending, is_db_enabled, pop_pending, set_pending
from .llm import FakeLLM, acompose, get_llm
from .logging_config import get_logger
from .memory import append_note
from .prompts import load_prompt
from .resilience import KILL_SWITCH, BreakerOpenError, get_breaker
from .resilience import enabled as resilience_enabled
from .rollout import resolve_version
from .state_utils import last_human
from .tools import NAMESPACES
from .tools.triage import _match_knowledge
from .tracing import span

log = get_logger()


def _msgs_text(messages) -> str:
    """把一组消息拼成纯文本，供无真实 usage 时估算 token。"""
    parts = []
    for m in messages or ():
        c = getattr(m, "content", "")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):  # content parts（部分模型返回）
            parts.append("".join(getattr(x, "text", str(x)) for x in c))
    return "\n".join(parts)


# 每个子 Agent 的系统提示从版本化 prompt 目录加载（PROMPT_VERSION 切换版本）。
# 为什么抽出：LLM 项目最高频的改动就是 prompt；版本化后可用 git diff 审计、
# 用 manifest 哈希做启动校验、用回归脚本在 CI 里卡「改 prompt 必须重跑评测」。
SYSTEM_PROMPTS = {
    "triage": load_prompt("triage"),
    "booking": load_prompt("booking"),
    "intake": load_prompt("intake"),
    "followup": load_prompt("followup"),
    "emergency": load_prompt("emergency"),
}
COMPOSE_PROMPT = load_prompt("compose")

# 敏感工具：执行前必须走 interrupt 人工审核门（AI 不直接放行）
#
# 设计取舍：``lock_appointment`` **不在**此集合——患者自助挂号若每个号源都要医生审批，
# 审批队列会被瞬间淹没，且锁号本身可逆（未支付会自动释放），风险可控。真正涉及资金与
# 不可逆后果的是医保结算、转诊与 120 呼叫。失控囤号风险改由
# ``MAX_APPOINTMENTS_PER_DAY``（单患者单日上限）与并发原子占位兜底。
SENSITIVE_TOOLS = {"medicare_settle", "handoff", "call_120"}

# 越权/拒绝时的固定回复（不依赖 LLM，不泄露「该患者是否存在」等可探测信息）
_TOOL_DENIED = "[denied] 该操作未获授权（仅可访问本人数据），已记录安全审计。"

# 韧性工程：工具被运行时停用 / 其下游熔断时的安全降级回复（不依赖 LLM，避免编造）
_TOOL_DISABLED = (
    "[disabled] 该服务当前已被运维主动停用（运行时 kill switch），请稍后重试或联系人工客服。"
)
_TOOL_DEGRADED = "[degraded] 该服务暂时不可用，已降级处理，请稍后重试。"


def _approval_payload(action: str, state, calls: list) -> dict:
    """构造人工审核门载荷：**必须携带完整参数与申请人**。

    旧实现只传工具名（如 ``medicare_settle``），医护看不到要结算哪笔预约、
    给谁转诊、为谁呼叫 120，审批实际沦为「盲批」——既无法判断风险，
    也不满足合规对「人工实质性审核」的要求。
    保留 ``tools`` 字段以兼容既有前端与测试断言。
    """
    return {
        "action": action,
        "intent": state.get("intent"),
        "requester": state.get("patient_id"),
        "calls": [{"name": c["name"], "args": c.get("args", {})} for c in calls],
        "tools": [c["name"] for c in calls],
    }


def _invoke_tool(fn, args):
    """执行工具并统一收敛越权 / 停用 / 熔断异常。

    对象级授权（OLP）由 ``integrations._resolve_patient`` 在访问层收口：
    一旦模型尝试越权访问他人档案会抛 ``PermissionError``。此处捕获后转为
    固定拒绝文本返回给模型，避免 500、也避免把异常细节回灌进对话上下文。

    韧性工程叠加两层保护：
    - 运行时 kill switch：运维主动停用某工具（其下游 HIS/短信网关宕机）时，
      直接返回 ``_TOOL_DISABLED`` 占位，不向故障依赖发请求。
    - 熔断器：工具下游持续失败被隔离（OPEN）时快速失败，返回 ``_TOOL_DEGRADED``，
      避免每次调用都打满超时把调用方拖死（与 retry.py 的瞬时自愈互补）。
    """
    name = getattr(fn, "name", "?")
    with span("tool.call", {"tool": name}):
        if resilience_enabled() and KILL_SWITCH.is_disabled(name):
            log.warning("resilience.tool_disabled", extra={"tool": name})
            return _TOOL_DISABLED
        try:
            if resilience_enabled():
                return get_breaker(f"tool:{name}").call_sync(lambda: fn.invoke(args or {}))
            return fn.invoke(args or {})
        except BreakerOpenError:
            # 工具下游持续失败被隔离：快速失败 + 降级占位，不向故障依赖打超时
            log.warning("resilience.breaker_open", extra={"tool": name})
            return _TOOL_DEGRADED
        except PermissionError:
            # 对象级授权（OLP）在访问层收口：越权访问他人档案被拒，转为固定拒绝文本，
            # 避免 500、也避免把异常细节回灌进对话上下文。
            log.warning("security.tool_denied", extra={"tool": name})
            return _TOOL_DENIED


# 人工审核门在审批存储里的 action 标识（也用于测试断言 / 前端展示）
APPROVAL_ACTION = {
    "booking": "medicare_settle",
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
    # 运行时 kill switch：整个意图被运维主动停用 → 不进入子 Agent，直接降级
    # （tool_result 为空，交给 final_answer 的 KB 命中 / 安全兜底），不向故障依赖发请求。
    if resilience_enabled() and KILL_SWITCH.is_disabled(f"agent:{state.get('intent')}"):
        log.warning("resilience.intent_disabled", extra={"intent": state.get("intent")})
        return [], ""

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
            _approval_payload(
                approval_action,
                state,
                [c for c in pending if c["name"] in sensitive_tools],
            )
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
            out = _invoke_tool(fn, c.get("args", {}))
            tool_msgs.append(ToolMessage(content=str(out), tool_call_id=c["id"]))
            parts.append(str(out))
        return [ai_msg, *tool_msgs], "\n".join(parts)

    # —— 首次运行：LLM 选工具；敏感动作累积，工具轮结束后再统一审批 ——
    collected = []
    tool_result_parts = []
    pending_sensitive: list = []
    MAX_STEPS = 4

    for _ in range(MAX_STEPS):
        # LLM 调用单独成 span：回答"慢是慢在模型还是工具"的关键依据
        with span("llm.invoke", {"model": type(llm_bound).__name__, "step": _}):
            if resilience_enabled():
                try:
                    ai = await get_breaker("llm").call_async(
                        lambda: llm_bound.ainvoke(msgs + collected)
                    )
                except BreakerOpenError:
                    # LLM 依赖已被隔离：本节点降级为「不调用 LLM」，循环退出后
                    # tool_result 为空，final_answer 会走 KB 命中 / 安全兜底，绝不编造。
                    log.warning(
                        "resilience.llm_breaker_open", extra={"intent": state.get("intent")}
                    )
                    break
            else:
                ai = await llm_bound.ainvoke(msgs + collected)
        # 成本归因：记录本次 LLM 调用的 token 消耗（真实 usage 优先，否则估算）。
        # 熔断器开启分支在上方已 break，不会到此处；此处 ai 必然来自一次成功调用。
        try:
            record_llm_tokens(
                patient_id=state.get("patient_id") or "anonymous",
                agent=state.get("intent") or "unknown",
                model=LLM_MODEL_NAME,
                prompt_text=_msgs_text(msgs + collected),
                completion_text=getattr(ai, "content", "") or "",
                message=ai,
            )
        except Exception:  # 成本埋点失败绝不影响主链路
            pass
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
            out = _invoke_tool(fn, c.get("args", {}))
            collected.append(ToolMessage(content=str(out), tool_call_id=c["id"]))
            tool_result_parts.append(str(out))

    # 工具轮结束：若有敏感动作，统一挂起等待一次人工审批
    if pending_sensitive:
        _PENDING[cache_key] = pending_sensitive
        decision = interrupt(_approval_payload(approval_action, state, pending_sensitive))
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
        # 多租户上下文：从 graph state 取租户（已由网关解析），确保经 LangGraph
        # ToolNode（可能跨任务执行）后仍能在工具内正确隔离科室主数据
        _tid = state.get("tenant_id")
        if _tid is not None:
            tenant_ctx.set(_tid)
        # 真实会话线程 ID（LangGraph 通过 config 传入），用于 HITL 待审批缓存的 key
        tid = ""
        if config:
            tid = (config.get("configurable") or {}).get("thread_id", "")
        thread_ctx.set(tid)

        # 灰度 / 金丝雀：按 feature 决定使用哪个 prompt 版本；未命中则回退默认 v1。
        # 稳定哈希保证同一患者多次请求体验一致。
        prompt_version = resolve_version(
            feature=f"{intent}-prompt",
            username=state.get("patient_id") or "anonymous",
            tenant_id=_tid,
            default="v1",
        )
        system_prompt = (
            load_prompt(intent, prompt_version)
            if prompt_version != "v1"
            else SYSTEM_PROMPTS[intent]
        )

        with span(f"agent.{intent}", {"intent": intent, "thread_id": tid, "prompt_version": prompt_version}):
            collected, tool_result = await run_agent_with_tools(
                get_llm(),
                tools,
                state,
                system_prompt,
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

    # ── ★ 分诊兜底：当 triage agent 的 LLM 未调用工具（tool_result 为空/模糊）时，
    #   直接用用户原始输入查本地知识库。命中则绕过 LLM 追问，千问风格直出 ──
    if intent == "triage" and (
        not tool_result or "需要更多信息" in tool_result or "未提供具体症状" in tool_result
    ):
        user_input = last_human(state)
        kb = _match_knowledge(user_input)
        if kb:
            # 把本地知识库数据组装成 try_format_knowledge_reply 能识别的格式
            std_dept = kb.get("department", "")
            fake_tool_result = (
                f"[医学参考信息]\n"
                f"症状：{user_input}\n"
                f"简介：{kb['description']}\n"
                f"常见原因：{kb['causes']}\n"
                f"{kb['home_care']}\n\n"
                f"{kb['when_see_doctor']}\n\n"
                f"预后：{kb['healing_time']}\n\n"
                f"推荐就诊科室：{std_dept}"
            )
            direct = try_format_knowledge_reply(fake_tool_result, pid)
            if direct:
                return {"messages": [AIMessage(content=f"【分诊建议】\n{direct}")]}

    # 回复生成模块同样走灰度 prompt
    compose_version = resolve_version(
        feature="compose-prompt",
        username=pid,
        tenant_id=state.get("tenant_id"),
        default="v1",
    )
    compose_prompt = (
        load_prompt("compose", compose_version)
        if compose_version != "v1"
        else COMPOSE_PROMPT
    )

    llm = get_llm()
    msgs = [
        SystemMessage(content=compose_prompt),
        HumanMessage(content=f"意图:{intent}\n工具结果:{tool_result}\n红线:{redline}\n患者:{pid}"),
    ]
    # ── 统一安全降级：LLM 超时/失败（熔断）时启用，绝不编造、不乱答 ──
    _SAFE_FALLBACK = (
        "您好，系统暂时无法为您生成个性化建议。为安全起见：\n"
        "· 若症状轻微，可先休息观察，注意补充水分与规律作息\n"
        "· 若症状持续、加重或出现明显不适，请尽快前往医院相应科室就诊\n"
        "· 紧急情况请立即拨打 120\n"
        "您也可以稍后重试或联系人工客服。"
    )
    try:
        # LLM 汇总同样走熔断器：依赖被隔离（OPEN）时快速失败，直接走统一安全降级，
        # 不再为每个请求打满超时。BreakerOpenError 属 Exception，下面的 except 统一兜底。
        if resilience_enabled():
            text = await get_breaker("llm").call_async(lambda: acompose(llm, msgs))
        else:
            text = await acompose(llm, msgs)
    except Exception as e:
        print(f"[final_answer] LLM 汇总失败（含熔断），启用统一安全降级: {e!r}")
        return {"messages": [AIMessage(content=_SAFE_FALLBACK)]}
    if not text or not text.strip():
        # 模型返回空（如限流/内容被过滤）：同样走安全降级
        return {"messages": [AIMessage(content=_SAFE_FALLBACK)]}
    # 成本归因：compose 汇总节点的一次 LLM 调用（真实 usage 优先，否则估算）
    try:
        record_llm_tokens(
            patient_id=pid,
            agent="compose",
            model=LLM_MODEL_NAME,
            prompt_text=_msgs_text(msgs),
            completion_text=text,
        )
    except Exception:  # 成本埋点失败绝不影响主链路
        pass
    return {"messages": [AIMessage(content=text)]}
