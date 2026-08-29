from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def read_vitals() -> str:
    """读取当前登录患者本人近期的生命体征。

    安全设计：patient_id 不作为参数暴露给模型，身份取自请求上下文。
    """
    return get_hub().read_vitals()


@tool
def plan_reminder(text: str) -> str:
    """为当前登录患者本人创建随访提醒。"""
    return get_hub().plan_reminder(text)


@tool
def memory_append(note: str) -> str:
    """为当前登录患者本人写入长期随访记忆。"""
    return get_hub().memory_append(note)
