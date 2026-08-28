"""离线评测：用 tests/eval 下的红线 / 意图数据集，批量端到端跑图并出报告。

用法：
    python scripts/eval_offline.py                 # 跑默认数据集，打印准确率与明细
    python scripts/eval_offline.py --out report.json
    python scripts/eval_offline.py --dataset langsmith  # 若已设 LANGSMITH_API_KEY，结果自动上报

说明：
- 红线 / 意图评测复用 tests/eval/*.json，与 CI（pytest）同一份数据，保证「评测集即守门员」。
- langgraph 在 LANGSMITH_TRACING 开启时会自动把每次 graph 运行上报到 LangSmith，
  因此本脚本无需额外埋点即可获得链路追踪 + 离线评测联动。
"""

import argparse
import asyncio
import functools
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.errors import GraphInterrupt  # noqa: E402
from src.graph import build_graph  # noqa: E402

print = functools.partial(print, flush=True)


async def _run_case(g, text: str, thread_id: str) -> dict:
    cfg = {"configurable": {"thread_id": thread_id}}
    try:
        r = await g.ainvoke(
            {"messages": [HumanMessage(text)], "patient_id": "eval"},
            cfg,
        )
    except GraphInterrupt:
        # 命中红线/敏感动作时图会 interrupt() 暂停：意图与红线判定已在
        # supervisor 前置节点完成，从中断前的 state 快照读回即可。
        r = g.get_state(cfg).values
    return {
        "intent": r.get("intent"),
        "redline_reason": r.get("redline_reason", ""),
    }


async def eval_redline(g, cases) -> dict:
    total = len(cases)
    passed = 0
    details = []
    for i, c in enumerate(cases):
        res = await _run_case(g, c["text"], f"eval-rl-{i}")
        # 红线命中 → 强制走 emergency 意图；未命中 → 不应进入 emergency
        hit = res["intent"] == "emergency"
        ok = hit == c["expect_hit"]
        if ok and c.get("reason_contains"):
            ok = c["reason_contains"] in res["redline_reason"]
        passed += int(ok)
        details.append(
            {
                "text": c["text"],
                "expect_hit": c["expect_hit"],
                "got_hit": hit,
                "reason": res["redline_reason"],
                "ok": ok,
            }
        )
    return {"name": "redline", "total": total, "passed": passed, "details": details}


async def eval_intent(g, cases) -> dict:
    total = len(cases)
    passed = 0
    details = []
    for i, c in enumerate(cases):
        res = await _run_case(g, c["text"], f"eval-it-{i}")
        # 红线优先：含急症的意图用例会被强制路由到 emergency，这里只评非红线意图
        if c["expect"] == "emergency":
            ok = res["intent"] == "emergency"
        else:
            ok = (res["intent"] == c["expect"]) and (res["redline_reason"] == "")
        passed += int(ok)
        details.append({"text": c["text"], "expect": c["expect"], "got": res["intent"], "ok": ok})
    return {"name": "intent", "total": total, "passed": passed, "details": details}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="报告输出路径（json）")
    ap.add_argument(
        "--dataset", default="", help="可选 langsmith：仅作语义标记，自动追踪由环境变量控制"
    )
    args = ap.parse_args()

    with open(os.path.join(ROOT, "tests/eval/redline_cases.json"), encoding="utf-8") as f:
        redline_cases = json.load(f)
    with open(os.path.join(ROOT, "tests/eval/intent_cases.json"), encoding="utf-8") as f:
        intent_cases = json.load(f)

    g = build_graph()
    rl = await eval_redline(g, redline_cases)
    it = await eval_intent(g, intent_cases)

    report = {
        "redline": rl,
        "intent": it,
        "accuracy": {
            "redline": f"{rl['passed']}/{rl['total']}",
            "intent": f"{it['passed']}/{it['total']}",
        },
    }

    print("=" * 60)
    print("离线评测报告 (tests/eval)")
    print("=" * 60)
    print(f"[红线] 命中准确性: {rl['passed']}/{rl['total']}")
    for d in rl["details"]:
        mark = "✓" if d["ok"] else "✗"
        print(f"  {mark} {d['text']} -> hit={d['got_hit']} (expect {d['expect_hit']})")
    print(f"[意图] 分类准确性: {it['passed']}/{it['total']}")
    for d in it["details"]:
        mark = "✓" if d["ok"] else "✗"
        print(f"  {mark} {d['text']} -> {d['got']} (expect {d['expect']})")

    if args.dataset == "langsmith":
        tracing = os.getenv("LANGSMITH_TRACING", "")
        print(
            f"\nLangSmith 追踪: {'已开启' if tracing else '未开启（设 LANGSMITH_TRACING=true 自动上报）'}"
        )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入: {args.out}")

    # 评测门禁：任一线 / 意图用例不通过则非零退出，作为 CI 守门员
    all_ok = (rl["passed"] == rl["total"]) and (it["passed"] == it["total"])
    print("\n" + ("全部通过 ✅" if all_ok else "存在未通过用例 ❌"))
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
