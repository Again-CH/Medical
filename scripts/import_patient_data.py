"""批量导入患者档案数据（检验结果 / 生命体征 / 病例小结）到其私有库。

与网关 ``POST /api/import/patient-data`` 共用 ``src.integrations.bulk_import_patient`` 落库逻辑，
区别是走离线脚本（适合历史数据迁移、批量灌库、CI 验证）。

支持两种文件格式：
  1) JSON：结构与 API 请求体一致
     {"patient":"alice",
      "lab_reports":[{"item":"血糖","result":"8.5","ref_range":"3.9-6.1","abnormal":true}],
      "vital_signs":[{"type":"血压","value":"150/95","unit":"mmHg"}],
      "case_summaries":[{"text":"2 型糖尿病史 5 年","category":"既往史"}]}
  2) CSV：必须含 ``kind`` 列（lab/vital/case），其余列按类型取对应字段
     kind,item,result,ref_range,abnormal,type,value,unit,text,category
     lab,血糖,8.5,3.9-6.1,true,,,,,
     vital,血压,,,,,150/95,mmHg,,
     case,,,,,,,,,2型糖尿病史 5 年,既往史

运行：
    python scripts/import_patient_data.py --patient alice --file data.json
    python scripts/import_patient_data.py --patient alice --file data.csv
    # 若 JSON/CSV 内已含 patient 字段，可省略 --patient
"""

import argparse
import csv
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()
# 真实 Postgres（已 migrate+seed），让数据真正落库
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://mac@localhost:5432/medical_agent")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "是")


def load_json(path: str, cli_patient: str | None):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(
            "✗ JSON 顶层应为对象，含 patient / lab_reports / vital_signs / case_summaries"
        )
    patient = cli_patient or data.get("patient")
    if not patient:
        raise SystemExit("✗ 未指定患者：JSON 内需含 patient 字段或传入 --patient")
    return (
        patient,
        data.get("lab_reports", []),
        data.get("vital_signs", []),
        data.get("case_summaries", []),
    )


def load_csv(path: str, cli_patient: str | None):
    if not cli_patient:
        raise SystemExit("✗ CSV 模式必须显式传入 --patient")
    lab, vital, case = [], [], []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "kind" not in (reader.fieldnames or []):
            raise SystemExit("✗ CSV 必须含 kind 列（lab/vital/case）")
        for row in reader:
            kind = (row.get("kind") or "").strip().lower()
            if kind == "lab":
                if not row.get("item") or not row.get("result"):
                    continue
                lab.append(
                    {
                        "item": row["item"],
                        "result": row["result"],
                        "ref_range": row.get("ref_range", ""),
                        "abnormal": _truthy(row.get("abnormal", "")),
                        "report_date": row.get("report_date", ""),
                    }
                )
            elif kind == "vital":
                if not row.get("type") or not row.get("value"):
                    continue
                vital.append(
                    {
                        "type": row["type"],
                        "value": row["value"],
                        "unit": row.get("unit", ""),
                        "measured_at": row.get("measured_at", ""),
                    }
                )
            elif kind == "case":
                text = row.get("text") or ""
                if not text:
                    continue
                case.append({"text": text, "category": row.get("category", "general")})
    return cli_patient, lab, vital, case


def main():
    p = argparse.ArgumentParser(description="批量导入患者档案数据到私有库")
    p.add_argument("--patient", help="患者用户名（JSON/CSV 内已含时可省略）")
    p.add_argument("--file", required=True, help="JSON 或 CSV 文件路径")
    args = p.parse_args()

    path = args.file
    if not os.path.exists(path):
        raise SystemExit(f"✗ 文件不存在：{path}")
    if path.lower().endswith(".csv"):
        patient, lab, vital, case = load_csv(path, args.patient)
    else:
        patient, lab, vital, case = load_json(path, args.patient)

    if not (lab or vital or case):
        raise SystemExit("✗ 没有可导入的数据（lab_reports/vital_signs/case_summaries 均为空）")

    from src.integrations import bulk_import_patient

    try:
        counts = bulk_import_patient(patient, lab, vital, case)
    except (ValueError, RuntimeError) as e:
        raise SystemExit(f"✗ 导入失败：{e}") from e

    print(f"✓ 已为 {patient} 导入：")
    print(f"    检验报告   : {counts['lab_reports']} 条")
    print(f"    生命体征   : {counts['vital_signs']} 条")
    print(f"    病例小结   : {counts['case_summaries']} 条")


if __name__ == "__main__":
    main()
