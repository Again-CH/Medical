"""真实多用户鉴权：密码哈希 + JWT + RBAC。

- 密码用 hashlib.pbkdf2_hmac（零原生依赖，避免 bcrypt 编译问题）。
- JWT（HS256）签发/校验；FastAPI 依赖 get_current_user / require_doctor 替换原静态 token。
- 患者存 users 表，医护存 doctors 表；RBAC 通过 token 的 role 声明区分。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException

from .db import Doctor, User, get_session

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGO = "HS256"
JWT_EXP_HOURS = int(os.getenv("JWT_EXP_HOURS", "12"))


# ---------------- 密码 ----------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000)
    return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000)
    return hmac.compare_digest(dk.hex(), digest)


# ---------------- JWT ----------------
def create_token(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXP_HOURS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])


# ---------------- 注册 / 登录 ----------------
def register_user(
    username: str,
    password: str,
    role: str = "patient",
    full_name: str = "",
    phone: str = "",
    title: str = "",
) -> str:
    """注册用户；返回角色。已存在则抛 ValueError。"""
    with get_session() as s:
        if role == "doctor":
            if s.query(Doctor).filter(Doctor.username == username).first():
                raise ValueError("doctor exists")
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
                raise ValueError("user exists")
            s.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    full_name=full_name or username,
                    phone=phone,
                )
            )
        s.commit()
    return role


def authenticate(username: str, password: str, role: str) -> Optional[str]:
    """校验凭据；成功返回 JWT，失败返回 None。"""
    with get_session() as s:
        rec = (
            s.query(Doctor).filter(Doctor.username == username).first()
            if role == "doctor"
            else s.query(User).filter(User.username == username).first()
        )
        if not rec or not verify_password(password, rec.password_hash):
            return None
        return create_token(rec.username, role)


# ---------------- FastAPI 依赖 ----------------
def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[7:].strip()
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail="invalid or expired token") from e
    return {"sub": payload["sub"], "role": payload["role"]}


def require_doctor(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "doctor":
        raise HTTPException(status_code=403, detail="doctor only")
    return user
