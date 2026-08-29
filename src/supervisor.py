from langchain_core.messages import HumanMessage, SystemMessage

from .config import LLM_MODE
from .llm import FakeLLM, get_llm
from .safety import assess_emergency, assess_scope_violation
from .state_utils import last_human

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


async def classify_intent(text: str) -> str:
    """意图分类：fake 模式用关键词；真实模型用 LLM 结构化分类，异常则回退关键词。"""
    if LLM_MODE == "fake":
        return _keyword_intent(text)

    llm = get_llm()
    # get_llm 已因 Ollama 不可用回退到 FakeLLM → 直接走关键词兜底
    if isinstance(llm, FakeLLM):
        return _keyword_intent(text)
    try:
        resp = await llm.ainvoke(
            [SystemMessage(content=SYSTEM_CLASSIFY), HumanMessage(content=text)]
        )
        label = (getattr(resp, "content", "") or "").strip().lower()
        for k in INTENT_LABELS:
            if k in label:
                return k
        return "triage"
    except Exception:
        return _keyword_intent(text)


async def supervisor(state):
    """编排中枢：红线前置 → 意图分类。

    红线判定统一走 ``safety`` 的确定性闸门（与网关入口**同一套词库**），
    不再经 ``redline`` 中间层，避免两套口径分叉导致「网关拦截、编排放行」。
    """
    text = last_human(state)
    emg = assess_emergency(text)
    if emg is not None:
        # 红线前置：写入 redline_reason 由 final_answer 组装急症提示。
        # 路由到 triage 而非 emergency agent——后者 token 会被网关过滤导致空响应。
        return {"intent": "triage", "redline_reason": f"命中急症关键词：{emg.keyword}"}
    scope = assess_scope_violation(text)
    if scope is not None:
        return {"intent": "triage", "redline_reason": f"违规请求：{scope.keyword}"}
    return {"intent": await classify_intent(text)}
