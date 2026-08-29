"""幂等键单测：写操作（创建预约 / 发送提醒）在重试/重复调用时只生效一次。

验证 run_idempotent 落库去重：相同 (患者, 动作, 参数) 在同一 ttl 内只执行一次，
确保网络重试、上游重放不会重复锁号 / 重复发提醒。
"""

from src.context import patient_ctx
from src.db import (
    Appointment,
    Department,
    Doctor,
    DoctorSchedule,
    Reminder,
    User,
    get_patient_session,
    get_session,
)
from src.integrations import get_hub


def _alice_id():
    with get_session() as s:
        u = s.query(User).filter(User.username == "alice").first()
        assert u is not None, "测试前提：种子数据应包含 alice"
        return u.id


def _first_available():
    """取一条尚有号源的排班，返回 (科室名, 日期)。"""
    with get_session() as s:
        sch = (
            s.query(DoctorSchedule)
            .filter(DoctorSchedule.booked_slots < DoctorSchedule.total_slots)
            .first()
        )
        assert sch is not None, "测试前提：应有可用号源"
        doc = s.get(Doctor, sch.doctor_id)
        dept = s.get(Department, doc.dept_id)
        return dept.name, sch.work_date


def _apt_count(patient_id: int) -> int:
    with get_session() as s:
        return s.query(Appointment).filter(Appointment.patient_id == patient_id).count()


def test_lock_appointment_idempotent():
    hub = get_hub()  # 测试环境 DATABASE_URL 已设 → DbHub
    tok = patient_ctx.set("alice")
    try:
        pid = _alice_id()
        dept_name, wdate = _first_available()
        before = _apt_count(pid)

        r1 = hub.lock_appointment(dept_name, wdate, "09:30")
        r2 = hub.lock_appointment(dept_name, wdate, "09:30")  # 重试/重放

        after = _apt_count(pid)
        assert after - before == 1, "重复调用（同 key）应只创建一条预约"
        assert r1 == r2, "幂等返回应与首次结果一致"
    finally:
        patient_ctx.reset(tok)


def test_plan_reminder_idempotent():
    import uuid

    hub = get_hub()
    tok = patient_ctx.set("alice")
    try:
        # 用唯一文本避免 data/<user>.db 跨测试遗留数据干扰（患者库不在 test.db 内，不随会话重建）
        text = f"按时服药、每日监测血压-{uuid.uuid4().hex[:8]}"
        with get_patient_session("alice") as s:
            before = s.query(Reminder).filter(Reminder.content == text).count()

        # 患者身份来自上下文（OLP），不再由调用方传入 patient_id
        r1 = hub.plan_reminder(text)
        r2 = hub.plan_reminder(text)  # 重复调用

        with get_patient_session("alice") as s:
            after = s.query(Reminder).filter(Reminder.content == text).count()
        assert after - before == 1, "重复调用（同 key）应只写入一条提醒"
        assert r1 == r2
    finally:
        patient_ctx.reset(tok)
