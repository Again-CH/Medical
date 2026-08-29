"""韧性工程：熔断器（Circuit Breaker） + 运行时 kill switch + 降级编排。

回答一个生产系统绕不开的问题：**下游依赖持续不可用时，系统怎么办？**

与 ``retry.py`` 的分工
---------------------
- ``retry.py`` 解决**瞬时故障自愈**：超时/连接抖动时指数退避重试，期望下一秒就好。
- 本模块解决**持续故障隔离**：依赖真的挂了（连重试都救不回），再重试只会
  叠加超时、把调用方线程/协程拖死、进而雪崩。此时应**快速失败**并**降级**，
  等依赖恢复后再**半开探测**把它接回来。

三个能力
--------
1. ``CircuitBreaker``：对反复失败的依赖（LLM / 外部工具）统计连续失败，
   超阈值即开启，开启期内直接抛 ``BreakerOpenError``（不浪费超时）；
   冷却结束后进入半开态放一个探测，成功若干次则闭合、失败则重新开启。
2. ``KillSwitch``：运维**运行时一键停用**某个工具（如 HIS 宕机）或整个意图
   （``agent:<intent>``），无需发版即可把流量从故障依赖上摘掉，并走安全降级。
3. **降级（degradation）**：熔断/停用后由调用方回退到「不依赖该依赖」的安全路径——
   LLM 熔断 → ``final_answer`` 直接返回兜底话术；工具停用 → 返回「暂不可用」占位。

默认开启（``RESILIENCE_ENABLED``）；关闭时所有包装降级为直连，方便压测对照与排查。
指标接入 ``metrics``（熔断开启数、快速拒绝数、停用目标数），可在 ``/metrics`` 观测。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

from . import config

# 网关等模块直接引用此常量（与 config 同步，捕获导入期值；env 在启动时已固定）
RESILIENCE_ENABLED = config.RESILIENCE_ENABLED


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerOpenError(Exception):
    """熔断器处于 OPEN / 冷却期内，调用被快速拒绝。

    调用方应据此走降级路径，而不是当成普通异常重试（那会重新打超时）。
    """


@dataclass
class BreakerConfig:
    failure_threshold: int = 5  # 连续失败达到此值即开启
    cooldown_seconds: float = 30.0  # 开启后保持多久再放半开探测
    half_open_successes: int = 2  # 半开态需连续成功多少次才闭合


def _default_config() -> BreakerConfig:
    return BreakerConfig(
        failure_threshold=config.BREAKER_FAILURE_THRESHOLD,
        cooldown_seconds=config.BREAKER_COOLDOWN_SECONDS,
        half_open_successes=config.BREAKER_HALFOPEN_SUCCESSES,
    )


def _on_breaker_opened(name: str) -> None:
    try:
        from . import metrics

        metrics.BREAKER_OPENS.labels(breaker=name).inc()
    except Exception:  # pragma: no cover - 指标不可用时不影响主流程
        pass


class CircuitBreaker:
    """线程安全的熔断器（单进程 asyncio 下亦安全，状态变更加锁保护）。

    用法::

        breaker = get_breaker("llm")
        try:
            result = await breaker.call_async(lambda: llm.ainvoke(msgs))
        except BreakerOpenError:
            ...  # 降级
    """

    def __init__(self, name: str, cfg: Optional[BreakerConfig] = None) -> None:
        self.name = name
        self.cfg = cfg or _default_config()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._half_successes = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()
        # 仅用于运维展示的计数（非精确，不进关键路径）
        self.total_calls = 0
        self.total_rejected = 0
        self.last_failure_at: Optional[float] = None

    # ---- 只读快照（供 /api/admin/resilience 展示） ----
    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def snapshot(self) -> dict:
        with self._lock:
            self._maybe_transition_to_half_open()
            return {
                "name": self.name,
                "state": self._state.value,
                "failures": self._failures,
                "total_calls": self.total_calls,
                "total_rejected": self.total_rejected,
                "opened_at": self._opened_at,
            }

    def reset(self) -> None:
        """手动复位（运维确认依赖已恢复后调用）。"""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._failures = 0
            self._half_successes = 0
            self._opened_at = 0.0

    # ---- 状态机（调用方需持锁） ----
    def _maybe_transition_to_half_open(self) -> None:
        if (
            self._state == BreakerState.OPEN
            and (time.monotonic() - self._opened_at) >= self.cfg.cooldown_seconds
        ):
            self._state = BreakerState.HALF_OPEN
            self._half_successes = 0

    def _acquire(self) -> bool:
        """是否允许本次调用。OPEN 且冷却未到 → 拒绝；半开/闭合 → 放行。"""
        with self._lock:
            self._maybe_transition_to_half_open()
            if self._state == BreakerState.OPEN:
                self.total_rejected += 1
                try:
                    from . import metrics

                    metrics.BREAKER_REJECTIONS.labels(breaker=self.name).inc()
                except Exception:  # pragma: no cover
                    pass
                return False
            return True

    def _record_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state == BreakerState.HALF_OPEN:
                self._half_successes += 1
                if self._half_successes >= self.cfg.half_open_successes:
                    self._state = BreakerState.CLOSED
                    self._half_successes = 0
            else:
                self._state = BreakerState.CLOSED

    def _record_failure(self) -> None:
        opened = False
        with self._lock:
            self._failures += 1
            self.last_failure_at = time.monotonic()
            if self._state == BreakerState.HALF_OPEN:
                # 半开探测失败 → 立刻重新开启（不再放后续探测）
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                self._half_successes = 0
                opened = True
            elif self._failures >= self.cfg.failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                opened = True
        if opened:
            _on_breaker_opened(self.name)

    # ---- 包装调用 ----
    # 注意：不可在 try 块内直接 ``return``，否则会跳过 else 子句导致 record_success 不执行
    # （Python 语义：try 内 return 会直接离开函数，else 不运行）。故先存结果再在 else 后返回。
    async def call_async(self, coro_factory: Callable[[], Awaitable], label: str = "call"):
        self.total_calls += 1
        if not self._acquire():
            raise BreakerOpenError(f"circuit breaker '{self.name}' is OPEN")
        try:
            result = await coro_factory()
        except BaseException:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def call_sync(self, func: Callable[[], object], label: str = "call") -> object:
        self.total_calls += 1
        if not self._acquire():
            raise BreakerOpenError(f"circuit breaker '{self.name}' is OPEN")
        try:
            result = func()
        except BaseException:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result


# ---------------- 熔断器注册表 ----------------
_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKER_CONFIGS: dict[str, BreakerConfig] = {}
_BREAKERS_LOCK = threading.Lock()


def configure_breaker(name: str, **kwargs) -> None:
    """为指定 name 预设熔断参数（在首次 get_breaker 前调用）。"""
    _BREAKER_CONFIGS[name] = BreakerConfig(**kwargs)


def get_breaker(name: str, default_cfg: Optional[BreakerConfig] = None) -> CircuitBreaker:
    b = _BREAKERS.get(name)
    if b is None:
        with _BREAKERS_LOCK:
            b = _BREAKERS.get(name)
            if b is None:
                cfg = _BREAKER_CONFIGS.get(name) or default_cfg or _default_config()
                b = CircuitBreaker(name, cfg)
                _BREAKERS[name] = b
    return b


def all_breakers() -> dict[str, CircuitBreaker]:
    return dict(_BREAKERS)


def reset_breakers() -> None:
    """测试 / 运维复位全部熔断器。"""
    with _BREAKERS_LOCK:
        _BREAKERS.clear()


# ---------------- 运行时 kill switch ----------------
class KillSwitch:
    """运维运行时停用某个工具或意图（``agent:<intent>``）。

    与熔断的区别：熔断是「系统自动」根据失败统计隔离依赖；
    kill switch 是「人主动」摘流量——下游已知宕机、或要灰度某个能力时，
    不用发版即可让系统绕开它走降级。生产应落配置表并广播，本实现用进程内内存 +
    启动期 env 预设，足以支撑演示与单实例运维。
    """

    def __init__(self) -> None:
        self._disabled: set[str] = set()
        self._lock = threading.Lock()

    def disable(self, target: str) -> None:
        with self._lock:
            self._disabled.add(target)

    def enable(self, target: str) -> None:
        with self._lock:
            self._disabled.discard(target)

    def toggle(self, target: str, disabled: bool) -> None:
        if disabled:
            self.disable(target)
        else:
            self.enable(target)

    def is_disabled(self, target: str) -> bool:
        with self._lock:
            return target in self._disabled

    def list_disabled(self) -> list[str]:
        with self._lock:
            return sorted(self._disabled)

    def reset(self) -> None:
        with self._lock:
            self._disabled.clear()


KILL_SWITCH = KillSwitch()


def _sync_killswitch_metric() -> None:
    try:
        from . import metrics

        metrics.KILLSWITCH_ACTIVE.set(len(KILL_SWITCH.list_disabled()))
    except Exception:  # pragma: no cover
        pass


def _bootstrap_disabled() -> None:
    """启动期按 env（RESILIENCE_DISABLED）预设停用目标。"""
    for t in config.RESILIENCE_DISABLED:
        KILL_SWITCH.disable(t)
    _sync_killswitch_metric()


# 进程启动即应用预设（模块导入一次）；多 worker 各自独立，生产应改为共享存储。
if config.RESILIENCE_DISABLED:
    _bootstrap_disabled()


def enabled() -> bool:
    """总开关：关闭时调用方应跳过熔断/降级包装（直连）。"""
    return config.RESILIENCE_ENABLED
