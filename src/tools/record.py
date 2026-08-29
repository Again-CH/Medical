from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def record_lab_result(
    item: str,
    result: str,
    ref_range: str = "",
    abnormal: bool = False,
    report_date: str = "",
) -> str:
    """记录当前登录患者本人一条检验报告（如「血糖」「白细胞计数」「肌酐」）。

    当患者在对话中提供了某项检查结果的具体数值时调用，自动写入该患者私有档案库，
    供后续 read_lab_report 读取与医护端调阅。异常值请置 abnormal=True。
    患者身份一律来自请求上下文（JWT subject），不接受外部传入，杜绝越权写入他人档案。
    """
    return get_hub().record_lab_result(item, result, ref_range, abnormal, report_date)


@tool
def record_vital(type: str, value: str, unit: str = "") -> str:
    """记录当前登录患者本人一条生命体征（如「血压」「心率」「体温」「血氧」）。

    当患者提到测量得到的数值时调用，自动写入私有档案库。例如 type="血压" value="150/95" unit="mmHg"。
    患者身份来自请求上下文，不接受外部传入。
    """
    return get_hub().record_vital(type, value, unit)


@tool
def record_case_summary(text: str, category: str = "general") -> str:
    """记录当前登录患者本人一段病例小结/主诉（如现病史、既往史、用药史、过敏史）。

    用于把对话中的关键信息沉淀为结构化病历，供后续随访与医护端调阅。
    category 可填 general/既往史/用药史/过敏史 等，便于分类检索。
    患者身份来自请求上下文，不接受外部传入。
    """
    return get_hub().record_case_summary(text, category)
