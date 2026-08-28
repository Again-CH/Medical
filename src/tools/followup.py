from langchain_core.tools import tool


@tool
def read_vitals(patient_id: str) -> str:
    """读取患者近期生命体征。"""
    return f"[vitals] {patient_id} 血压 128/82，心率 72"


@tool
def plan_reminder(patient_id: str, text: str) -> str:
    """创建随访提醒。"""
    return f"[reminder] 已为 {patient_id} 创建提醒：{text}"


@tool
def memory_append(patient_id: str, note: str) -> str:
    """写入长期记忆。"""
    return f"[memory] 已记录 {patient_id} 随访笔记：{note}"
