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

# 服务端会把客户端传入的 thread_id 派生为 "{role}:{sub}:{client_tid}"（防跨患者会话越权），
# 故按 thread_id 查审计/回放链路时需使用派生后的完整值。
THREAD_PREFIX = "patient:alice:"


def _consent(client, token=PATIENT_TOKEN):
    """让指定患者先签署知情同意书（Tier-0 闸门要求），以便后续对话走正常链路。"""
    client.post("/api/consent", headers={"Authorization": token}, json={})


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
        json={"approval_id": "x", "decision": "approve"},
    )
    assert r.status_code == 403


def test_login_flow(client):
    # 注册 → 登录 → 拿到 JWT（用唯一用户名，避免与已持久化的 sqlite 数据冲突）
    import uuid

    new_user = f"carol_{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/auth/register",
        json={"username": new_user, "password": "carol123", "role": "patient"},
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
    _consent(client)
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


def test_chat_writes_audit_log(client):
    """每轮对话应落库一条 ChatLog（支撑执行流程可回滚查看）。"""
    import uuid

    from src.db import ChatLog, get_session

    tid = f"gw-audit-{uuid.uuid4().hex[:8]}"
    _consent(client)
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": PATIENT_TOKEN},
        json={"message": "我嗓子疼挂什么科", "thread_id": tid},
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "done" in body

    with get_session() as s:
        row = s.query(ChatLog).filter(ChatLog.thread_id == THREAD_PREFIX + tid).first()
        assert row is not None, "ChatLog 未落库"
        assert row.patient_id == "alice"
        assert row.trace_id, "trace_id 应关联 LangSmith run，非空"
        assert row.input_text == "我嗓子疼挂什么科"
        assert row.tool_used in ("knowledge_base", "llm", "fallback", "timeout")
        assert isinstance(row.latency_ms, int) and row.latency_ms >= 0


def test_chat_log_masks_pii_before_store(client):
    """日志脱敏：含手机号的主诉落库前必须脱敏，数据库不存原始 PII。"""
    import uuid

    from src.db import ChatLog, get_session
    from src.masking import mask_pii_text

    tid = f"gw-pii-{uuid.uuid4().hex[:8]}"
    _consent(client)
    msg = "我头痛想挂号，联系电话13812345678"
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": PATIENT_TOKEN},
        json={"message": msg, "thread_id": tid},
    ) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    with get_session() as s:
        row = s.query(ChatLog).filter(ChatLog.thread_id == THREAD_PREFIX + tid).first()
        assert row is not None, "ChatLog 应落库"
        assert "13812345678" not in row.input_text, "落库前应对 PII 脱敏"
        assert row.input_text == mask_pii_text(msg), "无 PII 部分应原样保留"


def test_chat_logs_readable_by_doctor(client):
    """医患权限隔离：患者不可访问，医护可按 thread_id 回放执行链路。"""
    import uuid

    tid = f"gw-logs-{uuid.uuid4().hex[:8]}"
    _consent(client)
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": PATIENT_TOKEN},
        json={"message": "最近总是头晕", "thread_id": tid},
    ) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    # 患者越权访问 → 403
    r = client.get(
        f"/api/chat-logs?thread_id={THREAD_PREFIX}{tid}", headers={"Authorization": PATIENT_TOKEN}
    )
    assert r.status_code == 403
    # 医护可访问并查到该 thread 的链路记录
    r = client.get(
        f"/api/chat-logs?thread_id={THREAD_PREFIX}{tid}", headers={"Authorization": DOCTOR_TOKEN}
    )
    assert r.status_code == 200
    logs = r.json()["logs"]
    assert any(row["thread_id"] == THREAD_PREFIX + tid for row in logs)


# ===================== Tier-0 生命安全 / 法律责任硬闸 =====================


def test_emergency_hard_gate(client):
    """急症关键词（胸痛）→ 确定性硬闸：阻断 LLM，直接返回 120 急救话术并落库。"""
    import uuid

    tid = f"gw-emg-{uuid.uuid4().hex[:8]}"
    _consent(client)
    # 注意：紧急闸优先于知情同意，无需先 consent（此处仅为避免与 consent 闸顺序耦合）
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": PATIENT_TOKEN},
        json={"message": "我突然胸痛得厉害，喘不上气", "thread_id": tid},
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "emergency" in body, "应返回 emergency 事件"
    assert "120" in body, "急症话术必须含 120"
    # 不应出现「令牌流」式常规分诊回答（即未走 LLM 生成）
    assert "token" not in body, "紧急闸不应进入 LLM 流式生成"

    from src.db import ChatLog, get_session

    with get_session() as s:
        row = s.query(ChatLog).filter(ChatLog.thread_id == THREAD_PREFIX + tid).first()
        assert row is not None
        assert row.tool_used == "emergency_gate", "审计应记录为 emergency_gate"
        assert row.intent == "emergency"


def test_scope_violation_gate(client):
    """要求开药/诊断 → 定位违规门：固定回复「不诊断不开方」，不进入 LLM。"""
    import uuid

    tid = f"gw-scope-{uuid.uuid4().hex[:8]}"
    _consent(client)
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": PATIENT_TOKEN},
        json={"message": "请给我开点降压药", "thread_id": tid},
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "scope" in body, "应返回 scope 事件"
    assert "开" in body and ("诊断" in body or "处方" in body), "应明示不诊断不开方"

    from src.db import ChatLog, get_session

    with get_session() as s:
        row = s.query(ChatLog).filter(ChatLog.thread_id == THREAD_PREFIX + tid).first()
        assert row is not None and row.tool_used == "scope_gate"


def test_consent_required_blocks_then_allows(client):
    """未签同意书的患者对话被拦截；签署后可正常对话。"""
    import uuid

    new_user = f"dave_{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/auth/register",
        json={"username": new_user, "password": "dave12345", "role": "patient"},
    )
    assert r.status_code == 200
    tok = "Bearer " + create_token(new_user, "patient")

    tid1 = f"gw-cons1-{uuid.uuid4().hex[:8]}"
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": tok},
        json={"message": "我最近老头痛", "thread_id": tid1},
    ) as resp:
        body = "".join(resp.iter_text())
    assert "consent_required" in body, "未同意应被拦截并提示 consent_required"

    # 签署同意书
    rc = client.post("/api/consent", headers={"Authorization": tok}, json={})
    assert rc.status_code == 200 and rc.json().get("ok") is True

    # 状态变为已同意
    st = client.get("/api/consent/status", headers={"Authorization": tok})
    assert st.status_code == 200 and st.json()["required"] is False

    # 现在可正常对话（走 LLM 链路，出现 token 或 done）
    tid2 = f"gw-cons2-{uuid.uuid4().hex[:8]}"
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": tok},
        json={"message": "我最近老头痛挂什么科", "thread_id": tid2},
    ) as resp:
        body2 = "".join(resp.iter_text())
    assert "consent_required" not in body2, "签署后不应再被拦截"
    assert "done" in body2

    from src.db import ConsentRecord, get_session

    with get_session() as s:
        rec = s.query(ConsentRecord).filter(ConsentRecord.username == new_user).first()
        assert rec is not None, "同意书应持久化"
        assert rec.consent_version, "应记录同意书版本"
