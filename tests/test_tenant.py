"""多院区 / 多租户：departments 按 tenant_id 隔离的单元测试 + 端点校验。

重点验证：
1. 默认租户被种子数据创建（向后兼容）。
2. resolve_tenant_id 在「无上下文」时回退默认租户。
3. 科室主数据按租户严格隔离（不同租户的 symptom→科室映射互不可见）。
4. X-Tenant-Id 请求头在 /api/departments 端点生效。
5. 管理员可新建租户 / 在指定租户下新建科室。
6. **业务主数据隔离（第二轮扩展）**：doctors / doctor_schedules / appointments /
   exam_steps 同样按 tenant_id 隔离；跨院区读不到他院区的医生、号源与检查单。
7. ``users`` 刻意不加 tenant_id（患者可跨院区就诊，身份全局共享）——由
   ``test_patient_identity_is_global`` 固化该建模决策，防止后人误加。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from src.context import patient_ctx
from src.db import (
    Appointment,
    Department,
    Doctor,
    DoctorSchedule,
    ExamStep,
    SymptomDeptMap,
    Tenant,
    User,
    get_session,
)
from src.integrations import DbHub
from src.tenant import (
    DEFAULT_TENANT_CODE,
    resolve_tenant_id,
    set_tenant_context,
)


def _default_tid() -> int:
    with get_session() as s:
        return s.query(Tenant).filter(Tenant.code == DEFAULT_TENANT_CODE).first().id


@pytest.fixture
def second_tenant():
    """创建一个隔离的第二个院区及其科室，测试结束后清理，避免污染共享 sqlite。"""
    with get_session() as s:
        t = Tenant(code="CAMPUS_E", name="城东院区", is_default=False)
        s.add(t)
        s.flush()
        tid = t.id
        d = Department(code="ORTHO_E", name="骨科东", description="骨科", tenant_id=tid)
        s.add(d)
        s.flush()
        s.add(SymptomDeptMap(keyword="腰疼", dept_id=d.id, weight=1, tenant_id=tid))
        s.commit()
    try:
        yield tid
    finally:
        set_tenant_context(None)
        with get_session() as s:
            s.execute(text("DELETE FROM symptom_dept_map WHERE tenant_id = :t"), {"t": tid})
            s.execute(text("DELETE FROM departments WHERE tenant_id = :t"), {"t": tid})
            s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
            s.commit()


@pytest.fixture
def tenant_with_doctor():
    """第二个院区 + 科室 + 医生 + 今日排班，用于验证「业务主数据」的租户隔离。

    与 second_tenant 分开，避免牵动仅关心科室映射的既有用例。
    """
    today = date.today().isoformat()
    with get_session() as s:
        t = Tenant(code="CAMPUS_S", name="城南院区", is_default=False)
        s.add(t)
        s.flush()
        tid = t.id
        d = Department(code="ORTHO_S", name="骨科南", description="骨科", tenant_id=tid)
        s.add(d)
        s.flush()
        doc = Doctor(
            username="dr_south",
            password_hash="not-a-real-hash",
            full_name="南医生",
            title="主任医师",
            dept_id=d.id,
            tenant_id=tid,
        )
        s.add(doc)
        s.flush()
        s.add(
            DoctorSchedule(
                doctor_id=doc.id,
                work_date=today,
                period="AM",
                total_slots=5,
                booked_slots=0,
                tenant_id=tid,
            )
        )
        s.commit()
    try:
        yield tid, today
    finally:
        set_tenant_context(None)
        patient_ctx.set(None)
        with get_session() as s:
            s.execute(text("DELETE FROM exam_steps WHERE tenant_id = :t"), {"t": tid})
            s.execute(text("DELETE FROM appointments WHERE tenant_id = :t"), {"t": tid})
            s.execute(
                text(
                    "DELETE FROM doctor_schedules WHERE doctor_id IN "
                    "(SELECT id FROM doctors WHERE tenant_id = :t)"
                ),
                {"t": tid},
            )
            s.execute(text("DELETE FROM doctors WHERE tenant_id = :t"), {"t": tid})
            s.execute(text("DELETE FROM departments WHERE tenant_id = :t"), {"t": tid})
            s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
            s.commit()


def test_default_tenant_seeded():
    assert _default_tid() is not None


def test_resolve_falls_back_to_default():
    set_tenant_context(None)
    assert resolve_tenant_id() == _default_tid()


def test_department_scoping_isolation(second_tenant):
    dtid = _default_tid()
    t2 = second_tenant
    hub = DbHub()

    # 在 t2 上下文下，腰疼 → 骨科东
    set_tenant_context(t2)
    res = hub.search_department("我最近腰疼")
    assert "骨科东" in res

    # 在默认租户上下文下，腰疼不应命中 t2 的映射（互不可见）
    set_tenant_context(dtid)
    res2 = hub.search_department("我最近腰疼")
    assert "骨科东" not in res2


def test_departments_endpoint_header_filter(second_tenant):
    from fastapi.testclient import TestClient
    from src import gateway as gw

    t2 = second_tenant
    client = TestClient(gw.app)

    # 无头：返回默认租户科室（含神经内科），不含 t2 的骨科东
    r = client.get("/api/departments")
    assert r.status_code == 200
    codes = {d["code"] for d in r.json()}
    assert "NEUROLOGY" in codes
    assert "ORTHO_E" not in codes

    # 带 X-Tenant-Id: 只返回该租户科室
    r2 = client.get("/api/departments", headers={"X-Tenant-Id": str(t2)})
    assert r2.status_code == 200
    codes2 = {d["code"] for d in r2.json()}
    assert codes2 == {"ORTHO_E"}


def test_admin_tenant_endpoints(monkeypatch):
    from fastapi.testclient import TestClient
    from src import gateway as gw

    monkeypatch.setattr(gw, "ADMIN_API_KEY", "test-admin-key")
    client = TestClient(gw.app)
    headers = {"X-Admin-Key": "test-admin-key"}

    # 未授权 → 401
    r0 = client.get("/api/admin/tenants")
    assert r0.status_code == 401

    # 新建租户
    r1 = client.post(
        "/api/admin/tenants",
        json={"code": "CAMPUS_W", "name": "城西院区"},
        headers=headers,
    )
    assert r1.status_code == 200
    tid = r1.json()["id"]
    assert r1.json()["code"] == "CAMPUS_W"

    # 重复 code → 409
    r_dup = client.post(
        "/api/admin/tenants",
        json={"code": "CAMPUS_W", "name": "重复"},
        headers=headers,
    )
    assert r_dup.status_code == 409

    # 列出租户（应含默认 + 新建）
    r2 = client.get("/api/admin/tenants", headers=headers)
    assert r2.status_code == 200
    codes = {t["code"] for t in r2.json()}
    assert "DEFAULT" in codes and "CAMPUS_W" in codes

    # 在指定租户下新建科室
    r3 = client.post(
        "/api/admin/departments",
        json={"code": "ORTHO_W", "name": "骨科西", "tenant_id": tid},
        headers=headers,
    )
    assert r3.status_code == 200
    assert r3.json()["tenant_id"] == tid

    # 清理
    with get_session() as s:
        s.execute(text("DELETE FROM departments WHERE tenant_id = :t"), {"t": tid})
        s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        s.commit()


# ---------------- 业务主数据的租户隔离（第二轮扩展） ----------------


def test_doctor_and_schedule_tenant_isolation(tenant_with_doctor):
    """医生与排班按租户隔离：默认院区查不到城南院区的医生与其号源。"""
    t2, today = tenant_with_doctor
    dtid = _default_tid()

    with get_session() as s:
        # 城南院区能看到自己的医生与排班
        docs_t2 = s.query(Doctor).filter(Doctor.tenant_id == t2).all()
        assert [d.username for d in docs_t2] == ["dr_south"]
        sch_t2 = s.query(DoctorSchedule).filter(DoctorSchedule.tenant_id == t2).all()
        assert len(sch_t2) == 1 and sch_t2[0].total_slots == 5

        # 默认院区看不到城南院区的医生与排班
        docs_default = s.query(Doctor).filter(Doctor.tenant_id == dtid).all()
        assert "dr_south" not in {d.username for d in docs_default}
        sch_default = s.query(DoctorSchedule).filter(DoctorSchedule.tenant_id == dtid).all()
        south_doc_ids = {d.id for d in docs_t2}
        assert not any(sch.doctor_id in south_doc_ids for sch in sch_default)
        # 排班日期确实落在今天（供后续用例复用同一日期口径）
        assert sch_t2[0].work_date == today


def test_availability_is_tenant_scoped(tenant_with_doctor):
    """号源查询租户隔离：城南院区的科室只在城南上下文中可见。"""
    t2, today = tenant_with_doctor
    dtid = _default_tid()
    hub = DbHub()

    # 城南上下文：能查到「骨科南」的 5 个号源
    set_tenant_context(t2)
    res_t2 = hub.query_availability("骨科南", today)
    assert "剩余号源：5" in res_t2, res_t2

    # 默认上下文：查无此科室（而非返回城南的号源）
    set_tenant_context(dtid)
    res_default = hub.query_availability("骨科南", today)
    assert "未找到科室" in res_default, res_default


def test_appointment_carries_tenant_id(tenant_with_doctor):
    """挂号产生的预约带有正确的 tenant_id（归属发生挂号的院区）。

    自建专用患者：全新库上 TestClient 不触发 lifespan 播种，不能依赖 alice 存在，
    否则用例会被 skip —— 跳过的断言等于没测。
    """
    t2, today = tenant_with_doctor
    hub = DbHub()
    probe = "tenant_book_patient"

    with get_session() as s:
        if s.query(User).filter(User.username == probe).first() is None:
            s.add(
                User(
                    username=probe,
                    password_hash="not-a-real-hash",
                    full_name="挂号探针",
                    token_version=0,
                )
            )
            s.commit()

    set_tenant_context(t2)
    patient_ctx.set(probe)
    try:
        out = hub.lock_appointment("骨科南", today, "AM")
        assert out.startswith("[locked]"), out
        # 锁号成功应扣减城南院区的号源，且返回的医生来自城南院区
        assert "南医生" in out, out
    finally:
        patient_ctx.set(None)
        set_tenant_context(None)

    try:
        with get_session() as s:
            appts = s.query(Appointment).filter(Appointment.tenant_id == t2).all()
            assert len(appts) == 1, f"应恰好产生 1 条城南院区预约，实际 {len(appts)}"
            assert appts[0].tenant_id == t2
    finally:
        with get_session() as s:
            s.execute(
                text(
                    "DELETE FROM appointments WHERE patient_id IN "
                    "(SELECT id FROM users WHERE username = :p)"
                ),
                {"p": probe},
            )
            s.execute(text("DELETE FROM users WHERE username = :p"), {"p": probe})
            s.commit()


def test_exam_order_tenant_isolation(tenant_with_doctor):
    """检查单按租户隔离：城南开单，默认院区查询看不到。"""
    from fastapi.testclient import TestClient
    from src import gateway as gw
    from src.auth import create_token

    t2, _today = tenant_with_doctor
    client = TestClient(gw.app)
    headers = {"Authorization": "Bearer " + create_token("drwang", "doctor")}

    # 自建专用患者，不依赖种子数据与测试执行顺序：
    # 全新库上 TestClient 不触发 lifespan 播种，alice 此时可能还不存在。
    probe = "tenant_probe_patient"
    with get_session() as s:
        if s.query(User).filter(User.username == probe).first() is None:
            s.add(
                User(
                    username=probe,
                    password_hash="not-a-real-hash",
                    full_name="租户探针",
                    token_version=0,
                )
            )
            s.commit()

    try:
        # 在城南院区为该患者开一张检查单
        r = client.post(
            "/api/doctor/exam-orders",
            json={"patient_username": probe, "steps": [{"name": "PET-CT"}]},
            headers={**headers, "X-Tenant-Id": str(t2)},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["created"]) == 1

        # 默认院区查询：看不到城南的检查单
        r_default = client.get(f"/api/doctor/exam-orders?patient={probe}", headers=headers)
        assert r_default.status_code == 200
        assert r_default.json()["steps"] == [], "默认院区不应看到城南检查单"

        # 切到城南院区：可见
        r_t2 = client.get(
            f"/api/doctor/exam-orders?patient={probe}",
            headers={**headers, "X-Tenant-Id": str(t2)},
        )
        assert r_t2.status_code == 200
        names_t2 = [st["name"] for st in r_t2.json()["steps"]]
        assert "PET-CT" in names_t2, f"城南院区应看到自己的检查单：{names_t2}"

        # 直接按 id 改单也受租户约束：默认院区改城南的单 → 404（视为不存在）
        step_id = r_t2.json()["steps"][0]["id"]
        r_cross = client.put(
            f"/api/doctor/exam-steps/{step_id}",
            json={"status": "DONE"},
            headers=headers,
        )
        assert r_cross.status_code == 404, "跨院区按 id 改单应视为不存在"
    finally:
        with get_session() as s:
            s.execute(text("DELETE FROM exam_steps WHERE patient_username = :p"), {"p": probe})
            s.execute(text("DELETE FROM users WHERE username = :p"), {"p": probe})
            s.commit()


def test_patient_identity_is_global():
    """固化建模决策：users 表**不**加 tenant_id（患者可跨院区就诊）。

    若未来有人误给 users 加 tenant_id 并把账号按院区切分，会导致同一患者
    在不同院区重复建档、病历碎片化。此用例把「users 无 tenant_id」钉死。
    """
    cols = set(User.__table__.columns.keys())
    assert "tenant_id" not in cols, "users 表不应有 tenant_id（身份应全局共享）"

    # 反向确认：业务表都带上了 tenant_id
    for model in (Doctor, DoctorSchedule, Appointment, ExamStep):
        assert "tenant_id" in set(model.__table__.columns.keys()), f"{model.__name__} 缺 tenant_id"
