"""端到端闭环评测（数据驱动）：

覆盖「意图路由 → 人工审核门(HITL) → 批准后真实落库」完整链路，是本项目
对 Agent 行为最关键的守门测试。全部跑在 fake 模型 + 真实数据库（conftest 注入的
sqlite test.db，已由 seed 播种神经内科/王医生/排班等），无需任何 API key。

断言维度（全部确定性，不依赖 LLM 生成文案的随机性）：
- 敏感动作（锁号+结算/急症）必须触发 interrupt 人工审核门，且 action 与敏感工具集合精确匹配；
- 非敏感意图（分诊/检验/随访）不得触发 interrupt，直接产出回复；
- 审核门批准 → 真实落库（新增 appointments 行 status=LOCKED 且 medicare_settled=True）；
- 审核门拒绝 → 不落库。

数据集：tests/eval/e2e_cases.json（与 src 行为同一份真相源，CI 门禁共用）。
"""

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402
from src.agents import _PENDING  # noqa: E402
from src.db import get_session  # noqa: E402
from src.graph import build_graph  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_cases():
    with open(os.path.join(HERE, "e2e_cases.json"), encoding="utf-8") as f:
        return json.load(f)


def _run(coro):
    return asyncio.run(coro)


def _max_appointment_id() -> int:
    with get_session() as s:
        return s.execute(text("SELECT COALESCE(max(id), 0) FROM appointments")).scalar() or 0


def _new_appointments_since(before_id) -> list:
    """返回 id 大于 before_id 的新预约行，归一化为 (status, medicare_settled)。"""
    with get_session() as s:
        rows = s.execute(
            text("SELECT status, medicare_settled FROM appointments WHERE id > :mid"),
            {"mid": before_id},
        ).fetchall()
    return [(r[0], bool(r[1])) for r in rows]


def _run_case(case: dict) -> dict:
    _PENDING.clear()
    g = build_graph()
    cfg = {"configurable": {"thread_id": f"e2e-{case['id']}"}}
    r = _run(
        g.ainvoke(
            {
                "messages": [HumanMessage(case["text"])],
                "patient_id": case.get("patient_id", "alice"),
            },
            cfg,
        )
    )
    result = {"interrupted": "__interrupt__" in r}
    if result["interrupted"]:
        payload = r["__interrupt__"][0].value
        result["action"] = payload.get("action")
        result["tools"] = payload.get("tools")

        if "after_approve" in case["expect"]:
            before_id = _max_appointment_id()
            r2 = _run(g.ainvoke(Command(resume={"approved": True}), cfg))
            result["after_approve_final"] = r2["messages"][-1].content
            result["new_appointments"] = _new_appointments_since(before_id)
        elif "after_reject" in case["expect"]:
            before_id = _max_appointment_id()
            r2 = _run(g.ainvoke(Command(resume={"approved": False}), cfg))
            result["after_reject_final"] = r2["messages"][-1].content
            result["new_appointments"] = _new_appointments_since(before_id)
    else:
        result["final"] = r["messages"][-1].content
    return result


def test_e2e_closed_loop():
    for case in _load_cases():
        exp = case["expect"]
        res = _run_case(case)
        cid = case["id"]

        # 1) interrupt 是否按预期触发
        assert res["interrupted"] == exp["interrupt"], f"[{cid}] interrupt 不符合预期"

        if exp["interrupt"]:
            # 2) 审核门 action / 敏感工具集合精确匹配（集合比较，顺序无关）
            assert res["action"] == exp["action"], f"[{cid}] action={res['action']}"
            if "tools" in exp:
                assert sorted(res["tools"]) == sorted(exp["tools"]), (
                    f"[{cid}] tools={res['tools']}"
                )

            # 3) 批准后：真实落库（最关键的闭环验证）—— 行级断言 status=LOCKED 且 medicare_settled
            if "after_approve" in exp:
                assert res.get("after_approve_final"), f"[{cid}] 批准后应产出回复"
                new = res["new_appointments"]
                assert len(new) >= 1, f"[{cid}] 批准后未真实落库"
                assert all(s == "LOCKED" and m for s, m in new), (
                    f"[{cid}] 落库记录未满足 LOCKED+medicare_settled: {new}"
                )
            # 4) 拒绝后：不落库
            elif "after_reject" in exp:
                assert res.get("after_reject_final"), f"[{cid}] 拒绝后应产出回复"
                assert res["new_appointments"] == [], (
                    f"[{cid}] 拒绝后仍落库: {res['new_appointments']}"
                )
        else:
            # 5) 非敏感意图：不得触发审核门，且能产出回复
            if "final_contains" in exp:
                assert exp["final_contains"] in res["final"], (
                    f"[{cid}] 回复缺少「{exp['final_contains']}」: {res['final']}"
                )
