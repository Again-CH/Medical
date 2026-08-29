"""种子数据：幂等地把演示用的科室/医生/排班/检验/生命体征/用户写入数据库。

仅在设置了 DATABASE_URL 时生效。可重复执行（已存在则跳过），便于本地起库与 CI 集成测试。
真实生产应替换为医院主数据同步（HIS），此处仅为可运行 demo / 测试提供基础数据。
"""

from __future__ import annotations

from datetime import date

from .auth import hash_password
from .db import (
    Department,
    Doctor,
    DoctorSchedule,
    ExamStep,
    LabReport,
    Reminder,
    SymptomDeptMap,
    User,
    VitalSign,
    ensure_patient_db,
    get_patient_session,
    get_session,
    is_db_enabled,
    resolve_exam_location,
)

# 科室主数据（code, name, description）
DEPARTMENTS = [
    ("NEUROLOGY", "神经内科", "头痛、眩晕、脑血管病"),
    ("RESPIRATORY", "呼吸内科", "咳嗽、哮喘、肺部感染"),
    ("GASTRO", "消化内科", "腹痛、腹泻、胃肠疾病"),
    ("INFECT", "感染科", "发热、感染、传染病"),
    ("DERM", "皮肤科", "皮疹、皮炎、过敏"),
    ("CARDIO", "心血管内科", "胸闷、心悸、高血压"),
    ("STOMATOLOGY", "口腔科", "口腔溃疡、牙痛、牙周病、黏膜病变"),
]

# 症状 → 科室映射（用于分诊）
SYMPTOM_MAPS = [
    ("头痛", "NEUROLOGY"),
    ("头晕", "NEUROLOGY"),
    ("咳嗽", "RESPIRATORY"),
    ("胸闷", "CARDIO"),
    ("腹痛", "GASTRO"),
    ("肝痛", "GASTRO"),
    ("肝区不适", "GASTRO"),
    ("胆囊", "GASTRO"),
    ("腹胀", "GASTRO"),
    ("恶心", "GASTRO"),
    ("呕吐", "GASTRO"),
    ("腹泻", "GASTRO"),
    ("发烧", "INFECT"),
    ("皮疹", "DERM"),
    ("口腔溃疡", "STOMATOLOGY"),
    ("牙痛", "STOMATOLOGY"),
    ("牙龈出血", "STOMATOLOGY"),
    ("牙周病", "STOMATOLOGY"),
]

# 医生（username, 姓名, 职称, 科室code）
DOCTORS = [
    ("drwang", "王医师", "主任医师", "NEUROLOGY"),
    ("drli", "李医师", "副主任医师", "RESPIRATORY"),
    ("drzhang", "张医师", "主治医师", "STOMATOLOGY"),
]

# 患者演示账号（username, 姓名, 密码）
PATIENTS = [
    ("alice", "Alice", "alice123"),
    ("bob", "Bob", "bob123"),
]

# 各患者的检验 / 生命体征示例（key=username）
LAB_REPORTS = {
    "alice": [
        ("血常规", "WBC 6.2", "3.5-9.5×10⁹/L", False, "2026-08-20"),
        ("CRP", "12 mg/L", "0-10 mg/L", True, "2026-08-20"),
        ("肝功能 ALT", "38 U/L", "7-40 U/L", False, "2026-08-20"),
        ("空腹血糖", "5.4 mmol/L", "3.9-6.1 mmol/L", False, "2026-08-20"),
    ],
    "bob": [
        ("血常规", "WBC 11.8", "3.5-9.5×10⁹/L", True, "2026-08-22"),
        ("CRP", "45 mg/L", "0-10 mg/L", True, "2026-08-22"),
        ("胸片", "右肺下叶斑片影", "—", True, "2026-08-22"),
    ],
}
VITALS = {
    "alice": [
        ("BP", "128/82", "mmHg", "2026-08-25 09:10"),
        ("HR", "72", "bpm", "2026-08-25 09:10"),
        ("TEMP", "36.7", "℃", "2026-08-25 09:10"),
        ("SpO2", "98", "%", "2026-08-25 09:10"),
    ],
    "bob": [
        ("BP", "138/88", "mmHg", "2026-08-22 14:00"),
        ("HR", "92", "bpm", "2026-08-22 14:00"),
        ("TEMP", "38.4", "℃", "2026-08-22 14:00"),
        ("SpO2", "95", "%", "2026-08-22 14:00"),
    ],
}

# 随访提醒（key=username）：content, remind_at, channel, status
REMINDERS = {
    "alice": [
        ("复查空腹血糖，评估近期饮食控制效果", "2026-08-30 09:00", "APP", "PENDING"),
        ("记录本周居家血压，早晚各一次", "2026-08-31 20:00", "SMS", "PENDING"),
    ],
    "bob": [
        ("肺部感染复查胸片，评估抗感染疗效", "2026-08-25 15:00", "APP", "PENDING"),
        ("体温每日监测两次，持续 3 天", "2026-08-23 21:00", "APP", "DONE"),
    ],
}


