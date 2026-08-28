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
    assert "建议科室" in r["messages"][-1].content


def test_booking_interrupt_and_resume():
    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-booking"}}
    r = _run(
        g.ainvoke({"messages": [HumanMessage("我要挂号预约神经内科")], "patient_id": "alice"}, cfg)
    )
    assert "__interrupt__" in r
    payload = r["__interrupt__"][0].value
    assert payload["action"] == "lock_and_settle"

    r2 = _run(g.ainvoke(Command(resume={"approved": True}), cfg))
    assert "已锁定" in r2["messages"][-1].content

    # 拒绝分支
    g2 = build_graph()
    cfg2 = {"configurable": {"thread_id": "t-booking-reject"}}
    _run(
        g2.ainvoke(
            {"messages": [HumanMessage("我要挂号预约神经内科")], "patient_id": "alice"}, cfg2
        )
    )
    r3 = _run(g2.ainvoke(Command(resume={"approved": False}), cfg2))
    assert "已取消" in r3["messages"][-1].content


def test_redline_emergency():
    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-emergency"}}
    r = _run(
        g.ainvoke({"messages": [HumanMessage("我胸痛呼吸困难快救我")], "patient_id": "alice"}, cfg)
    )
    assert r["intent"] == "emergency"
    # 急症强制走人工审核门：触发 interrupt（emergency_handoff）
    assert "__interrupt__" in r
    payload = r["__interrupt__"][0].value
    assert payload["action"] == "emergency_handoff"


def test_bind_tools_invoked():
    """验证子 Agent 是『通过 bind_tools 让 LLM 自主选工具』，而非硬编码调用。"""
    g = build_graph()
    cfg = {"configurable": {"thread_id": "t-bindtools"}}
    r = _run(g.ainvoke({"messages": [HumanMessage("我头痛挂什么科")], "patient_id": "alice"}, cfg))
    # 分诊会调用 search_department / dept_map_rag（bind_tools 范式 → messages 内含 ToolMessage）
    assert any(getattr(m, "type", None) == "tool" for m in r["messages"])
    assert "建议科室" in r["messages"][-1].content
