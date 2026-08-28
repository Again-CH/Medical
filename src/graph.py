from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .agents import _make_agent_node, final_answer
from .state import AgentState
from .supervisor import supervisor
from .tools import NAMESPACES


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("supervisor", supervisor)

    # 每个子 Agent 一个独立节点（中枢辐射编排的「辐」）
    for intent in NAMESPACES:
        g.add_node(f"agent_{intent}", _make_agent_node(intent))
    g.add_node("final_answer", final_answer)

    g.add_edge(START, "supervisor")
    # Supervisor 红线 + 意图分类后，路由到对应子 Agent
    g.add_conditional_edges(
        "supervisor",
        lambda s: f"agent_{s['intent']}",
        {f"agent_{intent}": f"agent_{intent}" for intent in NAMESPACES},
    )
    for intent in NAMESPACES:
        g.add_edge(f"agent_{intent}", "final_answer")
    g.add_edge("final_answer", END)

    return g.compile(checkpointer=MemorySaver())
