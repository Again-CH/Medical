from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def read_vitals(patient_id: str) -> str:
    """读取患者近期生命体征。"""
    return get_hub().read_vitals(patient_id)


@tool
def plan_reminder(patient_id: str, text: str) -> str:
    """创建随访提醒。"""
    return get_hub().plan_reminder(patient_id, text)


@tool
def memory_append(patient_id: str, note: str) -> str:
    """写入长期记忆。"""
    return get_hub().memory_append("", patient_id, note)
