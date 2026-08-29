import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402
from src.graph import build_graph  # noqa: E402


def _run(coro):
    """图含 async 节点（final_answer 流式），统一用 async 运行时。"""
    return asyncio.run(coro)


def test_triage_flow():
    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-triage"}}
    r = _run(g.ainvoke({"messages": [HumanMessage("我头痛挂什么科")], "patient_id": "alice"}, cfg))
    assert r["intent"] == "triage"
    # 知识库直出格式：含结构化科室推荐（📌 建议就诊科室 或 建议科室）
    content = r["messages"][-1].content
    assert "建议就诊科室" in content or "建议科室" in content, f"缺科室推荐: {content[:100]}"


def test_booking_auto_lock():
    """纯挂号（不含医保/结算）→ 自动执行，无 HITL 中断。"""
    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-booking-auto"}}
    r = _run(
        g.ainvoke(
            {"messages": [HumanMessage("我要预约呼吸内科今天上午的号")], "patient_id": "alice"}, cfg
        )
    )
    # 纯挂号不应触发 interrupt
    assert "__interrupt__" not in r, f"纯挂号不应触发中断: {r.get('__interrupt__')}"
    # 应产出含挂号结果的回复
    final = r["messages"][-1].content
    assert final, "纯挂号应产出回复"


def test_booking_settle_interrupt():
    """挂号+医保结算 → 仅 medicare_settle 走 HITL 审批。"""
    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-booking-settle"}}
    r = _run(
        g.ainvoke(
            {
                "messages": [HumanMessage("我要预约挂号神经内科今天，并办理医保结算")],
                "patient_id": "alice",
            },
            cfg,
        )
    )
    assert "__interrupt__" in r
    payload = r["__interrupt__"][0].value
    # lock_appointment 已自动执行，审批列表中只有 medicare_settle
    assert payload["action"] == "medicare_settle"
    assert "medicare_settle" in payload["tools"]
    assert "lock_appointment" not in payload["tools"]

    r2 = _run(g.ainvoke(Command(resume={"approved": True}), cfg))
    assert "已锁定" in r2["messages"][-1].content or "医保" in r2["messages"][-1].content

    # 拒绝分支
    g2 = build_graph()
    cfg2 = {"configurable": {"thread_id": "t-booking-reject"}}
    _run(
        g2.ainvoke(
            {
                "messages": [HumanMessage("我要预约挂号神经内科今天，并办理医保结算")],
                "patient_id": "alice",
            },
            cfg2,
        )
    )
    r3 = _run(g2.ainvoke(Command(resume={"approved": False}), cfg2))
    assert "已取消" in r3["messages"][-1].content or "拒绝" in r3["messages"][-1].content


def test_redline_emergency():
    """红线关键词命中 → 路由到 triage（非 emergency agent），由 final_answer 输出红线提示。

    架构说明：gateway 只推送 final_answer 节点的 token，emergency agent 的 token 会被过滤
    导致空响应，故红线命中后改走 triage + final_answer 输出含红线提示的完整回复。
    """
    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-emergency"}}
    r = _run(
        g.ainvoke({"messages": [HumanMessage("我胸痛呼吸困难快救我")], "patient_id": "alice"}, cfg)
    )
    # 红线命中后走 triage（非 emergency agent）
    assert r["intent"] == "triage"
    # 不再触发 interrupt（已改为 final_answer 直接输出红线提示）
    assert "__interrupt__" not in r
    # redline_reason 应写入 state
    assert r.get("redline_reason"), "红线命中应设置 redline_reason"
    # 最终回复应包含红线相关内容
    final = r["messages"][-1].content
    assert "红线" in final or "急症" in final or "120" in final, f"回复应含红线提示: {final[:200]}"


def test_bind_tools_invoked():
    """验证子 Agent 是『通过 bind_tools 让 LLM 自主选工具』，而非硬编码调用。"""
    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-bindtools"}}
    r = _run(g.ainvoke({"messages": [HumanMessage("我头痛挂什么科")], "patient_id": "alice"}, cfg))
    # 分诊会调用 search_department / dept_map_rag（bind_tools 范式 → messages 内含 ToolMessage）
    assert any(getattr(m, "type", None) == "tool" for m in r["messages"])
    content = r["messages"][-1].content
    assert "建议就诊科室" in content or "建议科室" in content, f"缺科室推荐: {content[:100]}"
