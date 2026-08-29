"""入库前数据质量门测试。

覆盖：完整性 / 值域 / 单位一致性 / 日期 / **异常标记与参考范围的语义一致性** /
门禁放行与拒收行为（拒收要计数、仅警告要放行并附提示）。
"""

from __future__ import annotations

from datetime import date, timedelta

from src.data_quality import (
    gate,
    validate_case_summary,
    validate_lab,
    validate_vital,
)

FUTURE = (date.today() + timedelta(days=3)).isoformat()
PAST = (date.today() - timedelta(days=3)).isoformat()


# ---------------- 检验报告 ----------------


def test_lab_accepts_valid_record():
    r = validate_lab("血红蛋白", "130", "115-150", False, PAST)
    assert r.ok, r.summary()
    assert not r.warnings, r.summary()


def test_lab_rejects_missing_fields():
    r = validate_lab("", "", "", False, "")
    assert not r.ok
    assert {i.field_name for i in r.errors} >= {"item", "result"}


def test_lab_rejects_future_report_date():
    r = validate_lab("血糖", "5.0", "3.9-6.1", False, FUTURE)
    assert not r.ok
    assert any(i.code == "future_date" for i in r.errors)


def test_lab_rejects_bad_date_format():
    r = validate_lab("血糖", "5.0", "", False, "2026/08/30")
    assert not r.ok
    assert any(i.code == "bad_format" for i in r.errors)


def test_lab_detects_abnormal_flag_contradiction():
    """数值落在参考范围内却标了异常 → 语义矛盾，必须拦下。"""
    r = validate_lab("血红蛋白", "130", "115-150", True, PAST)
    assert not r.ok
    assert any(i.code == "inconsistent" for i in r.errors)


def test_lab_warns_when_out_of_range_but_not_flagged():
    """数值超出参考范围却没标异常 → 警告（可能是录入漏标，但不直接拒收）。"""
    r = validate_lab("血红蛋白", "90", "115-150", False, PAST)
    assert r.ok, "仅警告应放行"
    assert any(i.code == "inconsistent" for i in r.warnings)


def test_lab_rejects_inverted_ref_range():
    r = validate_lab("血红蛋白", "130", "150-115", False, PAST)
    assert not r.ok
    assert any(i.code == "inverted" for i in r.errors)


# ---------------- 生命体征 ----------------


def test_vital_accepts_normal_values():
    for t, v, u in [("体温", "36.8", "℃"), ("心率", "78", "次/分"), ("血氧", "98", "%")]:
        r = validate_vital(t, v, u)
        assert r.ok, f"{t}={v} 应通过：{r.summary()}"


def test_vital_rejects_physiologically_impossible():
    """体温 370（单位错写成 37.0 的多打零）必须拦下。"""
    r = validate_vital("体温", "370", "℃")
    assert not r.ok
    assert any(i.code == "out_of_range" for i in r.errors)


def test_vital_rejects_bad_blood_pressure():
    r = validate_vital("血压", "1200", "mmHg")
    assert not r.ok
    assert any(i.code in ("bad_bp", "out_of_range") for i in r.errors)


def test_vital_accepts_blood_pressure_pair():
    r = validate_vital("血压", "120/80", "mmHg")
    assert r.ok, r.summary()


def test_vital_rejects_inverted_blood_pressure():
    r = validate_vital("血压", "80/120", "mmHg")
    assert not r.ok
    assert any(i.code == "inconsistent" for i in r.errors)


def test_vital_warns_on_unit_mismatch():
    r = validate_vital("体温", "36.8", "次/分")  # 单位与体征类型不匹配
    assert r.ok, "单位不匹配只警告，不拒收（避免误杀合法数据）"
    assert any(i.code == "unit_mismatch" for i in r.warnings)


def test_vital_warns_on_unknown_type():
    r = validate_vital("未知指标", "12", "")
    assert r.ok
    assert any(i.code == "unknown_type" for i in r.warnings)


def test_vital_rejects_missing_value():
    r = validate_vital("体温", "", "℃")
    assert not r.ok
    assert any(i.code == "required" for i in r.errors)


# ---------------- 病例小结 ----------------


def test_case_summary_rejects_empty():
    r = validate_case_summary("   ", "general")
    assert not r.ok


def test_case_summary_warns_unknown_category():
    r = validate_case_summary("既往体健", "奇怪分类")
    assert r.ok
    assert any(i.code == "unknown_category" for i in r.warnings)


def test_case_summary_rejects_too_long():
    r = validate_case_summary("x" * 5000, "general")
    assert not r.ok
    assert any(i.code == "too_long" for i in r.errors)


# ---------------- 门禁行为 ----------------


def test_gate_blocks_on_errors():
    ok, note = gate("lab", validate_lab("", "130", "", False, ""))
    assert ok is False
    assert "已拒绝" in note


def test_gate_allows_with_warning_note():
    ok, note = gate("vital", validate_vital("体温", "36.8", "次/分"))
    assert ok is True
    assert "请注意" in note


def test_gate_clean_record_has_empty_note():
    ok, note = gate("lab", validate_lab("血红蛋白", "130", "115-150", False, PAST))
    assert ok is True
    assert note == ""
