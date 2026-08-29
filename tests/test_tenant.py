"""多院区 / 多租户：departments 按 tenant_id 隔离的单元测试 + 端点校验。

重点验证：
1. 默认租户被种子数据创建（向后兼容）。
2. resolve_tenant_id 在「无上下文」时回退默认租户。
3. 科室主数据按租户严格隔离（不同租户的 symptom→科室映射互不可见）。
4. X-Tenant-Id 请求头在 /api/departments 端点生效。
5. 管理员可新建租户 / 在指定租户下新建科室。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from src.db import Department, SymptomDeptMap, Tenant, get_session
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
