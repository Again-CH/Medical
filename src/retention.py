"""PHI 留存策略与删除权（数据合规核心）。

两条互补的合规能力，对应法规对患者健康信息的不同要求：

1. **留存最小化（retention / data minimization）**
   易变的对话类 PHI（自由文本随访笔记、病例小结、紧急事件、对话日志）设留存上限，
   超期即抹除或脱敏；临床记录（检验报告 / 生命体征 / 预约 / 检查单）按更长法定留存期
   保留，不参与自动清理，仅随「删除权」整体抹除。
   - 对话日志（chat_logs）超期**脱敏**而非删除：保留「发生了多少次对话、耗时多少」
     等运营/合规指标，仅擦除其中的患者输入与系统输出（PHI）。
   - 自由文本笔记 / 紧急事件超期**直接删除**行。

2. **删除权（right-to-erasure / 被遗忘权）**
   患者或管理员可触发整体抹除：删除其独立 SQLite 私有库（data/<username>.db）、主库中
   一切可定位到该患者的记录（账号、令牌、预约、检查单、审批、对话、知情同意），并对
   含该标识符的历史审计日志做**假名化**（用盐哈希替换原始用户名，保留可追溯性但不留存
   直接标识符）。最后写入一条「删除权执行」审计记录（同样只存假名令牌）。

设计要点
--------
- 与加密（src/phi.py）解耦：本模块只负责「保留什么、删什么」，字段是否加密由列类型决定。
- 所有销毁动作可追溯：删除权执行本身留痕（合规审计），且假名令牌让「删除事件」仍能和
  历史审计关联，而不暴露患者是谁。
- 离线 / 无 DB 模式下所有函数安全返回空结果（不报错），保证 demo 与测试确定性。
"""

from __future__ import annotations

import hashlib
import os
import re as _re
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from .db import (
    Appointment,
    Approval,
    AuditLog,
    ChatLog,
    ConsentRecord,
    ConversationMemory,
    EmergencyEvent,
    RefreshToken,
    User,
    get_patient_engine,
    get_session,
    is_db_enabled,
)
from .metrics import PHI_ERASURES, PHI_PURGED

_USER_RE = _re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff(days: int) -> datetime:
    return _utcnow() - timedelta(days=days)


def _data_dir() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "data")


def _patient_db_path(username: str) -> str:
    if not _USER_RE.match(username or ""):
        raise ValueError(f"非法用户名：{username!r}")
    return os.path.join(_data_dir(), f"{username}.db")


