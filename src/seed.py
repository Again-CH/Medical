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
    LabReport,
    SymptomDeptMap,
    User,
    VitalSign,
    get_session,
    is_db_enabled,
)

# 科室主数据（code, name, description）
DEPARTMENTS = [
    ("NEUROLOGY", "神经内科", "头痛、眩晕、脑血管病"),
    ("RESPIRATORY", "呼吸内科", "咳嗽、哮喘、肺部感染"),
    ("GASTRO", "消化内科", "腹痛、腹泻、胃肠疾病"),
    ("INFECT", "感染科", "发热、感染、传染病"),
    ("DERM", "皮肤科", "皮疹、皮炎、过敏"),
    ("CARDIO", "心血管内科", "胸闷、心悸、高血压"),
]

# 症状 → 科室映射（用于分诊）
SYMPTOM_MAPS = [
    ("头痛", "NEUROLOGY"),
    ("头晕", "NEUROLOGY"),
    ("咳嗽", "RESPIRATORY"),
    ("胸闷", "CARDIO"),
    ("腹痛", "GASTRO"),
    ("发烧", "INFECT"),
    ("皮疹", "DERM"),
]

# 医生（username, 姓名, 职称, 科室code）
DOCTORS = [
    ("drwang", "王医师", "主任医师", "NEUROLOGY"),
    ("drli", "李医师", "副主任医师", "RESPIRATORY"),
]

# 患者演示账号（username, 姓名, 密码）
PATIENTS = [
    ("alice", "Alice", "alice123"),
]

# alice 的检验 / 生命体征示例
LAB_REPORTS = [
    ("alice", "血常规", "WBC 6.2", "3.5-9.5×10⁹/L", False, "2026-08-20"),
    ("alice", "CRP", "12 mg/L", "0-10 mg/L", True, "2026-08-20"),
]
VITALS = [
    ("alice", "BP", "128/82", "mmHg", "2026-08-25 09:10"),
    ("alice", "HR", "72", "bpm", "2026-08-25 09:10"),
]


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

        # 检验报告 / 生命体征
        alice = s.query(User).filter(User.username == "alice").first()
        if alice:
            if not s.query(LabReport).filter(LabReport.patient_id == alice.id).first():
                for _pid, item, result, ref, abn, rd in LAB_REPORTS:
                    s.add(
                        LabReport(
                            patient_id=alice.id,
                            item=item,
                            result=result,
                            ref_range=ref,
                            abnormal=abn,
                            report_date=rd,
                        )
                    )
            if not s.query(VitalSign).filter(VitalSign.patient_id == alice.id).first():
                for _pid, t, v, u, m in VITALS:
                    s.add(
                        VitalSign(
                            patient_id=alice.id,
                            type=t,
                            value=v,
                            unit=u,
                            measured_at=m,
                        )
                    )
        s.commit()
    print("[seed] 种子数据已就绪")


if __name__ == "__main__":
    seed_all()
