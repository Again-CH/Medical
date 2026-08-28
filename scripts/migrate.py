"""数据库迁移入口：执行 Alembic 迁移到最新版本（幂等）。

schema 版本由 alembic/ 管理；init_db() 内部即调用 alembic upgrade head。
旧库（曾用 create_all 建表、无 alembic_version）首次运行会自动 stamp head，不会重复建表。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.db import init_db, is_db_enabled  # noqa: E402


def main() -> None:
    if not is_db_enabled():
        print("DATABASE_URL 未设置，跳过建表（使用内存 demo 模式）")
        return
    init_db()
    print("数据库 schema 初始化完成")


if __name__ == "__main__":
    main()
