"""PHI 透明加密测试：加解密正确性、篡改检测、明文兼容、fail-closed、ORM 透明性。"""

from __future__ import annotations

import hashlib

import pytest
import src.phi as phi
from sqlalchemy import Column, Integer, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


@pytest.fixture(autouse=True)
def _phi_config(monkeypatch):
    """每个测试前注入确定密钥并启用加密，结束后复位。"""
    monkeypatch.setattr(phi, "_ENABLED", True)
    monkeypatch.setattr(phi, "_RAW_KEY", hashlib.sha256(b"test-secret").digest())
    monkeypatch.setattr(phi, "_FERNET_OK", False)  # 本环境无 cryptography，强制走标准库后端
    yield
    monkeypatch.setattr(phi, "_ENABLED", False)
    monkeypatch.setattr(phi, "_RAW_KEY", None)
    monkeypatch.setattr(phi, "_FERNET_OK", False)


def test_roundtrip_and_prefix():
    pt = "患者血压 128/82，心率 72"
    tok = phi.encrypt_field(pt)
    assert tok.startswith("enc:v1:")
    assert phi.decrypt_field(tok) == pt
    assert phi.decrypt_field(None) is None


def test_tamper_detected():
    tok = phi.encrypt_field("secret-value")
    payload = tok[len("enc:v1:") :]
    # 翻转 base64 中的一个字符 → 完整性校验必须失败
    bad = "enc:v1:" + ("A" if payload[0] != "A" else "B") + payload[1:]
    with pytest.raises(ValueError):
        phi.decrypt_field(bad)


def test_plaintext_passthrough():
    # 存量明文行（未加密）读取时原样返回，保证向后兼容
    assert phi.decrypt_field("hello plaintext") == "hello plaintext"
    assert phi.is_encrypted("hello plaintext") is False
    assert phi.is_encrypted("enc:v1:xxxx") is True


def test_fail_closed_when_key_missing(monkeypatch):
    # 启用但无密钥：写加密必须 fail-closed 拒绝，绝不拿空密钥加密
    monkeypatch.setattr(phi, "_RAW_KEY", None)
    monkeypatch.setattr(phi, "_ENABLED", True)
    with pytest.raises(RuntimeError):
        phi.encrypt_field("x")


def test_disabled_passthrough(monkeypatch):
    # 未启用：值与明文一致，行为与改造前完全相同（零摩擦默认）
    monkeypatch.setattr(phi, "_ENABLED", False)
    monkeypatch.setattr(phi, "_RAW_KEY", hashlib.sha256(b"k").digest())
    assert phi.encrypt_field("x") == "x"
    assert phi.decrypt_field("x") == "x"


def test_backend_name():
    assert phi.backend_name() == "stdlib"


def test_orm_transparency(tmp_path):
    """EncryptedText 对业务透明：写库即加密、读库即解密，且落盘为密文。"""
    Base = declarative_base()

    class Note(Base):
        __tablename__ = "notes"
        id = Column(Integer, primary_key=True)
        body = Column(phi.EncryptedText)

    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng)
    with Sess() as s:
        s.add(Note(body="患者主诉：持续头痛三天"))
        s.commit()
        # 直接读底层存储，确认落盘为密文（前缀 + base64）
        raw = s.execute(__import__("sqlalchemy").text("SELECT body FROM notes")).scalar()
        assert raw.startswith("enc:v1:")
        assert "头痛" not in raw
        # 通过 ORM 读回，自动解密
        n = s.query(Note).first()
        assert n.body == "患者主诉：持续头痛三天"
