"""把主库遗留的 PHI 表数据迁入每患者私有库，恢复「PHI 物理隔离」不变量。

背景
----
初始迁移 ``21d137bd21a7`` 建表时，患者私有模型（lab_reports / vital_signs /
conversation_memory / reminders / emergency_events）还挂在共享 ``Base`` 下，
于是这 5 张表被建进了**主库**，且带 ``patient_id INTEGER`` 外键指向 users.id。

后来架构演进为「每患者一个独立 SQLite（PatientBase）」，私有表不再进主库，
但主库里的历史表从未清理 —— 导致两个后果：

1. **合规问题**：PHI 实际躺在共享主库，与项目宣称的「PHI 物理隔离」矛盾；
2. **功能问题**：主库私有表的 FK 指向 users.id，使「删除权」删用户时被外键拦下
   （``conversation_memory_patient_id_fkey`` 违反），擦除流程在 Postgres 上失败。

本脚本做**数据抢救**：把主库遗留行按 patient_id → username 映射，逐条写入
``data/<username>.db``（走 PatientBase 模型，自动获得 EncryptedText 加密）。
幂等：按业务键去重，重复执行不会产生副本。

用法::

    export DATABASE_URL=postgresql+psycopg2://...
    python scripts/migrate_phi_to_private_dbs.py            # 执行迁移
    python scripts/migrate_phi_to_private_dbs.py --dry-run  # 只报告不写入

迁移完成并核对无误后，再由 Alembic 迁移删除主库的这 5 张遗留表。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from src.db import (  # noqa: E402
    ConversationMemory,
    LabReport,
    Reminder,
    VitalSign,
    get_patient_session,
    get_session,
    is_db_enabled,
)

# 主库遗留表（旧 schema：patient_id 为整数 users.id）
LEGACY_TABLES = (
    "lab_reports",
    "vital_signs",
    "conversation_memory",
    "reminders",
    "emergency_events",
)


def _username_map() -> dict[int, str]:
    """主库 users.id → username。"""
    with get_session() as s:
        rows = s.execute(text("SELECT id, username FROM users")).all()
    return {int(r[0]): r[1] for r in rows}


def _table_exists(name: str) -> bool:
    from sqlalchemy import inspect

    return name in inspect(get_session().get_bind()).get_table_names()


def _load_legacy(table: str) -> list[dict]:
    with get_session() as s:
        rows = s.execute(text(f"SELECT * FROM {table}")).mappings().all()  # noqa: S608
    return [dict(r) for r in rows]


def migrate(dry_run: bool = False) -> dict[str, int]:
    if not is_db_enabled():
        print("DATABASE_URL 未设置，主库不可用，无需迁移。")
        return {}

    umap = _username_map()
    stats: dict[str, int] = {}

    for table in LEGACY_TABLES:
        if not _table_exists(table):
            print(f"  {table}: 主库无此表，跳过")
            continue
        rows = _load_legacy(table)
        moved = skipped = unmapped = 0

        for row in rows:
            username = umap.get(int(row["patient_id"]))
            if not username:
                unmapped += 1
                continue

            if dry_run:
                moved += 1
                continue

            with get_patient_session(username) as ps:
                if table == "lab_reports":
                    exists = (
                        ps.query(LabReport)
                        .filter(
                            LabReport.patient_id == username,
                            LabReport.item == row["item"],
                            LabReport.report_date == row["report_date"],
                        )
                        .first()
                    )
                    if exists:
                        skipped += 1
                        continue
                    ps.add(
                        LabReport(
                            patient_id=username,
                            item=row["item"],
                            result=row["result"],
                            ref_range=row.get("ref_range"),
                            abnormal=bool(row.get("abnormal")),
                            report_date=row.get("report_date"),
                        )
                    )
                elif table == "vital_signs":
                    exists = (
                        ps.query(VitalSign)
                        .filter(
                            VitalSign.patient_id == username,
                            VitalSign.type == row["type"],
                            VitalSign.measured_at == row["measured_at"],
                        )
                        .first()
                    )
                    if exists:
                        skipped += 1
                        continue
                    ps.add(
                        VitalSign(
                            patient_id=username,
                            type=row["type"],
                            value=row["value"],
                            unit=row.get("unit"),
                            measured_at=row.get("measured_at"),
                        )
                    )
                elif table == "conversation_memory":
                    exists = (
                        ps.query(ConversationMemory)
                        .filter(
                            ConversationMemory.patient_id == username,
                            ConversationMemory.thread_id == row.get("thread_id"),
                            ConversationMemory.key == row.get("key"),
                        )
                        .first()
                    )
                    if exists:
                        skipped += 1
                        continue
                    ps.add(
                        ConversationMemory(
                            patient_id=username,
                            thread_id=row.get("thread_id"),
                            key=row.get("key"),
                            value=row.get("value"),
                        )
                    )
                elif table == "reminders":
                    exists = (
                        ps.query(Reminder)
                        .filter(
                            Reminder.patient_id == username,
                            Reminder.content == row["content"],
                            Reminder.remind_at == row.get("remind_at"),
                        )
                        .first()
                    )
                    if exists:
                        skipped += 1
                        continue
                    ps.add(
                        Reminder(
                            patient_id=username,
                            content=row["content"],
                            remind_at=row.get("remind_at"),
                            channel=row.get("channel") or "APP",
                            status=row.get("status") or "PENDING",
                        )
                    )
                else:  # emergency_events 无独立私有模型字段差异，按 content 去重
                    from src.db import EmergencyEvent

                    exists = (
                        ps.query(EmergencyEvent)
                        .filter(
                            EmergencyEvent.patient_id == username,
                            EmergencyEvent.content == row["content"],
                        )
                        .first()
                    )
                    if exists:
                        skipped += 1
                        continue
                    ps.add(
                        EmergencyEvent(
                            patient_id=username,
                            content=row["content"],
                        )
                    )
                ps.commit()
            moved += 1

        stats[table] = moved
        suffix = "（dry-run）" if dry_run else ""
        print(
            f"  {table}: 主库 {len(rows)} 行 → 迁出 {moved}、已存在跳过 {skipped}、"
            f"无法映射用户 {unmapped}{suffix}"
        )

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="把主库遗留 PHI 迁入每患者私有库")
    ap.add_argument("--dry-run", action="store_true", help="只报告将要迁移的行数，不写入")
    args = ap.parse_args()

    print("PHI 抢救迁移：主库遗留私有表 → data/<username>.db")
    if args.dry_run:
        print("（dry-run 模式，不写入任何数据）")
    migrate(dry_run=args.dry_run)
    print("完成。请核对 data/ 下的私有库后，再执行删除主库遗留表的迁移。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
