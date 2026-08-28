"""审批存储单测：覆盖内存存储 + SQLAlchemy Core 实现（含 Postgres 集成）。

- test_memory_store_*：默认内存/JSON 存储往返。
- test_postgres_store_sqlite：用 sqlite 文件库验证 SQLAlchemy Core 的建表/读写/审计逻辑，
  保证 SQL 可移植，本地无需真实 Postgres 也能覆盖核心路径。
- test_postgres_store_real：仅当 CI 设置 DATABASE_URL（PostgreSQL 服务容器）时运行，
  验证生产级持久化。
"""

import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.store import ApprovalStore, PostgresApprovalStore  # noqa: E402


def test_memory_store_roundtrip():
    s = ApprovalStore(path=":memory:")
    payload = {"action": "lock_and_settle", "intent": "booking", "tools": ["lock_appointment"]}
    aid = s.create("thread-1", payload)
    assert any(r["approval_id"] == aid for r in s.pending())
    rec = s.get(aid)
    assert rec["status"] == "pending"
    assert rec["payload"] == payload

    s.resolve(aid, {"approved": True})
    assert s.get(aid)["status"] == "resolved"
    assert s.pending() == []  # 已审批的不在待审列表

    audit = s.audit_log()
    actions = [a["action"] for a in audit]
    assert "create" in actions and "resolve" in actions


def test_memory_store_reject():
    s = ApprovalStore(path=":memory:")
    aid = s.create("thread-2", {"action": "x"})
    s.resolve(aid, {"approved": False})
    assert s.get(aid)["decision"] == {"approved": False}


def _sqlite_url():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return "sqlite:///" + path, path


def _migrate(url):
    """用 Alembic 在给定连接串上建全部表（含审批/审计），替代原先 store 内的 create_all。"""
    import src.db as db

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    db._engines.clear()
    db._sessions.clear()
    try:
        db.migrate_db()
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        db._engines.clear()
        db._sessions.clear()


def test_postgres_store_sqlite():
    """用 sqlite 验证 PostgresApprovalStore 的 SQLAlchemy Core 逻辑可移植。

    schema 现由 Alembic 管理（不再由 store 内 create_all），故先跑迁移建表。
    """
    url, path = _sqlite_url()
    _migrate(url)
    try:
        s = PostgresApprovalStore(url)
        payload = {"action": "emergency_handoff", "intent": "emergency", "tools": ["call_120"]}
        aid = s.create("thread-pg", payload)
        assert aid in [r["id"] for r in s.pending()]
        rec = s.get(aid)
        assert rec["status"] == "pending"
        assert rec["payload"]["action"] == "emergency_handoff"

        s.resolve(aid, {"approved": True})
        assert s.get(aid)["status"] == "resolved"
        assert s.pending() == []

        audit = s.audit_log()
        actions = [a["action"] for a in audit]
        assert "create" in actions and "resolve" in actions
    finally:
        if os.path.exists(path):
            os.remove(path)


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="需设置 DATABASE_URL（CI 中由 PostgreSQL 服务容器提供）"
)
def test_postgres_store_real():
    _migrate(os.getenv("DATABASE_URL"))
    s = PostgresApprovalStore(os.getenv("DATABASE_URL"))
    payload = {"action": "lock_and_settle", "intent": "booking", "tools": ["lock_appointment"]}
    aid = s.create("thread-real", payload)
    assert aid in [r["id"] for r in s.pending()]
    s.resolve(aid, {"approved": True})
    assert s.get(aid)["status"] == "resolved"
