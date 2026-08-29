from . import booking, emergency, followup, intake, record, triage

# 每个子 Agent 独立工具命名空间（图里「子 Agent 各自独立工具集」的代码落点）
NAMESPACES = {
    "triage": [
        triage.search_health_info,
        triage.search_department,
        triage.dept_map_rag,
        triage.hospital_rag,
        record.record_lab_result,
        record.record_vital,
        record.record_case_summary,
    ],
    "booking": [
        booking.query_availability,
        booking.lock_appointment,
        booking.confirm_appointment,
        booking.medicare_settle,
    ],
    "intake": [
        intake.read_lab_report,
        intake.clinical_kb,
        intake.hospital_rag,
        record.record_lab_result,
        record.record_vital,
        record.record_case_summary,
    ],
    "followup": [followup.read_vitals, followup.plan_reminder, followup.memory_append],
    "emergency": [emergency.handoff, emergency.call_120],
}

__all__ = ["NAMESPACES", "triage", "booking", "intake", "followup", "emergency", "record"]
