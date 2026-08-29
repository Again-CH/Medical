"""真实多用户鉴权：密码哈希 + JWT(短效访问/可吊销刷新) + RBAC + 账号防爆破。

安全设计要点：
- 密码：PBKDF2-HMAC-SHA256，轮数可配（默认 60 万），带方案版本号；旧方案登录时透明重哈希。
- JWT：访问令牌 15 分钟（含 jti/iss/nbf/type/tv），刷新令牌 7 天（仅存哈希于库）。
- 吊销：① 刷新令牌按条吊销（登出/盗用）；② 访问令牌经用户 ``token_version`` 全局一键作废
  （改密/登出即 bump，所有已发访问令牌立即失效，跨进程/重启有效）。
- 防爆破：连续失败达阈值锁定账户 N 分钟，成功登录清零。
- RBAC：token 的 role 声明区分患者/医护；get_current_user / require_doctor 替换静态 token。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import jwt
from fastapi import Depends, Header, HTTPException

from .config import (
    ACCOUNT_LOCKOUT_ATTEMPTS,
    ACCOUNT_LOCKOUT_MINUTES,
    AUTH_FAIL_MODE,
    JWT_ACCESS_EXP_MINUTES,
    JWT_ISSUER,
    JWT_REFRESH_EXP_DAYS,
    JWT_SECRET,
    MIN_PASSWORD_LEN,
    PASSWORD_REQUIRE_COMPLEXITY,
    PBKDF2_ROUNDS,
    REFRESH_ABSOLUTE_EXP_DAYS,
)
from .db import (
    AuditLog,
    Doctor,
    RefreshToken,
    User,
    get_session,
    is_db_enabled,
)
from .logging_config import get_logger

log = get_logger()

JWT_ALGO = "HS256"


# ---------------- 密码强度 ----------------
def validate_password_strength(password: str) -> tuple[bool, str]:
    """校验密码强度：长度 + 复杂度（字母与数字并存）。

    弱口令是医疗系统最常见的入侵入口；注册与改密两处统一调用。
    """
    if not password:
        return False, "密码不能为空"
    if len(password) < MIN_PASSWORD_LEN:
        return False, f"密码至少 {MIN_PASSWORD_LEN} 位"
    if PASSWORD_REQUIRE_COMPLEXITY:
        has_alpha = any(c.isalpha() for c in password)
        has_digit = any(c.isdigit() for c in password)
        if not (has_alpha and has_digit):
            return False, "密码需同时包含字母与数字"
    return True, ""


# ---------------- 审计 ----------------
def record_audit(actor: str, action: str, detail: dict | None = None) -> None:
    """统一审计落库：actor 必须是**真实操作者**（用户/管理员），绝不用 thread_id 顶替。

    审计的价值在于可追责：必须能回答「谁、何时、对谁、做了什么」。
    写入失败只告警、不阻断主流程。
    """
    if not is_db_enabled():
        return
    import json

    try:
        with get_session() as s:
            s.add(
                AuditLog(
                    actor=actor or "system",
                    action=action,
                    detail=json.dumps(detail or {}, ensure_ascii=False),
                )
            )
            s.commit()
    except Exception:  # noqa: BLE001
        log.warning("audit.write_fail", extra={"actor": actor, "action": action})


# ---------------- 密码 ----------------
_SCHEMA = "pbkdf2_sha256"
_SCHEME_VERSION = "v2"


def hash_password(password: str) -> str:
    """生成带方案版本与轮数的强哈希；轮数随配置升级，verify 时按存储轮数校验。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return f"{_SCHEMA}${_SCHEME_VERSION}${PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """返回 (是否匹配, 是否需要用当前强方案重哈希)。

    兼容两种历史格式，避免存量账号登录失效：
    - 新格式：pbkdf2_sha256$v2$<轮数>$<salt>$<digest>（5 段）
    - 旧格式：pbkdf2_sha256$<salt>$<digest>（3 段，历史默认 10 万轮）
    旧格式登录成功时由 authenticate 透明升级为新格式。
    """
    try:
        parts = stored.split("$")
        if len(parts) == 5:
            schema, ver, rounds_s, salt, digest = parts
            rounds = int(rounds_s)
        elif len(parts) == 3:
            schema, salt, digest = parts
            rounds = 100_000  # 历史默认轮数
        else:
            return False, False
    except (ValueError, TypeError):
        return False, False
    if schema != _SCHEMA:
        return False, False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds)
    ok = hmac.compare_digest(dk.hex(), digest)
    # 旧格式或轮数落后 → 登录成功后升级
    needs_rehash = ok and (len(parts) == 3 or rounds != PBKDF2_ROUNDS)
    return ok, needs_rehash


# ---------------- JWT ----------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_naive() -> datetime:
    """数据库列（DateTime 无时区）统一用 naive UTC，避免 SQLite 读回后与时区感知时间比较报错。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _base_claims(username: str, role: str, token_type: str, ttl) -> dict:
    now = _now()
    return {
        "sub": username,
        "role": role,
        "type": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "iss": JWT_ISSUER,
        "jti": secrets.token_hex(16),
    }


