"""敏感信息脱敏工具（PII / PHI 最小化）。

设计原则（合规导向）：
- 默认**不存储原始直接标识符**：手机号、身份证、邮箱等在落库/展示前即被掩码，
  降低拖库/日志泄露导致的合规风险（《个人信息保护法》最小必要原则）。
- 脱敏是**幂等**的：对已脱敏文本再次脱敏结果不变，可在写层与读层双重应用（纵深防御）。
- **保留临床价值**：仅掩码直接标识符，不改动症状/诊断/科室等医疗文本，
  确保医生回放对话链路时仍能获得有效信息。
- 不依赖 LLM：纯正则，毫秒级、确定性，可用于网关入口与审计落库路径。

典型用法：
- 日志脱敏：``record_chat_log`` 落库前 ``mask_pii_text(message)``。
- 数据脱敏：``/api/chat-logs``、``/api/doctor/patients`` 等跨角色展示前对字段脱敏。
"""

from __future__ import annotations

import re

_MASK = "***"  # 身份证掩码用 6 个星（见 mask_pii_text）
_PHONE_STARS = "****"  # 手机号中间固定 4 星
_NAME_STARS = "**"  # 姓名隐藏部分固定 2 星

# 中国大陆手机号：1[3-9] 开头共 11 位，前后非数字（避免误伤长编号）
_CN_MOBILE = re.compile(r"(?<![\d])(1[3-9]\d{9})(?![\d])")
# 身份证：17 位数字 + 校验位（数字或 X）
_ID_CARD = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
# 邮箱
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 座机/固话：区号(0 开头 3-4 位) + 7-8 位号码，可带连字符
_PHONE_GENERIC = re.compile(r"(?<!\d)(0\d{2,3}-?\d{7,8})(?!\d)")


def mask_phone(phone: str) -> str:
    """手机号掩码：保留前 3 后 4，中间 4 星（如 13812345678 → 138****5678）。"""
    if not phone:
        return phone
    digits = re.sub(r"\D", "", phone)
    if len(digits) >= 7:
        return f"{digits[:3]}{_PHONE_STARS}{digits[-4:]}"
    if digits:
        return _PHONE_STARS
    return phone


def mask_name(name: str) -> str:
    """姓名掩码：保留姓氏，其余打 2 星（如「张伟」→「张**」）。"""
    if not name:
        return name
    name = name.strip()
    if len(name) <= 1:
        return _NAME_STARS
    return name[0] + _NAME_STARS


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{_MASK}@{domain}"
    return f"{local[:2]}{_MASK}@{domain}"


def mask_pii_text(text: str) -> str:
    """对自由文本中的直接标识符（邮箱/手机号/身份证/固话）做掩码，保留其余内容。

    幂等：对已是脱敏结果的文本再次调用不会引入新的变化。
    """
    if not text:
        return text
    text = _EMAIL.sub(lambda m: _mask_email(m.group(0)), text)
    text = _CN_MOBILE.sub(lambda m: mask_phone(m.group(1)), text)
    text = _ID_CARD.sub(_MASK * 6, text)
    text = _PHONE_GENERIC.sub(lambda m: mask_phone(m.group(1)), text)
    return text


def mask_ip(ip: str) -> str:
    """IP 掩码：IPv4 保留前三段、IPv6 保留前四组，其余置零。

    IP 属个人信息（可识别到户/设备），落库应最小化。保留网段既满足
    「证明签署来源网段」的举证需要，又避免长期留存可直接定位的完整地址。
    """
    if not ip:
        return ""
    ip = ip.strip()
    if ":" in ip:  # IPv6：保留前四组
        parts = ip.split(":")
        return ":".join(parts[:4] + ["0"] * max(0, len(parts) - 4))
    if "." in ip:  # IPv4：保留前三段
        parts = ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0"
    return ""


def mask_pii(obj):
    """递归对 str/dict/list 中的字符串脱敏（用于 API 响应体统一收口）。"""
    if isinstance(obj, str):
        return mask_pii_text(obj)
    if isinstance(obj, dict):
        return {k: mask_pii(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_pii(v) for v in obj]
    return obj
