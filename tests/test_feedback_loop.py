"""反馈驱动自我优化闭环测试。

核心要验证的安全属性：**系统绝不能无人监督地改写自己的知识库。**
患者反馈只是信号（可能误解、可能恶意），让 Agent 据此自动改医学知识，
等于把「什么是对的」交给一个会漂移的非确定性系统。

因此闭环必须是：采集 → 聚类找缺口 → **生成提案** → 医护审批 → 批准后落地。
本文件逐段钉死这条链路，特别是「未审批不得落地」这一条。
"""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import text
from src.db import Approval, Feedback, get_session
from src.feedback import (
    ACTION,
    apply_approval,
    find_knowledge_gaps,
    propose_from_gaps,
    recent_feedback,
    record_feedback,
)

# 闭环的最后一步是把提案写进企业知识库（knowledge_documents），
# 而该表依赖 pgvector，**只在 Postgres 主库创建**（sqlite 方言下迁移会跳过）。
# 因此本模块在 sqlite 上整体跳过 —— 这是方言限制，不是功能缺陷；
# CI 的 integration job（pgvector/pgvector:pg16）会真实跑这些用例。
pytestmark = pytest.mark.skipif(
    "postgresql" not in os.getenv("DATABASE_URL", ""),
    reason="反馈闭环的知识库落地依赖 pgvector（仅 Postgres 主库），sqlite 下跳过",
)


@pytest.fixture
def clean_feedback():
    """用例前后都清理，避免残留数据在测试间互相污染。

    清理范围要覆盖闭环可能写过的**所有**表：反馈、知识更新审批单、
    以及已落地的知识条目（否则「PENDING 不得提前写入」这类断言会被上一轮
    残留的 FEEDBACK-* 条目误判为失败）。
    """

    def _purge() -> None:
        with get_session() as s:
            s.execute(text("DELETE FROM feedback"))
            s.execute(
                text("DELETE FROM approvals WHERE action = :a OR id LIKE 'not-kb-%'"),
                {"a": ACTION},
            )
            s.execute(text("DELETE FROM knowledge_documents WHERE source = 'feedback-loop'"))
            s.commit()

    _purge()
    yield
    _purge()


def _seed_downs(intent: str, n: int, prefix: str = "反馈"):
    for i in range(n):
        record_feedback("fbtest", "down", f"{prefix}{i}", intent=intent)


# ---------------- 采集 ----------------


def test_record_feedback_valid(clean_feedback):
    msg = record_feedback("fbtest", "up", "回答很清楚", intent="booking")
    assert "已记录" in msg
    rows = recent_feedback(rating="up")
    assert any(r["username"] == "fbtest" for r in rows)


def test_record_feedback_rejects_invalid_rating(clean_feedback):
    msg = record_feedback("fbtest", "maybe", "", intent="booking")
    assert "无效" in msg
    assert recent_feedback() == []


# ---------------- 聚类：信号需要阈值 ----------------


def test_single_negative_is_noise_not_gap(clean_feedback):
    """单条差评是噪声，不应被认定为知识缺口。"""
    _seed_downs("intake", 1)
    assert find_knowledge_gaps(threshold=2) == []


def test_repeated_negatives_form_gap(clean_feedback):
    """同一意图反复差评 → 认定为缺口，并带上样本证据。"""
    _seed_downs("triage", 3, prefix="分诊不准")
    gaps = find_knowledge_gaps(threshold=2)
    assert [g["intent"] for g in gaps] == ["triage"]
    assert gaps[0]["count"] == 3
    assert any("分诊不准" in s for s in gaps[0]["samples"])


def test_gaps_ranked_by_count(clean_feedback):
    _seed_downs("intake", 2)
    _seed_downs("booking", 5)
    gaps = find_knowledge_gaps(threshold=2)
    assert [g["intent"] for g in gaps] == ["booking", "intake"]


