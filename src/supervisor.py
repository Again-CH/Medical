from langchain_core.messages import HumanMessage, SystemMessage

from .config import LLM_MODE
from .llm import FakeLLM, get_llm
from .redline import check_redline
from .state_utils import last_human

INTENT_KEYWORDS = {
    "booking": ["挂号", "预约", "号源", "锁号"],
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
    text = last_human(state)
    hit, reason = check_redline(text)
    if hit:
        # 红线前置：急症/违规请求强制走 emergency（人工）
        return {"intent": "emergency", "redline_reason": reason}
    return {"intent": await classify_intent(text)}
