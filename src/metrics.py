"""Prometheus 指标：把「Agent 在干什么、慢在哪、拦截了多少」变成可观测的数字。

为什么单独成模块
----------------
本项目的风险不在 QPS，而在三件只有指标才能回答的事：

1. **安全闸有没有真的拦住**：急症 / 定位违规 / 知情同意三道 Tier-0 硬闸的命中量，
   以及输出侧护栏的拦截量——这些是合规审计要拿数字说话的地方；
2. **延迟花在哪一段**：SSE 首字节（患者感知的"卡不卡"）与端到端耗时是两回事，
   分开看才能定位是 LLM 慢还是工具慢；
3. **人工介入的积压**：审批单从创建到批准的等待时长，直接决定患者要不要干等。

指标命名遵循 Prometheus 惯例：``<namespace>_<name>_<unit>``，计数器以 ``_total`` 结尾。

端点鉴权
--------
``/metrics`` 默认需要 ``X-Admin-Key``（与知识库管理接口同一把钥匙），
避免把内部结构、版本号、流量特征暴露给匿名访问者；
设 ``METRICS_PUBLIC=1`` 可关闭（仅限内网 / sidecar 抓取场景）。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Optional

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

NAMESPACE = "medical_agent"

# ---------- HTTP 层（中间件自动采集） ----------
HTTP_REQUESTS = Counter(
    "http_requests",
    "HTTP 请求总数",
    ["method", "route", "status"],
    namespace=NAMESPACE,
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "route"],
    namespace=NAMESPACE,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

# ---------- 对话链路（/api/chat 埋点） ----------
CHAT_TURNS = Counter(
    "chat_turns",
    "对话轮次总数（按结束方式分类）",
    ["intent", "turn", "tool_used"],
    namespace=NAMESPACE,
)
CHAT_DURATION = Histogram(
    "chat_duration_seconds",
    "一轮对话端到端耗时（秒）",
    namespace=NAMESPACE,
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)
CHAT_FIRST_TOKEN = Histogram(
    "chat_first_token_seconds",
    "首字节延迟（秒）：患者发出消息到收到第一个 token，直接决定体感",
    namespace=NAMESPACE,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20),
)
CHAT_TIMEOUTS = Counter(
    "chat_timeouts",
    "对话超时次数（CHAT_TIMEOUT_SECONDS 熔断）",
    namespace=NAMESPACE,
)

# ---------- Tier-0 安全闸与输出护栏 ----------
SAFETY_GATE_HITS = Counter(
    "safety_gate_hits",
    "Tier-0 确定性安全闸命中次数（先于一切 LLM 调用短路）",
    ["gate"],
    namespace=NAMESPACE,
)
GUARD_BLOCKS = Counter(
    "guard_output_blocked",
    "输出侧护栏拦截次数（模型自发的诊断/处方/剂量）",
    namespace=NAMESPACE,
)

# ---------- 人工审核门（HITL） ----------
APPROVALS_CREATED = Counter("approvals_created", "审批单创建数", ["action"], namespace=NAMESPACE)
APPROVALS_RESOLVED = Counter(
    "approvals_resolved", "审批单处理数", ["action", "decision"], namespace=NAMESPACE
)
APPROVALS_PENDING = Gauge(
    "approvals_pending", "当前待审批数量（患者正在干等的积压）", namespace=NAMESPACE
)
APPROVAL_WAIT = Histogram(
    "approval_wait_seconds",
    "审批单从创建到处理的等待时长（秒）",
    namespace=NAMESPACE,
    buckets=(1, 5, 15, 30, 60, 300, 900, 3600),
)

# ---------- LLM 与工具 ----------
LLM_CALLS = Counter("llm_calls", "LLM 调用次数", ["node", "model"], namespace=NAMESPACE)
LLM_DURATION = Histogram(
    "llm_duration_seconds",
    "LLM 调用耗时（秒）",
    ["node"],
    namespace=NAMESPACE,
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
LLM_FALLBACKS = Counter(
    "llm_fallbacks", "LLM 失败降级次数（走兜底话术而非编造）", namespace=NAMESPACE
)
TOOL_CALLS = Counter("tool_calls", "工具调用次数", ["tool", "status"], namespace=NAMESPACE)
TOOL_DURATION = Histogram(
    "tool_duration_seconds",
    "工具调用耗时（秒）",
    ["tool"],
    namespace=NAMESPACE,
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5),
)

# ---------- 韧性工程：熔断与降级 ----------
BREAKER_OPENS = Counter(
    "breaker_opens",
    "熔断器开启次数（依赖被隔离，停止向其发送请求）",
    ["breaker"],
    namespace=NAMESPACE,
)
BREAKER_REJECTIONS = Counter(
    "breaker_rejections",
    "熔断器开启期间被快速拒绝的调用数（未打超时直接失败）",
    ["breaker"],
    namespace=NAMESPACE,
)
KILLSWITCH_ACTIVE = Gauge(
    "killswitch_active", "当前处于运行时停用状态的目标数量（工具 / 意图）", namespace=NAMESPACE
)

# ---------- LLM 成本归因（按 Agent / 模型 / 患者） ----------
# 注意：patient 维度不进 Prometheus 标签（避免高基数拖垮 TSDB），
# 仅在内存分账 ledger 里统计；Prometheus 侧只保留低基数的 (agent, model, kind)。
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "LLM token 消耗（按调用方与模型）",
    ["agent", "model", "kind"],
    namespace=NAMESPACE,
)
LLM_COST_USD = Counter(
    "llm_cost_usd_total",
    "LLM 估算费用（美元，按调用方与模型）",
    ["agent", "model"],
    namespace=NAMESPACE,
)


@contextmanager
def track(node: str, counter: Counter, histogram: Histogram, **labels) -> Iterator[None]:
    """统一的耗时埋点：正常/异常都记录，并标注 status。

    用法::

        with track("triage", LLM_CALLS, LLM_DURATION, node="triage", model=model_name):
            ...
    """
    start = time.monotonic()
    status = "ok"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        histogram.labels(**{k: v for k, v in labels.items() if k != "status"}).observe(
            time.monotonic() - start
        )
        counter.labels(**labels, status=status).inc()


def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    """中间件调用：记录一次 HTTP 请求。

    ``route`` 用路由模板（如 ``/api/chat``）而非实际路径，
    否则带 ID 的路径会把指标基数打爆（高基数会拖垮 Prometheus）。
    """
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    HTTP_LATENCY.labels(method=method, route=route).observe(seconds)


def render() -> tuple[bytes, str]:
    """返回 (指标文本, Content-Type)，供 /metrics 端点直接响应。"""
    return generate_latest(), CONTENT_TYPE_LATEST


def set_pending_approvals(count: Optional[int]) -> None:
    """刷新待审批积压（Gauge 需要主动写入，不能靠累加）。"""
    if count is not None:
        APPROVALS_PENDING.set(count)
