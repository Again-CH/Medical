"""多模态扩展点的契约测试。

重点不是测「分析得准不准」（没有后端，无从谈起），而是钉死三件事：
1. **无后端时必须明确说不支持**，绝不编造分析结果 —— 这是医疗红线；
2. 后端抛异常时降级为不可用，**不拖垮主对话链路**；
3. 注册契约可用：一个示例后端注册后能被正确分派。

这样当真正的视觉后端就绪时，接入点、契约与失败语义都已就位。
"""

from __future__ import annotations

import pytest
from src.multimodal import (
    _UNSUPPORTED_MSG,
    ModalityBackend,
    ModalityResult,
    analyze,
    available_modalities,
    is_available,
    register,
    unregister_all,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    unregister_all()
    yield
    unregister_all()


class _FakeBackend(ModalityBackend):
    """仅用于验证注册/分派契约的示例后端，不做任何真实分析。"""

    name = "fake-for-contract"
    modalities = ("image",)

    def analyze(self, payload: bytes, modality: str, hint: str = "") -> ModalityResult:
        return ModalityResult(
            supported=True,
            modality=modality,
            summary=f"契约测试后端收到 {len(payload)} 字节",
            findings=["仅用于验证分派"],
            backend=self.name,
        )


# ---------------- 无后端时的安全语义 ----------------


def test_no_backend_returns_unsupported_not_fabricated():
    """关键：没有后端时必须明确说不支持，绝不能编造分析结果。"""
    r = analyze(b"some-image-bytes", "image")
    assert r.supported is False
    assert r.backend == "none"
    assert _UNSUPPORTED_MSG in r.summary
    # 不得出现任何"看起来像结论"的内容
    assert not r.findings
    assert "未见异常" not in r.summary and "正常" not in r.summary


def test_unknown_modality_is_unsupported():
    r = analyze(b"x", "pathology")
    assert r.supported is False
    assert r.modality == "pathology"


def test_disclaimer_present_on_every_result():
    """任何结果都必须带免责声明（含不支持的兜底结果）。"""
    r = analyze(b"x", "image")
    assert "不能替代医师诊断" in r.disclaimer


def test_available_modalities_empty_by_default():
    """默认情况下没有任何多模态能力 —— 这是当前真实状态，不要伪装。"""
    assert available_modalities() == []
    assert is_available("image") is False


# ---------------- 注册与分派契约 ----------------


def test_register_enables_dispatch():
    register(_FakeBackend())
    assert is_available("image") is True
    r = analyze(b"12345", "image")
    assert r.supported is True
    assert r.backend == "fake-for-contract"
    assert "5 字节" in r.summary


def test_unregistered_modality_still_unsupported():
    register(_FakeBackend())
    # 只注册了 image，其他类型仍不可用
    assert analyze(b"x", "pathology").supported is False


# ---------------- 后端故障不影响主链路 ----------------


def test_backend_exception_degrades_gracefully():
    class _Boom(ModalityBackend):
        name = "boom"
        modalities = ("image",)

        def analyze(self, payload: bytes, modality: str, hint: str = "") -> ModalityResult:
            raise RuntimeError("backend down")

    register(_Boom())
    r = analyze(b"x", "image")
    # 不得抛异常到主链路，降级为不可用
    assert r.supported is False
    assert "暂时不可用" in r.summary