def create_access_token(username: str, role: str, token_version: int = 0) -> str:
    claims = _base_claims(username, role, "access", timedelta(minutes=JWT_ACCESS_EXP_MINUTES))
    claims["tv"] = token_version  # 用于全局吊销校验
    return jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGO)


def create_refresh_token(
    username: str, role: str, token_version: int = 0, orig_iat: Optional[int] = None
) -> str:
    """签发刷新令牌。

    ``oiat``（原始签发时间）在续期时**原样继承**，用于实现「绝对有效期」：
    滑动续期会让令牌无限期有效，设备丢失或令牌泄露后无法收敛。
    """
    claims = _base_claims(username, role, "refresh", timedelta(days=JWT_REFRESH_EXP_DAYS))
    claims["tv"] = token_version
    claims["oiat"] = int(orig_iat if orig_iat is not None else claims["iat"])
    return jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGO)


def create_token_pair(
    username: str, role: str, token_version: int = 0, orig_iat: Optional[int] = None
) -> dict:
    return {
        "access_token": create_access_token(username, role, token_version),
        "refresh_token": create_refresh_token(username, role, token_version, orig_iat),
        "token_type": "bearer",
        "expires_in": JWT_ACCESS_EXP_MINUTES * 60,
        "role": role,
    }


def decode_token(token: str, expected_type: Optional[str] = None) -> dict:
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    if payload.get("iss") != JWT_ISSUER:
        raise jwt.InvalidTokenError("invalid issuer")
    if expected_type and payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


# 兼容旧调用（测试/内部）：默认签发访问令牌
def create_token(username: str, role: str) -> str:
    return create_access_token(username, role)


