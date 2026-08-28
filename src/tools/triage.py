from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def search_department(symptom: str) -> str:
    """根据症状检索推荐科室。"""
    return get_hub().search_department(symptom)


@tool
def dept_map_rag(symptom: str) -> str:
    """基于 RAG 知识库的科室映射（向量检索替换点）。"""
    return get_hub().dept_map_rag(symptom)