def test_different_intents_not_merged(clean_feedback):
    """不同意图各 1 条差评不应被合并成假缺口。"""
    _seed_downs("intake", 1)
    _seed_downs("booking", 1)
    assert find_knowledge_gaps(threshold=2) == []


# ---------------- 提案 ----------------


def test_propose_creates_pending_approval(clean_feedback):
    _seed_downs("triage", 3)
    ids = propose_from_gaps(threshold=2)
    assert len(ids) == 1
    with get_session() as s:
        ap = s.get(Approval, ids[0])
        assert ap.status == "PENDING"
        assert ap.action == ACTION
        payload = json.loads(ap.payload)
        assert payload["intent"] == "triage"
        assert payload["negative_count"] == 3
        assert payload["doc"]["doc_id"] == "FEEDBACK-triage"


def test_propose_is_idempotent(clean_feedback):
    """同一批反馈不应被反复提案（proposed 标记生效）。"""
    _seed_downs("triage", 3)
    first = propose_from_gaps(threshold=2)
    second = propose_from_gaps(threshold=2)
    assert len(first) == 1
    assert second == [], "已提案的反馈不应再次生成提案"


def test_propose_marks_feedback_as_proposed(clean_feedback):
    _seed_downs("triage", 2)
    propose_from_gaps(threshold=2)
    with get_session() as s:
        assert all(bool(r.proposed) for r in s.query(Feedback).all())


# ---------------- 落地：必须审批 ----------------


def test_apply_rejects_pending_approval(clean_feedback):
    """关键安全属性：PENDING 状态不得落地 —— 无人监督的自我改写必须被挡住。"""
    _seed_downs("triage", 3)
    aid = propose_from_gaps(threshold=2)[0]
    msg = apply_approval(aid, resolved_by="drwang")
    assert "状态为 PENDING" in msg
    # 知识库未被改动（直接查表，不依赖检索返回结构）
    with get_session() as s:
        n = s.execute(
            text("SELECT COUNT(*) FROM knowledge_documents WHERE doc_id = :d"),
            {"d": "FEEDBACK-triage"},
        ).scalar()
    assert n == 0, "PENDING 提案不得提前写入知识库"


def test_apply_rejects_wrong_action_type(clean_feedback):
    """其他类型的审批单（如挂号）不能被当成知识更新执行。"""
    with get_session() as s:
        s.add(
            Approval(
                id="not-kb-1",
                thread_id="x",
                action="book_appointment",
                payload=json.dumps({"doc": {"doc_id": "X", "content": "y"}}),
                status="APPROVED",
            )
        )
        s.commit()
    msg = apply_approval("not-kb-1", resolved_by="drwang")
    assert "类型不符" in msg


def test_apply_rejects_missing_approval(clean_feedback):
    msg = apply_approval("does-not-exist", resolved_by="drwang")
    assert "不存在" in msg


def test_apply_succeeds_when_approved(clean_feedback):
    """医护批准后，提案才能落地为知识条目。"""
    _seed_downs("triage", 3)
    aid = propose_from_gaps(threshold=2)[0]

    with get_session() as s:
        ap = s.get(Approval, aid)
        ap.status = "APPROVED"
        ap.decision = json.dumps({"approved": True})
        s.commit()

    msg = apply_approval(aid, resolved_by="drwang")
    assert "已把 FEEDBACK-triage 写入知识库" in msg

    # 审批单记录了处理人，审计可追溯
    with get_session() as s:
        ap = s.get(Approval, aid)
        assert ap.resolved_by == "drwang"
        assert ap.resolved_at is not None

    # 知识库里确实落库了（直接查表，不依赖检索返回结构）
    with get_session() as s:
        row = s.execute(
            text("SELECT title, source FROM knowledge_documents WHERE doc_id = :d"),
            {"d": "FEEDBACK-triage"},
        ).first()
    assert row is not None, "批准后应写入知识条目"
    assert "反馈" in row[0] or "分诊" in row[0]
    assert row[1] == "feedback-loop"