# ---------------- 刷新令牌持久化（仅存哈希） ----------------
def _hash_rt(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def store_refresh_token(raw: str, username: str, role: str) -> None:
    if not is_db_enabled():
        return
    with get_session() as s:
        s.add(
            RefreshToken(
                username=username,
                role=role,
                token_hash=_hash_rt(raw),
                # 用 naive UTC 存储，避免 SQLite DateTime 丢失时区信息后与 tz-aware 比较报错
                expires_at=_utcnow_naive() + timedelta(days=JWT_REFRESH_EXP_DAYS),
            )
        )
        s.commit()


def consume_refresh_token(raw: str, username: str, role: str) -> bool:
    """校验刷新令牌有效且未吊销/未过期；有效则吊销旧令牌并 True（用于旋转）。"""
    if not is_db_enabled():
        return True
    with get_session() as s:
        rt = s.query(RefreshToken).filter_by(token_hash=_hash_rt(raw), username=username).first()
        if rt is None or rt.revoked or rt.expires_at < _utcnow_naive():
            return False
        rt.revoked = True
        s.commit()
        return True


def revoke_refresh_token(raw: str) -> None:
    if not is_db_enabled():
        return
    with get_session() as s:
        rt = s.query(RefreshToken).filter_by(token_hash=_hash_rt(raw)).first()
        if rt:
            rt.revoked = True
            s.commit()


def bump_token_version(username: str, role: str) -> None:
    """全局吊销该用户所有访问令牌（登出/改密调用）。"""
    if not is_db_enabled():
        return
    with get_session() as s:
        rec = (
            s.query(Doctor).filter_by(username=username).first()
            if role == "doctor"
            else s.query(User).filter_by(username=username).first()
        )
        if rec is not None:
            rec.token_version = (getattr(rec, "token_version", 0) or 0) + 1
            s.commit()


# ---------------- 注册 / 登录 ----------------
class AuthResult(NamedTuple):
    ok: bool = False
    token_pair: Optional[dict] = None
    locked: bool = False
    reason: str = ""


class UserExistsError(ValueError):
    """账号已存在。

    单独成类而非靠错误消息文本区分：网关层据此决定 HTTP 409（冲突）而不是 400，
    避免「字符串嗅探」这种脆弱契约——改一次文案就会让接口语义悄悄退化。
    """


def register_user(
    username: str,
    password: str,
    role: str = "patient",
    full_name: str = "",
    phone: str = "",
    title: str = "",
) -> str:
    """注册用户；返回角色。已存在则抛 UserExistsError，口令不合格抛 ValueError。

    注意：本函数不限制角色，但**网关层只允许患者自助注册**（role 固定 patient）。
    医护账号必须经 ``/admin/doctors``（管理员鉴权）开通，杜绝自助提权。
    """
    ok, why = validate_password_strength(password)
    if not ok:
        raise ValueError(why)
    with get_session() as s:
        if role == "doctor":
            if s.query(Doctor).filter(Doctor.username == username).first():
                raise UserExistsError("医护账号已存在")
            s.add(
                Doctor(
                    username=username,
                    password_hash=hash_password(password),
                    full_name=full_name or username,
                    title=title,
                    dept_id=None,
                )
            )
        else:
            if s.query(User).filter(User.username == username).first():
                raise UserExistsError("用户已存在")
            s.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    full_name=full_name or username,
                    phone=phone,
                )
            )
        s.commit()
    record_audit(username, "register", {"role": role})
    return role


def load_principal(username: str, role: str):
    """按角色加载用户记录（患者 User / 医护 Doctor），不存在返回 None。"""
    with get_session() as s:
        if role == "doctor":
            return s.query(Doctor).filter(Doctor.username == username).first()
        return s.query(User).filter(User.username == username).first()


def authenticate(username: str, password: str, role: str) -> AuthResult:
    """校验凭据；成功返回令牌对（含刷新令牌存储），失败含锁定状态。

    - 命中锁定窗口直接返回 locked=True（423）。
    - 密码错误累计失败次数，达阈值锁定账户。
    - 成功重置失败计数；若密码哈希方案落后则透明升级。
    """
    with get_session() as s:
        rec = (
            s.query(Doctor).filter(Doctor.username == username).first()
            if role == "doctor"
            else s.query(User).filter(User.username == username).first()
        )
        if rec is None:
            return AuthResult(ok=False, reason="no_user")

        now = _utcnow_naive()
        if rec.locked_until and rec.locked_until > now:
            return AuthResult(ok=False, locked=True, reason="locked")

        ok, needs_rehash = verify_password(password, rec.password_hash)
        if not ok:
            rec.failed_attempts = (rec.failed_attempts or 0) + 1
            if rec.failed_attempts >= ACCOUNT_LOCKOUT_ATTEMPTS:
                rec.locked_until = now + timedelta(minutes=ACCOUNT_LOCKOUT_MINUTES)
                s.commit()
                record_audit(username, "login_locked", {"role": role})
                log.warning("auth.locked", extra={"user": username, "role": role})
                return AuthResult(ok=False, locked=True, reason="locked")
            s.commit()
            record_audit(username, "login_failed", {"role": role, "attempts": rec.failed_attempts})
            return AuthResult(ok=False, reason="bad_pw")

        # 成功：清零失败计数，必要时透明升级哈希
        rec.failed_attempts = 0
        rec.locked_until = None
        if needs_rehash:
            rec.password_hash = hash_password(password)
        s.commit()
        tv = getattr(rec, "token_version", 0) or 0
        pair = create_token_pair(rec.username, role, tv)
        store_refresh_token(pair["refresh_token"], rec.username, role)
        record_audit(username, "login_success", {"role": role})
        return AuthResult(ok=True, token_pair=pair)


