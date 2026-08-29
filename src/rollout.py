"""灰度 / 金丝雀发布：按 feature + 用户维度决定使用哪个版本。

核心设计
--------
- 配置落在主库 ``rollout_configs`` 表，多实例共享一致视图；
- 命中优先级：user > tenant > global，同一 feature 不叠加，取最高优先级命中；
- percentage 基于用户名的稳定哈希，保证同一用户多次请求体验一致；
- 未命中任何灰度时回退到 default_version（通常 = ``PROMPT_VERSION`` 环境变量）。

使用场景
--------
1. 新 prompt v2 仅对 tenant=2 全开：scope=tenant, scope_value=2, percentage=100
2. 新 prompt v2 对全量用户 5% 灰度：scope=global, scope_value=*, percentage=5
3. 对单个测试账号提前验证：scope=user, scope_value=alice, percentage=100

为什么不直接用第三方 feature flag SaaS？
- 医疗数据本地化部署，不依赖外部网络；
- 配置与审计日志同库，便于合规追溯；
- 面试作品需要体现「自己实现的版本控制与灰度机制」。
"""

from __future__ import annotations

import hashlib
from typing import Sequence

from sqlalchemy import select

from .db import RolloutConfig, get_session
from .metrics import ROLLOUT_EXPOSURES


def _user_in_percentage(username: str, feature: str, percentage: int) -> bool:
    """对 username + feature 做稳定哈希，映射到 0-99，判断是否落在灰度桶。"""
    if percentage <= 0:
        return False
    if percentage >= 100:
        return True
    digest = hashlib.sha256(f"{feature}:{username}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < percentage


def _to_int(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except ValueError:
        return None


def resolve_version(
    feature: str,
    username: str,
    tenant_id: int | str | None = None,
    default: str = "v1",
) -> str:
    """根据灰度配置返回目标版本。

    :param feature: 发布单元名，如 ``triage-prompt``。
    :param username: 当前用户（患者或医护）用户名，用于稳定哈希。
    :param tenant_id: 可选租户 ID，用于 tenant 级灰度。
    :param default: 未命中时回退版本。
    :return: 应使用的版本字符串。
    """
    tenant_id_int = _to_int(tenant_id)

    try:
        with get_session() as session:
            stmt = select(RolloutConfig).where(RolloutConfig.feature == feature)
            rows: Sequence[RolloutConfig] = session.execute(stmt).scalars().all()
    except RuntimeError:
        # 离线 demo 无 DATABASE_URL，灰度表不存在，直接回退默认版本
        return default

    # 按 scope 优先级排序并去重：user > tenant > global
    candidates = {"global": None, "tenant": None, "user": None}
    for r in rows:
        candidates[r.scope] = r  # 同 scope 取最后一条（应用层应保证唯一）

    for scope, scope_value, pct in [
        ("user", username, candidates.get("user")),
        ("tenant", str(tenant_id_int) if tenant_id_int is not None else None, candidates.get("tenant")),
        ("global", "*", candidates.get("global")),
    ]:
        cfg = pct
        if cfg is None or scope_value is None:
            continue
        if cfg.scope_value != scope_value and cfg.scope_value != "*":
            continue
        if _user_in_percentage(username, feature, cfg.percentage):
            if cfg.version != default:
                ROLLOUT_EXPOSURES.labels(
                    feature=feature, version=cfg.version, scope=scope
                ).inc()
            return cfg.version

    return default


def list_rollouts() -> list[dict]:
    """返回所有灰度配置（供管理接口）。"""
    with get_session() as session:
        rows = session.execute(select(RolloutConfig)).scalars().all()
        return [
            {
                "id": r.id,
                "feature": r.feature,
                "version": r.version,
                "scope": r.scope,
                "scope_value": r.scope_value,
                "percentage": r.percentage,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def upsert_rollout(
    feature: str,
    version: str,
    scope: str,
    scope_value: str,
    percentage: int,
) -> RolloutConfig:
    """新增或覆盖一条灰度配置。"""
    if scope not in {"global", "tenant", "user"}:
        raise ValueError(f"Invalid scope: {scope}")
    if not 0 <= percentage <= 100:
        raise ValueError("percentage must be 0-100")

    with get_session() as session:
        row = session.execute(
            select(RolloutConfig).where(
                RolloutConfig.feature == feature,
                RolloutConfig.scope == scope,
                RolloutConfig.scope_value == scope_value,
            )
        ).scalar_one_or_none()
        if row is None:
            row = RolloutConfig(
                feature=feature,
                version=version,
                scope=scope,
                scope_value=scope_value,
                percentage=percentage,
            )
            session.add(row)
        else:
            row.version = version
            row.percentage = percentage
        session.commit()
        session.refresh(row)
        return row


def delete_rollout(rollout_id: int) -> bool:
    """删除灰度配置。"""
    with get_session() as session:
        row = session.get(RolloutConfig, rollout_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
