"""OpenTelemetry 链路追踪：把一次问诊拆成可回放的四段 span。

为什么需要它（而 logs 不够）
----------------------------
指标能告诉你"p95 是 3 秒"，但答不了"这 3 秒花在哪"。
一次患者提问会穿过：supervisor 意图分类 → 某个子 Agent → N 次工具调用 → LLM 生成。
只有把这些串成一棵 trace 树，才能区分是**检索慢**、**LLM 慢**还是**工具慢**——
这直接决定优化方向（加缓存 / 换模型 / 改索引）。

设计取舍
--------
1. **默认关闭，未装包自动降级为 no-op**：追踪是诊断设施，绝不能成为启动依赖或
   故障源。``OTEL_ENABLED=1`` 且包装了才真正上报。
2. **与日志共用 trace_id**：gateway 已用 ``new_trace_id()`` 生成 16 位十六进制串，
   这里把它换算成合法的 W3C trace-id（32 位十六进制），使日志与 trace 能互查。
3. **span 覆盖四段**：supervisor / agent / tool / llm，正好对应架构图上的四类节点。

用法::

    from .tracing import span
    with span("agent.triage", {"intent": intent}):
        ...
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

_TRACER = None
_INITIALIZED = False


def _enabled() -> bool:
    return os.getenv("OTEL_ENABLED", "0").lower() in ("1", "true", "yes")


def _tracer():
    """惰性获取 tracer；未启用或未装包时返回 None，调用方降级为 no-op。"""
    global _TRACER, _INITIALIZED
    if _INITIALIZED:
        return _TRACER
    _INITIALIZED = True
    if not _enabled():
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:  # noqa: BLE001 - 追踪不可用不得影响主流程
        return None

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "medical-agent")})
    provider = TracerProvider(resource=resource)
    if endpoint:
        # 未配端点时只保留内存 provider：进程内仍可打点，但不外发，避免默认上报到公网
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("medical-agent")
    return _TRACER


@contextmanager
def span(name: str, attributes: Optional[dict] = None) -> Iterator[None]:
    """开一段 span；追踪未启用时是零成本的空上下文。"""
    tracer = _tracer()
    if tracer is None:
        yield
        return
    # OTel 属性值只接受基础类型，这里统一转字符串，避免 None/对象导致整条 trace 丢弃
    attrs = {k: _coerce(v) for k, v in (attributes or {}).items()}
    with tracer.start_as_current_span(name, attributes=attrs):
        yield


def _coerce(v) -> object:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def hex_to_trace_id(short_hex: str) -> str:
    """把 16 位短 trace_id 补成 W3C 要求的 32 位十六进制。

    这样日志里的 ``trace_id`` 与链路追踪平台的 trace-id 一一对应，
    排查时可以从一条慢日志直接跳到完整调用链。
    """
    s = (short_hex or "").strip().lower()
    if len(s) >= 32:
        return s[:32]
    return s.ljust(32, "0")


def shutdown() -> None:
    """优雅关闭：flush 未上报的 span（供 lifespan shutdown 调用）。"""
    global _INITIALIZED
    if not _INITIALIZED or _TRACER is None:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:  # noqa: BLE001
        pass