def change_password(
    username: str, role: str, old_password: str, new_password: str
) -> tuple[bool, str]:
    """修改密码：校验旧密码 + 强度校验 + 全局吊销已发令牌。

    改密后 bump token_version，使此前签发的所有访问令牌立即失效——
    凭据泄露后用户可自助止损（前提：攻击者尚未改密）。
    """
    ok, why = validate_password_strength(new_password)
    if not ok:
        return False, why
    with get_session() as s:
        rec = (
            s.query(Doctor).filter(Doctor.username == username).first()
            if role == "doctor"
            else s.query(User).filter(User.username == username).first()
        )
        if rec is None:
            return False, "用户不存在"
        verified, _ = verify_password(old_password, rec.password_hash)
        if not verified:
            record_audit(username, "change_password_failed", {"role": role})
            return False, "原密码不正确"
        rec.password_hash = hash_password(new_password)
        rec.token_version = (getattr(rec, "token_version", 0) or 0) + 1
        s.commit()
    bump_token_version(username, role)
    record_audit(username, "change_password", {"role": role})
    return True, ""


# ---------------- FastAPI 依赖 ----------------
def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[7:].strip()
    try:
        payload = decode_token(token, expected_type="access")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail="invalid or expired token") from e

    sub = payload["sub"]
    role = payload["role"]
    tv = payload.get("tv", 0)

    if is_db_enabled():
        try:
            rec = load_principal(sub, role)
            revoked = rec is None or getattr(rec, "token_version", 0) != tv
            if revoked:
                log.warning(
                    "auth.revoked", extra={"sub": sub, "role": role, "reason": "token_version"}
                )
                raise HTTPException(status_code=401, detail="token revoked")
        except HTTPException:
            raise
        except Exception:
            # 安全决策：吊销校验不可用时的处置（默认 fail_closed）。
            # 医疗系统宁可短暂不可用，也不能放行已登出/已改密的令牌。
            if AUTH_FAIL_MODE == "fail_closed":
                log.error(
                    "auth.revocation_check_unavailable_fail_closed",
                    extra={"sub": sub, "role": role},
                )
                raise HTTPException(
                    status_code=503, detail="鉴权服务暂时不可用，请稍后重试"
                ) from None
            log.warning("auth.db_revocation_check_failed", extra={"sub": sub})

    return {"sub": sub, "role": role}


def require_doctor(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "doctor":
        log.warning(
            "auth.forbidden",
            extra={"sub": user.get("sub"), "role": user.get("role"), "need": "doctor"},
        )
        raise HTTPException(status_code=403, detail="doctor only")
    return user


def rotate_refresh_token(payload: dict) -> tuple[bool, str, Optional[dict]]:
    """校验刷新令牌并轮换：返回 (是否通过, 原因, 新令牌对)。

    安全要点：
    - 校验用户仍存在、未被锁定（用户已锁定/删除时刷新通道必须同步失效）。
    - 校验 ``token_version`` 未被 bump（登出/改密后旧刷新令牌立即作废）。
    - 校验绝对有效期（``oiat``），防止滑动续期导致令牌无限期有效。
    """
    sub = payload["sub"]
    role = payload["role"]
    tv = payload.get("tv", 0)
    oiat = payload.get("oiat") or payload.get("iat") or 0

    if int(_now().timestamp()) - int(oiat) > REFRESH_ABSOLUTE_EXP_DAYS * 86400:
        return False, "刷新令牌已超过最长有效期，请重新登录", None

    if is_db_enabled():
        rec = load_principal(sub, role)
        if rec is None:
            return False, "用户不存在", None
        if rec.locked_until and rec.locked_until > _utcnow_naive():
            return False, "账户已锁定", None
        if getattr(rec, "token_version", 0) != tv:
            return False, "令牌已作废，请重新登录", None

    pair = create_token_pair(sub, role, tv, orig_iat=oiat)
    store_refresh_token(pair["refresh_token"], sub, role)
    return True, "", pair
