"""LLM 成本归因：把「token 花在哪、花了多少钱」变成可回答的数字。

为什么单独成模块
----------------
医疗场景里老板最关心的两个数字就是**幻觉率**和**成本**。没有成本归因，
就无法回答「这个月 AI 对话烧了多少钱、哪个患者/哪个 Agent 最烧、换模型能省多少」。
本模块在 LLM 调用链路上做轻量埋点：

- **Prometheus 侧**（``metrics.py`` 的 ``LLM_TOKENS`` / ``LLM_COST_USD``）：
  只保留低基数标签 ``(agent, model, kind)``，用于 Grafana 长期趋势与告警。
- **内存分账 ledger**（本模块 ``_LEDGER``）：按 ``(患者, agent, 模型)`` 三维聚合，
  供 ``/api/admin/cost`` 即时给出「按患者 / 按 Agent / 按模型」的明细。
  patient 不进 Prometheus 标签——高基数（每患者一个序列）是拖垮 TSDB 最常见的坑。

token 取值优先级
----------------
1. 真实用量：langchain 在 ``AIMessage.usage_metadata`` 上回吐的
   ``input_tokens`` / ``output_tokens``（真实 API 模式最准，不依赖文本长度猜）；
2. 估算：没有 usage 时（fake 模式、或旧版 langchain），按
   中日韩字符≈1 token/字、拉丁词≈1.3 token/词 粗略估算，足以做相对归因。

费用
----
``config.LLM_PRICING`` 给出各模型单价（美元/1K tokens）；fake / 本地自托管计 0，
真实 API 用官方公开报价。费用仅为**估算**，用于容量规划而非财务结算。
"""

from __future__ import annotations

import math
import re
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import config
from .metrics import LLM_BUDGET_BLOCKS, LLM_COST_USD, LLM_TOKENS

# ── 内存分账 ledger ──
# 结构：_LEDGER[(patient, agent, model)] = {"prompt": int, "completion": int, "cost_usd": float}
# 进程内有效；多 worker / 重启会清零——这是已知边界（见 README），
# 长期持久化应落库或靠 Prometheus TSDB 的 (agent, model) 维度。
_LOCK = threading.Lock()
_LEDGER: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
    lambda: {"prompt": 0, "completion": 0, "cost_usd": 0.0}
)


_CJK_RE = re.compile(r"[㐀-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z0-9]+")


class BudgetExceeded(Exception):
    """token 预算已耗尽，本次 LLM 调用被拒绝。

    调用方必须**转安全降级**而不是继续调用：目的是止损，不是报错。
    """

    def __init__(self, scope: str, limit: int, used: int) -> None:
        self.scope = scope
        self.limit = limit
        self.used = used
        super().__init__(f"{scope} token 预算耗尽：已用 {used} / 上限 {limit}")


# ── 当日预算计数器（进程内，跨 UTC 日界自动重置）──
# 结构：{"date": "YYYY-MM-DD", "total": int, "by_patient": {pid: int}}
# 与 _LEDGER 同样是进程内状态：多 worker 不共享、重启清零——这是已知边界。
# 生产若需严格全局限流，应改用 Redis 计数器（原子 INCRBY + 过期键）。
_BUDGET_LOCK = threading.Lock()
_BUDGET: dict = {"date": "", "total": 0, "by_patient": defaultdict(int)}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _budget_rollover() -> None:
    """跨日重置计数器（惰性，在每次读写前调用）。"""
    today = _today()
    if _BUDGET["date"] != today:
        _BUDGET["date"] = today
        _BUDGET["total"] = 0
        _BUDGET["by_patient"] = defaultdict(int)


