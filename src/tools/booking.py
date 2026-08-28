from langchain_core.tools import tool


@tool
def query_availability(department: str, date: str = "today") -> str:
    """查询某科室某日的号源。"""
    return f"[availability] {department} {date} 剩余号源：3"


@tool
def lock_appointment(department: str, date: str, slot: str) -> str:
    """锁定号源（敏感动作，需人工确认）。"""
    return f"[locked] {department} {date} {slot} 已锁定 appointment_id=APT-1001"


@tool
def medicare_settle(appointment_id: str) -> str:
    """医保结算（敏感动作，需人工确认）。"""
    return f"[settled] {appointment_id} 医保结算完成"
