"""反馈驱动的自我优化闭环（feedback → 沉淀 → 人工审批 → 知识库更新）。

设计原则（医疗场景的关键约束）
------------------------------
**系统绝不能无人监督地改写自己的知识库。** 患者反馈是信号，不是真理：
可能误解、可能恶意、可能只是措辞问题。让 Agent 根据反馈自动改医学知识，
等于把「什么是对的」交给了一个会漂移的非确定性系统。

因此闭环设计成：

    采集反馈 → 按意图聚类找知识缺口 → **生成提案**（不是直接改）
             → 走 HITL 审批门（医护审核） → 批准后幂等写入知识库

与既有 HITL 的一致性：复用 ``approvals`` 表（``action="knowledge_update"``），
由医护在 ``/api/review`` 工作台审批，审批记录含完整 payload 与 resolved_by，
审计可追溯 —— 与挂号审批走的是同一套机制，不另起炉灶。

为什么按「意图 + 关键词」聚类而不是逐条处理：单条差评是噪声，
同一意图下反复出现的差评才是知识缺口（信号需要阈值）。
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from .db import Approval, Feedback, get_session, is_db_enabled
from .logging_config import get_logger

log = get_logger(__name__)

# 同一意图下达到该差评数才认定为「知识缺口」（低于阈值只记录不提案）
DEFAULT_GAP_THRESHOLD = 2

# 提案走审批门时使用的 action 标记
ACTION = "knowledge_update"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_feedback(
    username: str,
    rating: str,
    comment: str = "",
    intent: str = "",
    thread_id: str = "",
    trace_id: str = "",
) -> str:
    """记录一条患者反馈。``rating`` 取 ``up`` / ``down``。

    DB 不可用时安全返回（演示/离线模式下反馈不落库，但调用方不受影响）。
    """
    rating = (rating or "").strip().lower()
    if rating not in ("up", "down"):
        return "[feedback] 评价无效（应为 up / down）"
    if not is_db_enabled():
        return f"[feedback] (demo) 已记录评价：{rating}"

    with get_session() as s:
        s.add(
            Feedback(
                username=username,
                thread_id=thread_id,
                trace_id=trace_id,
                intent=intent,
                rating=rating,
                comment=comment or "",
            )
        )
        s.commit()
    return f"[feedback] 已记录评价：{rating}"


def recent_feedback(limit: int = 100, rating: str = "") -> list[dict]:
    """读取近期反馈（管理员视图）。"""
    if not is_db_enabled():
        return []
    with get_session() as s:
        q = s.query(Feedback)
        if rating:
            q = q.filter(Feedback.rating == rating)
        rows = q.order_by(Feedback.created_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "username": r.username,
            "rating": r.rating,
            "intent": r.intent or "",
            "comment": r.comment or "",
            "proposed": bool(r.proposed),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def find_knowledge_gaps(threshold: int = DEFAULT_GAP_THRESHOLD) -> list[dict]:
    """按「意图」聚合未提案的差评，找出达到阈值的知识缺口。

    返回每项含 ``intent``、``count``、``samples``（患者补充文本，已解密）。
    阈值存在的意义：单条差评是噪声，反复出现才是缺口。
    """
    if not is_db_enabled():
        return []
    with get_session() as s:
        rows = (
            s.query(Feedback).filter(Feedback.rating == "down", Feedback.proposed.is_(False)).all()
        )
    buckets: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        buckets[r.intent or "unknown"].append(r.comment or "")

    gaps = []
    for intent, comments in buckets.items():
        if len(comments) < threshold:
            continue
        samples = [c for c in comments if c][:5]
        gaps.append({"intent": intent, "count": len(comments), "samples": samples})
    gaps.sort(key=lambda g: g["count"], reverse=True)
    return gaps


def propose_from_gaps(
    threshold: int = DEFAULT_GAP_THRESHOLD, requester: str = "system"
) -> list[str]:
    """把达到阈值的知识缺口转成**审批提案**（不是直接改知识库）。

    返回创建的审批单 id 列表。提案 payload 含缺口证据与建议补充的知识条目，
    由医护在工作台审核后决定是否落地。
    """
    if not is_db_enabled():
        return []
    gaps = find_knowledge_gaps(threshold)
    created = []
    with get_session() as s:
        for g in gaps:
            aid = f"kb-{uuid.uuid4().hex[:12]}"
            payload = {
                "intent": g["intent"],
                "negative_count": g["count"],
                "samples": g["samples"],
                # 建议新增的知识条目：doc_id 稳定，便于幂等覆盖
                "doc": {
                    "doc_id": f"FEEDBACK-{g['intent']}",
                    "doc_type": "enterprise",
                    "title": f"常见问题补充（来自患者反馈·{g['intent']}）",
                    "content": _draft_content(g),
                    "tags": ["feedback", g["intent"]],
                    "source": "feedback-loop",
                },
            }
            s.add(
                Approval(
                    id=aid,
                    thread_id=f"feedback:{g['intent']}",
                    action=ACTION,
                    payload=json.dumps(payload, ensure_ascii=False),
                    status="PENDING",
                    created_at=utcnow(),
                )
            )
            created.append(aid)
        if created:
            # 标记这些反馈已纳入提案，避免重复提案
            for g in gaps:
                (
                    s.query(Feedback)
                    .filter(
                        Feedback.rating == "down",
                        Feedback.proposed.is_(False),
                        Feedback.intent == g["intent"],
                    )
                    .update({Feedback.proposed: True}, synchronize_session=False)
                )
            s.commit()
    if created:
        log.info("feedback.proposals_created", extra={"count": len(created), "by": requester})
    return created


def _draft_content(gap: dict) -> str:
    """根据聚类结果草拟知识条目正文（**待医护审核**，不是最终内容）。"""
    lines = [
        f"意图分类：{gap['intent']}",
        f"患者差评次数：{gap['count']}",
        "患者反馈摘录：",
    ]
    for i, c in enumerate(gap["samples"], 1):
        lines.append(f"  {i}. {c}")
    lines.append("")
    lines.append("（本条目由反馈闭环自动生成草稿，需医护审核、补充与修正后方可生效。）")
    return "\n".join(lines)


def apply_approval(approval_id: str, resolved_by: str) -> str:
    """审批通过后把提案落地为知识条目（幂等，按 doc_id 覆盖）。

    只处理 ``action=knowledge_update`` 且状态为 APPROVED 的审批单，
    避免误把其他类型的审批当知识更新执行。
    """
    if not is_db_enabled():
        return "[kb-update] DB 未启用，无法写入知识库"
    from . import kb

    with get_session() as s:
        ap = s.get(Approval, approval_id)
        if ap is None:
            return f"[kb-update] 审批单不存在：{approval_id}"
        if ap.action != ACTION:
            return f"[kb-update] 审批单类型不符（{ap.action}），拒绝执行"
        if (ap.status or "").upper() != "APPROVED":
            return f"[kb-update] 审批单状态为 {ap.status}，仅 APPROVED 可落地"

        payload = json.loads(ap.payload or "{}")
        doc = payload.get("doc") or {}
        if not doc.get("doc_id") or not doc.get("content"):
            return "[kb-update] 提案缺少 doc_id 或 content，无法落地"

        try:
            kb.add_knowledge(
                doc_id=doc["doc_id"],
                doc_type=doc.get("doc_type", "enterprise"),
                title=doc.get("title", doc["doc_id"]),
                content=doc["content"],
                tags=doc.get("tags"),
                source=doc.get("source", "feedback-loop"),
            )
        except RuntimeError as e:
            return f"[kb-update] 写入知识库失败：{e}"

        ap.resolved_at = utcnow()
        ap.resolved_by = resolved_by
        s.commit()
    log.info("feedback.knowledge_applied", extra={"approval_id": approval_id, "by": resolved_by})
    return f"[kb-update] 已把 {doc['doc_id']} 写入知识库（审批人：{resolved_by}）"
