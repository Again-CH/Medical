"""多模态扩展点（**预留接口，当前没有任何后端接入**）。

诚实边界
--------
本模块**不是**一个能用的影像分析功能。它只做一件事：把「多模态能力」在架构上
预留出一个**有契约、有测试、有明确失败语义**的接入位置。

为什么不直接实现：多模态医疗分析需要 ① 视觉/多模态模型（本机无）、
② 医学影像语料与标注（无）、③ 临床验证与合规审批（远超个人项目范围）。
在缺这三者的情况下写一个"能跑通"的影像分析，产出的只是**看起来像功能的玩具**，
而且它会出现在医疗场景里 —— 那比没有更危险。

这个模块保证的是：当真正的后端就绪时，接入点已经存在、契约已经定义、
失败语义已经确定，**主链路不需要改动**；而在它就绪之前，
系统对多模态请求返回的是明确的「暂不支持」，而不是编造的分析结果。

设计要点
--------
- ``ModalityBackend`` 是抽象契约，任何后端（本地 CLIP/BLIP、云端视觉 API、
  院内 PACS 对接）实现它即可注册。
- ``analyze`` 在无可用后端时返回 ``supported=False`` 与安全引导文案，
  **绝不返回猜测性结论** —— 这与项目「绝不编造诊断」的 Tier-0 红线一致。
- 注册表按 ``modality`` 分派，便于逐步接入（先影像，后病理、超声等）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional

from .logging_config import get_logger

log = get_logger(__name__)


@dataclass
class ModalityResult:
    """多模态分析的统一返回结构。"""

    supported: bool
    modality: str
    summary: str = ""
    findings: list[str] = None  # type: ignore[assignment]
    backend: str = ""
    # 任何面向患者的医学结论都必须附此免责声明
    disclaimer: str = "本结果由算法生成，不能替代医师诊断，请以主诊医生意见为准。"

    def __post_init__(self) -> None:
        if self.findings is None:
            self.findings = []


class ModalityBackend(ABC):
    """多模态后端契约。

    实现类需提供 ``name``、支持的 ``modalities``，以及 ``analyze``。
    任何异常都应由实现内部消化并返回 ``supported=False``，不得抛出到主链路。
    """

    name: str = "abstract"
    modalities: tuple[str, ...] = ()

    @abstractmethod
    def analyze(self, payload: bytes, modality: str, hint: str = "") -> ModalityResult:
        """分析一份多模态数据（如影像字节流）并返回结构化结果。"""


# 注册表：modality → backend。当前为空（无任何后端接入）
_REGISTRY: Dict[str, ModalityBackend] = {}

# 无后端时的安全兜底：明确说「不支持」，绝不编造
_UNSUPPORTED_MSG = (
    "暂不支持该类型的多模态分析（当前未接入影像/病理分析后端）。"
    "如需影像解读，请携带胶片或电子影像前往放射科/专科门诊，由医师出具报告。"
)


def register(backend: ModalityBackend) -> None:
    """注册一个多模态后端（按 modality 分派，同名覆盖）。"""
    for m in backend.modalities:
        _REGISTRY[m] = backend
    log.info(
        "multimodal.registered", extra={"backend": backend.name, "modalities": backend.modalities}
    )


def available_modalities() -> list[str]:
    """当前已接入的多模态类型；无后端时返回空列表。"""
    return sorted(_REGISTRY)


def is_available(modality: str) -> bool:
    return modality in _REGISTRY


def analyze(payload: bytes, modality: str, hint: str = "") -> ModalityResult:
    """统一分析入口。无后端时返回明确的不支持结果（**不猜测**）。"""
    backend: Optional[ModalityBackend] = _REGISTRY.get(modality)
    if backend is None:
        log.info("multimodal.unsupported", extra={"modality": modality})
        return ModalityResult(
            supported=False,
            modality=modality,
            summary=_UNSUPPORTED_MSG,
            backend="none",
        )
    try:
        return backend.analyze(payload, modality, hint)
    except Exception as e:  # noqa: BLE001 - 多模态能力不应拖垮主对话链路
        log.warning(
            "multimodal.backend_failed",
            extra={"modality": modality, "backend": backend.name, "error": type(e).__name__},
        )
        return ModalityResult(
            supported=False,
            modality=modality,
            summary="多模态分析后端暂时不可用，请稍后重试或前往专科门诊。",
            backend=backend.name,
        )


def unregister_all() -> None:
    """清空注册表（测试用）。"""
    _REGISTRY.clear()
