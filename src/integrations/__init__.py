"""业务集成层（端口 + 适配器）。

这是「真实落地」的关键抽象：工具只依赖下面的端口（Protocol）契约，具体实现可插拔。
- MemoryHub：原 demo 的写死逻辑，零依赖、确定性，用于离线/测试。
- DbHub：基于 SQLAlchemy ORM + 真实数据库（Postgres/SQLite 同构），生产实现。

要接真实医院系统（HIS / 医保网关 / LIS / 短信网关），只需新增一个实现了这些端口的类
（例如 ``ApiHub``），在 ``get_hub()`` 里按配置切换即可 —— 工具与编排代码一行都不用改。

get_hub() 的选择逻辑：
- 设置 DATABASE_URL        → DbHub（真实持久化）
- 未设置（离线/demo/测试） → MemoryHub

安全设计（对象级授权 OLP）：
- **患者身份一律来自请求上下文**（contextvars，源头是 JWT subject），
  **绝不接受模型/客户端传入的 patient_id** —— 工具的 schema 中已彻底移除该参数，
  使 prompt injection 无法操纵「读取/写入他人的档案」。
- 所有涉及患者档案的方法在入口调用 ``_resolve_patient()``：显式传入的 patient_id
  必须与当前上下文一致，否则抛 ``PermissionError``（纵深防御：即便将来有人误把
  参数加回工具签名，访问层依然拦得住）。
- 任何越权尝试都会记 ``security.denied`` 结构化告警，供安全审计与告警联动。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Optional, Protocol, runtime_checkable

from sqlalchemy import text

from .. import data_quality as dq
from ..config import MAX_APPOINTMENTS_PER_DAY
from ..context import patient_ctx
from ..db import (
    Appointment,
    ConversationMemory,
    Department,
    Doctor,
    DoctorSchedule,
    EmergencyEvent,
    LabReport,
    Reminder,
    SymptomDeptMap,
    User,
    VitalSign,
    get_patient_session,
    get_session,
    is_db_enabled,
    run_idempotent,
)
from ..logging_config import get_logger
from ..tenant import resolve_tenant_id

log = get_logger()

# 无请求上下文时（如离线脚本/评测）的兜底标识；此时任何读写都应视为匿名，不落真实档案
ANONYMOUS = "anonymous"


# ---------------------------------------------------------------------------
# 对象级授权（OLP）：患者身份只能来自上下文，不能来自调用方
# ---------------------------------------------------------------------------
def current_patient() -> str:
    """当前请求上下文绑定的患者标识（源自 JWT subject）。"""
    return patient_ctx.get() or ANONYMOUS


def _resolve_patient(explicit: str | None = None) -> str:
    """解析并校验目标患者：显式传入值必须与上下文一致，否则拒绝。

    这是防跨患者越权的**唯一收口**。所有触碰患者档案的方法都必须先过这里。
    """
    cur = current_patient()
    if explicit is None:
        return cur
    if explicit != cur:
        log.warning(
            "security.denied",
            extra={
                "reason": "cross_patient_access",
                "context_patient": cur,
                "requested_patient": explicit,
            },
        )
        raise PermissionError("cross-patient access denied")
    return cur


def _require_patient() -> str:
    """要求必须存在已认证患者（匿名上下文不得读写任何档案）。"""
    cur = current_patient()
    if cur == ANONYMOUS:
        log.warning("security.denied", extra={"reason": "anonymous_patient_access"})
        raise PermissionError("anonymous patient access denied")
    return cur


def stable_key(*parts) -> str:
    """生成稳定的幂等键（sha256，跨进程/重启一致）。

    不使用 Python 内置 hash()：其受 PYTHONHASHSEED 随机化影响，重启后即失效，
    且存在碰撞风险，会导致幂等保护静默失效（重复锁号 / 重复发提醒）。
    """
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 端口（契约）
# ---------------------------------------------------------------------------
@runtime_checkable
class HISPort(Protocol):
    def search_department(self, symptom: str) -> str: ...
    def dept_map_rag(self, symptom: str) -> str: ...
    def hospital_rag(self, query: str) -> str: ...
    def query_availability(self, department: str, date: str) -> str: ...
    def lock_appointment(self, department: str, date: str, slot: str) -> str: ...
    def confirm_appointment(self, appointment_id: str = "") -> str: ...
    def medicare_settle(self, appointment_id: str) -> str: ...


@runtime_checkable
class LISPort(Protocol):
    def read_lab_report(
        self, patient_id: Optional[str] = None, item_name: Optional[str] = None
    ) -> str: ...
    def clinical_kb(self, query: str) -> str: ...
    def read_vitals(self, patient_id: Optional[str] = None) -> str: ...


@runtime_checkable
class NotifyPort(Protocol):
    def plan_reminder(self, text: str, patient_id: Optional[str] = None) -> str: ...


@runtime_checkable
class MemoryPort(Protocol):
    def memory_append(self, note: str, patient_id: Optional[str] = None) -> str: ...


@runtime_checkable
class EmergencyPort(Protocol):
    def handoff(self) -> str: ...
    def call_120(self, patient_id: Optional[str] = None) -> str: ...


# ---------------------------------------------------------------------------
# MemoryHub：离线/demo 的写死实现（与旧行为完全一致，保证测试确定性）
# ---------------------------------------------------------------------------
class MemoryHub:
    def search_department(self, symptom: str) -> str:
        mapping = {
            "头痛": "神经内科",
            "发烧": "感染科",
            "咳嗽": "呼吸内科",
            "腹痛": "消化内科",
            "皮疹": "皮肤科",
            "胸闷": "心血管内科",
        }
        for k, v in mapping.items():
            if k in symptom:
                return f"建议科室：{v}"
        return "建议科室：请到分诊台由导诊进一步评估"

    def dept_map_rag(self, symptom: str) -> str:
        return f"[RAG] 症状「{symptom}」→ 科室画像与候诊时长已检索"

    def hospital_rag(self, query: str) -> str:
        from ..kb import retrieve_hospital

        return retrieve_hospital(query)

    def query_availability(self, department: str, date: str = "today") -> str:
        return f"[availability] {department} {date} 剩余号源：3"

    def lock_appointment(self, department: str, date: str, slot: str) -> str:
        pid = _resolve_patient()
        key = stable_key("apt:lock", pid, department, date, slot)
        return run_idempotent(
            key,
            lambda: (
                f"[locked] 医院={_HOSPITAL_NAME} 科室={department} 日期={date} "
                f"时段={slot} 就诊序号=11号 预计就诊时间={slot} "
                f"appointment_id=APT-1001"
            ),
        )

    def confirm_appointment(self, appointment_id: str = "") -> str:
        pid = _resolve_patient()
        key = stable_key("apt:confirm", pid, appointment_id or "latest")
        return run_idempotent(
            key,
            lambda: (
                f"[confirmed] 医院={_HOSPITAL_NAME} 科室=神经内科 医生=王医师 "
                f"日期={date.today().isoformat()} 时段=PM 就诊序号=11号 "
                f"预计就诊时间=16:30 appointment_id=APT-1001"
            ),
        )

    def medicare_settle(self, appointment_id: str) -> str:
        return f"[settled] {appointment_id} 医保结算完成"

    def read_lab_report(self, item_name: Optional[str] = None) -> str:
        pid = _resolve_patient()
        suffix = f"（指定项目：{item_name}）" if item_name else ""
        return f"[LIS] {pid}{suffix} 血常规：WBC 正常，CRP 轻度升高"

    def clinical_kb(self, query: str) -> str:
        return f"[KB] 关于「{query}」的临床指引已检索"

    def read_vitals(self) -> str:
        pid = _resolve_patient()
        return f"[vitals] {pid} 血压 128/82，心率 72"

    def record_lab_result(self, item, result, ref_range="", abnormal=False, report_date="") -> str:
        pid = _require_patient()
        return f"[LIS] (demo) 已记录 {pid} 检验报告：{item}={result}"

    def record_vital(self, type, value, unit="") -> str:
        pid = _require_patient()
        return f"[vitals] (demo) 已记录 {pid} 生命体征：{type} {value}{unit or ''}"

    def record_case_summary(self, text, category="general") -> str:
        pid = _require_patient()
        return f"[memory] (demo) 已记录 {pid} 病例小结：{text[:40]}"

    def plan_reminder(self, text: str) -> str:
        pid = _resolve_patient()
        key = stable_key("reminder", pid, text)
        return run_idempotent(key, lambda: f"[reminder] 已为 {pid} 创建提醒：{text}")

    def memory_append(self, note: str) -> str:
        pid = _resolve_patient()
        return f"[memory] 已记录 {pid} 随访笔记：{note}"

    def handoff(self) -> str:
        return "[emergency] 已转接急诊人工台，请保持通话"

    def call_120(self) -> str:
        return "[emergency] 已触发 120 呼叫流程"


# ---------------------------------------------------------------------------
# DbHub：基于 ORM 的真实实现
# ---------------------------------------------------------------------------
def _today() -> str:
    return date.today().isoformat()


_HOSPITAL_NAME = "康宁医院"


def _slot_time(period: str, slot_index: int) -> str:
    """根据上午/下午和就诊序号，返回预计就诊时间（HH:MM）。

    演示规则：上午 08:00 开始，下午 14:00 开始，每号 15 分钟。
    """
    idx = max(1, slot_index) - 1
    base_h, base_m = (8, 0) if period == "AM" else (14, 0)
    total_min = base_h * 60 + base_m + idx * 15
    h = total_min // 60
    m = total_min % 60
    return f"{h:02d}:{m:02d}"


def _id_of(aid: str):
    if isinstance(aid, str) and aid.startswith("APT-"):
        try:
            return int(aid[4:])
        except ValueError:
            return None
    if isinstance(aid, int):
        return aid
    return None


def _resolve_user_id(username: str) -> int:
    """把患者标识（JWT sub）解析为 users.id。

    安全约束：**不再自动建档**。任意用户名自动创建 User 记录会引入用户枚举、
    数据污染与「password_hash 为空」的畸形账号。用户不存在即视为越权/失效请求。
    """
    with get_session() as s:
        u = s.query(User).filter(User.username == username).first()
        if not u:
            log.warning(
                "security.denied",
                extra={"reason": "unknown_patient", "requested_patient": username},
            )
            raise PermissionError("patient not found")
        return u.id


class DbHub:
    # --- HIS ---
    def search_department(self, symptom: str) -> str:
        tid = resolve_tenant_id()
        with get_session() as s:
            rows = s.query(SymptomDeptMap).filter(SymptomDeptMap.tenant_id == tid).all()
            for r in rows:
                if r.keyword and r.keyword in symptom:
                    dept = s.get(Department, r.dept_id)
                    if dept and dept.tenant_id == tid:
                        return f"建议科室：{dept.name}"
            return "建议科室：请到分诊台由导诊进一步评估"

    def dept_map_rag(self, symptom: str) -> str:
        # 本地轻量 RAG：症状→科室画像检索（生产可替换为 Milvus/ES 向量检索）
        from ..kb import retrieve_department

        return retrieve_department(symptom)

    def hospital_rag(self, query: str) -> str:
        # 院内资料 RAG：就诊流程/医保/检查须知等「医院事务」检索（pgvector，doc_type='hospital'）
        from ..kb import retrieve_hospital

        return retrieve_hospital(query)

    def query_availability(self, department: str, date: str = "today") -> str:
        if date in ("", "today"):
            date = _today()
        with get_session() as s:
            tid = resolve_tenant_id()
            dept = (
                s.query(Department)
                .filter(Department.name == department, Department.tenant_id == tid)
                .first()
            )
            if not dept:
                return f"[availability] 未找到科室「{department}」"
            remaining = 0
            # 显式按 tenant_id 过滤医生与排班：科室已按租户隔离，此处再显式过滤
            # 构成第二道防线，即使未来有人绕过科室 JOIN 也不会跨院区读号源。
            for doc in s.query(Doctor).filter(Doctor.dept_id == dept.id, Doctor.tenant_id == tid):
                for sch in s.query(DoctorSchedule).filter_by(
                    doctor_id=doc.id, work_date=date, tenant_id=tid
                ):
                    remaining += max(0, sch.total_slots - sch.booked_slots)
            return f"[availability] {department} {date} 剩余号源：{remaining}"

    def lock_appointment(self, department: str, date: str, slot: str) -> str:
        pid_username = _require_patient()
        pid = _resolve_user_id(pid_username)
        key = stable_key("apt:lock", pid, department, date, slot)
        return run_idempotent(key, lambda: self._lock_appointment_impl(department, date, slot, pid))

    def _lock_appointment_impl(self, department: str, date: str, slot: str, pid: int) -> str:
        if date in ("", "today"):
            date = _today()
        with get_session() as s:
            tid = resolve_tenant_id()
            dept = (
                s.query(Department)
                .filter(Department.name == department, Department.tenant_id == tid)
                .first()
            )
            if not dept:
                return f"[locked] 未找到科室「{department}」，锁号失败"

            # 单患者单日挂号上限：防止 Agent 失控循环或恶意囤号耗尽号源
            booked_today = (
                s.query(Appointment)
                .filter(Appointment.patient_id == pid, Appointment.work_date == date)
                .count()
            )
            if booked_today >= MAX_APPOINTMENTS_PER_DAY:
                return (
                    f"[locked] 当日挂号已达上限（{MAX_APPOINTMENTS_PER_DAY} 个），"
                    f"如需继续请前往人工窗口"
                )

            scheds = (
                s.query(DoctorSchedule)
                .join(Doctor)
                .filter(
                    Doctor.dept_id == dept.id,
                    Doctor.tenant_id == tid,
                    DoctorSchedule.work_date == date,
                    DoctorSchedule.tenant_id == tid,
                    DoctorSchedule.booked_slots < DoctorSchedule.total_slots,
                )
                .all()
            )
            if not scheds:
                return f"[locked] {department} {date} 号源已约满"

            # 并发安全：用原子 UPDATE 占位（读改写 booked_slots += 1 在 Postgres
            # 并发下会丢更新导致超卖）。逐个候选尝试，直到某条 rowcount==1 即占位成功。
            sched = None
            for cand in scheds:
                res = s.execute(
                    text(
                        "UPDATE doctor_schedules SET booked_slots = booked_slots + 1 "
                        "WHERE id = :sid AND booked_slots < total_slots"
                    ),
                    {"sid": cand.id},
                )
                if res.rowcount == 1:
                    sched = cand
                    break
            if sched is None:
                s.rollback()
                return f"[locked] {department} {date} 号源刚被抢完，请重新选择"

            s.expire(sched)  # 原子 UPDATE 后刷新 ORM 缓存，读回真实 booked_slots
            doc = s.get(Doctor, sched.doctor_id)
            appt = Appointment(
                patient_id=pid,
                doctor_id=doc.id,
                schedule_id=sched.id,
                work_date=date,
                period=sched.period,
                slot_index=sched.booked_slots,
                status="LOCKED",
                tenant_id=tid,
            )
            s.add(appt)
            s.commit()
            visit_time = _slot_time(sched.period, sched.booked_slots)
            return (
                f"[locked] 医院={_HOSPITAL_NAME} 科室={department} 医生={doc.full_name} "
                f"日期={date} 时段={sched.period} 就诊序号={sched.booked_slots}号 "
                f"预计就诊时间={visit_time} appointment_id=APT-{appt.id}"
            )

    def confirm_appointment(self, appointment_id: str = "") -> str:
        pid_username = _require_patient()
        pid = _resolve_user_id(pid_username)
        with get_session() as s:
            if appointment_id:
                aid = _id_of(appointment_id)
                if aid is None:
                    return "[confirmed] 无效的预约号"
                appt = s.get(Appointment, aid)
            else:
                appt = (
                    s.query(Appointment)
                    .filter(Appointment.patient_id == pid, Appointment.status == "LOCKED")
                    .order_by(Appointment.created_at.desc())
                    .first()
                )
            if not appt or appt.patient_id != pid:
                return "[confirmed] 未找到可确认的预约"
            appt.status = "CONFIRMED"
            s.commit()
            doc = s.get(Doctor, appt.doctor_id)
            dept = s.get(Department, doc.dept_id) if doc else None
            visit_time = _slot_time(appt.period, appt.slot_index)
            return (
                f"[confirmed] 医院={_HOSPITAL_NAME} 科室={dept.name if dept else '—'} "
                f"医生={doc.full_name if doc else '—'} 日期={appt.work_date} "
                f"时段={appt.period} 就诊序号={appt.slot_index}号 "
                f"预计就诊时间={visit_time} appointment_id=APT-{appt.id}"
            )

    def medicare_settle(self, appointment_id: str) -> str:
        """医保结算：严格校验预约归属，越权即拒绝。

        安全要点：
        - 预约必须存在，且 ``patient_id`` 必须等于当前上下文患者 —— 杜绝
          「为他人的预约办理医保结算」（资金/医保欺诈）。
        - 移除旧的「退而求其次结算该患者最近一笔预约」兜底：那是绕过归属校验的暗门。
        """
        pid_username = _require_patient()
        pid = _resolve_user_id(pid_username)
        aid = _id_of(appointment_id)
        if aid is None:
            return f"[settled] 无效的预约号：{appointment_id}"
        with get_session() as s:
            appt = s.get(Appointment, aid)
            if appt is None or appt.patient_id != pid:
                log.warning(
                    "security.denied",
                    extra={
                        "reason": "settle_foreign_appointment",
                        "context_patient": pid_username,
                        "appointment_id": aid,
                    },
                )
                return "[settled] 预约不存在或不属于当前患者，无法结算"
            appt.medicare_settled = True
            s.commit()
            return f"[settled] appointment_id=APT-{appt.id} 医保结算完成（统筹按本地政策）"

    # --- LIS ---
    def read_lab_report(
        self, patient_id: Optional[str] = None, item_name: Optional[str] = None
    ) -> str:
        # 患者私有档案：仅读取当前上下文患者本人的独立库（身份不可由调用方指定）
        patient_id = _resolve_patient(patient_id)
        with get_patient_session(patient_id) as s:
            q = s.query(LabReport).filter(LabReport.patient_id == patient_id)
            if item_name:
                q = q.filter(LabReport.item.ilike(f"%{item_name}%"))
            rows = q.order_by(LabReport.report_date.desc(), LabReport.id.desc()).all()
            if not rows:
                suffix = f"（项目 {item_name}）" if item_name else ""
                return f"[LIS] {patient_id}{suffix} 暂无检验报告"
            parts = [
                f"{r.item}:{r.result}(参考{r.ref_range}){' 异常' if r.abnormal else ''}"
                for r in rows
            ]
            return "[LIS] " + "; ".join(parts)

    def clinical_kb(self, query: str) -> str:
        # 本地轻量 RAG：临床指引检索（生产可替换为 SNOMED/Milvus 向量检索）
        from ..kb import retrieve

        return retrieve(query)

    def read_vitals(self, patient_id: Optional[str] = None) -> str:
        patient_id = _resolve_patient(patient_id)
        with get_patient_session(patient_id) as s:
            rows = s.query(VitalSign).filter(VitalSign.patient_id == patient_id).all()
            if not rows:
                return f"[vitals] {patient_id} 暂无生命体征记录"
            parts = [f"{r.type} {r.value}{r.unit or ''}" for r in rows]
            return "[vitals] " + "; ".join(parts)

    # --- 患者档案写入（自动导入：对话中识别到结果即落库） ---
    def record_lab_result(
        self,
        item: str,
        result: str,
        ref_range: str = "",
        abnormal: bool = False,
        report_date: str = "",
    ) -> str:
        """写入一条检验报告到当前登录患者本人的私有库（自动导入）。"""
        pid = _require_patient()
        report_date = report_date or _today()
        key = stable_key("lab", pid, item, result, report_date)
        return run_idempotent(
            key,
            lambda: self._record_lab_impl(pid, item, result, ref_range, abnormal, report_date),
        )

    def _record_lab_impl(self, pid, item, result, ref_range, abnormal, report_date) -> str:
        # 数据质量门：脏数据绝不静默落库（错误的健康数据危害不亚于泄漏）
        vr = dq.validate_lab(item, result, ref_range, abnormal, report_date)
        allowed, note = dq.gate("lab", vr)
        if not allowed:
            log.warning("data_quality.rejected", extra={"kind": "lab", "item": item})
            return f"[LIS] {note}"
        with get_patient_session(pid) as s:
            s.add(
                LabReport(
                    patient_id=pid,
                    item=item,
                    result=result,
                    ref_range=ref_range,
                    abnormal=abnormal,
                    report_date=report_date,
                )
            )
            s.commit()
        base = (
            f"[LIS] 已记录 {pid} 检验报告：{item}={result}"
            f"（参考{ref_range}）{' 异常' if abnormal else ''}"
        )
        return f"{base} {note}".rstrip()

    def record_vital(self, type: str, value: str, unit: str = "") -> str:
        """写入一条生命体征到当前登录患者本人的私有库（自动导入）。"""
        pid = _require_patient()
        measured = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # 同日同类型同数值视为重试，去重（避免 LLM 多轮重复写入）
        key = stable_key("vital", pid, type, value, measured[:10])
        return run_idempotent(
            key, lambda: self._record_vital_impl(pid, type, value, unit, measured)
        )

    def _record_vital_impl(self, pid, type, value, unit, measured) -> str:
        # 数据质量门：生理上不可能的数值直接拦下（如体温 370、血压 1200）
        vr = dq.validate_vital(type, value, unit)
        allowed, note = dq.gate("vital", vr)
        if not allowed:
            log.warning("data_quality.rejected", extra={"kind": "vital", "type": type})
            return f"[vitals] {note}"
        with get_patient_session(pid) as s:
            s.add(
                VitalSign(patient_id=pid, type=type, value=value, unit=unit, measured_at=measured)
            )
            s.commit()
        base = f"[vitals] 已记录 {pid} 生命体征：{type} {value}{unit or ''}"
        return f"{base} {note}".rstrip()

    def record_case_summary(self, text: str, category: str = "general") -> str:
        """写入一段病例小结/主诉到当前登录患者本人的长期记忆（自动导入）。"""
        pid = _require_patient()
        # 数据质量门：空内容不写库（LLM 抽取失败时的常见产物）
        vr = dq.validate_case_summary(text, category)
        allowed, note = dq.gate("case_summary", vr)
        if not allowed:
            log.warning("data_quality.rejected", extra={"kind": "case_summary"})
            return f"[memory] {note}"
        with get_patient_session(pid) as s:
            s.add(
                ConversationMemory(
                    thread_id="",
                    patient_id=pid,
                    key=f"case_summary:{category}",
                    value=text,
                )
            )
            s.commit()
        return f"[memory] 已记录 {pid} 病例小结（{category}）：{text[:40]}"

    # --- Notify ---
    def plan_reminder(self, text: str, patient_id: Optional[str] = None) -> str:
        # 幂等：同一患者+相同提醒内容在 ttl 内只写一条（防止重试重复发提醒）
        patient_id = _resolve_patient(patient_id)
        key = stable_key("reminder", patient_id, text)
        return run_idempotent(key, lambda: self._plan_reminder_impl(patient_id, text))

    def _plan_reminder_impl(self, patient_id: str, text: str) -> str:
        with get_patient_session(patient_id) as s:
            s.add(Reminder(patient_id=patient_id, content=text, channel="APP"))
            s.commit()
        return f"[reminder] 已为 {patient_id} 创建提醒：{text}"

    # --- Memory ---
    def memory_append(self, note: str, patient_id: Optional[str] = None) -> str:
        patient_id = _resolve_patient(patient_id)
        with get_patient_session(patient_id) as s:
            s.add(
                ConversationMemory(
                    thread_id="",
                    patient_id=patient_id,
                    key="followup_note",
                    value=note,
                )
            )
            s.commit()
        return f"[memory] 已记录 {patient_id} 随访笔记：{note}"

    # --- Emergency ---
    def handoff(self) -> str:
        return "[emergency] 已转接急诊人工台，请保持通话"

    def call_120(self, patient_id: Optional[str] = None) -> str:
        patient_id = _resolve_patient(patient_id)
        if patient_id == ANONYMOUS:
            return "[emergency] 已触发 120 呼叫流程（匿名，不记录档案）"
        with get_patient_session(patient_id) as s:
            s.add(EmergencyEvent(patient_id=patient_id, content="120 呼叫"))
            s.commit()
        return "[emergency] 已触发 120 呼叫流程"


# ---------------------------------------------------------------------------
# 批量导入（管理端 API 与离线脚本共用的落库函数）
# ---------------------------------------------------------------------------
def bulk_import_patient(
    patient: str,
    lab_reports: list | None = None,
    vital_signs: list | None = None,
    case_summaries: list | None = None,
) -> dict:
    """批量导入某患者的检验结果 / 生命体征 / 病例小结到其私有库。

    供 ``POST /api/import/patient-data`` 与 ``scripts/import_patient_data.py`` 复用。
    安全约定：**不自动建档** —— 患者必须是已注册用户名，否则抛 ``ValueError``。
    """
    if not is_db_enabled():
        raise RuntimeError("DB 未启用，批量导入不可用（请设置 DATABASE_URL）")
    with get_session() as s:
        u = s.query(User).filter(User.username == patient).first()
        if not u:
            raise ValueError(f"患者 {patient} 不存在，请先注册/建档后再导入")
    counts = {"lab_reports": 0, "vital_signs": 0, "case_summaries": 0}
    with get_patient_session(patient) as s:
        for r in lab_reports or []:
            s.add(
                LabReport(
                    patient_id=patient,
                    item=str(r.get("item", "")),
                    result=str(r.get("result", "")),
                    ref_range=str(r.get("ref_range", "")),
                    abnormal=bool(r.get("abnormal", False)),
                    report_date=str(r.get("report_date") or _today()),
                )
            )
            counts["lab_reports"] += 1
        for v in vital_signs or []:
            s.add(
                VitalSign(
                    patient_id=patient,
                    type=str(v.get("type", "")),
                    value=str(v.get("value", "")),
                    unit=str(v.get("unit", "")),
                    measured_at=str(
                        v.get("measured_at")
                        or datetime.now(timezone.utc).isoformat(timespec="seconds")
                    ),
                )
            )
            counts["vital_signs"] += 1
        for c in case_summaries or []:
            text = c.get("text") if isinstance(c, dict) else str(c)
            cat = c.get("category", "general") if isinstance(c, dict) else "general"
            s.add(
                ConversationMemory(
                    thread_id="", patient_id=patient, key=f"case_summary:{cat}", value=text
                )
            )
            counts["case_summaries"] += 1
        s.commit()
    return counts


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def get_hub():
    """按配置返回适配器：设了 DATABASE_URL → 真实 DbHub，否则 MemoryHub。"""
    if is_db_enabled():
        return DbHub()
    return MemoryHub()
