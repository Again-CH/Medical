import json
import os
import threading
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

APPROVAL_STORE = os.getenv("APPROVAL_STORE", ":memory:")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _jsonable(obj):
    """把 Postgres 返回的 datetime 等对象转成可 JSON 序列化的形式。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


class ApprovalStore:
    """审批存储 + 审计日志。Memory / Json 文件可插拔（生产换 Postgres）。"""

    def __init__(self, path: str = APPROVAL_STORE):
        self.path = path
        self._lock = threading.Lock()
        self._approvals: dict = {}
        self._audit: list = []
        if path not in (":memory:", None) and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._approvals = data.get("approvals", {})
            self._audit = data.get("audit", [])

    def create(self, thread_id, payload):
        aid = "APR-" + uuid.uuid4().hex[:8]
        rec = {
            "approval_id": aid,
            "thread_id": thread_id,
            "payload": payload,
            "status": "pending",
            "created_at": _now(),
        }
        with self._lock:
            self._approvals[aid] = rec
            self._audit.append({"action": "create", **rec})
            self._persist()
        return aid

    def resolve(self, approval_id, decision):
        with self._lock:
            rec = self._approvals.get(approval_id)
            if not rec:
                raise KeyError(approval_id)
            rec["status"] = "resolved"
            rec["decision"] = decision
            rec["resolved_at"] = _now()
            self._audit.append({"action": "resolve", **rec})
            self._persist()
        return rec

    def pending(self):
        with self._lock:
            return [r for r in self._approvals.values() if r["status"] == "pending"]

    def audit_log(self):
        with self._lock:
            return list(self._audit)

    def get(self, approval_id):
        with self._lock:
            return self._approvals.get(approval_id)

    def _persist(self):
        if self.path in (":memory:", None):
            return
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {"approvals": self._approvals, "audit": self._audit},
                f,
                ensure_ascii=False,
                indent=2,
            )


class PostgresApprovalStore:
    """生产级审批存储：基于 SQLAlchemy Core + PostgreSQL。

    仅在设置了 DATABASE_URL 时启用；sqlalchemy/psycopg 为 lazy import，
    因此内存/文件模式下无需安装这两个依赖，脚手架仍可开箱即跑。
    """

    def __init__(self, url: str):
        from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, create_engine

        self.engine = create_engine(url, future=True)
        self.metadata = MetaData()
        self.approvals = Table(
            "approvals",
            self.metadata,
            Column("id", String(64), primary_key=True),
            Column("thread_id", String(128)),
            Column("payload", JSON),
            Column("status", String(16)),
            Column("created_at", DateTime),
            Column("decision", JSON),
            Column("resolved_at", DateTime),
        )
        self.audit = Table(
            "audit_log",
            self.metadata,
            Column("id", String(64), primary_key=True),
            Column("action", String(16)),
            Column("approval_id", String(64)),
            Column("thread_id", String(128)),
            Column("payload", JSON),
            Column("status", String(16)),
            Column("decision", JSON),
            Column("created_at", DateTime),
        )
        self.metadata.create_all(self.engine)

    def create(self, thread_id, payload):
        aid = "APR-" + uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc)
        rec = {
            "id": aid,
            "thread_id": thread_id,
            "payload": payload,
            "status": "pending",
            "created_at": now,
        }
        with self.engine.begin() as conn:
            conn.execute(self.approvals.insert().values(**rec))
            conn.execute(
                self.audit.insert().values(
                    id=uuid.uuid4().hex,
                    action="create",
                    approval_id=aid,
                    thread_id=thread_id,
                    payload=payload,
                    status="pending",
                    created_at=now,
                )
            )
        return aid

    def resolve(self, approval_id, decision):
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(
                self.approvals.update()
                .where(self.approvals.c.id == approval_id)
                .values(
                    status="resolved",
                    decision=decision,
                    resolved_at=now,
                )
            )
            row = (
                conn.execute(self.approvals.select().where(self.approvals.c.id == approval_id))
                .mappings()
                .first()
            )
            conn.execute(
                self.audit.insert().values(
                    id=uuid.uuid4().hex,
                    action="resolve",
                    approval_id=approval_id,
                    thread_id=row["thread_id"],
                    payload=row["payload"],
                    status="resolved",
                    decision=decision,
                    created_at=now,
                )
            )
            return _jsonable(dict(row))

    def pending(self):
        with self.engine.connect() as conn:
            rows = (
                conn.execute(self.approvals.select().where(self.approvals.c.status == "pending"))
                .mappings()
                .all()
            )
        return _jsonable([dict(r) for r in rows])

    def audit_log(self):
        with self.engine.connect() as conn:
            rows = (
                conn.execute(self.audit.select().order_by(self.audit.c.created_at)).mappings().all()
            )
        return _jsonable([dict(r) for r in rows])

    def get(self, approval_id):
        with self.engine.connect() as conn:
            row = (
                conn.execute(self.approvals.select().where(self.approvals.c.id == approval_id))
                .mappings()
                .first()
            )
        return _jsonable(dict(row)) if row else None


def get_store():
    url = os.getenv("DATABASE_URL")
    if url:
        try:
            return PostgresApprovalStore(url)
        except Exception as e:  # 配置了但连不上：降级内存（生产应接告警）
            print(f"[store] DATABASE_URL 已配置但初始化失败，降级为内存存储: {e}")
            return ApprovalStore()
    return ApprovalStore()


STORE = get_store()