def pseudonymize(username: str) -> str:
    """把直接标识符变成不可逆的假名令牌（审计轨迹用，避免留存明文用户名）。

    用 JWT_SECRET 作盐：无密钥环境退化为纯 SHA-256（仍不可逆，只是少了盐）。
    """
    salt = (config.JWT_SECRET or "medical-agent")[:32]
    return "anon:" + hashlib.sha256((salt + "|" + username).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 留存策略
# ---------------------------------------------------------------------------
def apply_retention(dry_run: bool = False, retention_days: Optional[int] = None) -> dict:
    """执行 PHI 留存策略：易变对话类 PHI 超期清理 / 脱敏。

    返回各作用域的处理计数，便于运维核对与告警。
    ``dry_run=True`` 只统计不改动（合规审计前的安全预览）。
    """
    if not is_db_enabled():
        return {"dry_run": dry_run, "enabled_db": False, "scopes": {}}

    days = retention_days if retention_days is not None else config.PHI_RETENTION_DAYS
    cut = _cutoff(days)
    counts: dict[str, int] = {}

    # ---- 主库：对话日志脱敏（保留运营/合规指标，擦除 PHI） ----
    with get_session() as s:
        old_chats = s.query(ChatLog).filter(ChatLog.created_at < cut).all()
        n_chat = len(old_chats)
        if not dry_run:
            for row in old_chats:
                row.input_text = "[redacted by retention policy]"
                row.output_text = "[redacted by retention policy]"
            s.commit()
        counts["chat_logs_redacted"] = n_chat

    # ---- 每患者私有库：自由文本笔记 / 紧急事件超期删除 ----
    purged_notes = 0
    purged_emerg = 0
    data_dir = _data_dir()
    if os.path.isdir(data_dir):
        for fn in os.listdir(data_dir):
            if not fn.endswith(".db"):
                continue
            username = fn[:-3]
            if not _USER_RE.match(username):
                continue
            try:
                eng = get_patient_engine(username)
            except Exception:  # noqa: BLE001 - 损坏/非法库跳过
                continue
            from sqlalchemy.orm import sessionmaker

            sess = sessionmaker(bind=eng, expire_on_commit=False)()
            try:
                n1 = (
                    sess.query(ConversationMemory)
                    .filter(ConversationMemory.created_at < cut)
                    .delete()
                )
                n2 = sess.query(EmergencyEvent).filter(EmergencyEvent.created_at < cut).delete()
                if not dry_run:
                    sess.commit()
                else:
                    sess.rollback()
                purged_notes += n1
                purged_emerg += n2
            finally:
                sess.close()
    counts["conversation_memory_purged"] = purged_notes
    counts["emergency_events_purged"] = purged_emerg

    total_purged = purged_notes + purged_emerg + counts["chat_logs_redacted"]
    if not dry_run:
        PHI_PURGED.labels(scope="conversation_memory").inc(purged_notes)
        PHI_PURGED.labels(scope="emergency_events").inc(purged_emerg)
        PHI_PURGED.labels(scope="chat_logs_redacted").inc(counts["chat_logs_redacted"])

    result = {
        "dry_run": dry_run,
        "enabled_db": True,
        "retention_days": days,
        "scopes": counts,
        "total_purged": total_purged,
    }
    return result


# ---------------------------------------------------------------------------
# 删除权（right-to-erasure）
# ---------------------------------------------------------------------------
def erase_patient(username: str, actor: str = "system") -> dict:
    """整体抹除某患者的所有可定位数据（删除权 / 被遗忘权）。

    - 删除其独立 SQLite 私有库文件；
    - 主库：删除账号 / 刷新令牌 / 预约 / 检查单 / 审批 / 对话 / 知情同意；
    - 对含该标识符的历史审计日志做假名化（不可逆令牌替换），保留可追溯性；
    - 写入一条「删除权执行」审计记录（同样只存假名令牌）。

    返回各作用域删除计数。``username`` 不存在时仍尽力清理可能残留的数据。
    """
    if not _USER_RE.match(username or ""):
        raise ValueError(f"非法用户名：{username!r}")
    if not is_db_enabled():
        return {"erased": False, "enabled_db": False, "username": username}

    token = pseudonymize(username)
    counts: dict[str, int] = {}

    # ---- 删除独立私有库文件（含检验报告 / 生命体征 / 笔记 / 紧急事件） ----
    db_path = _patient_db_path(username)
    removed_file = False
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
            removed_file = True
        except OSError:
            removed_file = False
    counts["patient_db_file"] = 1 if removed_file else 0

    with get_session() as s:
        user = s.query(User).filter(User.username == username).first()
        user_id = user.id if user else None

        # 刷新令牌
        n = s.query(RefreshToken).filter(RefreshToken.username == username).delete()
        counts["refresh_tokens"] = n

        # 预约（按 users.id 外键）
        if user_id is not None:
            n = s.query(Appointment).filter(Appointment.patient_id == user_id).delete()
            counts["appointments"] = n
        else:
            counts["appointments"] = 0

        # 检查单（按 username 字符串）
        from .db import ExamStep

        n = s.query(ExamStep).filter(ExamStep.patient_username == username).delete()
        counts["exam_steps"] = n

        # 审批（thread_id 形如 role:sub:tid，含 username 作为 sub）
        n = s.query(Approval).filter(Approval.thread_id.like(f"%:{username}:%")).delete()
        counts["approvals"] = n

        # 对话日志（PHI 整体抹除）
        n = s.query(ChatLog).filter(ChatLog.patient_id == username).delete()
        counts["chat_logs"] = n

        # 知情同意
        n = s.query(ConsentRecord).filter(ConsentRecord.username == username).delete()
        counts["consent_records"] = n

        # 历史审计日志：把含明文的 username 假名化（保留轨迹但去标识）
        audited = s.query(AuditLog).filter(AuditLog.detail.like(f"%{username}%")).all()
        for a in audited:
            if a.detail and username in a.detail:
                a.detail = a.detail.replace(username, token)
        counts["audit_logs_scrubbed"] = len(audited)

        # 账号本身最后删（避免外键约束问题）
        if user is not None:
            s.delete(user)
            counts["user"] = 1
        else:
            counts["user"] = 0

        # 删除权执行留痕（合规审计，仅存假名令牌）
        s.add(
            AuditLog(
                actor=actor or "system",
                action="patient_erasure",
                detail=(
                    f"patient={token} scopes=all removed_file={removed_file} "
                    f"appointments={counts['appointments']} exam_steps={counts['exam_steps']} "
                    f"approvals={counts['approvals']} chat_logs={counts['chat_logs']} "
                    f"consent={counts['consent_records']} audit_scrubbed={counts['audit_logs_scrubbed']}"
                ),
            )
        )
        s.commit()

    PHI_ERASURES.inc()
    counts["erased"] = True
    counts["enabled_db"] = True
    counts["username"] = username
    counts["pseudonym"] = token
    return counts
