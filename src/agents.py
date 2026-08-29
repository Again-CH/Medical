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
from .context import patient_ctx, thread_ctx
from .db import clear_pending, is_db_enabled, pop_pending, set_pending
from .llm import FakeLLM, acompose, get_llm
from .logging_config import get_logger
from .memory import append_note
from .resilience import KILL_SWITCH, BreakerOpenError, get_breaker
from .resilience import enabled as resilience_enabled
from .state_utils import last_human
from .tools import NAMESPACES
from .tools.triage import _match_knowledge
from .tracing import span

log = get_logger()

# 每个子 Agent 的系统提示（告诉模型它的职责与可选工具）
SYSTEM_PROMPTS = {
    "triage": (
        "你是康宁健康服务的贴心分诊助手。你的目标是让患者感受到被关怀和专业。"
        "\n\n【重要前置判断】"
        "\n如果用户没有描述具体症状（如只是打招呼/问好/说「身体不舒服」但没说哪里不舒服/消息过短），"
        "不要调用任何工具！直接用温暖友好的语气回复，并引导用户描述具体不适。"
        "例如：「您好！我是康宁健康服务助手。请问您有什么需要帮助的？可以说说您哪里不舒服，我来帮您分析。」"
        "\n\n【工具分工：症状问题 vs 医院事务问题】"
        "\n- 症状医学科普（头痛/发烧/咳嗽等「我哪里不舒服」）→ 调用 search_health_info 获取医疗参考信息，"
        "并调用 dept_map_rag 检索「症状-科室对应表」（覆盖发热咳嗽/胃痛反酸/心慌胸闷/头痛失眠/关节疼晨僵/"
        "皮肤瘙痒/牙疼/外伤骨折/尿频/月经不调等细分症状→科室，映射更全），据此给出就诊科室；"
        "必要时再辅以 search_department 兜底。"
        "\n- 医院事务咨询（就诊/挂号流程、门诊时间、医保报销、检查化验注意事项、体检、院区交通停车、"
        "便民服务、互联网医院线上复诊、急诊/住院须知、科室介绍等「医院怎么运作」类问题）→ 调用 hospital_rag "
        "检索院内权威资料并据此作答，文末注明「以上为院内公开资料，具体以现场公示为准」。这类问题不要调用 search_health_info。"
        "\n\n【当用户描述了具体症状后，按以下流程处理】"
        "\n1. 调用 search_health_info 搜索该症状的医疗参考信息（含全网大数据和医学知识库）"
        "\n2. 调用 dept_map_rag 检索「症状-科室对应表」，确定推荐就诊科室（如症状在表中已有明确对应，以该表为准）"
        "\n3. 若 dept_map_rag 未给出明确科室，再用 search_department 兜底获取科室推荐"
        "\n4. 基于搜索结果，用温暖、专业的语气组织回复"
        "\n\n【自动沉淀病历】当用户在对话中主动提供了任何可结构化的医疗信息——检验/检查结果数值"
        "（如「血糖 8.5」「血压 150/95」）、生命体征、或病情/现病史/既往史/用药史/过敏史/诊断等描述——"
        "在给出分诊建议的同时，调用 record_lab_result / record_vital / record_case_summary 自动写入其私有档案库，"
        "便于后续随访与医护调阅。不要编造数值，只记录患者明确提供的客观数据。"
        "\n\n回复要求："
        "- 先共情（「理解您的不适」「这种情况很常见」）"
        "- 给出实用的参考建议（原因、护理方法、注意事项）"
        "- 明确推荐就诊科室"
        "- 温馨提示何时需要就医/急诊"
        "- 语气亲切自然，像一位有经验的健康顾问朋友"
        "- 不要机械罗列，要用连贯的段落组织语言"
    ),
    "booking": (
        "你是挂号助手。"
        "\n流程：1) 先调用 query_availability 查号源；2) 有号源则调用 lock_appointment 锁定号源；"
        "3) 若用户明确说「确认预约/确认挂号」或追问预约结果，调用 confirm_appointment 完成确认；"
        "4) 若用户要求医保结算，再调用 medicare_settle（敏感动作，需人工审批）。"
        "\n\n重要：锁定号源即视为预约成功，不要反问患者「是否需要确认」「是否确认预约」。"
        "锁定完成后直接生成包含以下信息的回复：医院、科室、医生、就诊日期、时段、就诊序号、预计就诊时间。"
        "若用户未指定具体时段/号源，直接锁定该科室当天第一个可用号源（上午优先）。"
        "用户要求医保结算时才调 medicare_settle。直接执行工具调用。"
    ),
    "intake": "你是诊前问诊助手，负责解读报告与沉淀病历。\n"
    "【必须遵守：自动入库】只要患者在对话中提供了任何可结构化的医疗信息，你必须先调用写入工具、再给出解读/建议：\n"
    "1) 单项检验或检查结果数值（如「我血糖 8.5」「总胆固醇 5.8」「肌酐 90」）或生命体征（「血压 150/95」「心率 88」）→ 调用 record_lab_result / record_vital 写入其私有档案库；\n"
    "2) 一整份体检报告（如「我的体检报告：血糖 7.2，总胆固醇 5.8，血压 140/90」）→ 对其中每一个项目分别调用 record_lab_result，并把整份报告的整体结论/主诉调用 record_case_summary 沉淀为结构化病历；\n"
    "3) 病情、现病史、既往史、用药史、过敏史、诊断等描述 → 调用 record_case_summary（用合适 category）沉淀为结构化病历。\n"
    "不要编造数值，只记录患者明确提供的客观数据；写入后再结合 read_lab_report / clinical_kb 给出解读与建议。\n"
    "当用户要求解读「某一项」具体检验报告（如「解读我的血糖报告」）时，务必把项目名传给 read_lab_report 的 item_name 参数，"
    "避免返回全部无关项目，使解读更精准。"
    "\n\n【诊前检查须知主动提示】当患者表示即将做检查/化验/抽血，或询问体检、B超、CT、核磁、超声、胃镜、"
    "肠镜、造影、穿刺等检查前的准备与注意事项（如是否空腹、是否憋尿、是否停药）时，"
    "应调用 hospital_rag 检索院内权威检查须知，并主动、清晰地提示患者关键准备事项；"
    "此场景优先于报告解读，属于「医院事务」而非症状科普。",
    "followup": "你是慢病随访助手。调用 read_vitals / plan_reminder / memory_append 完成随访动作。",
    "emergency": "你是应急转诊助手。根据用户描述判断严重程度并给出明确行动建议："
    "\n情况A（常见小病，不是急诊）：感冒/发烧(非40℃+)/咳嗽/口腔溃疡/轻微外伤/皮肤过敏/"
    "蚊虫叮咬/消化不良/便秘/轻度头痛/肌肉酸痛/疲劳失眠等 → "
    "直接回复告知用户这不属于急诊，并建议合适的就诊科室（如口腔溃疡→口腔科）。"
    "\n情况B（真急症，危及生命）：胸痛/呼吸困难/大出血/昏迷/卒中症状(面瘫肢体无力言语不清)/"
    "过敏性休克/剧烈疼痛(炸裂样头痛刀割样腹痛)/意识丧失等 → "
    "先在回复中明确告知用户属于急症需立即就医，再调用 handoff 或 call_120 工具（均需人工确认）。",
}

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
        # 真实会话线程 ID（LangGraph 通过 config 传入），用于 HITL 待审批缓存的 key
        tid = ""
        if config:
            tid = (config.get("configurable") or {}).get("thread_id", "")
        thread_ctx.set(tid)
        with span(f"agent.{intent}", {"intent": intent, "thread_id": tid}):
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

    llm = get_llm()
    msgs = [
        SystemMessage(
            content="你是医疗预约诊疗助手的回复生成模块，基于工具结果用中文自然回复。\n\n"
            "重要约束：只有当「红线」字段非空时才提及急诊/120/红线/紧急就医；"
            "红线为空时绝对不要自行判断为急症，不要生成任何急诊/红线/120/无法自动分诊/联系人工相关内容。"
            "常见小病（感冒发烧咳嗽口腔溃疡轻微外伤皮肤过敏消化不良便秘轻度头痛肌肉酸痛）一律按普通分诊回复。\n\n"
            "【当工具结果为空或包含「需要更多信息」时】\n"
            "说明用户未提供具体症状。此时用温暖友好的语气回复（不超过80字），引导用户描述具体不适。\n"
            "例如：「您好！请问您哪里不舒服？可以详细说说症状，比如疼痛部位、持续时间等，我来帮您分析和推荐科室。」\n\n"
            "【分诊意图(triage)且有具体工具结果时 —— 必须严格遵循以下格式】\n"
            "当工具结果含「[医学参考信息]」（即知识库已命中该症状），你必须严格基于工具结果中的结构化数据组织回复，格式如下：\n\n"
            "第一段：症状概述 + 预后（1-2句话，来自「简介」和「预后」字段）\n\n"
            "【护理与调理建议】\n"
            "· 要点1（来自护理建议，提炼为简洁的分点）\n"
            "· 要点2\n"
            "· 要点3\n\n"
            "【以下情况建议就医】\n"
            "· 情况1（来自「建议就诊情况」）\n"
            "· 情况2\n\n"
            "📌 建议就诊科室：XXX\n\n"
            "（本回复供参考，不能替代医生诊断；如有不适请及时就医）\n\n"
            "关键要求：\n"
            "- 字数控制在 200-350 字，简明扼要，不啰嗦\n"
            "- 直接引用知识库数据，不要自行编造内容或添加知识库没有的信息\n"
            "- 用「·」分点列出要点，清晰易读\n"
            "- 语气专业温暖，像千问那样的健康顾问风格\n"
            "- 绝对不要出现 Markdown 标记（**加粗**、`代码`、##标题等）\n\n"
            "【预约挂号意图(booking) —— 必须严格遵循以下格式】\n"
            "当工具结果含「[locked]」或「[confirmed]」时，代表号源已锁定/预约已完成。"
            "你必须直接输出预约结果，禁止反问「是否需要确认」「是否确认预约」。\n"
            "回复必须包含：医院、科室、医生、就诊日期、时段、就诊序号、预计就诊时间。\n"
            "示例格式：\n"
            "已为您预约康宁医院 神经内科 王医师，2026-08-29 下午，就诊序号第11号，预计就诊时间 16:30。"
            "请携带身份证/医保卡，提前 15 分钟到院取号候诊。\n\n"
            "其他意图：简洁回复，不超过 120 字。"
        ),
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
    return {"messages": [AIMessage(content=text)]}
