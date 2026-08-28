from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def handoff() -> str:
    """转接急诊人工台。"""
    return get_hub().handoff()


@tool
def call_120() -> str:
    """拨打 120 急救。"""
    return get_hub().call_120(None, "")