def check_budget(patient_id: str = "anonymous") -> None:
    """调用 LLM **之前**的预检：超预算即抛 :class:`BudgetExceeded`。

    判定口径是「当日累计已用 >= 预算」。无法预知单次调用会烧多少 token，
    所以按累计额卡口——这是自托管与云 API 都通用的做法。
    预算为 0 表示不限制（默认），便于本地开发与演示。
    """
    global_limit = config.LLM_DAILY_TOKEN_BUDGET
    patient_limit = config.LLM_PER_PATIENT_DAILY_TOKEN_BUDGET
    if not global_limit and not patient_limit:
        return
    with _BUDGET_LOCK:
        _budget_rollover()
        used_total = _BUDGET["total"]
        used_patient = _BUDGET["by_patient"].get(patient_id or "anonymous", 0)
    if global_limit and used_total >= global_limit:
        LLM_BUDGET_BLOCKS.labels(scope="global").inc()
        raise BudgetExceeded("全局单日", global_limit, used_total)
    if patient_limit and used_patient >= patient_limit:
        LLM_BUDGET_BLOCKS.labels(scope="patient").inc()
        raise BudgetExceeded(f"患者 {patient_id} 单日", patient_limit, used_patient)


def budget_status() -> dict:
    """返回当前预算使用情况（供 /api/admin/cost 与运维排查）。"""
    with _BUDGET_LOCK:
        _budget_rollover()
        by_patient = dict(_BUDGET["by_patient"])
        return {
            "date": _BUDGET["date"],
            "global": {
                "limit": config.LLM_DAILY_TOKEN_BUDGET,
                "used": _BUDGET["total"],
                "exceeded": bool(
                    config.LLM_DAILY_TOKEN_BUDGET
                    and _BUDGET["total"] >= config.LLM_DAILY_TOKEN_BUDGET
                ),
            },
            "per_patient_limit": config.LLM_PER_PATIENT_DAILY_TOKEN_BUDGET,
            "top_patients": sorted(
                (
                    {
                        "patient": k,
                        "used": v,
                        "exceeded": bool(
                            config.LLM_PER_PATIENT_DAILY_TOKEN_BUDGET
                            and v >= config.LLM_PER_PATIENT_DAILY_TOKEN_BUDGET
                        ),
                    }
                    for k, v in by_patient.items()
                ),
                key=lambda x: x["used"],
                reverse=True,
            )[:10],
        }


def reset_budget() -> None:
    """清空预算计数器（测试隔离 / 运维手动解除熔断）。"""
    with _BUDGET_LOCK:
        _BUDGET["date"] = _today()
        _BUDGET["total"] = 0
        _BUDGET["by_patient"] = defaultdict(int)


