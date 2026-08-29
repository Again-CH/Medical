"""PHI 静态加密（应用层透明列加密）。

设计目标：让「患者健康信息（PHI）落盘即加密」成为一行配置即可启用的能力，
而不是散落在各处的手写 base64。所有 PHI 列通过 SQLAlchemy ``TypeDecorator``
（``EncryptedText``）接入，写库时加密、读库时解密，对业务代码完全透明。

加密后端（可插拔，按可用性自动选择）：
- ``FernetBackend``：用 ``cryptography.Fernet``（AES-128-CBC + HMAC-SHA256），
  生产环境首选，依赖库可用时自动启用。
- ``StdlibBackend``：仅用 Python 标准库（HMAC-SHA256 计数器流密码 + Encrypt-then-MAC
  完整性标签）实现的认证加密，作为「无第三方依赖」的降级路径，保证本仓库在离线/
  受限环境下也能跑通「静态加密」且测试可覆盖。

令牌格式：``enc:<scheme>:<payload>``
- ``enc:v1:`` 标准库后端；``enc:f1:`` Fernet 后端；``<payload>`` 为 base64。
- 不带 ``enc:`` 前缀的值按明文处理 —— 既兼容存量明文行，也保证「未启用加密时」
  行为与改造前完全一致（fail-open 仅作用于「未启用」，密钥缺失时写加密为 fail-closed）。

密钥管理：
- 主密钥来自环境变量 ``PHI_ENCRYPTION_KEY``（任意长度的秘密串），统一派生为 32 字节。
- ``PHI_ENCRYPTION_ENABLED=1`` 且密钥已设置 → 新写入加密；密钥缺失则启动即报错
  （fail-closed，绝不拿空密钥加密）。
- 解密与「是否启用写入加密」解耦：只要密钥在场，历史密文随时可解；切换开关不会
  让已加密数据变不可读（但仍建议启用后跑一次 ``scripts/encrypt_existing_phi.py``
  把存量明文补齐加密）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

# ---------------------------------------------------------------------------
# 配置（来自环境变量，导入即用）
# ---------------------------------------------------------------------------
_ENABLED = os.getenv("PHI_ENCRYPTION_ENABLED", "0").lower() in ("1", "true", "yes")
_SECRET = os.getenv("PHI_ENCRYPTION_KEY", "")

# 派生 32 字节原始密钥；密钥缺省时为 None（未配置）
_RAW_KEY: Optional[bytes] = hashlib.sha256(_SECRET.encode("utf-8")).digest() if _SECRET else None

# cryptography 是否可用（决定首选后端）
try:
    from cryptography.fernet import Fernet  # type: ignore

    _FERNET_OK = True
except Exception:  # noqa: BLE001 - 离线/受限环境缺包时降级
    Fernet = None  # type: ignore
    _FERNET_OK = False


_PREFIX = "enc:"
_SCHEME_STDLIB = "v1"
_SCHEME_FERNET = "f1"


def enabled() -> bool:
    """当前是否对「新写入」启用加密。"""
    return _ENABLED


def is_configured() -> bool:
    """是否具备解密能力（密钥已设置）。"""
    return _RAW_KEY is not None


def backend_name() -> str:
    """当前生效的加密后端名（诊断用）。"""
    if not _ENABLED:
        return "disabled"
    return "fernet" if _FERNET_OK else "stdlib"


def generate_secret() -> str:
    """生成一个新的随机主密钥（写入 ``PHI_ENCRYPTION_KEY``）。"""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------
class CryptoBackend:
    scheme: str

    def encrypt(self, plaintext: str) -> str:  # pragma: no cover - 抽象
        raise NotImplementedError

    def decrypt(self, payload: str) -> str:  # pragma: no cover - 抽象
        raise NotImplementedError


class StdlibBackend(CryptoBackend):
    """标准库实现的认证加密：HMAC-SHA256 计数器流密码 + Encrypt-then-MAC。

    - 机密性：``C = P XOR HMAC(K, nonce || counter)``（密钥流）。
    - 完整性/真实性：``tag = HMAC(K, nonce || C)``，解密前先恒定时间校验。
    - 每个明文用 16 字节随机 nonce，避免密钥流复用（复用会泄露异或差）。
    """

    scheme = _SCHEME_STDLIB

    def __init__(self, key: bytes):
        self._k = key

    @staticmethod
    def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
        out = b""
        i = 0
        while len(out) < length:
            blk = hmac.new(key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
            out += blk
            i += 1
        return out[:length]

    def encrypt(self, plaintext: str) -> str:
        p = plaintext.encode("utf-8")
        n = secrets.token_bytes(16)
        ks = self._keystream(self._k, n, len(p))
        c = bytes(a ^ b for a, b in zip(p, ks, strict=True))
        tag = hmac.new(self._k, n + c, hashlib.sha256).digest()
        blob = base64.urlsafe_b64encode(n + c + tag).decode("ascii")
        return f"{_PREFIX}{self.scheme}:{blob}"

    def decrypt(self, payload: str) -> str:
        raw = base64.urlsafe_b64decode(payload)
        if len(raw) < 16 + 32:
            raise ValueError("PHI 密文长度异常（可能被截断）")
        n, c, tag = raw[:16], raw[16:-32], raw[-32:]
        exp = hmac.new(self._k, n + c, hashlib.sha256).digest()
        if not hmac.compare_digest(exp, tag):
            raise ValueError("PHI 完整性校验失败（密文被篡改或密钥不匹配）")
        ks = self._keystream(self._k, n, len(c))
        p = bytes(a ^ b for a, b in zip(c, ks, strict=True))
        return p.decode("utf-8")


class FernetBackend(CryptoBackend):
    """生产首选后端：AES-128-CBC + HMAC-SHA256（cryptography.Fernet）。"""

    scheme = _SCHEME_FERNET

    def __init__(self, key: bytes):
        if not _FERNET_OK or Fernet is None:
            raise RuntimeError("cryptography 不可用，无法使用 Fernet 后端")
        self._f = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, plaintext: str) -> str:
        tok = self._f.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{_PREFIX}{self.scheme}:{tok}"

    def decrypt(self, payload: str) -> str:
        return self._f.decrypt(payload.encode("ascii")).decode("utf-8")


def _backend_for_scheme(scheme: str) -> CryptoBackend:
    """按方案前缀构造对应后端（解密时无论是否启用写入都可用，只要密钥在场）。"""
    if not _RAW_KEY:
        raise RuntimeError("无法解密 PHI：PHI_ENCRYPTION_KEY 未设置")
    if scheme == _SCHEME_STDLIB:
        return StdlibBackend(_RAW_KEY)
    if scheme == _SCHEME_FERNET:
        if not _FERNET_OK:
            raise RuntimeError("PHI 密文为 Fernet 格式但 cryptography 不可用")
        return FernetBackend(_RAW_KEY)
    raise ValueError(f"未知 PHI 加密方案：{scheme!r}")


# ---------------------------------------------------------------------------
# 对外 API（供 EncryptedText 与脚本调用）
# ---------------------------------------------------------------------------
def encrypt_field(plaintext: Optional[str]) -> Optional[str]:
    """加密单个字段值；未启用或值为 None 时原样返回。"""
    if plaintext is None:
        return None
    if not _ENABLED:
        return plaintext
    if not _RAW_KEY:
        # 启用但无密钥：fail-closed，绝不拿空密钥加密
        raise RuntimeError("PHI_ENCRYPTION_ENABLED=1 但 PHI_ENCRYPTION_KEY 未设置")
    backend: CryptoBackend = FernetBackend(_RAW_KEY) if _FERNET_OK else StdlibBackend(_RAW_KEY)
    return backend.encrypt(plaintext)


def decrypt_field(token: Optional[str]) -> Optional[str]:
    """解密单个字段值；非前缀值按明文返回（向后兼容存量行）。"""
    if token is None:
        return None
    if not token.startswith(_PREFIX):
        return token
    scheme, _, payload = token[len(_PREFIX) :].partition(":")
    return _backend_for_scheme(scheme).decrypt(payload)


def is_encrypted(token: Optional[str]) -> bool:
    """该值是否已被加密（诊断/迁移脚本判断用）。"""
    return bool(token) and token.startswith(_PREFIX)


# ---------------------------------------------------------------------------
# SQLAlchemy 透明列类型：业务代码零改动接入加密
# ---------------------------------------------------------------------------
from sqlalchemy.types import Text as _SAText  # noqa: E402
from sqlalchemy.types import TypeDecorator  # noqa: E402


class EncryptedText(TypeDecorator):
    """透明加密的 TEXT 列：底层仍是 TEXT，无需改 schema / 迁移。

    - ``process_bind_param``：写库前加密（未启用时原样）。
    - ``process_result_value``：读库后解密（非 ``enc:`` 前缀按明文，兼容存量）。
    - ``cache_ok=True``：允许 SQLAlchemy 2.0 在类型缓存中复用，避免告警。
    """

    impl = _SAText
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[override]
        return encrypt_field(value)

    def process_result_value(self, value, dialect):  # type: ignore[override]
        return decrypt_field(value)
