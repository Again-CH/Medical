"""确定性安全网关（Tier-0）单元测试。

验证：急症识别 / 否定词守卫 / 定位违规识别 均为确定性、不依赖 LLM、毫秒级同步。
"""

from src.safety import (
    CONSENT_VERSION,
    DISCLAIMER_TEXT,
    SCOPE_STATEMENT,
    assess_emergency,
    assess_scope_violation,
)


def test_emergency_cardiac():
    hit = assess_emergency("我突然胸痛得厉害，出冷汗")
    assert hit is not None
    assert hit.category == "cardiac"
    assert "120" in hit.response


def test_emergency_stroke():
    hit = assess_emergency("老人家嘴歪了，半边身子没力气，说话不清")
    assert hit is not None
    assert hit.category == "neuro"


def test_emergency_bleeding():
    hit = assess_emergency("咳出来的痰里带血，还咯血")
    assert hit is not None
    assert hit.category == "bleeding"


def test_emergency_negation_suppressed():
    # 明确否认句不应触发急症闸（避免误报打扰）
    assert assess_emergency("我没有胸痛，就是有点胸闷气短") is None
    assert assess_emergency("不胸痛，也没头晕") is None


def test_emergency_question_still_triggers():
    # 非否认的急症描述（即使是询问语气）仍触发，宁可误报
    assert assess_emergency("胸痛是什么原因导致的") is not None


def test_emergency_normal_safe():
    assert assess_emergency("今天天气不错，想咨询一下口腔溃疡") is None
    assert assess_emergency("") is None
    assert assess_emergency(None) is None


def test_scope_violation_prescription():
    hit = assess_scope_violation("请给我开点感冒药")
    assert hit is not None
    assert "处方" in hit.response or "开药" in hit.response
    # 固定话术，不进入 LLM
    assert "不提供诊断" in hit.response or "不诊断" in hit.response


def test_scope_violation_diagnosis():
    hit = assess_scope_violation("帮我确诊一下是不是糖尿病")
    assert hit is not None


def test_scope_normal_safe():
    assert assess_scope_violation("我头痛挂什么科") is None


def test_constants_present():
    assert CONSENT_VERSION
    assert "不能替代" in DISCLAIMER_TEXT or "不能替代" in SCOPE_STATEMENT
    assert "不提供诊断" in SCOPE_STATEMENT or "不开具处方" in SCOPE_STATEMENT
