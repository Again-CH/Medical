"""业务集成层（端口 + 适配器）。

这是「真实落地」的关键抽象：工具只依赖下面的端口（Protocol）契约，具体实现可插拔。
- MemoryHub：原 demo 的写死逻辑，零依赖、确定性，用于离线/测试。
- DbHub：基于 SQLAlchemy ORM + 真实数据库（Postgres/SQLite 同构），生产实现。

要接真实医院系统（HIS / 医保网关 / LIS / 短信网关），只需新增一个实现了这些端口的类
（例如 ``ApiHub``），在 ``get_hub()`` 里按配置切换即可 —— 工具与编排代码一行都不用改。

get_hub() 的选择逻辑：
- 设置 DATABASE_URL        → DbHub（真实持久化）
- 未设置（离线/demo/测试） → MemoryHub
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

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
    get_session,
    is_db_enabled,
)


# ---------------------------------------------------------------------------
# 端口（契约）
# ---------------------------------------------------------------------------
@runtime_checkable
class HISPort(Protocol):
    def search_department(self, symptom: str) -> str: ...
    def dept_map_rag(self, symptom: str) -> str: ...
    def query_availability(self, department: str, date: str) -> str: ...
    def lock_appointment(self, department: str, date: str, slot: str) -> str: ...
    def medicare_settle(self, appointment_id: str) -> str: ...


@runtime_checkable
class LISPort(Protocol):
    def read_lab_report(self, patient_id: str) -> str: ...
    def clinical_kb(self, query: str) -> str: ...
    def read_vitals(self, patient_id: str) -> str: ...


@runtime_checkable
class NotifyPort(Protocol):
    def plan_reminder(self, patient_id: str, text: str) -> str: ...


@runtime_checkable
class MemoryPort(Protocol):
    def memory_append(self, thread_id: str, patient_id: str, note: str) -> str: ...


@runtime_checkable
class EmergencyPort(Protocol):
    def handoff(self) -> str: ...
    def call_120(self, patient_id: str, content: str) -> str: ...


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
        return "建议科室：全科 / 分诊台进一步评估"

    def dept_map_rag(self, symptom: str) -> str:
        return f"[RAG] 症状「{symptom}」→ 科室画像与候诊时长已检索"

    def query_availability(self, department: str, date: str = "today") -> str:
        return f"[availability] {department} {date} 剩余号源：3"

    def lock_appointment(self, department: str, date: str, slot: str) -> str:
        return f"[locked] {department} {date} {slot} 已锁定 appointment_id=APT-1001"

    def medicare_settle(self, appointment_id: str) -> str:
        return f"[settled] {appointment_id} 医保结算完成"

    def read_lab_report(self, patient_id: str) -> str:
        return f"[LIS] {patient_id} 血常规：WBC 正常，CRP 轻度升高"

    def clinical_kb(self, query: str) -> str:
        return f"[KB] 关于「{query}」的临床指引已检索"

    def read_vitals(self, patient_id: str) -> str:
        return f"[vitals] {patient_id} 血压 128/82，心率 72"

    def plan_reminder(self, patient_id: str, text: str) -> str:
        return f"[reminder] 已为 {patient_id} 创建提醒：{text}"

    def memory_append(self, thread_id: str, patient_id: str, note: str) -> str:
        return f"[memory] 已记录 {patient_id} 随访笔记：{note}"

    def handoff(self) -> str:
        return "[emergency] 已转接急诊人工台，请保持通话"

    def call_120(self, patient_id: str = None, content: str = "") -> str:
        return "[emergency] 已触发 120 呼叫流程"


# ---------------------------------------------------------------------------
# DbHub：基于 ORM 的真实实现
# ---------------------------------------------------------------------------
def _today() -> str:
    return date.today().isoformat()


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
    """把患者标识（JWT sub / patient_id）解析为 users.id；不存在则按用户名自动建档。"""
    with get_session() as s:
        u = s.query(User).filter(User.username == username).first()
        if not u:
            u = User(username=username, password_hash="", full_name=username)
            s.add(u)
            s.commit()
            return u.id
        return u.id


class DbHub:
    # --- HIS ---
    def search_department(self, symptom: str) -> str:
        with get_session() as s:
            rows = s.query(SymptomDeptMap).all()
            for r in rows:
                if r.keyword and r.keyword in symptom:
                    dept = s.get(Department, r.dept_id)
                    return f"建议科室：{dept.name}"
            return "建议科室：全科 / 分诊台进一步评估"

    def dept_map_rag(self, symptom: str) -> str:
        # 真实可替换为 Milvus / ES 向量检索；此处仅占位
        return f"[RAG] 症状「{symptom}」→ 科室画像与候诊时长已检索（向量检索接入点）"

    def query_availability(self, department: str, date: str = "today") -> str:
        if date in ("", "today"):
            date = _today()
        with get_session() as s:
            dept = s.query(Department).filter(Department.name == department).first()
            if not dept:
                return f"[availability] 未找到科室「{department}」"
            remaining = 0
            for doc in s.query(Doctor).filter(Doctor.dept_id == dept.id):
                for sch in s.query(DoctorSchedule).filter_by(doctor_id=doc.id, work_date=date):
                    remaining += max(0, sch.total_slots - sch.booked_slots)
            return f"[availability] {department} {date} 剩余号源：{remaining}"

    def lock_appointment(self, department: str, date: str, slot: str) -> str:
        pid = _resolve_user_id(patient_ctx.get())
        if date in ("", "today"):
            date = _today()
        with get_session() as s:
            dept = s.query(Department).filter(Department.name == department).first()
            if not dept:
                return f"[locked] 未找到科室「{department}」，锁号失败"
            sched = (
                s.query(DoctorSchedule)
                .join(Doctor)
                .filter(
                    Doctor.dept_id == dept.id,
                    DoctorSchedule.work_date == date,
                    DoctorSchedule.booked_slots < DoctorSchedule.total_slots,
                )
                .first()
            )
            if not sched:
                return f"[locked] {department} {date} 号源已约满"
            sched.booked_slots += 1
            doc = s.get(Doctor, sched.doctor_id)
            appt = Appointment(
                patient_id=pid,
                doctor_id=doc.id,
                schedule_id=sched.id,
                work_date=date,
                period=sched.period,
                slot_index=sched.booked_slots,
                status="LOCKED",
            )
            s.add(appt)
            s.commit()
            return (
                f"[locked] 已锁定预约 appointment_id=APT-{appt.id} "
                f"科室={department} 医生={doc.full_name} 时间={date} {sched.period} "
                f"第{sched.booked_slots}号"
            )

    def medicare_settle(self, appointment_id: str) -> str:
        aid = _id_of(appointment_id)
        with get_session() as s:
            appt = None
            if aid is not None:
                appt = s.get(Appointment, aid)
            if appt is None and patient_ctx.get() != "anonymous":
                # fake 模式下 LLM 给的是固定 APT-1001，退而求其次：结算该患者最近一笔预约
                pid = _resolve_user_id(patient_ctx.get())
                appt = (
                    s.query(Appointment)
                    .filter(Appointment.patient_id == pid)
                    .order_by(Appointment.id.desc())
                    .first()
                )
            if appt is not None:
                appt.medicare_settled = True
                s.commit()
                return f"[settled] appointment_id=APT-{appt.id} 医保结算完成（统筹按本地政策）"
            return f"[settled] {appointment_id} 医保结算完成（演示）"

    # --- LIS ---
    def read_lab_report(self, patient_id: str) -> str:
        pid = _resolve_user_id(patient_id)
        with get_session() as s:
            rows = s.query(LabReport).filter(LabReport.patient_id == pid).all()
            if not rows:
                return f"[LIS] {patient_id} 暂无检验报告"
            parts = [
                f"{r.item}:{r.result}(参考{r.ref_range}){' 异常' if r.abnormal else ''}"
                for r in rows
            ]
            return "[LIS] " + "; ".join(parts)

    def clinical_kb(self, query: str) -> str:
        # 真实可替换为 RAG 知识库检索；此处返回结构化占位答案
        return f"[KB] 关于「{query}」的临床指引已检索（RAG 知识库接入点）"

    def read_vitals(self, patient_id: str) -> str:
        pid = _resolve_user_id(patient_id)
        with get_session() as s:
            rows = s.query(VitalSign).filter(VitalSign.patient_id == pid).all()
            if not rows:
                return f"[vitals] {patient_id} 暂无生命体征记录"
            parts = [f"{r.type} {r.value}{r.unit or ''}" for r in rows]
            return "[vitals] " + "; ".join(parts)

    # --- Notify ---
    def plan_reminder(self, patient_id: str, text: str) -> str:
        pid = _resolve_user_id(patient_id)
        with get_session() as s:
            s.add(Reminder(patient_id=pid, content=text, channel="APP"))
            s.commit()
        return f"[reminder] 已为 {patient_id} 创建提醒：{text}"

    # --- Memory ---
    def memory_append(self, thread_id: str, patient_id: str, note: str) -> str:
        pid = _resolve_user_id(patient_id)
        with get_session() as s:
            s.add(
                ConversationMemory(
                    thread_id=thread_id or "",
                    patient_id=pid,
                    key="followup_note",
                    value=note,
                )
            )
            s.commit()
        return f"[memory] 已记录 {patient_id} 随访笔记：{note}"

    # --- Emergency ---
    def handoff(self) -> str:
        return "[emergency] 已转接急诊人工台，请保持通话"

    def call_120(self, patient_id: str = None, content: str = "") -> str:
        subj = patient_id or patient_ctx.get()
        pid = None if subj == "anonymous" else _resolve_user_id(subj)
        with get_session() as s:
            s.add(EmergencyEvent(patient_id=pid, content=content or "120 呼叫"))
            s.commit()
        return "[emergency] 已触发 120 呼叫流程"


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def get_hub():
    """按配置返回适配器：设了 DATABASE_URL → 真实 DbHub，否则 MemoryHub。"""
    if is_db_enabled():
        return DbHub()
    return MemoryHub()