def estimate_tokens(text: str) -> int:
    """粗略估算一段文本的 token 数（无真实 usage 时的兜底）。

    中日韩统一表意文字≈1 token/字；拉丁字母数字词≈1.3 token/词（取上限）。
    这是归因用的相对量，不追求与具体 tokenizer 完全一致。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    return cjk + math.ceil(latin * 1.3)


def _price_of(model: str) -> dict[str, float]:
    return config.LLM_PRICING.get(model, {"prompt": 0.0, "completion": 0.0})


def _usage_from_message(message) -> Optional[dict[str, int]]:
    """尝试从 langchain 返回的 AIMessage 上读取真实 token 用量。

    不同 langchain 版本字段名不一：新版本用 ``usage_metadata``（dict），
    部分流式版本在 chunk 上带 ``usage``。取不到返回 None，交给估算兜底。
    """
    if message is None:
        return None
    meta = getattr(message, "usage_metadata", None)
    if isinstance(meta, dict):
        # 兼容 input_tokens / output_tokens 与 prompt_tokens / completion_tokens 两种命名
        p = meta.get("input_tokens", meta.get("prompt_tokens"))
        c = meta.get("output_tokens", meta.get("completion_tokens"))
        if p is not None or c is not None:
            return {"prompt_tokens": int(p or 0), "completion_tokens": int(c or 0)}
    usage = getattr(message, "usage", None)
    if isinstance(usage, dict):
        p = usage.get("prompt_tokens", usage.get("input_tokens"))
        c = usage.get("completion_tokens", usage.get("output_tokens"))
        if p is not None or c is not None:
            return {"prompt_tokens": int(p or 0), "completion_tokens": int(c or 0)}
    return None


def record_llm_tokens(
    patient_id: str,
    agent: str,
    model: str,
    prompt_text: str = "",
    completion_text: str = "",
    message=None,
) -> dict[str, int]:
    """记录一次 LLM 调用的 token 消耗与估算费用。

    参数
    ----
    patient_id: 请求级患者标识（分账维度之一）。
    agent:      调用方节点（意图名 triage/booking/...，或 compose 汇总节点）。
    model:      模型展示名（``config.LLM_MODEL_NAME``）。
    prompt_text / completion_text: 无真实 usage 时用于估算的文本。
    message:    真实 LLM 返回的 AIMessage，优先从中读 ``usage_metadata``。

    返回本次记录的 token 数 ``{"prompt_tokens", "completion_tokens"}``，便于调用方断言。
    """
    if not config.COST_TRACKING_ENABLED:
        return {"prompt_tokens": 0, "completion_tokens": 0}

    usage = _usage_from_message(message)
    if usage is not None:
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
    else:
        prompt_tokens = estimate_tokens(prompt_text)
        completion_tokens = estimate_tokens(completion_text)

    # Prometheus：低基数 (agent, model, kind)
    LLM_TOKENS.labels(agent=agent, model=model, kind="prompt").inc(prompt_tokens)
    LLM_TOKENS.labels(agent=agent, model=model, kind="completion").inc(completion_tokens)

    # 当日预算累计（供 check_budget 预检卡口）
    with _BUDGET_LOCK:
        _budget_rollover()
        _BUDGET["total"] += prompt_tokens + completion_tokens
        _BUDGET["by_patient"][patient_id or "anonymous"] += prompt_tokens + completion_tokens

    price = _price_of(model)
    cost_usd = (
        prompt_tokens / 1000 * price["prompt"] + completion_tokens / 1000 * price["completion"]
    )
    if cost_usd > 0:
        LLM_COST_USD.labels(agent=agent, model=model).inc(cost_usd)

    # 内存分账：按 (患者, agent, 模型) 三维聚合
    key = (patient_id or "anonymous", agent, model)
    with _LOCK:
        row = _LEDGER[key]
        row["prompt"] += prompt_tokens
        row["completion"] += completion_tokens
        row["cost_usd"] += cost_usd

    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}


def _aggregate(by: int) -> list[dict]:
    """按某一维聚合：by=0 患者 / 1 agent / 2 model。"""
    acc: dict[str, dict] = defaultdict(lambda: {"prompt": 0, "completion": 0, "cost_usd": 0.0})
    with _LOCK:
        for (patient, agent, model), v in _LEDGER.items():
            dim = (patient, agent, model)[by]
            bucket = acc[dim]
            bucket["prompt"] += v["prompt"]
            bucket["completion"] += v["completion"]
            bucket["cost_usd"] += v["cost_usd"]
    out = [
        {
            "key": k,
            "prompt_tokens": int(v["prompt"]),
            "completion_tokens": int(v["completion"]),
            "total_tokens": int(v["prompt"] + v["completion"]),
            "cost_usd": round(v["cost_usd"], 6),
        }
        for k, v in acc.items()
    ]
    out.sort(key=lambda x: x["cost_usd"], reverse=True)
    return out


def cost_breakdown() -> dict:
    """返回成本归因全景：总量 + 按患者 / 按 Agent / 按模型 三维明细。"""
    with _LOCK:
        total_prompt = sum(v["prompt"] for v in _LEDGER.values())
        total_completion = sum(v["completion"] for v in _LEDGER.values())
        total_cost = sum(v["cost_usd"] for v in _LEDGER.values())
    return {
        "model": config.LLM_MODEL_NAME,
        "tracking_enabled": config.COST_TRACKING_ENABLED,
        "budget": budget_status(),
        "note": "patient 维度与预算计数为进程内分账（重启/多 worker 清零）；agent/model 维度同步进 Prometheus TSDB。",
        "totals": {
            "prompt_tokens": int(total_prompt),
            "completion_tokens": int(total_completion),
            "total_tokens": int(total_prompt + total_completion),
            "cost_usd": round(total_cost, 6),
        },
        "by_patient": _aggregate(0),
        "by_agent": _aggregate(1),
        "by_model": _aggregate(2),
    }


def reset_ledger() -> None:
    """清空内存分账（测试隔离 / 演示复位用）。"""
    with _LOCK:
        _LEDGER.clear()


def iter_ledger() -> Iterable[tuple[tuple[str, str, str], dict]]:
    """暴露 ledger 快照（测试断言用）。"""
    with _LOCK:
        return list(_LEDGER.items())
