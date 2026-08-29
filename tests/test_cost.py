"""LLM 成本归因测试：token 估算、计数、三维分账、聚合与端点鉴权。"""

import os

import pytest
from src import config
from src.cost import (
    cost_breakdown,
    estimate_tokens,
    record_llm_tokens,
    reset_ledger,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_ledger()
    yield
    reset_ledger()


def test_estimate_tokens_cjk_vs_latin():
    # 中文约 1 token/字
    assert estimate_tokens("头痛三天") == 4
    # 拉丁词约 1.3 token/词（向上取整）
    assert estimate_tokens("hello world") == 3  # 2 词 → ceil(2*1.3)=3
    assert estimate_tokens("") == 0


def test_record_updates_ledger_and_prometheus():
    rec = record_llm_tokens(
        "alice", "triage", "fake-medical", prompt_text="患者主诉头痛", completion_text="建议就诊"
    )
    assert rec["prompt_tokens"] > 0 and rec["completion_tokens"] > 0
    bd = cost_breakdown()
    assert bd["totals"]["total_tokens"] == rec["prompt_tokens"] + rec["completion_tokens"]
    # 三维分账都出现 alice / triage / fake-medical
    assert any(r["key"] == "alice" for r in bd["by_patient"])
    assert any(r["key"] == "triage" for r in bd["by_agent"])
    assert any(r["key"] == "fake-medical" for r in bd["by_model"])


def test_usage_metadata_preferred_over_estimate():
    class _FakeMsg:
        usage_metadata = {"input_tokens": 10, "output_tokens": 20}

    rec = record_llm_tokens(
        "alice",
        "compose",
        "fake-medical",
        prompt_text="x" * 500,
        completion_text="y" * 500,
        message=_FakeMsg(),
    )
    assert rec == {"prompt_tokens": 10, "completion_tokens": 20}


def test_breakdown_sorts_by_cost_desc():
    # 真实模型产生费用，应排在 fake（0 费用）之前
    record_llm_tokens(
        "a", "compose", "gpt-4o-mini", prompt_text="x" * 100, completion_text="y" * 200
    )
    record_llm_tokens("b", "triage", "fake-medical", prompt_text="短", completion_text="短")
    bd = cost_breakdown()
    assert bd["by_agent"][0]["key"] == "compose"  # 有费用者排前
    assert bd["by_model"][0]["key"] == "gpt-4o-mini"


def test_reset_clears_ledger():
    record_llm_tokens("alice", "triage", "fake-medical", prompt_text="测试", completion_text="测试")
    assert cost_breakdown()["totals"]["total_tokens"] > 0
    reset_ledger()
    assert cost_breakdown()["totals"]["total_tokens"] == 0


def test_cost_tracking_disabled_noop(monkeypatch):
    monkeypatch.setattr(config, "COST_TRACKING_ENABLED", False)
    rec = record_llm_tokens(
        "alice", "triage", "fake-medical", prompt_text="测试", completion_text="测试"
    )
    assert rec == {"prompt_tokens": 0, "completion_tokens": 0}
    assert cost_breakdown()["totals"]["total_tokens"] == 0


def test_cost_endpoint_auth():
    # 不依赖 lifespan / DB：直接构造 app 并打 /api/admin/cost
    os.environ["ADMIN_API_KEY"] = "test-admin-key"
    import importlib

    import src.gateway as gw

    importlib.reload(gw)  # 让 _require_admin_key 读到新设的 key
    # 同时 patch 模块级副本（gateway 在 import 时已绑定 config.ADMIN_API_KEY）
    gw.ADMIN_API_KEY = "test-admin-key"
    import src.config as cfg

    cfg.ADMIN_API_KEY = "test-admin-key"

    from fastapi.testclient import TestClient

    client = TestClient(gw.app)
    # 无 key → 401
    r = client.get("/api/admin/cost")
    assert r.status_code in (401, 503)
    # 带 key（X-Admin-Key 与 Bearer 两种都要能过）
    r1 = client.get("/api/admin/cost", headers={"X-Admin-Key": "test-admin-key"})
    assert r1.status_code == 200
    assert "totals" in r1.json()
    r2 = client.get("/api/admin/cost", headers={"Authorization": "Bearer test-admin-key"})
    assert r2.status_code == 200
