from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def handoff() -> str:
    """转接急诊人工台。"""
    return get_hub().handoff()


@tool
def call_120() -> str:
    """拨打 120 急救（敏感动作，需人工确认）。

    安全设计：不接收 patient_id 参数，急救对象固定为当前登录患者本人，
    防止被诱导为他人触发 120 呼叫或污染他人档案。
    """
    return get_hub().call_120()
