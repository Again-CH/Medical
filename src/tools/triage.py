from langchain_core.tools import tool


@tool
def search_department(symptom: str) -> str:
    """根据症状检索推荐科室。"""
    mapping = {
        "头痛": "神经内科",
        "发烧": "感染科",
        "咳嗽": "呼吸内科",
        "腹痛": "消化内科",
        "皮疹": "皮肤科",
        "胸闷": "心血管内科",
    }
    for k, v in mapping.items():
        if k in symptom:
            return f"建议科室：{v}"
    return "建议科室：全科 / 分诊台进一步评估"


@tool
def dept_map_rag(symptom: str) -> str:
    """基于 RAG 知识库的科室映射（SNOMED / Milvus 替换点）。"""
    return f"[RAG] 症状「{symptom}」→ 科室画像与候诊时长已检索"
