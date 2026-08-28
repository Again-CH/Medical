from langchain_core.tools import tool


@tool
def read_lab_report(patient_id: str) -> str:
    """读取 LIS 检验报告。"""
    return f"[LIS] {patient_id} 血常规：WBC 正常，CRP 轻度升高"


@tool
def clinical_kb(query: str) -> str:
    """检索临床知识库（RAG kb.py）。"""
    return f"[KB] 关于「{query}」的临床指引已检索"