def seed_all() -> None:
    if not is_db_enabled():
        print("[seed] DATABASE_URL 未设置，跳过种子数据")
        return
    today = date.today().isoformat()
    with get_session() as s:
        # 科室
        dept_id = {}
        for code, name, desc in DEPARTMENTS:
            d = s.query(Department).filter(Department.code == code).first()
            if not d:
                d = Department(code=code, name=name, description=desc)
                s.add(d)
                s.flush()
            dept_id[code] = d.id

        # 症状映射
        for kw, code in SYMPTOM_MAPS:
            exists = (
                s.query(SymptomDeptMap)
                .filter(SymptomDeptMap.keyword == kw, SymptomDeptMap.dept_id == dept_id[code])
                .first()
            )
            if not exists:
                s.add(SymptomDeptMap(keyword=kw, dept_id=dept_id[code], weight=1))

        # 医生
        doc_id = {}
        for username, full_name, title, code in DOCTORS:
            d = s.query(Doctor).filter(Doctor.username == username).first()
            if not d:
                d = Doctor(
                    username=username,
                    password_hash=hash_password("dr123456"),
                    full_name=full_name,
                    title=title,
                    dept_id=dept_id[code],
                )
                s.add(d)
                s.flush()
            doc_id[username] = d.id

        # 今日排班（每个医生 AM/PM 各 20 号）
        for username in doc_id:
            for period in ("AM", "PM"):
                sch = (
                    s.query(DoctorSchedule)
                    .filter(
                        DoctorSchedule.doctor_id == doc_id[username],
                        DoctorSchedule.work_date == today,
                        DoctorSchedule.period == period,
                    )
                    .first()
                )
                if not sch:
                    s.add(
                        DoctorSchedule(
                            doctor_id=doc_id[username],
                            work_date=today,
                            period=period,
                            total_slots=20,
                            booked_slots=0,
                        )
                    )

        # 患者
        for username, full_name, pwd in PATIENTS:
            u = s.query(User).filter(User.username == username).first()
            if not u:
                s.add(
                    User(
                        username=username,
                        password_hash=hash_password(pwd),
                        full_name=full_name,
                    )
                )

        # 检验报告 / 生命体征 / 随访提醒（按患者写入各自独立库，物理隔离）
        for username in LAB_REPORTS:
            u = s.query(User).filter(User.username == username).first()
            if not u:
                continue
            ensure_patient_db(username)
            with get_patient_session(username) as ps:
                if not ps.query(LabReport).filter(LabReport.patient_id == username).first():
                    for item, result, ref, abn, rd in LAB_REPORTS[username]:
                        ps.add(
                            LabReport(
                                patient_id=username,
                                item=item,
                                result=result,
                                ref_range=ref,
                                abnormal=abn,
                                report_date=rd,
                            )
                        )
                ps.commit()
        for username in VITALS:
            u = s.query(User).filter(User.username == username).first()
            if not u:
                continue
            ensure_patient_db(username)
            with get_patient_session(username) as ps:
                if not ps.query(VitalSign).filter(VitalSign.patient_id == username).first():
                    for t, v, u_unit, m in VITALS[username]:
                        ps.add(
                            VitalSign(
                                patient_id=username,
                                type=t,
                                value=v,
                                unit=u_unit,
                                measured_at=m,
                            )
                        )
                ps.commit()
        # 随访提醒（按患者写入各自独立库，幂等）
        for username in REMINDERS:
            u = s.query(User).filter(User.username == username).first()
            if not u:
                continue
            ensure_patient_db(username)
            with get_patient_session(username) as ps:
                if not ps.query(Reminder).filter(Reminder.patient_id == username).first():
                    for content, remind_at, channel, status in REMINDERS[username]:
                        ps.add(
                            Reminder(
                                patient_id=username,
                                content=content,
                                remind_at=remind_at,
                                channel=channel,
                                status=status,
                            )
                        )
                ps.commit()
        s.commit()

        # 示例体检流程单（让 alice 患者端“体检详细流程”立即有内容可见）
        # 流程：先验血(B栋2楼) → 彩超(A栋3楼) → CT(A栋3楼)，模拟主诊医生开具
        EXAM_FLOW = [
            ("alice", "验血", "空腹采血，建议就诊前勿进食", "PENDING"),
            ("alice", "彩超", "肝胆胰脾肾彩超，需憋尿", "PENDING"),
            ("alice", "CT", "上腹部 CT 平扫，评估腹腔情况", "PENDING"),
        ]
        for username, name, note, status in EXAM_FLOW:
            u = s.query(User).filter(User.username == username).first()
            if not u:
                continue
            exists = (
                s.query(ExamStep)
                .filter(ExamStep.patient_username == username, ExamStep.step_name == name)
                .first()
            )
            if not exists:
                s.add(
                    ExamStep(
                        patient_username=username,
                        seq=len(
                            s.query(ExamStep).filter(ExamStep.patient_username == username).all()
                        ),
                        step_name=name,
                        location=resolve_exam_location(name),
                        note=note,
                        status=status,
                        created_by="drwang",
                    )
                )
        s.commit()
    print("[seed] 种子数据已就绪")


if __name__ == "__main__":
    seed_all()
