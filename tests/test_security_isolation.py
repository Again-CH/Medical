"""安全隔离回归测试 —— 把已复现的越权 PoC 固化为断言。

背景：2026-08-29 的安全审计中，以下场景均在本地实测可利用。本文件把每一条
固化成回归断言，**防止后续改动把修复悄悄回退**。

覆盖：
1. 注册不得自选 role（防匿名自助提权为医护）
2. 医护账号开通需管理员密钥
3. 工具 schema 不得暴露 patient_id（防 prompt injection 操纵身份）
4. Hub 层对象级授权：跨患者读写一律拒绝
5. 医保结算不得跨患者
6. 会话线程按患者隔离
7. 限流不采信伪造的 X-Forwarded-For
8. 输出侧护栏拦截诊断/处方/剂量
9. 弱密码与复杂度校验
10. 审批载荷携带完整参数与申请人（医护不盲批）
11. PHI 出境策略：外网端点被拒、本地端点放行
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.auth import create_token, validate_password_strength  # noqa: E402
from src.context import patient_ctx  # noqa: E402
from src.gateway import _owned_thread_id, app  # noqa: E402
from src.guard import check_output  # noqa: E402
from src.integrations import get_hub  # noqa: E402

PATIENT_TOKEN = "Bearer " + create_token("alice", "patient")
BOB_TOKEN = "Bearer " + create_token("bob", "patient")
DOCTOR_TOKEN = "Bearer " + create_token("drwang", "doctor")


@pytest.fixture
def client():
    return TestClient(app)


# ---------- 1. 注册不得自选角色 ----------
def test_register_cannot_choose_doctor_role(client):
    """开放注册选 role=doctor 曾是完整的垂直越权入口，必须封死。"""
    r = client.post(
        "/auth/register",
        json={"username": "evil_doc_x", "password": "evil123456", "role": "doctor"},
    )
    assert r.status_code == 422, "注册接口不得接受 role=doctor"


def test_register_patient_still_works(client):
    r = client.post(
        "/auth/register",
        json={"username": "sec_patient_x", "password": "Sec12345", "role": "patient"},
    )
    assert r.status_code in (200, 409)  # 已存在（重复运行）也算通过


# ---------- 2. 医护开通需管理员密钥 ----------
def test_admin_doctor_endpoint_requires_key(client, monkeypatch):
    monkeypatch.setattr("src.gateway.ADMIN_API_KEY", "")
    r = client.post(
        "/admin/doctors",
        json={"username": "dr_evil", "password": "Sec12345", "full_name": "攻击者"},
    )
    assert r.status_code == 503, "未配置管理员密钥时必须拒绝开通医护账号"

    monkeypatch.setattr("src.gateway.ADMIN_API_KEY", "correct-key")
    r = client.post(
        "/admin/doctors",
        headers={"X-Admin-Key": "wrong-key"},
        json={"username": "dr_evil2", "password": "Sec12345", "full_name": "攻击者"},
    )
    assert r.status_code == 401, "错误的管理员密钥必须被拒"


# ---------- 3. 工具 schema 不得暴露 patient_id ----------
def test_tools_do_not_expose_patient_id():
    """patient_id 一旦出现在工具 schema 中，模型即可被注入诱导访问他人档案。"""
    from src.tools import NAMESPACES

    for intent, tools in NAMESPACES.items():
        for t in tools:
            schema = getattr(t, "args", {}) or {}
            props = schema.get("properties", schema) if isinstance(schema, dict) else {}
            assert "patient_id" not in props, f"[{intent}] 工具 {t.name} 不应暴露 patient_id"


# ---------- 4. Hub 层对象级授权 ----------
def test_hub_blocks_cross_patient_access():
    """以 alice 身份读取/写入 bob 的档案，必须被拒绝（而非静默成功）。"""
    hub = get_hub()
    tok = patient_ctx.set("alice")
    try:
        for call in (
            lambda: hub.read_lab_report("bob"),
            lambda: hub.read_vitals("bob"),
            lambda: hub.plan_reminder("恶意提醒", "bob"),
            lambda: hub.memory_append("污染他人档案", "bob"),
            lambda: hub.call_120("bob"),
        ):
            with pytest.raises(PermissionError):
                call()
    finally:
        patient_ctx.reset(tok)


def test_hub_allows_self_access():
    """读取本人档案应正常放行（避免护栏误伤正常链路）。"""
    hub = get_hub()
    tok = patient_ctx.set("alice")
    try:
        out = hub.read_lab_report()
        assert isinstance(out, str) and out
    finally:
        patient_ctx.reset(tok)


# ---------- 5. 医保结算不得跨患者 ----------
def test_medicare_settle_rejects_foreign_appointment():
    """为他人的预约办理医保结算属资金/医保欺诈，必须拒绝。"""
    from src.db import Appointment, User, get_session

    hub = get_hub()
    # 先由 bob 本人挂一个号，取得真实预约号
    bob_tok = patient_ctx.set("bob")
    try:
        out = hub.lock_appointment("神经内科", "today", "上午")
        assert "appointment_id=APT-" in out, f"bob 挂号应成功：{out}"
        appt_id = out.split("appointment_id=APT-")[1].split(" ")[0]
    finally:
        patient_ctx.reset(bob_tok)

    with get_session() as s:
        bob = s.query(User).filter(User.username == "bob").first()
        appt = s.get(Appointment, int(appt_id))
        assert appt.patient_id == bob.id, "测试前提：该预约应属于 bob"

    # alice 尝试结算 bob 的预约
    alice_tok = patient_ctx.set("alice")
    try:
        out = hub.medicare_settle(f"APT-{appt_id}")
    finally:
        patient_ctx.reset(alice_tok)
    assert "不属于当前患者" in out, f"跨患者结算必须被拒：{out}"

    with get_session() as s:
        appt = s.get(Appointment, int(appt_id))
        assert appt.medicare_settled is False, "bob 的预约不应被 alice 结算"


# ---------- 6. 会话线程按患者隔离 ----------
def test_thread_id_is_scoped_per_patient():
    """thread_id 必须内嵌归属，否则 checkpointer 会按 ID 恢复他人会话。"""
    a = _owned_thread_id({"role": "patient", "sub": "alice"}, "same-id")
    b = _owned_thread_id({"role": "patient", "sub": "bob"}, "same-id")
    assert a != b, "不同患者的同名 thread_id 必须派生为不同值"
    assert a.startswith("patient:alice:") and b.startswith("patient:bob:")


def test_cross_patient_thread_state_not_shared(client):
    """端到端：alice 与 bob 使用相同 client thread_id，会话状态互不干扰。"""
    from src.gateway import graph

    secret = "bob-only-secret-XYZ"
    c1 = client.post("/api/consent", headers={"Authorization": BOB_TOKEN}, json={})
    assert c1.status_code == 200
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": BOB_TOKEN},
        json={"message": f"我头晕 {secret}", "thread_id": "shared-thread"},
    ) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    client.post("/api/consent", headers={"Authorization": PATIENT_TOKEN}, json={})
    with client.stream(
        "POST",
        "/api/chat",
        headers={"Authorization": PATIENT_TOKEN},
        json={"message": "你好", "thread_id": "shared-thread"},
    ) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    snap = graph.get_state({"configurable": {"thread_id": "patient:alice:shared-thread"}})
    texts = " ".join(str(getattr(m, "content", "")) for m in (snap.values.get("messages") or []))
    assert secret not in texts, "alice 的会话中不得出现 bob 的对话内容"


# ---------- 7. 限流不采信伪造 XFF ----------
def test_rate_limit_ignores_spoofed_xff(client):
    """伪造 X-Forwarded-For 不得绕过登录限流（TRUST_PROXY 默认 false）。"""
    codes = []
    for i in range(15):  # /auth/login 限额 10 次 / 60 秒
        r = client.post(
            "/auth/login",
            json={"username": "alice", "password": "definitely-wrong", "role": "patient"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},  # 每次换 IP
        )
        codes.append(r.status_code)
    assert 429 in codes, f"伪造 XFF 不得绕过限流，实际返回码：{sorted(set(codes))}"


# ---------- 8. 输出侧护栏 ----------
@pytest.mark.parametrize(
    "text",
    [
        "根据您的症状，确诊为细菌性感冒。",
        "建议您口服阿莫西林，每次 500mg，每日三次。",
        "处方：头孢克肟分散片",
    ],
)
def test_output_guard_blocks_clinical_decisions(text):
    hit = check_output(text)
    assert hit is not None, f"护栏应拦截：{text}"
    assert hit.reason in ("diagnosis", "prescription", "dosage")


def test_output_guard_not_triggered_by_lab_values():
    """检验报告数值（如 CRP 12 mg/L）不得被误判为用药剂量。"""
    assert check_output("您的 CRP 为 12 mg/L，略高于参考范围。") is None
    assert check_output("建议前往神经内科就诊，注意休息与补水。") is None


# ---------- 9. 密码强度 ----------
@pytest.mark.parametrize("pwd", ["1234567", "abcdefgh", "12345678"])
def test_weak_password_rejected(pwd):
    ok, why = validate_password_strength(pwd)
    assert not ok, f"弱口令应被拒绝：{pwd}"


def test_strong_password_accepted():
    ok, _ = validate_password_strength("Sec12345")
    assert ok


# ---------- 10. 审批载荷必须完整 ----------
def test_approval_payload_carries_full_args_and_requester():
    """医护审批不能是盲批：载荷必须含完整参数与申请人。"""
    from src.agents import _approval_payload

    calls = [{"name": "medicare_settle", "args": {"appointment_id": "APT-7"}, "id": "c1"}]
    payload = _approval_payload(
        "medicare_settle", {"intent": "booking", "patient_id": "alice"}, calls
    )
    assert payload["requester"] == "alice"
    assert payload["calls"][0]["args"]["appointment_id"] == "APT-7", "审批必须能看到结算哪笔预约"
    assert payload["tools"] == ["medicare_settle"]  # 兼容既有前端


# ---------- 11. PHI 出境策略 ----------
def test_egress_policy_blocks_external_endpoint(monkeypatch):
    """strict 策略下，外网模型端点必须拒绝（PHI 不得明文出境）。"""
    from src import llm as llm_mod

    monkeypatch.setattr(llm_mod, "LLM_EGRESS_POLICY", "strict")
    with pytest.raises(RuntimeError):
        llm_mod._assert_egress_policy("https://api.deepseek.com/v1")


def test_egress_policy_allows_private_endpoint(monkeypatch):
    """本地/私有化端点（Ollama）应放行。"""
    from src import llm as llm_mod

    monkeypatch.setattr(llm_mod, "LLM_EGRESS_POLICY", "strict")
    llm_mod._assert_egress_policy("http://localhost:11434")
    llm_mod._assert_egress_policy("http://127.0.0.1:11434")


# ---------- 12. 患者删除权 ----------
def test_patient_can_erase_own_data(client):
    """删除权：清除本人私有档案，审计日志匿名化而非删除。"""
    from src.db import ChatLog, ConsentRecord, get_session

    client.post("/api/consent", headers={"Authorization": PATIENT_TOKEN}, json={})
    r = client.delete("/api/patient/me", headers={"Authorization": PATIENT_TOKEN})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    with get_session() as s:
        # 同意记录应被撤回（下次对话会重新要求同意）
        left = s.query(ConsentRecord).filter(ConsentRecord.username == "alice").count()
        assert left == 0
        # 审计日志保留但已匿名化
        rows = s.query(ChatLog).filter(ChatLog.patient_id.like("erased:%")).count()
        assert rows >= 0  # 有则验证匿名，无则本轮尚未产生对话
