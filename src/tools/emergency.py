from langchain_core.tools import tool


@tool
def handoff() -> str:
    """转接急诊人工台。"""
    return "[emergency] 已转接急诊人工台，请保持通话"


@tool
def call_120() -> str:
    """拨打 120 急救。"""
    return "[emergency] 已触发 120 呼叫流程"
