"""入库前的数据质量门（ETL quality gate）。

为什么需要
----------
患者检验报告与生命体征会经多条路径写入：LLM 从对话里抽取、批量导入脚本、
外部 LIS/可穿戴设备对接。这些来源共同的问题是**脏数据会静默落库** ——
比如体温写成 370（单位错）、收缩压 1200（多打一个 0）、报告日期是未来、
``abnormal`` 标记与参考范围自相矛盾。一旦写进患者档案，医生看到的就是错的。

设计取舍：
- **拦在写入前**，而不是事后清洗 —— 脏数据进了患者档案就已经造成临床风险。
- **不合格进隔离区，不静默写库**：错误级问题拒绝入库并返回原因；
  警告级（如单位不在已知表内）允许写入但明确标注，避免误杀合法数据。
- **判定确定性**：值域、格式、一致性都是规则判断，不依赖模型，可复现、可审计。

与合规的关系：数据质量本身是数据治理的一部分（完整/准确/一致），
医疗等保与个保法都要求个人信息准确；错误健康数据的危害不亚于泄漏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

# 已知生命体征：类型 → (中文名, 合理值域, 常见单位)
# 值域刻意放宽到「生理不可能」的边界之外：只拦明显错误，不做临床判断
# （判断是否正常是医生的事，本模块只保证数据在物理上说得通）。
KNOWN_VITALS: dict[str, tuple[str, tuple[float, float], tuple[str, ...]]] = {
    "体温": ("体温", (25.0, 45.0), ("℃", "C", "度")),
    "心率": ("心率", (20.0, 250.0), ("次/分", "bpm", "次/分钟")),
    "脉搏": ("脉搏", (20.0, 250.0), ("次/分", "bpm")),
    "呼吸": ("呼吸频率", (5.0, 60.0), ("次/分", "次/分钟")),
    "血氧": ("血氧饱和度", (50.0, 100.0), ("%", "百分比")),
    "血糖": ("血糖", (0.5, 60.0), ("mmol/L", "mg/dL")),
    "体重": ("体重", (1.0, 300.0), ("kg", "千克", "公斤")),
    "身高": ("身高", (30.0, 250.0), ("cm", "厘米")),
}
# 血压是复合值（收缩压/舒张压），单独处理
BP_ALIASES = ("血压", "blood_pressure", "bp")

MAX_TEXT_LEN = 2000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
RANGE_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*[-~至]\s*(-?\d+(?:\.\d+)?)$")


@dataclass
class Issue:
    """一条质量问题。severity: error（拒绝入库）/ warning（允许但标注）。"""

    field_name: str
    code: str
    message: str
    severity: str = "error"


@dataclass
class ValidationResult:
    ok: bool = True
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def add(self, field_name: str, code: str, message: str, severity: str = "error") -> None:
        self.issues.append(Issue(field_name, code, message, severity))
        if severity == "error":
            self.ok = False

    def summary(self) -> str:
        if not self.issues:
            return ""
        parts = [f"{i.field_name}: {i.message}" for i in self.errors]
        parts += [f"{i.field_name}: {i.message}（警告）" for i in self.warnings]
        return "；".join(parts)


def _num(v: str) -> Optional[float]:
    """解析数字；失败返回 None。"""
    s = (v or "").strip()
    return float(s) if NUM_RE.match(s) else None


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def validate_report_date(report_date: str, result: ValidationResult) -> None:
    """日期格式 + 不得是未来日期（未来报告日几乎一定是解析/录入错误）。"""
    if not report_date:
        return
    if not DATE_RE.match(report_date):
        result.add("report_date", "bad_format", f"日期格式应为 YYYY-MM-DD，收到 {report_date!r}")
        return
    try:
        d = date.fromisoformat(report_date)
    except ValueError:
        result.add("report_date", "invalid_date", f"不是合法日期：{report_date!r}")
        return
    if d > date.today():
        result.add("report_date", "future_date", f"报告日期在未来：{report_date}")


def validate_ref_range(ref_range: str, result: ValidationResult) -> Optional[tuple[float, float]]:
    """校验参考范围写法，并解析出上下界供一致性检查。"""
    if not ref_range:
        return None
    m = RANGE_RE.match(ref_range.strip())
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            result.add("ref_range", "inverted", f"参考范围上下界倒置：{ref_range}")
            return None
        return (lo, hi)
    # 也允许 "<3.5" / ">10" 这类单边写法，不做严格解析
    result.add(
        "ref_range",
        "unparsed",
        f"参考范围非 min-max 形式，跳过一致性校验：{ref_range}",
        severity="warning",
    )
    return None


def validate_lab(
    item: str,
    result: str,
    ref_range: str = "",
    abnormal: bool = False,
    report_date: str = "",
) -> ValidationResult:
    """检验报告入库前校验。

    校验项：完整性 → 长度 → 日期 → 参考范围格式 → 异常标记与参考范围的一致性。
    最后一项是**语义一致性**检查：数值在参考范围内却标了 abnormal（或反之），
    说明抽取/录入环节出了问题，这类矛盾数据比缺失更危险（会误导医生）。
    """
    r = ValidationResult()

    if not (item or "").strip():
        r.add("item", "required", "检验项目不能为空")
    elif len(item) > 64:
        r.add("item", "too_long", f"检验项目超长（>64）：{len(item)}")

    if not (result or "").strip():
        r.add("result", "required", "检验结果不能为空")
    elif len(result) > 64:
        r.add("result", "too_long", f"检验结果超长（>64）：{len(result)}")

    validate_report_date(report_date or "", r)
    bounds = validate_ref_range(ref_range or "", r)

    # 一致性：结果可解析且参考范围可解析时，交叉验证 abnormal 标记
    val = _num(result)
    if val is not None and bounds:
        lo, hi = bounds
        in_range = lo <= val <= hi
        if abnormal and in_range:
            r.add(
                "abnormal",
                "inconsistent",
                f"标记为异常，但结果 {val} 落在参考范围 {ref_range} 内",
            )
        elif not abnormal and not in_range:
            r.add(
                "abnormal",
                "inconsistent",
                f"未标记异常，但结果 {val} 超出参考范围 {ref_range}",
                severity="warning",
            )
    return r


def validate_vital(type: str, value: str, unit: str = "") -> ValidationResult:
    """生命体征入库前校验：类型、可解析性、生理值域、单位一致性。"""
    r = ValidationResult()
    t = (type or "").strip()
    v = (value or "").strip()

    if not t:
        r.add("type", "required", "体征类型不能为空")
    if not v:
        r.add("value", "required", "体征数值不能为空")

    if t in BP_ALIASES:
        # 血压：允许 "120/80"
        parts = [p.strip() for p in re.split(r"[/／]", v)]
        if len(parts) != 2 or any(_num(p) is None for p in parts):
            r.add("value", "bad_bp", f"血压应为 收缩压/舒张压 形式，收到 {v!r}")
        else:
            sys_v, dia_v = _num(parts[0]), _num(parts[1])
            if not (40.0 <= (sys_v or 0) <= 300.0):
                r.add("value", "out_of_range", f"收缩压 {sys_v} 超出生理可能范围")
            if not (20.0 <= (dia_v or 0) <= 200.0):
                r.add("value", "out_of_range", f"舒张压 {dia_v} 超出生理可能范围")
            if sys_v is not None and dia_v is not None and sys_v <= dia_v:
                r.add("value", "inconsistent", f"收缩压 {sys_v} 应高于舒张压 {dia_v}")
        return r

    num = _num(v)
    if num is None:
        # 非数值型体征（如「心律齐」）允许，但要提示
        r.add("value", "not_numeric", f"数值非数字，按文本记录：{v!r}", severity="warning")
        return r

    spec = KNOWN_VITALS.get(t)
    if spec is None:
        r.add("type", "unknown_type", f"未知体征类型：{t}（请确认命名）", severity="warning")
        return r

    _name, (lo, hi), units = spec
    if not (lo <= num <= hi):
        r.add("value", "out_of_range", f"{t}={num} 超出生理可能范围 [{lo}, {hi}]")

    if unit and units and unit.strip() not in units:
        r.add(
            "unit",
            "unit_mismatch",
            f"{t} 的单位 {unit!r} 不在常见单位 {units} 内，请确认是否为录入错误",
            severity="warning",
        )
    return r


def validate_case_summary(text: str, category: str = "general") -> ValidationResult:
    """病例小结校验：非空、长度上限、类别白名单。"""
    r = ValidationResult()
    if not (text or "").strip():
        r.add("text", "required", "病例小结内容不能为空")
    elif len(text) > MAX_TEXT_LEN:
        r.add("text", "too_long", f"病例小结超长（>{MAX_TEXT_LEN}）：{len(text)}")
    allowed = {"general", "既往史", "过敏史", "家族史", "用药史", "手术史"}
    if category and category not in allowed:
        r.add(
            "category",
            "unknown_category",
            f"类别 {category!r} 不在白名单 {sorted(allowed)} 内",
            severity="warning",
        )
    return r


# ---------------- 门禁入口 ----------------
# 被拒记录数是运维信号（抽取/对接出问题会体现为突增），与 metrics 打通。
try:  # metrics 依赖 prometheus_client，缺失时静默降级为无计数
    from .metrics import DATA_QUALITY_REJECTED
except Exception:  # noqa: BLE001 - 质量门不应因监控缺失而失效

    class _NullCounter:
        def labels(self, **_kw):
            return self

        def inc(self, *_a, **_kw) -> None:
            return None

    DATA_QUALITY_REJECTED = _NullCounter()


def gate(kind: str, result: ValidationResult) -> tuple[bool, str]:
    """统一门禁：判定是否放行，并给出可返回给调用方的说明。

    返回 ``(放行?, 说明)``。有 error → 不放行并计数；仅 warning → 放行但附提示。
    """
    if not result.ok:
        DATA_QUALITY_REJECTED.labels(kind=kind, reason="validation_error").inc()
        return False, f"已拒绝（数据校验未通过）：{result.summary()}"
    if result.warnings:
        return True, f"已记录，但请注意：{result.summary()}"
    return True, ""
