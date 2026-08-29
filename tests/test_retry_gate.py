"""重试策略单测 + 安全硬闸豁免重试的架构保证。

核心断言：``assess_emergency`` 等确定性安全闸在网关入口**只求值一次**，
即便下游外部调用（LLM/工具）在重试边界内失败多次也不被重算——
否则会延误 120 提示、或让已同意状态被反复挑战。
"""

import asyncio

import pytest
from src import safety
from src.retry import NonRetryableError, RetryPolicy, with_retry


def test_safety_gate_evaluated_once_outside_retry(monkeypatch):
    calls = {"gate": 0, "downstream": 0}

    def fake_emergency(text):
        calls["gate"] += 1
        return None  # 模拟「无急症」，不阻断

    monkeypatch.setattr(safety, "assess_emergency", fake_emergency)

    # 与 gateway.chat 同构：先确定性求值安全闸（仅一次），再在重试边界内跑下游
    policy = RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0)

    async def run():
        safety.assess_emergency("我头痛")  # 入口闸口，落重试边界外

        async def downstream_factory():
            calls["downstream"] += 1
            if calls["downstream"] < 3:
                raise ConnectionError("transient network blip")
            return "llm-ok"

        return await with_retry(policy, downstream_factory)

    assert asyncio.run(run()) == "llm-ok"
    assert calls["gate"] == 1, "安全闸不应被重试重复求值"
    assert calls["downstream"] == 3, "下游应重试至成功"


def test_non_retryable_aborts_immediately():
    policy = RetryPolicy(max_attempts=5, base_delay=0.0, jitter=0.0)
    calls = {"n": 0}

    async def run():
        async def factory():
            calls["n"] += 1
            raise NonRetryableError("auth failed")

        return await with_retry(policy, factory)

    with pytest.raises(NonRetryableError):
        asyncio.run(run())
    assert calls["n"] == 1, "不可重试错误应立即终止，不进行退避"


def test_retry_exhausts_then_raises_last():
    policy = RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0)
    calls = {"n": 0}

    async def run():
        async def factory():
            calls["n"] += 1
            raise ConnectionError("always down")

        return await with_retry(policy, factory)

    with pytest.raises(ConnectionError):
        asyncio.run(run())
    assert calls["n"] == 3, "应恰好重试到最大次数后抛出最后一次异常"


def test_exponential_backoff_increases():
    # 退避应随次数递增（封顶 max_delay），用于避免重试惊群
    p = RetryPolicy(base_delay=0.2, max_delay=5.0)
    d1, d2, d3 = p.delay(1), p.delay(2), p.delay(3)
    assert d1 <= d2 <= d3
    assert d3 <= p.max_delay + 1e-6
