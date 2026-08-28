"""Alembic 环境：把 src/db.Base.metadata 作为迁移的目标元数据，连接串来自环境变量。

支持两种用法：
1. CLI：``DATABASE_URL=... .venv/bin/alembic upgrade head``
2. 程序内：``from src.db import migrate_db``（gateway 启动 / 测试 fixture 自动调用）

无论哪种，DATABASE_URL 都从进程环境变量读取，schema 定义统一来自 src/db.py 的 ORM 模型，
因此「模型即真相来源（single source of truth）」，未来加表只需改模型 + 生成新迁移。
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# 让 `src` 包可导入（alembic 可能运行在独立子进程中）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.db import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 动态注入 DATABASE_URL（来自环境变量，避免硬编码到 ini）
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL:
    # 统一把 +psycopg2 同步驱动用作迁移引擎（sqlite / postgres 均兼容）
    config.set_main_option("sqlalchemy.url", DATABASE_URL)

# 目标元数据：所有 ORM 模型都挂在 src.db.Base 下
target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    """autogenerate / check 时排除非本项目的表。

    LangGraph 的 checkpoint_* 表由 AsyncPostgresSaver.setup() 自行管理，
    不属于 src.db.Base.metadata；若不排除，alembic check 会误报「应删除这些表」，
    且 autogenerate 可能错误地生成 drop 语句。这里统一忽略，保持 Alembic 只管业务表。
    """
    if type_ == "table" and name and name.startswith("checkpoint"):
        return False
    return True


def run_migrations_offline() -> None:
    """离线模式：只产出 SQL 脚本，不连库。"""
    url = DATABASE_URL or config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库并执行迁移。"""
    connectable = config.attributes.get("connection", None)
    if connectable is None:
        url = DATABASE_URL or config.get_main_option("sqlalchemy.url")
        connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
