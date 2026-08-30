from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


def add_usage(a: dict | None, b: dict | None) -> dict:
    """token 用量的 state reducer：各节点上报的用量累加。

    为什么走 state 而不是 contextvar
    --------------------------------
    LangGraph 的 ToolNode / 节点可能在**独立的异步任务**里执行，而 contextvars
    不会自动跨任务继承（本项目里 patient_ctx / tenant_ctx 都得在 agent 节点里
    从 state 重新 set 才拿得到）。token 累计若用 contextvar，子 Agent 那一跳
    的消耗会直接丢失。走 state + reducer 则由 LangGraph 自己负责合并，
    跨任务、跨节点都可靠。
    """
    left = a or {}
    right = b or {}
    return {
        "prompt": int(left.get("prompt", 0)) + int(right.get("prompt", 0)),
        "completion": int(left.get("completion", 0)) + int(right.get("completion", 0)),
    }


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    intent: str
    patient_id: str
    tool_result: str
    redline_reason: str
    # 本轮累计 token 消耗（各节点上报后由 add_usage 合并），供网关落审计与成本归因
    token_usage: Annotated[dict, add_usage]
    # 本轮实际生效的 prompt 版本（灰度发布时用来定位「出问题的是 v1 还是 v2」）
    prompt_version: str
