"""审批存储 + 审计日志。

- ApprovalStore：内存/JSON 文件（离线 demo 与单测用）。
- PostgresApprovalStore：基于 SQLAlchemy ORM + 真实数据库（Postgres/SQLite 同构），
  仅在设置 DATABASE_URL 时启用；sqlalchemy/psycopg 为懒导入，内存模式下无需安装。
- get_store()：按 DATABASE_URL 自动选择后端（带缓存）。
"""

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
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


class ApprovalStore:
    """审批存储 + 审计日志。Memory / Json 文件可插拔。"""

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

    def resolve(self, approval_id, decision, actor: str = ""):
        with self._lock:
            rec = self._approvals.get(approval_id)
            if not rec:
                raise KeyError(approval_id)
            rec["status"] = "resolved"
            rec["decision"] = decision
            rec["resolved_at"] = _now()
            rec["resolved_by"] = actor
            self._audit.append({"action": "resolve", "actor": actor, **rec})
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
    """生产级审批存储：基于 SQLAlchemy ORM + 真实数据库（Postgres/SQLite 同构）。

    表结构与 db.py 的 Approval / AuditLog / PendingCall 模型一致；sqlalchemy 为懒导入，
    因此内存/文件模式下无需安装该依赖，脚手架仍可开箱即跑。

    **schema 由 Alembic 统一管理**：`src/db.py` 的 ``init_db()`` 执行 ``alembic upgrade head``
    负责建全部表（含审批/审计/待审批缓存），本类不再 ``create_all``，避免与迁移双源、保证可演进。
    使用前提：调用方必须先跑迁移（gateway lifespan / pytest conftest / eval 入口均已保证）。
    """

    def __init__(self, url: str):
        from sqlalchemy import create_engine

        # 不再 Base.metadata.create_all：schema 完全交给 Alembic（见上）。
        self.engine = create_engine(url, pool_pre_ping=True, future=True)

    def create(self, thread_id, payload):
        from sqlalchemy.orm import sessionmaker

        from .db import Approval, AuditLog

        Session = sessionmaker(bind=self.engine)
        aid = "APR-" + uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc)
        with Session() as s:
            s.add(
                Approval(
                    id=aid,
                    thread_id=thread_id,
                    action=(payload or {}).get("action"),
                    payload=json.dumps(payload, ensure_ascii=False),
                    status="pending",
                    created_at=now,
                )
            )
            s.add(
                AuditLog(
                    # actor 必须是**真实申请人**（患者 username），
                    # 用 thread_id 顶替会让审计无法回答「谁发起的」
                    actor=(payload or {}).get("requester") or thread_id,
                    action="approval_create",
                    detail=json.dumps(payload, ensure_ascii=False),
                )
            )
            s.commit()
        return aid

    def resolve(self, approval_id, decision, actor: str = ""):
        """审批落库：记录**审批人身份**（resolved_by / AuditLog.actor）。

        敏感操作（医保结算 / 转诊 / 120 呼叫）必须可追责：
        事后要能回答「哪一单、被谁、以什么决定批准」。
        """
        from sqlalchemy.orm import sessionmaker

        from .db import Approval, AuditLog

        Session = sessionmaker(bind=self.engine)
        now = datetime.now(timezone.utc)
        with Session() as s:
            ap = s.get(Approval, approval_id)
            if ap is None:
                raise KeyError(approval_id)
            ap.status = "resolved"
            ap.decision = json.dumps(decision, ensure_ascii=False)
            ap.resolved_at = now
            ap.resolved_by = actor
            s.add(
                AuditLog(
                    actor=actor or "unknown",
                    action="approval_resolve",
                    detail=json.dumps(
                        {"approval_id": approval_id, "decision": decision}, ensure_ascii=False
                    ),
                )
            )
            s.commit()
            return {
                "id": ap.id,
                "approval_id": ap.id,
                "thread_id": ap.thread_id,
                "payload": json.loads(ap.payload),
                "status": ap.status,
                "created_at": ap.created_at,
                "decision": decision,
                "resolved_by": actor,
            }

    def pending(self):
        from sqlalchemy.orm import sessionmaker

        from .db import Approval

        Session = sessionmaker(bind=self.engine)
        with Session() as s:
            rows = s.query(Approval).filter(Approval.status == "pending").all()
            return [
                {
                    "id": r.id,
                    "approval_id": r.id,
                    "thread_id": r.thread_id,
                    "payload": json.loads(r.payload),
                    "status": r.status,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    def audit_log(self):
        from sqlalchemy import select
        from sqlalchemy.orm import sessionmaker

        from .db import AuditLog

        Session = sessionmaker(bind=self.engine)
        with Session() as s:
            rows = s.execute(select(AuditLog).order_by(AuditLog.created_at)).scalars().all()
            return _jsonable(
                [
                    {
                        "id": r.id,
                        "actor": r.actor,
                        "action": r.action,
                        "detail": r.detail,
                        "created_at": r.created_at,
                    }
                    for r in rows
                ]
            )

    def get(self, approval_id):
        from sqlalchemy.orm import sessionmaker

        from .db import Approval

        Session = sessionmaker(bind=self.engine)
        with Session() as s:
            ap = s.get(Approval, approval_id)
            if ap is None:
                return None
            return {
                "id": ap.id,
                "approval_id": ap.id,
                "thread_id": ap.thread_id,
                "payload": json.loads(ap.payload),
                "status": ap.status,
                "created_at": ap.created_at,
                "decision": json.loads(ap.decision) if ap.decision else None,
                "resolved_by": ap.resolved_by,
            }


_store_cache: dict = {}


def get_store():
    """按 DATABASE_URL 自动选择后端（带缓存）。"""
    url = os.getenv("DATABASE_URL")
    if url:
        if url not in _store_cache:
            try:
                _store_cache[url] = PostgresApprovalStore(url)
            except Exception as e:  # 配置了但连不上：降级内存（生产应接告警）
                print(f"[store] DATABASE_URL 已配置但初始化失败，降级为内存存储: {e}")
                return ApprovalStore()
        return _store_cache[url]
    if "memory" not in _store_cache:
        _store_cache["memory"] = ApprovalStore()
    return _store_cache["memory"]
