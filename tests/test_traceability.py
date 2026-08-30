"""可追溯性与成本治理的回归测试。

覆盖本轮补上的五处能力：
1. ``token_usage`` 的 state reducer 能正确累加各节点上报的用量；
2. supervisor 意图分类**不再漏记** token（此前真实模型模式下完全隐形）；
3. SSE 首帧 ``meta`` 事件把 trace_id 下发给客户端（患者报障可定位）；
4. ``GET /api/trace/{trace_id}`` 按问题编号直达该轮完整审计记录；
5. token 预算熔断：超限后拒绝新的 LLM 调用并走降级，而非继续烧钱。

⚠️ 账号策略：**自建专用账号，不依赖种子数据**。
``tests/test_security_isolation.py`` 里有用例会行使删除权把 ``alice`` 抹掉，
其他测试若复用 alice 就会在它之后拿到 401。种子账号还会因「全新库上
TestClient 不触发 lifespan 播种」而根本不存在。
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from src import cost as cost_mod  # noqa: E402
from src import gateway as gw  # noqa: E402
from src.auth import create_token, hash_password  # noqa: E402
from src.db import ChatLog, Department, Doctor, Tenant, User, get_session  # noqa: E402
from src.state import add_usage  # noqa: E402

TRACE_PATIENT = "trace_patient"
TRACE_DOCTOR = "trace_doctor"


@pytest.fixture(autouse=True)
def _ensure_principals():
    """幂等创建本模块专用账号，并把 token_version 归零。

    必须在 fixture 里创建（而非模块导入时）：conftest 的建表播种是 session 级
    fixture，模块导入发生在它之前，那时还不能写库。
    """
    with get_session() as s:
        if s.query(User).filter(User.username == TRACE_PATIENT).first() is None:
            s.add(
                User(
                    username=TRACE_PATIENT,
                    password_hash=hash_password("Trace@12345"),
                    full_name="",
                    phone="",
                )
            )
        if s.query(Doctor).filter(Doctor.username == TRACE_DOCTOR).first() is None:
            dept = s.query(Department).first()
            tenant = s.query(Tenant).filter(Tenant.is_default.is_(True)).first()
            s.add(
                Doctor(
                    username=TRACE_DOCTOR,
                    password_hash=hash_password("Trace@12345"),
                    full_name="可追溯测试医师",
                    title="测试",
                    dept_id=dept.id if dept else None,
                    tenant_id=tenant.id if tenant else 1,
                )
            )
        s.execute(
            text("UPDATE users SET token_version = 0, failed_attempts = 0, locked_until = NULL "
                 "WHERE username = :u"),
            {"u": TRACE_PATIENT},
        )
        s.execute(
            text("UPDATE doctors SET token_version = 0, failed_attempts = 0, locked_until = NULL "
                 "WHERE username = :u"),
            {"u": TRACE_DOCTOR},
        )
        s.commit()
    yield


@pytest.fixture
def patient_token() -> str:
    return "Bearer " + create_token(TRACE_PATIENT, "patient")


@pytest.fixture
def doctor_token() -> str:
    return "Bearer " + create_token(TRACE_DOCTOR, "doctor")


@pytest.fixture
def consented(patient_token):
    """确保患者已签署知情同意，避免 /api/chat 被 consent 闸门短路。"""
    client = TestClient(gw.app)
    client.post("/api/consent", headers={"Authorization": patient_token}, json={})
    return patient_token


# ---------------- 1. token 累计（state reducer） ----------------


def test_add_usage_accumulates():
    merged = add_usage({"prompt": 10, "completion": 5}, {"prompt": 3, "completion": 2})
    assert merged == {"prompt": 13, "completion": 7}


def test_add_usage_handles_none_and_missing_keys():
    assert add_usage(None, {"prompt": 7}) == {"prompt": 7, "completion": 0}
    assert add_usage({"completion": 4}, None) == {"prompt": 0, "completion": 4}
    assert add_usage({}, {}) == {"prompt": 0, "completion": 0}


# ---------------- 2. supervisor 不再漏记 token ----------------


def test_classify_intent_returns_usage_tuple():
    """classify_intent 必须返回 (intent, usage)，否则调用方无法累加成本。"""
    import asyncio

    from src.supervisor import classify_intent

    intent, usage = asyncio.run(classify_intent("我想挂号预约明天的号", TRACE_PATIENT))
    assert intent == "booking"
    assert set(usage) >= {"prompt_tokens", "completion_tokens"}


def test_classify_intent_keyword_fallback_has_zero_usage():
    """fake 模式走关键词分类，不产生 token（不应虚报消耗）。"""
    import asyncio

    from src.supervisor import classify_intent

    intent, usage = asyncio.run(classify_intent("我最近腰疼", TRACE_PATIENT))
    assert intent == "triage"
    # 键名必须与真实模型模式一致，否则调用方要写两套取值逻辑
    assert usage == {"prompt_tokens": 0, "completion_tokens": 0}


# ---------------- 3. SSE 下发 trace_id ----------------


def _first_meta_event(payload: str) -> dict | None:
    for blk in payload.split("\n\n"):
        if not blk.startswith("data:"):
            continue
        try:
            p = json.loads(blk[5:])
        except Exception:  # noqa: BLE001
            continue
        if p.get("type") == "meta":
            return p
    return None


def test_chat_stream_emits_trace_id(consented):
    """SSE 首帧必须带 trace_id —— 否则患者报障时给不出任何可定位的 ID。"""
    client = TestClient(gw.app)
    r = client.post(
        "/api/chat",
        headers={"Authorization": consented},
        json={"message": "我最近有点头痛"},
    )
    assert r.status_code == 200
    meta = _first_meta_event(r.text)
    assert meta is not None, "SSE 流里没有 meta 事件，trace_id 未下发"
    assert meta.get("trace_id"), "meta 事件缺少 trace_id"


def test_safety_gate_stream_also_emits_trace_id(patient_token):
    """硬闸（急症）路径同样要下发 trace_id，不能只有正常链路有。"""
    client = TestClient(gw.app)
    r = client.post(
        "/api/chat",
        headers={"Authorization": patient_token},
        json={"message": "我胸痛得厉害，呼吸困难"},
    )
    meta = _first_meta_event(r.text)
    assert meta is not None and meta.get("trace_id"), "硬闸路径未下发 trace_id"


# ---------------- 4. 按 trace_id 查询 ----------------


def test_trace_lookup_roundtrip(doctor_token):
    """写入一条审计记录后，能用 trace_id 精确查回来（含新增可追溯字段）。"""
    tid = "trace-roundtrip-0001"
    with get_session() as s:
        s.query(ChatLog).filter(ChatLog.trace_id == tid).delete()
        s.add(
            ChatLog(
                trace_id=tid,
                patient_id=TRACE_PATIENT,
                thread_id=f"patient:{TRACE_PATIENT}:t1",
                intent="triage",
                input_text="我头痛",
                output_text="建议神经内科",
                tool_used="llm",
                latency_ms=123,
                model="fake-medical",
                prompt_version="v1",
                tenant_id=1,
                status="ok",
                prompt_tokens=100,
                completion_tokens=50,
            )
        )
        s.commit()

    client = TestClient(gw.app)
    r = client.get(f"/api/trace/{tid}", headers={"Authorization": doctor_token})
    assert r.status_code == 200
    body = r.json()
    assert body["trace_id"] == tid
    # W3C 32 位：可直接粘进 Jaeger / Tempo
    assert len(body["otel_trace_id"]) == 32
    rec = body["records"][0]
    assert rec["model"] == "fake-medical"
    assert rec["prompt_version"] == "v1"
    assert rec["status"] == "ok"
    assert rec["total_tokens"] == 150
    assert body["totals"]["prompt_tokens"] == 100

    with get_session() as s:
        s.query(ChatLog).filter(ChatLog.trace_id == tid).delete()
        s.commit()


def test_trace_lookup_404_for_unknown(doctor_token):
    client = TestClient(gw.app)
    r = client.get("/api/trace/does-not-exist", headers={"Authorization": doctor_token})
    assert r.status_code == 404


def test_trace_lookup_requires_doctor(patient_token):
    """审计记录不能被患者自查（避免探测他人会话）。"""
    client = TestClient(gw.app)
    r = client.get("/api/trace/whatever", headers={"Authorization": patient_token})
    assert r.status_code == 403


def test_chat_logs_filter_by_trace_and_status(doctor_token):
    tid = "trace-filter-0001"
    with get_session() as s:
        s.query(ChatLog).filter(ChatLog.trace_id == tid).delete()
        s.add(
            ChatLog(
                trace_id=tid,
                patient_id=TRACE_PATIENT,
                thread_id="t",
                intent="triage",
                input_text="x",
                output_text="y",
                status="timeout",
                prompt_tokens=1,
                completion_tokens=1,
            )
        )
        s.commit()

    client = TestClient(gw.app)
    r = client.get(
        "/api/chat-logs",
        params={"trace_id": tid},
        headers={"Authorization": doctor_token},
    )
    assert r.status_code == 200
    logs = r.json()["logs"]
    assert {x["trace_id"] for x in logs} == {tid}

    r2 = client.get(
        "/api/chat-logs",
        params={"status": "timeout"},
        headers={"Authorization": doctor_token},
    )
    assert all(x["status"] == "timeout" for x in r2.json()["logs"])

    with get_session() as s:
        s.query(ChatLog).filter(ChatLog.trace_id == tid).delete()
        s.commit()


# ---------------- 5. token 预算熔断 ----------------


@pytest.fixture
def _budget_isolated():
    """隔离预算计数器，避免影响其它用例；并把上限临时设为极小值触发熔断。"""
    cost_mod.reset_budget()
    old_global = cost_mod.config.LLM_DAILY_TOKEN_BUDGET
    old_patient = cost_mod.config.LLM_PER_PATIENT_DAILY_TOKEN_BUDGET
    yield
    cost_mod.config.LLM_DAILY_TOKEN_BUDGET = old_global
    cost_mod.config.LLM_PER_PATIENT_DAILY_TOKEN_BUDGET = old_patient
    cost_mod.reset_budget()


def test_budget_disabled_by_default(_budget_isolated):
    """默认不限制（预算为 0），本地开发与演示不受影响。"""
    cost_mod.config.LLM_DAILY_TOKEN_BUDGET = 0
    cost_mod.check_budget(TRACE_PATIENT)  # 不应抛异常


def test_global_budget_blocks_once_exhausted(_budget_isolated):
    # "你好" * 30 = 60 个 CJK 字符 → 估算 60 token，远超这里设的上限 10
    cost_mod.config.LLM_DAILY_TOKEN_BUDGET = 10
    cost_mod.record_llm_tokens(TRACE_PATIENT, "triage", "fake-medical", "你好" * 30, "回复")
    assert cost_mod.budget_status()["global"]["exceeded"] is True
    with pytest.raises(cost_mod.BudgetExceeded) as ei:
        cost_mod.check_budget(TRACE_PATIENT)
    assert ei.value.scope == "全局单日"


def test_per_patient_budget_isolates_other_patients(_budget_isolated):
    """单患者预算只应卡住该患者，不能连坐其他患者。"""
    cost_mod.config.LLM_PER_PATIENT_DAILY_TOKEN_BUDGET = 5
    cost_mod.record_llm_tokens("alice", "triage", "fake-medical", "你好" * 30, "回复")
    with pytest.raises(cost_mod.BudgetExceeded):
        cost_mod.check_budget("alice")
    cost_mod.check_budget("bob")  # 不应受影响


def test_budget_reset_clears_counters(_budget_isolated):
    cost_mod.config.LLM_DAILY_TOKEN_BUDGET = 10
    cost_mod.record_llm_tokens(TRACE_PATIENT, "triage", "fake-medical", "你好" * 30, "回复")
    assert cost_mod.budget_status()["global"]["exceeded"] is True
    cost_mod.reset_budget()
    assert cost_mod.budget_status()["global"]["used"] == 0
    cost_mod.check_budget(TRACE_PATIENT)  # 不再抛


def test_cost_endpoint_reports_budget(_budget_isolated):
    """运维要能从一个端点同时看到消耗与预算状态。"""
    from src.config import ADMIN_API_KEY

    if not ADMIN_API_KEY:
        pytest.skip("未配置 ADMIN_API_KEY")
    client = TestClient(gw.app)
    r = client.get("/api/admin/cost", headers={"X-Admin-Key": ADMIN_API_KEY})
    assert r.status_code == 200
    assert "budget" in r.json()


def test_admin_cost_reset_clears_budget(_budget_isolated):
    from src.config import ADMIN_API_KEY

    if not ADMIN_API_KEY:
        pytest.skip("未配置 ADMIN_API_KEY")
    cost_mod.config.LLM_DAILY_TOKEN_BUDGET = 10
    cost_mod.record_llm_tokens(TRACE_PATIENT, "triage", "fake-medical", "你好" * 30, "回复")
    client = TestClient(gw.app)
    r = client.get("/api/admin/cost?reset=1", headers={"X-Admin-Key": ADMIN_API_KEY})
    assert r.status_code == 200
    assert r.json()["budget"]["global"]["used"] == 0
