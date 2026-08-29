from typing import Optional

from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def read_lab_report(item_name: Optional[str] = None) -> str:
    """读取当前登录患者本人的 LIS 检验报告。

    安全设计：patient_id 不作为参数暴露给模型。患者身份一律由请求上下文
    （contextvars，源自 JWT subject）注入，杜绝 prompt injection 操纵
    「读取他人报告」的越权风险。

    当用户要求解读某一具体项目（如「解读我的血糖报告」）时，把项目名传给
    item_name，可精准定位单条报告；未指定时返回全部报告。
    """
    return get_hub().read_lab_report(item_name=item_name)


@tool
def clinical_kb(query: str) -> str:
    """检索临床知识库（RAG kb.py）。"""
    return get_hub().clinical_kb(query)


@tool
def hospital_rag(query: str) -> str:
    """检索医院内部资料（就诊流程/医保/检查化验须知/体检/院区导航/便民/线上复诊等）。

    当患者提及即将做检查、化验、抽血、B超、CT、核磁、超声、胃镜、肠镜、造影、穿刺、
    或询问体检/检查前准备与注意事项时，调用本工具获取权威的院内检查须知并主动提示患者，
    区别于 read_lab_report（解读已有报告）与 clinical_kb（临床诊疗指引）。
    """
    return get_hub().hospital_rag(query)
