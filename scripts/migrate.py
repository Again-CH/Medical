"""数据库迁移入口：创建全部表（幂等）。

生产环境建议替换为 Alembic 做带版本的迁移；本地/CI 用 create_all 已足够跑通。
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
