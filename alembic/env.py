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

from src.db import Base, PatientBase  # noqa: E402

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

# 每患者私有库（data/<username>.db）的表：由 PatientBase 定义、运行时按患者建库，
# 永远不进共享主库，故排除在 Alembic 之外，否则 autogenerate 会误报「表被删除」。
_PATIENT_TABLES = set(PatientBase.metadata.tables.keys())

# pgvector 专属表：仅 Postgres 建（见 f6a7b8c9d0e1 的方言适配），sqlite 下不存在。
_PG_ONLY_TABLES = {"knowledge_documents"}

# pgvector 的 HNSW 功能索引：由迁移里的原生 SQL 创建（USING hnsw ... vector_cosine_ops），
# 无法用与方言无关的 ORM Index 表达，故不参与 autogenerate 比对，否则会被误判为「多余索引」。
_PG_ONLY_INDEXES = {"ix_knowledge_documents_embedding"}


def _excluded_tables(dialect_name: str) -> set[str]:
    excluded = set(_PATIENT_TABLES)
    if dialect_name != "postgresql":
        excluded |= _PG_ONLY_TABLES
    return excluded


def _make_include_object(dialect_name: str):
    excluded = _excluded_tables(dialect_name)

    def include_object(object, name, type_, reflected, compare_to):
        """autogenerate / check 时排除不该由主库迁移管理的表。

        三类表必须排除，否则 ``alembic check`` 会误报「应删除这些表」，
        或 autogenerate 生成危险的 drop 语句：

        1. ``checkpoint_*``：LangGraph AsyncPostgresSaver 自行 setup，
           不属于 src.db.Base.metadata；
        2. 患者私有库表（lab_reports / vital_signs / reminders /
           conversation_memory / emergency_events）：挂在 PatientBase 下，
           每患者一个独立 SQLite 文件，物理隔离 PHI，永不进共享主库；
        3. ``knowledge_documents``：依赖 pgvector 扩展，非 Postgres 方言下
           迁移会跳过建表。
        """
        if type_ == "table" and name:
            if name.startswith("checkpoint"):
                return False
            return name not in excluded
        if type_ == "index" and name in _PG_ONLY_INDEXES:
            return False
        return True

    return include_object


def run_migrations_offline() -> None:
    """离线模式：只产出 SQL 脚本，不连库。"""
    url = DATABASE_URL or config.get_main_option("sqlalchemy.url")
    dialect = url.split("+")[0].split(":")[0]
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_make_include_object(dialect),
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
            include_object=_make_include_object(connection.dialect.name),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
