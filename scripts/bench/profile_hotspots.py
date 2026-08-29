#!/usr/bin/env python3
"""热点剖析：checkpointer 写放大 + pgvector 检索耗时 + Tier-0 闸门开销。

压测（locust）给出端到端的延迟分布，但答不了"时间花在哪"。
本脚本直接量化三个架构热点，用于判断优化方向：

1. **checkpointer 写放大**：LangGraph 每步都落一次状态，一轮对话要往
   ``checkpoints`` / ``checkpoint_blobs`` / ``checkpoint_writes`` 写多少行？
   写放大直接决定 Postgres 会不会成为瓶颈，以及要不要做异步批量提交。
2. **pgvector 检索耗时**：``knowledge_documents`` 64 条语料的余弦近邻检索，
   以及 embedding 本身（零依赖特征哈希）的开销。
3. **Tier-0 安全闸开销**：确定性闸门每轮都跑，必须证明它是微秒级的，
   否则"安全优先于一切"的设计会拖垮整条链路。

用法（需真实 Postgres）::

    DATABASE_URL=postgresql+psycopg2://mac@localhost:5432/medical_agent \\
        .venv/bin/python scripts/bench/profile_hotspots.py
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("LLM_MODE", "fake")

from sqlalchemy import text  # noqa: E402
from src.db import get_session, init_db  # noqa: E402
from src.seed import seed_all  # noqa: E402


def _count(table: str) -> int:
    with get_session() as s:
        return s.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0


def profile_checkpointer() -> dict:
    """跑一轮完整对话，统计 checkpointer 三张表的写放大。

    必须用 **Postgres checkpointer**（``build_pg_checkpointer``）而不是默认内存版：
    内存版不落库，写永远是 0，测不出任何东西——这也是踩过的坑。
    """
    from langchain_core.messages import HumanMessage
    from src.agents import _PENDING
    from src.graph import build_graph, build_pg_checkpointer

    async def _run() -> float:
        _PENDING.clear()
        cp = await build_pg_checkpointer()
        if cp is None:
            return -1.0
        g = build_graph(checkpointer=cp)
        cfg = {"configurable": {"thread_id": f"bench-cp-{int(time.time())}"}}
        t0 = time.monotonic()
        await g.ainvoke(
            {"messages": [HumanMessage("我最近头痛，应该挂什么科")], "patient_id": "alice"},
            cfg,
        )
        return (time.monotonic() - t0) * 1000

    tables = ["checkpoints", "checkpoint_blobs", "checkpoint_writes"]
    before = {t: _count(t) for t in tables}
    elapsed_ms = asyncio.run(_run())
    after = {t: _count(t) for t in tables}
    delta = {t: after[t] - before[t] for t in tables}

    if elapsed_ms < 0:
        return {"error": "Postgres checkpointer 不可用（非 Postgres 或连接失败）"}
    return {
        "elapsed_ms": round(elapsed_ms, 1),
        "rows_written": delta,
        "rows_total": sum(delta.values()),
    }


def profile_vector_search(rounds: int = 30) -> dict:
    """pgvector 余弦检索 + embedding 计算耗时（p50 / p95）。"""
    try:
        from src.embeddings import embed
        from src.kb import search_knowledge
    except Exception as e:  # noqa: BLE001
        return {"error": f"pgvector 检索不可用：{e}"}

    queries = [
        "高血压饮食注意事项",
        "头痛伴手脚麻木挂什么科",
        "急诊分级标准",
        "空腹血糖参考范围",
        "胸痛持续十五分钟",
    ]

    embed_ms, search_ms = [], []
    for i in range(rounds):
        q = queries[i % len(queries)]
        t0 = time.monotonic()
        vec = embed(q)
        embed_ms.append((time.monotonic() - t0) * 1000)

        t0 = time.monotonic()
        search_knowledge(q, top_k=3)
        search_ms.append((time.monotonic() - t0) * 1000)

    def pct(xs, p):
        return round(statistics.quantiles(xs, n=100)[p - 1], 3) if len(xs) > 1 else round(xs[0], 3)

    with get_session() as s:
        corpus_size = s.execute(text("SELECT count(*) FROM knowledge_documents")).scalar() or 0

    return {
        "rounds": rounds,
        "dim": len(vec),
        "corpus_size": corpus_size,
        "embed_ms": {"p50": pct(embed_ms, 50), "p95": pct(embed_ms, 95)},
        "search_ms": {"p50": pct(search_ms, 50), "p95": pct(search_ms, 95)},
    }


def profile_safety_gate(rounds: int = 2000) -> dict:
    """Tier-0 三道确定性闸门的单次开销（微秒级才算不影响主链路）。"""
    from src.safety import assess_emergency, assess_scope_violation

    samples = ["我突然胸痛呼吸困难", "帮我开点阿莫西林", "我最近有点头痛"]
    t0 = time.monotonic()
    for i in range(rounds):
        s = samples[i % len(samples)]
        assess_emergency(s)
        assess_scope_violation(s)
    total_ms = (time.monotonic() - t0) * 1000
    return {
        "rounds": rounds,
        "us_per_call": round(total_ms * 1000 / rounds, 2),
        "total_ms": round(total_ms, 2),
    }


def main() -> int:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        print("需要 DATABASE_URL（checkpointer 与 pgvector 只在真实 Postgres 上有意义）")
        return 2

    init_db()
    seed_all()

    report = {
        "database": url.split("@")[-1],
        "checkpointer_write_amplification": profile_checkpointer(),
        "vector_search": profile_vector_search(),
        "safety_gate": profile_safety_gate(),
    }
    out = ROOT / "bench" / "hotspots.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n已写入: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
