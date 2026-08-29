"""灰度 / 金丝雀发布机制测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from src.db import get_session
from src.rollout import delete_rollout, list_rollouts, resolve_version, upsert_rollout


@pytest.fixture(autouse=True)
def _clean_rollouts():
    """每个用例前后清空 rollout_configs，避免相互影响。"""
    with get_session() as s:
        s.execute(text("DELETE FROM rollout_configs"))
        s.commit()
    yield
    with get_session() as s:
        s.execute(text("DELETE FROM rollout_configs"))
        s.commit()


def test_resolve_version_default():
    assert resolve_version("triage-prompt", "alice") == "v1"


def test_user_scope_full_rollout():
    upsert_rollout("triage-prompt", "v2", "user", "alice", 100)
    assert resolve_version("triage-prompt", "alice") == "v2"
    assert resolve_version("triage-prompt", "bob") == "v1"


def test_global_percentage_stable():
    upsert_rollout("triage-prompt", "v2", "global", "*", 50)
    # 同一用户多次调用结果应稳定
    first = resolve_version("triage-prompt", "alice")
    assert resolve_version("triage-prompt", "alice") == first
    # 100% 灰度必中
    assert resolve_version("triage-prompt", "alice", default="v1") in {"v1", "v2"}


def test_user_priority_over_global():
    upsert_rollout("triage-prompt", "v2", "global", "*", 100)
    upsert_rollout("triage-prompt", "v3", "user", "alice", 100)
    assert resolve_version("triage-prompt", "alice") == "v3"
    assert resolve_version("triage-prompt", "bob") == "v2"


def test_tenant_scope():
    upsert_rollout("triage-prompt", "v2", "tenant", "2", 100)
    assert resolve_version("triage-prompt", "alice", tenant_id=2) == "v2"
    assert resolve_version("triage-prompt", "alice", tenant_id=1) == "v1"


def test_user_priority_over_tenant():
    upsert_rollout("triage-prompt", "v2", "tenant", "2", 100)
    upsert_rollout("triage-prompt", "v3", "user", "alice", 100)
    assert resolve_version("triage-prompt", "alice", tenant_id=2) == "v3"


def test_invalid_scope_raises():
    with pytest.raises(ValueError):
        upsert_rollout("x", "v2", "invalid", "*", 100)  # type: ignore[arg-type]


def test_invalid_percentage_raises():
    with pytest.raises(ValueError):
        upsert_rollout("x", "v2", "global", "*", 101)


def test_list_and_delete():
    upsert_rollout("triage-prompt", "v2", "global", "*", 10)
    rollouts = list_rollouts()
    assert len(rollouts) == 1
    rid = rollouts[0]["id"]
    assert delete_rollout(rid) is True
    assert delete_rollout(rid) is False
    assert list_rollouts() == []
