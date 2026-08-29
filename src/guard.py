"""输出侧护栏（Output Guardrails）——确定性、不依赖 LLM。

与 ``safety.py`` 的入口硬闸构成**双向设防**：
- 入口闸（safety.py）：拦住「用户要求诊断/开药」这类请求。
- 出口闸（本模块）：拦住「模型自己输出诊断/处方/剂量」这类回复。

为什么必须做：入口闸挡不住模型自发越界。真实模型可能在正常分诊回复中
自行补一句「你这是细菌性感冒，建议服用阿莫西林 500mg」——这类幻觉医疗建议
是本系统最危险的失效模式。护栏用确定性正则在推送患者前拦截，命中即
替换为固定安全话术，绝不把未经校验的模型输出直接交付患者。

设计约束：
- **宁可误伤，不可漏放**：误伤只是多一次安全提示，漏放可能造成实质伤害。
- **避免误伤检验数据**：剂量类模式必须与「服用/每次/每日」等用药语境共现才命中，
  否则检验报告里的 ``CRP 12 mg/L`` 会被误判。
- 命中后**不推送原始文本**（即便只有部分），避免「前半句已发出、后半句被拦」的残缺输出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# 命中后统一返回的安全话术（硬编码，经审阅后固定，不由模型生成）
SAFE_REPLY = (
    "⚠️ 上述内容已由系统安全策略拦截：本服务仅提供健康科普、智能分诊与就医引导，"
    "不提供诊断结论、不开具处方、不推荐具体用药剂量。\n\n"
    "请您携带相关症状描述前往对应科室，由执业医师面诊评估后开具处方；"
    "急危重症请立即拨打 120。"
)


@dataclass(frozen=True)
class GuardHit:
    reason: str  # 命中类别（用于审计/告警）
    sample: str  # 命中的文本片段（脱敏后可入日志）


# （正则, 类别）。顺序无关，任一命中即拦截。
_OUTPUT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 诊断结论
    (re.compile(r"确诊(为|是)?\s*\S{0,12}"), "diagnosis"),
    (re.compile(r"诊断(为|是|结果\s*[:：]?)\s*\S+"), "diagnosis"),
    (re.compile(r"(你|您)(患|得)了\s*\S+"), "diagnosis"),
    (re.compile(r"(临床|最终)?诊断为\s*\S+"), "diagnosis"),
    # 处方 / 开药
    (re.compile(r"处方\s*[:：]?\s*\S+"), "prescription"),
    (re.compile(r"(建议|请|可)?\s*(口服|服用|注射|静脉滴注|肌肉注射)\s*\S+"), "prescription"),
    (re.compile(r"(开|开具)\s*(了)?\s*\S{0,6}(药|处方)"), "prescription"),
    # 具体用药剂量：必须与用药语境共现，避免误伤检验报告数值
    (
        re.compile(
            r"(口服|服用|每次|每日|一天|一次)\s*[^。；\n]{0,16}?\d+(\.\d+)?\s*(mg|g|ml|μg|ug|片|粒|袋|支|IU)"
        ),
        "dosage",
    ),
    (re.compile(r"\d+(\.\d+)?\s*(mg|g|ml|片|粒|袋|支)\s*[/、,，]?\s*(次|日|天|顿)"), "dosage"),
    (
        re.compile(
            r"(每日|一天)\s*[一二三两三四]?\s*次\s*[,，、]?\s*连?[服吃]?\s*\d*\s*\S{0,4}(天|日|周)"
        ),
        "dosage",
    ),
    # 手术/治疗决策
    (re.compile(r"(建议|需要)?\s*(立即|尽快)?\s*(进行|做)\s*\S{0,8}手术"), "treatment_decision"),
]


def check_output(text: str) -> Optional[GuardHit]:
    """检测模型输出是否越界给出诊断/处方/剂量。命中返回 GuardHit，否则 None。"""
    if not text:
        return None
    for pat, reason in _OUTPUT_PATTERNS:
        m = pat.search(text)
        if m:
            return GuardHit(reason=reason, sample=m.group(0)[:60])
    return None


# 句级冲刷阈值：累积到该长度或遇到句末标点即整体检测后推送（延迟极小，肉眼无感）
_FLUSH_MIN_LEN = 24
_SENT_END = ("。", "！", "？", "；", "\n", ".", "!", "?")


def should_flush(buf: str) -> bool:
    """判断缓冲文本是否应冲刷（检测后推送）。"""
    if not buf:
        return False
    if len(buf) >= _FLUSH_MIN_LEN:
        return True
    return buf.endswith(_SENT_END)
