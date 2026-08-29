from langchain_core.tools import tool

from ..integrations import get_hub


@tool
def query_availability(department: str, date: str = "today") -> str:
    """查询某科室某日的号源。"""
    return get_hub().query_availability(department, date)


@tool
def lock_appointment(department: str, date: str, slot: str) -> str:
    """锁定号源（有名额即自动执行）。"""
    return get_hub().lock_appointment(department, date, slot)


@tool
def confirm_appointment(appointment_id: str = "") -> str:
    """确认/完成已锁定的号源。appointment_id 为空时自动确认该患者最近一笔 LOCKED 预约。"""
    return get_hub().confirm_appointment(appointment_id)


@tool
def medicare_settle(appointment_id: str) -> str:
    """医保结算（敏感动作，需人工确认）。"""
    return get_hub().medicare_settle(appointment_id)
