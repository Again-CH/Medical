"""安全加固单元测试：密码哈希 / JWT 短效+刷新 / 账号锁定 / 令牌吊销 / 安全响应头。

覆盖 P2 之后的安全层：
- 密码 PBKDF2 版本化 + 透明重哈希（旧方案登录即升级）
- 访问令牌 15 分钟 + 刷新令牌 7 天，类型/签发方强校验
- 连续失败锁定账户（防爆破）
- 刷新令牌按条吊销 + 访问令牌经 token_version 全局一键吊销
- 统一安全响应头（CSP / X-Frame-Options / nosniff / Referrer-Policy）
"""

import hashlib
import secrets

import jwt
import pytest
from fastapi import HTTPException
from src.auth import (
    AuthResult,
    authenticate,
    bump_token_version,
    consume_refresh_token,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from src.db import RefreshToken, User, get_session


def _make_user(username: str, password: str, role: str = "patient"):
    """在测试库直接建/重置一个用户（避免触发 ensure_patient_db 副作用）。"""
    with get_session() as s:
        u = s.query(User).filter_by(username=username).first()
        if u:
            u.password_hash = hash_password(password)
            u.failed_attempts = 0
            u.locked_until = None
            u.token_version = 0
        else:
            s.add(User(username=username, password_hash=hash_password(password)))
        s.commit()


# ---------------- 密码 ----------------
def test_password_hash_versioned_and_verify():
    h = hash_password("Secret123")
    assert h.startswith("pbkdf2_sha256$v2$")
    ok, rehash = verify_password("Secret123", h)
    assert ok and rehash is False
    ok2, _ = verify_password("wrong", h)
    assert ok2 is False


def test_password_rehash_on_outdated_scheme():
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", b"Secret123", bytes.fromhex(salt), 100_000)
    old = f"pbkdf2_sha256$v1$100000${salt}${dk.hex()}"  # 旧方案（轮数/版本落后）
    ok, rehash = verify_password("Secret123", old)
    assert ok and rehash is True  # 需透明升级


# ---------------- JWT ----------------
def test_jwt_access_refresh_type_enforcement():
    at = create_access_token("u1", "patient", 0)
    rt = create_refresh_token("u1", "patient", 0)
    assert decode_token(at, expected_type="access")["type"] == "access"
    assert decode_token(rt, expected_type="refresh")["type"] == "refresh"
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(at, expected_type="refresh")  # 类型错用应拒绝


def test_jwt_issuer_enforced():
    with pytest.raises(jwt.InvalidTokenError):
        # 篡改签发方应被拒
        bad = jwt.encode(
            {
                "sub": "x",
                "role": "patient",
                "type": "access",
                "iss": "evil",
                "exp": 9_999_999_999,
                "jti": "a",
            },
            "x",
            algorithm="HS256",
        )
        decode_token(bad, expected_type="access")


# ---------------- 认证 / 锁定 ----------------
def test_authenticate_success_returns_pair_and_stores_refresh():
    _make_user("sec_a", "Passw0rd!", "patient")
    res = authenticate("sec_a", "Passw0rd!", "patient")
    assert isinstance(res, AuthResult) and res.ok and res.token_pair
    assert "access_token" in res.token_pair and "refresh_token" in res.token_pair
    with get_session() as s:
        assert s.query(RefreshToken).filter_by(username="sec_a").count() >= 1


def test_account_lockout_after_failures():
    _make_user("sec_b", "Passw0rd!", "patient")
    last = None
    for _ in range(5):  # 连续 5 次失败达到阈值
        last = authenticate("sec_b", "wrongpw", "patient")
    assert last.locked is True
    # 锁定期间即使密码正确也被拒
    locked = authenticate("sec_b", "Passw0rd!", "patient")
    assert locked.locked is True and locked.ok is False


def test_success_resets_failed_attempts():
    _make_user("sec_c", "Passw0rd!", "patient")
    authenticate("sec_c", "wrong1", "patient")
    authenticate("sec_c", "wrong2", "patient")
    res = authenticate("sec_c", "Passw0rd!", "patient")
    assert res.ok is True
    with get_session() as s:
        u = s.query(User).filter_by(username="sec_c").first()
        assert u.failed_attempts == 0 and u.locked_until is None


# ---------------- 刷新令牌吊销 / 访问令牌全局吊销 ----------------
def test_refresh_rotation_and_revoke():
    _make_user("sec_d", "Passw0rd!", "patient")
    res = authenticate("sec_d", "Passw0rd!", "patient")
    old_rt = res.token_pair["refresh_token"]
    assert consume_refresh_token(old_rt, "sec_d", "patient") is True  # 旋转：消费旧令牌
    assert consume_refresh_token(old_rt, "sec_d", "patient") is False  # 已吊销，二次消费失败


def test_logout_revokes_access_via_token_version():
    _make_user("sec_e", "Passw0rd!", "patient")
    res = authenticate("sec_e", "Passw0rd!", "patient")
    at = res.token_pair["access_token"]
    assert get_current_user("Bearer " + at)["sub"] == "sec_e"  # 登出前可用
    bump_token_version("sec_e", "patient")  # 模拟登出：全局吊销
    with pytest.raises(HTTPException):
        get_current_user("Bearer " + at)  # 旧访问令牌立即失效


# ---------------- 安全响应头 ----------------
def test_security_headers_present():
    from fastapi.testclient import TestClient
    from src.gateway import app

    c = TestClient(app)
    r = c.get("/health")
    h = r.headers
    assert h.get("X-Content-Type-Options") == "nosniff"
    assert h.get("X-Frame-Options") == "DENY"
    assert "default-src 'self'" in h.get("Content-Security-Policy", "")
    assert h.get("Referrer-Policy") == "no-referrer"
    assert "frame-ancestors 'none'" in h.get("Content-Security-Policy", "")
