"""网关 HTTP 测试：用 FastAPI TestClient 验证 JWT 鉴权、RBAC 与 SSE 流式。

全部跑在真实数据库（conftest 设置的 sqlite）上：注册/登录落库、审批/审计走 ORM 存储。
不依赖外部 LLM（默认 fake 模式即可跑通 SSE 流式）。
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.auth import create_token  # noqa: E402
from src.gateway import app  # noqa: E402

# 直接用 auth 的 create_token 签发 JWT（也可走 /auth/login 端点，见 test_login_flow）
PATIENT_TOKEN = "Bearer " + create_token("alice", "patient")
DOCTOR_TOKEN = "Bearer " + create_token("drwang", "doctor")


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_required(client):
    # 缺 token → 401
    assert client.get("/api/review/pending").status_code == 401
    # 错误 token → 401
    assert (
        client.get("/api/review/pending", headers={"Authorization": "Bearer bad"}).status_code
        == 401
    )
    # 患者 token 不能访问医护接口（需 doctor）→ 403
    assert (
        client.get("/api/review/pending", headers={"Authorization": PATIENT_TOKEN}).status_code
        == 403
    )
    # 医生 token → 200
    r = client.get("/api/review/pending", headers={"Authorization": DOCTOR_TOKEN})
    assert r.status_code == 200
    assert "pending" in r.json()


def test_rbac_doctor_only_on_resolve(client):
    r = client.post(
        "/api/review/resolve",
        headers={"Authorization": PATIENT_TOKEN},
        json={"approval_id": "x", "decision": {"approved": True}},
    )
    assert r.status_code == 403


def test_login_flow(client):
    # 注册 → 登录 → 拿到 JWT
    r = client.post(
        "/auth/register",
        json={"username": "bob", "password": "bob123", "role": "patient"},
    )
    assert r.status_code == 200
    r = client.post(
        "/auth/login",
        json={"username": "alice", "password": "alice123", "role": "patient"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]
    # 错误密码 → 401
    bad = client.post(
        "/auth/login",
        json={"username": "alice", "password": "wrong", "role": "patient"},
    )
    assert bad.status_code == 401


def test_audit_endpoint(client):
    r = client.get("/api/audit", headers={"Authorization": DOCTOR_TOKEN})
    assert r.status_code == 200
    assert "audit" in r.json()


def test_chat_sse_stream(client):
    """分诊类消息应触发 SSE 流式响应，且最终带 done 事件。"""
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": PATIENT_TOKEN},
        json={"message": "我头痛得厉害，挂什么科", "thread_id": "gw-smoke-triage"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    # 至少应出现 token（流式）或 system 兜底；最终必有 done 事件
    assert "type" in body and "done" in body
