"""结构化日志：JSON 单行输出，便于接入 Loki/ELK 检索与告警。

用法：
    from .logging_config import get_logger
    log = get_logger()
    log.info("chat.start", extra={"trace_id": tid, "user": uid, "intent": intent})

生产建议：将 stdout 交给容器/进程管理器收集（如 Docker → loki / filebeat）。
"""

import json
import logging
import sys
import uuid
from datetime import datetime, timezone

_BASIC_KEYS = {
    "args",
    "msg",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "stack_info",
    "name",
    "getMessage",
    "message",
    "taskName",  # asyncio 任务内部属性，非业务字段，剔除
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 把 extra 里非标准字段一并带出（trace_id / user / intent / latency_ms ...）
        for key, val in record.__dict__.items():
            if key in _BASIC_KEYS:
                continue
            try:
                json.dumps(val)  # 仅保留可序列化字段
                payload[key] = val
            except (TypeError, ValueError):
                payload[key] = str(val)
        return json.dumps(payload, ensure_ascii=False)


_LOGGERS: dict[str, logging.Logger] = {}


def get_logger(name: str = "medical-agent") -> logging.Logger:
    """返回带 JSON formatter 的 logger（单例初始化，避免重复 handler）。"""
    if name in _LOGGERS:
        return _LOGGERS[name]
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    _LOGGERS[name] = logger
    return logger


def new_trace_id() -> str:
    """生成本次请求/对话的链路追踪 ID（回放执行流程时关联 LangSmith run）。"""
    return uuid.uuid4().hex[:16]
