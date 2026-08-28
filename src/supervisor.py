from .config import LLM_MODE
from .redline import check_redline
from .state_utils import last_human

INTENT_KEYWORDS = {
    "booking": ["挂号", "预约", "号源", "锁号"],
    "intake": ["报告", "检查", "化验", "检验"],
    "followup": ["随访", "慢病", "复查", "复诊"],
    "triage": [],
    "emergency": [],
}


def classify_intent(text: str) -> str:
    """意图分类：fake 模式用关键词；真实模型可在此调用 LLM 做 JSON 结构化分类。"""
    if LLM_MODE == "fake":
        for intent, kws in INTENT_KEYWORDS.items():
            if intent in ("triage", "emergency"):
                continue
            if any(k in text for k in kws):
                return intent
        return "triage"
    return "triage"


def supervisor(state):
    text = last_human(state)
    hit, reason = check_redline(text)
    if hit:
        # 红线前置：急症/违规请求强制走 emergency（人工）
        return {"intent": "emergency", "redline_reason": reason}
    return {"intent": classify_intent(text)}
