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

# 评测强制使用本地 sqlite，保证离线、hermetic、不污染真实数据库（即使 .env 指向 Postgres）。
# 必须在 import src 之前设置，避免 config 的 load_dotenv 把 DATABASE_URL 覆盖成真实库。
os.environ["DATABASE_URL"] = "sqlite:///./eval.db"

# 评测强制 fake 模型：结果必须确定性、可复现，且不依赖外部 API（CI 无密钥、也不应烧钱）。
# 真实模型的表现由 LangSmith 在线追踪观察，不作为合并门禁的判据。
# 需要真实模型评测时用 ``--real-model`` 显式开启。
if "--real-model" not in sys.argv:
    os.environ["LLM_MODE"] = "fake"

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.errors import GraphInterrupt  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402
from src.agents import _PENDING  # noqa: E402
from src.db import get_session, init_db  # noqa: E402
from src.graph import build_graph  # noqa: E402
from src.seed import seed_all  # noqa: E402

print = functools.partial(print, flush=True)

# 强制 sqlite 后，红线 / 意图评测中涉及挂号、结算的用例也会走 DbHub 真实库，
# 因此必须在所有 eval 函数运行前建表并播种（否则首次命中 DbHub 查询会因缺表报错）。
init_db()
seed_all()


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
        _PENDING.clear()
        res = await _run_case(g, c["text"], f"eval-rl-{i}")
        # 红线命中判定：supervisor 命中红线后写入 redline_reason，并路由到 **triage**
        # （而非 emergency）由 final_answer 输出红线提示——因 emergency agent 的 token
        # 会被网关过滤导致空响应。故以 redline_reason 是否非空为准。
        hit = bool(res["redline_reason"])
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
        _PENDING.clear()
        res = await _run_case(g, c["text"], f"eval-it-{i}")
        # 红线优先：含急症的意图用例会被强制路由到 emergency，这里只评非红线意图
        if c["expect"] == "emergency":
            ok = res["intent"] == "emergency"
        else:
            ok = (res["intent"] == c["expect"]) and (res["redline_reason"] == "")
        passed += int(ok)
        details.append({"text": c["text"], "expect": c["expect"], "got": res["intent"], "ok": ok})
    return {"name": "intent", "total": total, "passed": passed, "details": details}


