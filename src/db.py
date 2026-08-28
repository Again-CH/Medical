"""数据库层：SQLAlchemy 2.0 ORM。

设计原则（生产化核心）：
- 代码只认一个连接串 ``DATABASE_URL``；不设置 → 内存 demo 模式（get_hub 返回 MemoryHub）。
- 设置成 ``postgresql+psycopg2://...`` → 真实持久化（生产）。
- 本地开发可用 ``sqlite:///./dev.db`` 跑通同构 SQL，无需起服务即可验证。
- 所有模型集中在 Base 下，schema 版本由 Alembic 管理（``alembic/`` 目录 + ``init_db()``
  调用 ``alembic upgrade head``）；离线/单测可走 sqlite 同构 SQL，生产走 postgres。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    mapped_column,
    sessionmaker,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------- 模型 ----------
class User(Base):
    __tablename__ = "users"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash = mapped_column(String(160), nullable=False)
    full_name = mapped_column(String(128))
    phone = mapped_column(String(32))
    created_at = mapped_column(DateTime, default=utcnow)


class Doctor(Base):
    __tablename__ = "doctors"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash = mapped_column(String(160), nullable=False)
    full_name = mapped_column(String(128), nullable=False)
    title = mapped_column(String(64))  # 职称
    dept_id = mapped_column(Integer, ForeignKey("departments.id"))


class Department(Base):
    __tablename__ = "departments"
    id = mapped_column(Integer, primary_key=True)
    code = mapped_column(String(32), unique=True, nullable=False)
    name = mapped_column(String(64), nullable=False)
    description = mapped_column(Text)


class SymptomDeptMap(Base):
    __tablename__ = "symptom_dept_map"
    id = mapped_column(Integer, primary_key=True)
    keyword = mapped_column(String(64), nullable=False, index=True)
    dept_id = mapped_column(Integer, ForeignKey("departments.id"), nullable=False)
    weight = mapped_column(Integer, default=1)


class DoctorSchedule(Base):
    __tablename__ = "doctor_schedules"
    id = mapped_column(Integer, primary_key=True)
    doctor_id = mapped_column(Integer, ForeignKey("doctors.id"), nullable=False)
    work_date = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    period = mapped_column(String(8), nullable=False)  # AM / PM
    total_slots = mapped_column(Integer, default=20)
    booked_slots = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint("doctor_id", "work_date", "period", name="uq_doc_date_period"),
    )


class Appointment(Base):
    __tablename__ = "appointments"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    doctor_id = mapped_column(Integer, ForeignKey("doctors.id"), nullable=False)
    schedule_id = mapped_column(Integer, ForeignKey("doctor_schedules.id"), nullable=False)
    work_date = mapped_column(String(10))
    period = mapped_column(String(8))
    slot_index = mapped_column(Integer)
    status = mapped_column(String(16), default="LOCKED")
    medicare_settled = mapped_column(Boolean, default=False)
    created_at = mapped_column(DateTime, default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id = mapped_column(String(64), primary_key=True)
    thread_id = mapped_column(String(128), index=True)
    action = mapped_column(String(64))
    payload = mapped_column(Text)  # JSON 文本
    status = mapped_column(String(16), default="PENDING")
    created_at = mapped_column(DateTime, default=utcnow)
    resolved_at = mapped_column(DateTime)
    resolved_by = mapped_column(String(64))
    decision = mapped_column(Text)  # JSON 文本


class ConversationMemory(Base):
    __tablename__ = "conversation_memory"
    id = mapped_column(Integer, primary_key=True)
    thread_id = mapped_column(String(64), index=True)
    patient_id = mapped_column(Integer, ForeignKey("users.id"))
    key = mapped_column(String(64))
    value = mapped_column(Text)
    created_at = mapped_column(DateTime, default=utcnow)


class LabReport(Base):
    __tablename__ = "lab_reports"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    item = mapped_column(String(64), nullable=False)
    result = mapped_column(String(64))
    ref_range = mapped_column(String(64))
    abnormal = mapped_column(Boolean, default=False)
    report_date = mapped_column(String(10))


class VitalSign(Base):
    __tablename__ = "vital_signs"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    type = mapped_column(String(32), nullable=False)
    value = mapped_column(String(32))
    unit = mapped_column(String(16))
    measured_at = mapped_column(String(19))


class Reminder(Base):
    __tablename__ = "reminders"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    content = mapped_column(Text, nullable=False)
    remind_at = mapped_column(String(19))
    channel = mapped_column(String(16), default="APP")
    status = mapped_column(String(16), default="PENDING")
    created_at = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = mapped_column(Integer, primary_key=True)
    actor = mapped_column(String(64))
    action = mapped_column(String(64))
    detail = mapped_column(Text)
    created_at = mapped_column(DateTime, default=utcnow)


class EmergencyEvent(Base):
    __tablename__ = "emergency_events"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(Integer, ForeignKey("users.id"))
    content = mapped_column(Text)
    created_at = mapped_column(DateTime, default=utcnow)


# ---------- 引擎 / 会话（懒加载，导入不建连） ----------
_engines: dict[str, "object"] = {}
_sessions: dict[str, sessionmaker] = {}


def _connect_args(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def get_engine():
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    if url not in _engines:
        _engines[url] = create_engine(url, pool_pre_ping=True, connect_args=_connect_args(url))
    return _engines[url]


def is_db_enabled() -> bool:
    return get_engine() is not None


def get_session() -> Session:
    """返回一个会话上下文管理器（with 使用）。未配置 DATABASE_URL 时抛错。"""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未设置，无法使用数据库（请设置以启用真实持久化，离线 demo 走 MemoryHub）"
        )
    if url not in _sessions:
        _sessions[url] = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _sessions[url]()


def _alembic_config() -> "object":
    """构造指向仓库内 alembic.ini 的 Alembic Config。

    真正的连接串由 alembic/env.py 从环境变量 DATABASE_URL 读取，
    因此同一份迁移既能跑 sqlite（本地/测试）也能跑 postgres（生产）。
    """
    from alembic.config import Config

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(repo_root, "alembic.ini"))
    return cfg


def migrate_db() -> None:
    """执行 Alembic 迁移到最新版本（生产级 schema 版本管理）。

    取代原先的 ``Base.metadata.create_all``：现在 schema 有版本号、可回滚、可演进。
    兼容两类库：
    - 全新库：``alembic upgrade head`` 按迁移文件建全部表。
    - 旧库（曾用 create_all 建表、但无 alembic_version 登记）：首次 upgrade 会因
      “表已存在” 报错，此时自动 ``stamp head`` 标记为最新版本（schema 与初始迁移一致），
      后续启动即变为幂等的无操作。
    """
    eng = get_engine()
    if eng is None:
        raise RuntimeError("DATABASE_URL 未设置，无法迁移")

    from alembic import command

    cfg = _alembic_config()
    try:
        command.upgrade(cfg, "head")
    except Exception as e:  # noqa: BLE001
        # 旧库已用 create_all 建过表但未登记版本 → 直接 stamp head，避免重复建表报错
        err = str(e)
        if "already exists" in err or type(e).__name__ == "ProgrammingError":
            try:
                command.stamp(cfg, "head")
                return
            except Exception:  # noqa: BLE001
                pass
        raise


def init_db() -> None:
    """建表（幂等）：执行 Alembic 迁移到最新版本。"""
    migrate_db()


def drop_db() -> None:
    eng = get_engine()
    if eng is not None:
        Base.metadata.drop_all(eng)


class PendingCall(Base):
    """待人工审批的敏感工具调用缓存（HITL 持久化）。

    LangGraph 的 interrupt() 在 resume 时会把节点从头重跑；若重跑时真实 LLM
    不再生成敏感工具调用，会导致「已批准却没执行」。故把待审批的 tool_calls
    落地到本表，resume 重跑直接读取执行，保证落库确定性，且跨进程/重启不丢。
    """

    __tablename__ = "pending_calls"
    cache_key = mapped_column(String(128), primary_key=True)
    calls = mapped_column(Text, nullable=False)  # 敏感 tool_calls 的 JSON
    created_at = mapped_column(DateTime, default=utcnow)


def set_pending(cache_key: str, calls: list) -> None:
    """持久化待审批的敏感工具调用（覆盖写）。"""
    from sqlalchemy import delete

    eng = get_engine()
    if eng is None:
        raise RuntimeError("DATABASE_URL 未设置，无法持久化 pending")
    with Session(eng) as s:
        s.execute(delete(PendingCall).where(PendingCall.cache_key == cache_key))
        s.add(PendingCall(cache_key=cache_key, calls=json.dumps(calls, ensure_ascii=False)))
        s.commit()


def pop_pending(cache_key: str):
    """取出并删除待审批调用；不存在返回 None。"""
    eng = get_engine()
    if eng is None:
        return None
    with Session(eng) as s:
        row = s.get(PendingCall, cache_key)
        if row is None:
            return None
        calls = json.loads(row.calls)
        s.delete(row)
        s.commit()
        return calls


def clear_pending() -> None:
    eng = get_engine()
    if eng is None:
        return
    from sqlalchemy import delete

    with Session(eng) as s:
        s.execute(delete(PendingCall))
        s.commit()
