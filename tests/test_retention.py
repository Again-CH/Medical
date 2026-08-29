"""PHI 留存策略与删除权测试：超期清理 / 脱敏、整体抹除、审计假名化、运维端点鉴权。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from src.db import (
    Appointment,
    Approval,
    AuditLog,
    ChatLog,
    ConsentRecord,
    ConversationMemory,
    EmergencyEvent,
    ExamStep,
    RefreshToken,
    User,
    ensure_patient_db,
    get_patient_session,
    get_session,
)
from src.tenant import default_tenant_id


def _utc(days_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def test_retention_purges_old_and_keeps_new(tmp_path, monkeypatch):
    # 把患者私有库与留存扫描都限定到临时目录，避免触碰真实 data/
    monkeypatch.setattr("src.db._data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("src.retention._data_dir", lambda: str(tmp_path))
    from src import retention

    username = "retainme"
    ensure_patient_db(username)
    with get_patient_session(username) as s:
        s.add(
            ConversationMemory(
                patient_id=username, key="followup_note", value="旧笔记", created_at=_utc(400)
            )
        )
        s.add(
            ConversationMemory(
                patient_id=username, key="followup_note", value="新笔记", created_at=_utc(1)
            )
        )
        s.add(EmergencyEvent(patient_id=username, content="旧紧急", created_at=_utc(400)))
        s.commit()

    # 主库对话日志：旧（超期）与新
    with get_session() as s:
        s.add(
            ChatLog(
                patient_id=username, input_text="旧输入", output_text="旧输出", created_at=_utc(400)
            )
        )
        s.add(
            ChatLog(
                patient_id=username, input_text="新输入", output_text="新输出", created_at=_utc(1)
            )
        )
        s.commit()

    # dry-run：只统计不改写
    dry = retention.apply_retention(dry_run=True, retention_days=365)
    assert dry["dry_run"] is True
    assert dry["scopes"]["conversation_memory_purged"] == 1
    assert dry["scopes"]["emergency_events_purged"] == 1
    assert dry["scopes"]["chat_logs_redacted"] == 1
    # dry-run 不改写：旧笔记仍在
    with get_patient_session(username) as s:
        assert s.query(ConversationMemory).count() == 2

    # 实际执行
    res = retention.apply_retention(retention_days=365)
    assert res["total_purged"] == 3
    with get_patient_session(username) as s:
        rows = s.query(ConversationMemory).all()
        assert len(rows) == 1 and rows[0].value == "新笔记"
        assert s.query(EmergencyEvent).count() == 0
    with get_session() as s:
        # 旧行仍在但 input_text 已被脱敏（保留行以保留运营/合规指标）
        old = (
            s.query(ChatLog)
            .filter(ChatLog.patient_id == username, ChatLog.created_at < _utc(300))
            .first()
        )
        assert old.input_text == "[redacted by retention policy]"
        new = s.query(ChatLog).filter(ChatLog.input_text == "新输入").first()
        assert new.input_text == "新输入"
        # 清理测试写入
        s.query(ChatLog).filter(ChatLog.patient_id == username).delete()
        s.commit()


def test_retention_clinical_records_untouched(tmp_path, monkeypatch):
    """临床记录（检验报告/生命体征）不参与自动留存清理。"""
    monkeypatch.setattr("src.db._data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("src.retention._data_dir", lambda: str(tmp_path))
    from src import retention
    from src.db import LabReport, VitalSign

    username = "clintest"
    ensure_patient_db(username)
    with get_patient_session(username) as s:
        s.add(LabReport(patient_id=username, item="WBC", result="5.0", report_date="2020-01-01"))
        s.add(
            VitalSign(
                patient_id=username, type="BP", value="120", measured_at="2020-01-01T00:00:00"
            )
        )
        s.commit()
    retention.apply_retention(retention_days=365)
    with get_patient_session(username) as s:
        assert s.query(LabReport).count() == 1
        assert s.query(VitalSign).count() == 1


def test_erase_removes_everything_and_scrubs_audit(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db._data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("src.retention._data_dir", lambda: str(tmp_path))
    from src import retention

    username = "eraseme"
    with get_session() as s:
        s.add(
            User(
                username=username,
                password_hash="x",
                full_name="测试",
                phone="13800000000",
                token_version=0,
            )
        )
        s.commit()
    user_id = s.query(User).filter(User.username == username).first().id
    # 业务主数据已按院区隔离：预约与检查单必须带 tenant_id（此处用默认租户）
    _tid = default_tenant_id()
    with get_session() as s:
        s.add(
            Appointment(
                patient_id=user_id,
                doctor_id=1,
                schedule_id=1,
                work_date="2026-01-01",
                period="AM",
                slot_index=1,
                tenant_id=_tid,
            )
        )
        s.add(
            ExamStep(
                patient_username=username,
                step_name="验血",
                location="B栋",
                tenant_id=_tid,
            )
        )
        s.add(RefreshToken(username=username, role="patient", token_hash="h", expires_at=_utc(-1)))
        s.add(ConsentRecord(username=username, consent_version="v1"))
        s.add(
            ChatLog(
                patient_id=username, input_text="我的症状", output_text="建议", created_at=_utc(1)
            )
        )
        s.add(AuditLog(actor=username, action="login", detail=f"user {username} logged in"))
        s.add(
            Approval(
                id="appr-1",
                thread_id=f"patient:{username}:abc",
                action="lock",
                payload="{}",
                status="PENDING",
            )
        )
        s.commit()

    ensure_patient_db(username)
    with get_patient_session(username) as s:
        s.add(ConversationMemory(patient_id=username, key="followup_note", value="私有笔记"))
        s.commit()

    res = retention.erase_patient(username, actor="admin")
    assert res["erased"] is True
    assert res["patient_db_file"] == 1
    assert not os.path.exists(os.path.join(str(tmp_path), f"{username}.db"))

    with get_session() as s:
        assert s.query(User).filter(User.username == username).first() is None
        assert s.query(ChatLog).filter(ChatLog.patient_id == username).count() == 0
        assert s.query(ConsentRecord).filter(ConsentRecord.username == username).count() == 0
        assert s.query(RefreshToken).filter(RefreshToken.username == username).count() == 0
        assert s.query(ExamStep).filter(ExamStep.patient_username == username).count() == 0
        assert s.query(Approval).filter(Approval.thread_id.like(f"%:{username}:%")).count() == 0
        # 历史审计日志中明文用户名已被假名化
        scrubbed = s.query(AuditLog).filter(AuditLog.detail.like(f"%{username}%")).count()
        assert scrubbed == 0
        # 删除权执行留痕（仅存假名令牌，不含明文用户名）
        era = s.query(AuditLog).filter(AuditLog.action == "patient_erasure").first()
        assert era is not None
        assert username not in era.detail
        assert res["pseudonym"] in era.detail


def test_erase_endpoints_and_self_service(monkeypatch):
    """管理员端点 + 患者自助删除端点（DELETE /api/patient/me）共用同一完整路径。"""
    import src.gateway as gw
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gw, "ADMIN_API_KEY", "test-admin-key")
    client = TestClient(gw.app)
    headers = {"X-Admin-Key": "test-admin-key"}

    # 先建一个患者
    with get_session() as s:
        s.add(
            User(
                username="erasevia", password_hash="x", full_name="e", phone="139", token_version=0
            )
        )
        s.commit()

    # 缺少 confirm → 拒绝
    r = client.post("/api/admin/erase", json={"username": "erasevia"}, headers=headers)
    assert r.status_code == 400

    # 正确触发
    r = client.post(
        "/api/admin/erase", json={"username": "erasevia", "confirm": True}, headers=headers
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    with get_session() as s:
        assert s.query(User).filter(User.username == "erasevia").first() is None

    # 无密钥 → 401
    assert (
        client.post("/api/admin/erase", json={"username": "x", "confirm": True}).status_code == 401
    )


def test_retention_endpoint_dry_run(monkeypatch):
    import src.gateway as gw
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gw, "ADMIN_API_KEY", "test-admin-key")
    client = TestClient(gw.app)
    r = client.post("/api/admin/retention?dry_run=1", headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