async def eval_e2e(g, cases) -> dict:
    """端到端闭环评测：覆盖「审核门触发 → 批准/拒绝 → 真实落库」全链路。

    顶部已强制 DATABASE_URL=sqlite:///./eval.db（hermetic，不污染真实库），
    与 tests/eval/test_e2e.py 共用同一份 e2e_cases.json（评测即守门员）。
    """
    init_db()  # 建表（幂等）
    seed_all()  # 幂等播种：确保神经内科/王医生/排班等被锁号逻辑依赖的数据存在

    # 评测为 hermetic 环境：清空历史预约与幂等键。
    # 否则跨轮累积会让「幂等键去重」与「单患者单日挂号上限」这些**正确的业务约束**
    # 干扰评测结论（例如第二轮锁号被幂等键命中而不再落库）。生产环境不受影响。
    with get_session() as s:
        s.execute(text("DELETE FROM appointments"))
        s.execute(text("DELETE FROM idempotency_keys"))
        s.commit()

    def _max_appointment_id() -> int:
        with get_session() as s:
            return s.execute(text("SELECT COALESCE(max(id), 0) FROM appointments")).scalar() or 0

    def _new_appointments_since(before_id) -> list:
        with get_session() as s:
            rows = s.execute(
                text("SELECT status, medicare_settled FROM appointments WHERE id > :mid"),
                {"mid": before_id},
            ).fetchall()
        return [(r[0], bool(r[1])) for r in rows]

    total = len(cases)
    passed = 0
    details = []
    for c in cases:
        _PENDING.clear()
        cfg = {"configurable": {"thread_id": f"eval-e2e-{c['id']}"}}
        # 基线必须在 invoke **之前**取：锁号属非敏感动作、在首轮即执行，
        # 若等到 interrupt 之后再取基线会把它漏掉，误判为「未落库」。
        before_id = _max_appointment_id()
        r = await g.ainvoke(
            {
                "messages": [HumanMessage(c["text"])],
                "patient_id": c.get("patient_id", "alice"),
            },
            cfg,
        )
        interrupted = "__interrupt__" in r
        ok = interrupted == c["expect"]["interrupt"]
        detail = {
            "id": c["id"],
            "expect_interrupt": c["expect"]["interrupt"],
            "got_interrupt": interrupted,
        }
        if interrupted and ok:
            payload = r["__interrupt__"][0].value
            detail["action"] = payload.get("action")
            detail["tools"] = payload.get("tools")
            ok = ok and (payload.get("action") == c["expect"]["action"])
            # 敏感工具集合匹配（顺序无关）
            ok = ok and (
                sorted(payload.get("tools") or []) == sorted(c["expect"].get("tools") or [])
            )
            if "after_approve" in c["expect"]:
                await g.ainvoke(Command(resume={"approved": True}), cfg)
                new = _new_appointments_since(before_id)
                # 批准 → 新预约应为 LOCKED 且已医保结算
                ok = ok and (len(new) >= 1)
                ok = ok and all(s == "LOCKED" and m for s, m in new)
                detail["persisted"] = len(new)
            elif "after_reject" in c["expect"]:
                await g.ainvoke(Command(resume={"approved": False}), cfg)
                new = _new_appointments_since(before_id)
                # 拒绝的是「医保结算」而非挂号本身：号源在首轮已锁定（非敏感动作），
                # 被拒后患者仍持号但需自费。故断言：不得出现「已结算」的新预约。
                ok = ok and all(not m for _, m in new)
                detail["persisted"] = len(new)
        elif (not interrupted) and ok:
            final = r["messages"][-1].content or ""
            if "final_contains" in c["expect"]:
                ok = ok and (c["expect"]["final_contains"] in final)
                # 便于失败时定位（评测报告 artifact 会带上这段摘要）
                detail["final_head"] = final[:120]
        detail["ok"] = ok
        passed += int(ok)
        details.append(detail)

    return {"name": "e2e_closed_loop", "total": total, "passed": passed, "details": details}


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
    with open(os.path.join(ROOT, "tests/eval/e2e_cases.json"), encoding="utf-8") as f:
        e2e_cases = json.load(f)

    g = build_graph()
    rl = await eval_redline(g, redline_cases)
    it = await eval_intent(g, intent_cases)
    e2e = await eval_e2e(g, e2e_cases)

    report = {
        "redline": rl,
        "intent": it,
        "e2e": e2e,
        "accuracy": {
            "redline": f"{rl['passed']}/{rl['total']}",
            "intent": f"{it['passed']}/{it['total']}",
            "e2e": f"{e2e['passed']}/{e2e['total']}",
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
    print(f"[端到端] 闭环准确性: {e2e['passed']}/{e2e['total']}")
    for d in e2e["details"]:
        mark = "✓" if d["ok"] else "✗"
        extra = f" tools={d.get('tools')}" if d.get("tools") else ""
        persist = d.get("persisted")
        extra += f" persisted={persist}" if persist is not None else ""
        print(f"  {mark} {d['id']} -> interrupt={d['got_interrupt']}{extra}")

    if args.dataset == "langsmith":
        tracing = os.getenv("LANGSMITH_TRACING", "")
        print(
            f"\nLangSmith 追踪: {'已开启' if tracing else '未开启（设 LANGSMITH_TRACING=true 自动上报）'}"
        )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入: {args.out}")

    # 评测门禁：任一线 / 意图 / 端到端用例不通过则非零退出，作为 CI 守门员
    all_ok = (
        (rl["passed"] == rl["total"])
        and (it["passed"] == it["total"])
        and (e2e["passed"] == e2e["total"])
    )
    print("\n" + ("全部通过 ✅" if all_ok else "存在未通过用例 ❌"))
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
