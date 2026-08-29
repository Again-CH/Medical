"""韧性工程测试：熔断器状态机、运行时 kill switch、工具/LLM 降级编排。

分三层验证「依赖不可用时系统怎么办」：
1. 熔断器单元：闭合→开启→半开→闭合 的状态机，以及快速失败语义。
2. kill switch 单元 + 工具调用降级：运维停用工具 → 不走故障依赖、返回占位。
3. 端到端降级：LLM 熔断器开启时，完整图 / final_answer 仍能优雅返回（不 500、不挂起）。
"""

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from src.agents import _invoke_tool, final_answer
from src.resilience import (
    KILL_SWITCH,
    BreakerConfig,
    BreakerOpenError,
    BreakerState,
    CircuitBreaker,
    configure_breaker,
    get_breaker,
    reset_breakers,
)


@pytest.fixture(autouse=True)
def _reset_resilience():
    """每个测试前清空熔断/停用状态，避免跨用例串扰。"""
    reset_breakers()
    KILL_SWITCH.reset()
    yield
    reset_breakers()
    KILL_SWITCH.reset()


def _boom(exc=ValueError):
    raise exc("boom")


def _ok():
    return "ok"


# ---------------- 熔断器状态机 ----------------
def test_breaker_opens_after_threshold_and_rejects_fast():
    b = CircuitBreaker(
        "t1", BreakerConfig(failure_threshold=3, cooldown_seconds=30, half_open_successes=2)
    )

    # 连续 3 次失败 → 开启（读内部状态，避免 state 属性在冷却期内自动半开）
    for _ in range(3):
        with pytest.raises(ValueError):
            b.call_sync(_boom)
    assert b._state == BreakerState.OPEN

    # 开启期内直接快速失败（不再执行函数体）
    called = {"n": 0}
    with pytest.raises(BreakerOpenError):
        b.call_sync(lambda: called.__setitem__("n", 1))
    assert called["n"] == 0, "开启期不应执行函数体"
    assert b.total_rejected >= 1


def test_breaker_half_open_recovers():
    # 用较长冷却先验证「开启」，再手动模拟冷却结束进入半开，验证恢复路径
    b = CircuitBreaker(
        "t2", BreakerConfig(failure_threshold=1, cooldown_seconds=30, half_open_successes=2)
    )

    with pytest.raises(RuntimeError):
        b.call_sync(lambda: _boom(RuntimeError))
    assert b._state == BreakerState.OPEN

    # 模拟冷却结束 → 半开
    b._opened_at = 0.0
    assert b.state == BreakerState.HALF_OPEN
    b.call_sync(_ok)
    assert b._state == BreakerState.HALF_OPEN  # 还需 1 次成功
    b.call_sync(_ok)
    assert b._state == BreakerState.CLOSED


def test_breaker_half_open_failure_reopens():
    b = CircuitBreaker(
        "t3", BreakerConfig(failure_threshold=1, cooldown_seconds=30, half_open_successes=2)
    )
    with pytest.raises(RuntimeError):
        b.call_sync(lambda: _boom(RuntimeError))
    assert b._state == BreakerState.OPEN
    b._opened_at = 0.0
    assert b.state == BreakerState.HALF_OPEN
    # 半开探测失败 → 重新开启
    with pytest.raises(RuntimeError):
        b.call_sync(lambda: _boom(RuntimeError))
    assert b._state == BreakerState.OPEN


def test_breaker_reset():
    b = CircuitBreaker(
        "t4", BreakerConfig(failure_threshold=1, cooldown_seconds=30, half_open_successes=2)
    )
    with pytest.raises(RuntimeError):
        b.call_sync(lambda: _boom(RuntimeError))
    assert b._state == BreakerState.OPEN
    b.reset()
    assert b._state == BreakerState.CLOSED
    assert b.failures == 0


def test_breaker_success_resets_failure_count():
    b = CircuitBreaker(
        "t5", BreakerConfig(failure_threshold=3, cooldown_seconds=30, half_open_successes=2)
    )
    with pytest.raises(RuntimeError):
        b.call_sync(lambda: _boom(RuntimeError))
    assert b.failures == 1
    b.call_sync(_ok)  # 成功清零
    assert b.failures == 0
    # 再来 2 次失败不应开启（计数已被成功清零）
    with pytest.raises(RuntimeError):
        b.call_sync(lambda: _boom(RuntimeError))
    with pytest.raises(RuntimeError):
        b.call_sync(lambda: _boom(RuntimeError))
    assert b.state == BreakerState.CLOSED


# ---------------- kill switch ----------------
def test_killswitch_toggle_and_list():
    assert not KILL_SWITCH.is_disabled("query_availability")
    KILL_SWITCH.disable("query_availability")
    assert KILL_SWITCH.is_disabled("query_availability")
    assert "query_availability" in KILL_SWITCH.list_disabled()
    KILL_SWITCH.enable("query_availability")
    assert not KILL_SWITCH.is_disabled("query_availability")
    assert KILL_SWITCH.list_disabled() == []


class _DummyTool:
    def __init__(self, name, exc=None):
        self.name = name
        self._exc = exc
        self.calls = 0

    def invoke(self, args):
        self.calls += 1
        if self._exc:
            raise self._exc
        return f"result-of-{self.name}"


