"""PHI 留存策略与删除权（right-to-erasure）运维 CLI。

子命令：
  retention              运行留存策略（易变对话类 PHI 超期清理 / 脱敏）
      --dry-run          只统计不改写（合规审计前的安全预览）
      --days N           覆盖默认留存天数（天）

  erase                  整体抹除某患者全部可定位数据（删除权）
      --username NAME    目标患者用户名（必填）
      --confirm          必须显式传入，防止误触

运行：
  python scripts/retention.py retention --dry-run
  python scripts/retention.py retention --days 180
  python scripts/retention.py erase --username alice --confirm
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
load_dotenv()

from src.retention import apply_retention, erase_patient  # noqa: E402


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def main() -> int:
    ap = argparse.ArgumentParser(description="PHI 留存与删除权运维工具")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("retention", help="运行 PHI 留存策略")
    r.add_argument("--dry-run", action="store_true", help="只统计不改写")
    r.add_argument("--days", type=int, default=None, help="留存天数（覆盖默认）")

    e = sub.add_parser("erase", help="整体抹除某患者数据（删除权）")
    e.add_argument("--username", required=True, help="目标患者用户名")
    e.add_argument("--confirm", action="store_true", help="必须显式确认")

    args = ap.parse_args()
    if args.cmd == "retention":
        res = apply_retention(dry_run=args.dry_run, retention_days=args.days)
        print(_dump(res))
        return 0
    if args.cmd == "erase":
        if not args.confirm:
            print("❌ 删除为高风险操作，必须显式传入 --confirm")
            return 1
        res = erase_patient(args.username, actor="admin-cli")
        print(_dump(res))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
