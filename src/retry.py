"""通用重试策略（指数退避 + 抖动 + 最大次数 + 非重试异常标记）。

用于「抗造」：对 LLM API、外部工具（HIS / 短信网关 / 知识库）等**不确定性瞬时失败**自动重试。

关键约束 —— **安全硬闸豁免重试**：
``assess_emergency`` / ``assess_scope_violation`` / 知情同意三道闸由网关在入口
**确定性求值一次**，绝不进入任何重试边界（见 ``gateway.chat`` 与 ``tests/test_retry_gate.py``）。
重试只包裹「下游外部依赖」，重试失败也不能重新触发急症/定位/同意判定——否则会延误 120 提示、
或让已同意状态被反复挑战。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Type

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.2
DEFAULT_MAX_DELAY = 5.0


class NonRetryableError(Exception):
    """标记「不应重试」的错误。

    被包裹调用抛出后立即向上传播，不再退避重试（如鉴权失败、参数非法、业务拒绝）。
    """


@dataclass
class RetryPolicy:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    jitter: float = 0.1
    retry_on: tuple[Type[BaseException], ...] = (Exception,)

    def should_retry(self, exc: BaseException) -> bool:
        if isinstance(exc, NonRetryableError):
            return False
        return isinstance(exc, self.retry_on)

    def delay(self, attempt: int) -> float:
        # 指数退避：base * 2**(attempt-1)，封顶 max_delay，叠加 ±jitter 抖动避免重试惊群
        exp = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return min(self.max_delay, max(0.0, exp + random.uniform(-self.jitter, self.jitter)))


async def with_retry(
    policy: RetryPolicy,
    coro_factory: Callable[[], Awaitable],
    label: str = "call",
) -> "object":
    """异步重试：每次调用 ``coro_factory()`` 产生一个新的协程（避免复用已关闭的协程）。

    - 命中 ``retry_on`` 且未达最大次数 → 指数退避后重试。
    - 命中 ``NonRetryableError`` 或超出次数 → 抛出最后一次异常。
    """
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await coro_factory()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if not policy.should_retry(exc):
                raise
            if attempt >= policy.max_attempts:
                raise
            await asyncio.sleep(policy.delay(attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry exhausted without result")  # 不可达，保险用


def with_retry_sync(
    policy: RetryPolicy, func: Callable[[], "object"], label: str = "call"
) -> "object":
    """同步版重试（包裹同步外部调用，如阻塞式 SDK）。规则同 ``with_retry``。"""
    import time

    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return func()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if not policy.should_retry(exc):
                raise
            if attempt >= policy.max_attempts:
                raise
            time.sleep(policy.delay(attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry exhausted without result")
