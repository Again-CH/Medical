"""网关 HTTP 冒烟测试：用 FastAPI TestClient 验证鉴权与服务可用。

不依赖真实 LLM / Postgres：使用 fake 模型 + 内存存储即可开箱验证。
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

# 确保能 import src 包（与 test_graph.py 一致）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.gateway import app  # noqa: E402

PATIENT_TOKEN = "Bearer patient:alice"
DOCTOR_TOKEN = "Bearer doctor:drwang"


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
    # 正确 token → 200
    r = client.get("/api/review/pending", headers={"Authorization": PATIENT_TOKEN})
    assert r.status_code == 200
    assert "pending" in r.json()


def test_rbac_doctor_only_on_resolve(client):
    # 患者 token 不能审批
    r = client.post(
        "/api/review/resolve",
        headers={"Authorization": PATIENT_TOKEN},
        json={"approval_id": "x", "decision": {"approved": True}},
    )
    assert r.status_code == 403


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
