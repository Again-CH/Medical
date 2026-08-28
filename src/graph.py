import os

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .agents import _make_agent_node, final_answer
from .state import AgentState
from .supervisor import supervisor
from .tools import NAMESPACES


def build_graph(checkpointer=None):
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

    # checkpointer 优先级：调用方显式传入 > 同步默认（内存）
    # 注意：真实 Postgres 的持久化 checkpointer 是异步的，必须由 build_pg_checkpointer()
    # 在事件循环内构建，并经 lifespan 注入，不能在同步 build_graph() 里直接连库。
    cp = checkpointer if checkpointer is not None else _build_checkpointer()
    if cp is None:
        cp = MemorySaver()
    return g.compile(checkpointer=cp)


def _build_checkpointer():
    """同步路径的默认 checkpointer：非 Postgres 场景一律内存 MemorySaver。

    MemorySaver 同时支持同步/异步图操作，满足 fake/sqlite/hermetic 测试与 demo。
    真实 Postgres 的持久化（异步 AsyncPostgresSaver）不在此处构建——同步函数无法
    安全持有 asyncio 事件循环绑定的异步连接，需经 build_pg_checkpointer() 注入。
    """
    return MemorySaver()


async def build_pg_checkpointer():
    """构建异步 Postgres checkpointer（AsyncPostgresSaver），实现会话状态跨进程/重启持久化。

    必须在运行中的事件循环内调用（如 FastAPI lifespan），使 AsyncConnection 绑定到
    该 loop，避免「跨事件循环使用异步连接」错误。连不上 Postgres 时返回 None（降级内存）。
    """
    url = os.getenv("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    try:
        import psycopg
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # SQLAlchemy 风格 postgresql+psycopg2:// 需转为 psycopg3 使用的 postgresql://
        conn_str = url.replace("postgresql+psycopg2://", "postgresql://")
        try:
            conn = await psycopg.AsyncConnection.connect(conn_str, autocommit=True)
        except Exception:
            conn = await psycopg.AsyncConnection.connect(
                conn_str.replace("localhost", "127.0.0.1"), autocommit=True
            )
        cp = AsyncPostgresSaver(conn)
        await cp.setup()  # 幂等建 checkpointer 所需的内部表
        return cp
    except Exception as e:  # 连不上 Postgres 时降级，保证服务可起
        print(f"[warn] AsyncPostgresSaver 初始化失败（{e}），回退到内存 MemorySaver")
        return None