def test_invoke_tool_respects_killswitch():
    KILL_SWITCH.disable("down_tool")
    fn = _DummyTool("down_tool")
    out = _invoke_tool(fn, {})
    assert fn.calls == 0, "停用的工具不应被实际调用"
    assert out.startswith("[disabled]"), "应返回停用占位，而非执行结果"


def test_invoke_tool_breaker_degrades():
    configure_breaker("tool:flaky", failure_threshold=1)
    fn = _DummyTool("flaky", exc=RuntimeError("downstream boom"))

    # 第一次：执行并失败，熔断器开启（异常向上传播）
    with pytest.raises(RuntimeError):
        _invoke_tool(fn, {})
    # 第二次：熔断器已开启 → 快速失败，返回降级占位（不执行函数体）
    out = _invoke_tool(fn, {})
    assert fn.calls == 1, "开启期不应再次调用故障依赖"
    assert out.startswith("[degraded]"), "应返回降级占位"


def test_invoke_tool_permission_denied():
    fn = _DummyTool("secret", exc=PermissionError("no"))
    out = _invoke_tool(fn, {})
    assert out.startswith("[denied]")


def test_invoke_tool_normal():
    fn = _DummyTool("ok_tool")
    out = _invoke_tool(fn, {})
    assert out == "result-of-ok_tool"


# ---------------- 端到端降级（LLM 熔断 → 安全兜底） ----------------
def test_final_answer_safe_fallback_when_llm_breaker_open():
    """LLM 熔断器开启时，final_answer 直接返回兜底话术，不执行 LLM、不抛 500。"""
    configure_breaker("llm", failure_threshold=1)
    b = get_breaker("llm")
    b._record_failure()  # 人为开启（模拟 LLM 持续失败被隔离）
    assert b.state == BreakerState.OPEN

    state = {
        "intent": "booking",  # 非 triage，跳过 KB 直出分支
        "tool_result": "",
        "redline_reason": "",
        "patient_id": "alice",
        "messages": [HumanMessage("你好")],
    }
    result = asyncio.run(final_answer(state))
    content = result["messages"][-1].content
    assert "系统暂时无法" in content, f"应走安全兜底，实际: {content[:80]}"
    assert b.total_rejected >= 1, "开启期应有快速拒绝计数"


def test_full_graph_degrades_with_llm_breaker_open():
    """完整图在 LLM 熔断开启时仍能返回响应（优雅降级，而非崩溃/挂起）。"""
    from src.graph import build_graph

    configure_breaker("llm", failure_threshold=1)
    get_breaker("llm")._record_failure()  # 开启 LLM 熔断

    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-resilience-breaker"}}
    # 不抛异常、能返回消息，即证明降级链路生效
    r = asyncio.run(g.ainvoke({"messages": [HumanMessage("你好")], "patient_id": "alice"}, cfg))
    assert r.get("messages"), "熔断开启下仍应产出回复（安全兜底）"
    assert get_breaker("llm").total_rejected >= 1


# ---------------- 运维端点（需 X-Admin-Key） ----------------
def test_resilience_endpoints_require_admin_key(monkeypatch):
    import src.gateway as gw
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gw, "ADMIN_API_KEY", "test-admin-key")
    client = TestClient(gw.app)

    assert client.get("/api/admin/resilience").status_code == 401
    assert (
        client.post("/api/admin/killswitch", json={"target": "x", "disabled": True}).status_code
        == 401
    )


def test_killswitch_endpoint_toggles_and_breaker_reset(monkeypatch):
    import src.gateway as gw
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gw, "ADMIN_API_KEY", "test-admin-key")
    client = TestClient(gw.app)
    headers = {"X-Admin-Key": "test-admin-key"}

    # 先开一个熔断器，便于验证 reset 端点
    configure_breaker("llm", failure_threshold=1)
    get_breaker("llm")._record_failure()
    assert get_breaker("llm").state.value == "open"

    # kill switch：停用某个工具
    r = client.post(
        "/api/admin/killswitch",
        json={"target": "query_availability", "disabled": True},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["disabled"] is True
    assert KILL_SWITCH.is_disabled("query_availability")

    # 状态端点可见
    status = client.get("/api/admin/resilience", headers=headers).json()
    assert "query_availability" in status["killswitch"]["disabled"]
    assert any(b["name"] == "llm" and b["state"] == "open" for b in status["breakers"])

    # 复位熔断器
    r2 = client.post("/api/admin/breaker/reset", json={"name": "llm"}, headers=headers)
    assert r2.status_code == 200
    assert get_breaker("llm").state.value == "closed"

    # 重新启用工具
    r3 = client.post(
        "/api/admin/killswitch",
        json={"target": "query_availability", "disabled": False},
        headers=headers,
    )
    assert r3.json()["disabled"] is False


def test_killswitch_endpoint_validates_body(monkeypatch):
    import src.gateway as gw
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gw, "ADMIN_API_KEY", "test-admin-key")
    client = TestClient(gw.app)
    headers = {"X-Admin-Key": "test-admin-key"}

    # target 缺失
    assert (
        client.post("/api/admin/killswitch", json={"disabled": True}, headers=headers).status_code
        == 400
    )
    # disabled 非布尔
    assert (
        client.post(
            "/api/admin/killswitch", json={"target": "x", "disabled": "yes"}, headers=headers
        ).status_code
        == 400
    )
