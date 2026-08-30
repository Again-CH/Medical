from langchain_core.messages import HumanMessage, SystemMessage

from .config import LLM_MODE, LLM_MODEL_NAME
from .cost import BudgetExceeded, check_budget, record_llm_tokens
from .llm import FakeLLM, get_llm
from .safety import assess_emergency, assess_scope_violation
from .state_utils import last_human
from .tracing import span

INTENT_KEYWORDS = {
    "booking": ["挂号", "预约", "号源", "锁号", "确认预约", "确认挂号", "确定预约"],
    "intake": ["报告", "检查", "化验", "检验"],
    "followup": ["随访", "慢病", "复查", "复诊"],
    "triage": [],
    "emergency": [],
}

# 真实模型可分类的意图全集（与 NAMESPACES 对齐）
INTENT_LABELS = ["triage", "booking", "intake", "followup", "emergency"]

SYSTEM_CLASSIFY = (
    "你是医疗分诊分类器。将用户请求分为且仅分为以下类别之一，只回复类别英文标签："
    "triage(分诊/科室咨询)、booking(挂号/预约)、intake(报告/检查解读)、"
    "followup(随访/慢病/复查)、emergency(急症/危及生命)。"
    "\n\n重要排除规则（以下情况绝对不是 emergency）："
    "感冒、发烧(非超高热)、咳嗽、口腔溃疡、轻微外伤、皮肤过敏、蚊虫叮咬、"
    "消化不良、便秘、轻度头痛、肌肉酸痛、疲劳失眠等常见小病 → 归为 triage。"
    "只有出现以下情况才归 emergency：胸痛/呼吸困难/大出血/昏迷/卒中症状/"
    "过敏性休克/剧烈疼痛(炸裂样/刀割样)/意识丧失等危及生命的征象。"
    "\n\n挂号/预约确认语（如「确认预约」「确认挂号」「确定预约」）归为 booking，不要归为 triage。"
)


def _keyword_intent(text: str) -> str:
    """fake 模式 / 真实模型不可用时的兜底关键词分类（保持确定性可复现）。"""
    for intent, kws in INTENT_KEYWORDS.items():
        if intent in ("triage", "emergency"):
            continue
        if any(k in text for k in kws):
            return intent
    return "triage"


async def classify_intent(text: str, patient_id: str = "anonymous") -> tuple[str, dict]:
    """意图分类：fake 模式用关键词；真实模型用 LLM 结构化分类，异常则回退关键词。

    返回 ``(intent, token_usage)``，其中 usage 恒为
    ``{"prompt_tokens": int, "completion_tokens": int}``——**两种模式下键名必须一致**，
    否则调用方要写两套取值逻辑，迟早漏掉一处。

    token 埋点为什么必须在这里补
    -----------------------------
    真实模型模式下这里会真实调用一次 LLM，但早期版本只在子 Agent 与汇总节点埋了点，
    导致**每轮对话都少算一次分类调用**——成本看板上这部分消耗完全隐形。
    """
    ZERO = {"prompt_tokens": 0, "completion_tokens": 0}
    if LLM_MODE == "fake":
        return _keyword_intent(text), dict(ZERO)

    llm = get_llm()
    # get_llm 已因 Ollama 不可用回退到 FakeLLM → 直接走关键词兜底
    if isinstance(llm, FakeLLM):
        return _keyword_intent(text), dict(ZERO)
    # 成本熔断预检：预算耗尽时不再为「意图分类」单独调一次 LLM，
    # 直接退化为确定性关键词分类（分类本身有零成本兜底，这正是它的价值）。
    try:
        check_budget(patient_id)
    except BudgetExceeded:
        return _keyword_intent(text), dict(ZERO)

    msgs = [SystemMessage(content=SYSTEM_CLASSIFY), HumanMessage(content=text)]
    try:
        resp = await llm.ainvoke(msgs)
    except Exception:
        return _keyword_intent(text), dict(ZERO)

    try:
        usage = record_llm_tokens(
            patient_id=patient_id,
            agent="supervisor",
            model=LLM_MODEL_NAME,
            prompt_text=SYSTEM_CLASSIFY + text,
            completion_text=getattr(resp, "content", "") or "",
            message=resp,
        )
    except Exception:  # 成本埋点失败绝不影响主链路
        usage = {"prompt_tokens": 0, "completion_tokens": 0}

    label = (getattr(resp, "content", "") or "").strip().lower()
    for k in INTENT_LABELS:
        if k in label:
            return k, usage
    return "triage", usage


async def supervisor(state):
    """编排中枢：红线前置 → 意图分类。

    红线判定统一走 ``safety`` 的确定性闸门（与网关入口**同一套词库**），
    不再经 ``redline`` 中间层，避免两套口径分叉导致「网关拦截、编排放行」。

    token 用量经 state 上报（``token_usage``），由 LangGraph reducer 累加——
    不用 contextvar 是因为节点可能跑在独立异步任务里，context 不会继承。
    """
    text = last_human(state)
    with span("supervisor.classify", {"msg_len": len(text or "")}):
        return await _route(text, state.get("patient_id") or "anonymous")


async def _route(text: str, patient_id: str = "anonymous") -> dict:
    emg = assess_emergency(text)
    if emg is not None:
        # 红线前置：写入 redline_reason 由 final_answer 组装急症提示。
        # 路由到 triage 而非 emergency agent——后者 token 会被网关过滤导致空响应。
        return {
            "intent": "triage",
            "redline_reason": f"命中急症关键词：{emg.keyword}",
            "token_usage": {"prompt": 0, "completion": 0},
        }
    scope = assess_scope_violation(text)
    if scope is not None:
        return {
            "intent": "triage",
            "redline_reason": f"违规请求：{scope.keyword}",
            "token_usage": {"prompt": 0, "completion": 0},
        }
    intent, usage = await classify_intent(text, patient_id)
    # 统一 state 契约：token_usage 恒用 prompt / completion 两个键（见 state.add_usage）。
    # classify_intent 返回的是 record_llm_tokens 格式（*_tokens），此处做一次转换，
    # 避免 reducer 拿到未知键而漏算。
    return {
        "intent": intent,
        "token_usage": {
            "prompt": int(usage.get("prompt_tokens", 0)),
            "completion": int(usage.get("completion_tokens", 0)),
        },
    }
