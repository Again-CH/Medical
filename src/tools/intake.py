from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def read_lab_report(patient_id: str) -> str:
    """读取 LIS 检验报告。"""
    return get_hub().read_lab_report(patient_id)


@tool
def clinical_kb(query: str) -> str:
    """检索临床知识库（RAG kb.py）。"""
    return get_hub().clinical_kb(query)
