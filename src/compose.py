"""回复组装：把工具结果 + 红线提示合成为面向患者的自然语言回复。

移出 agents.py 是因为 fake 模式下的 LLM(FakeLLM) 也需要在不依赖 agents 模块的前提下
复现同样的组装逻辑，从而让「SSE 直连 LLM token 流式」在 fake 模式下也能真实流式。

设计原则：
- 常见症状（知识库精确匹配）→ 直接用知识库数据生成结构化专业回复，不依赖 LLM 自由发挥
- 未知症状 / 复杂场景 → 走 LLM 组装（带严格格式约束）
"""

import re

INTENT_HEAD = {
    "triage": "【分诊建议】",
    "booking": "【挂号结果】",
    "intake": "【诊前问诊】",
    "followup": "【慢病随访】",
    "emergency": "【应急转诊】",
}

# 清洗 LLM 输出中的 Markdown / 技术标记，确保患者看到的纯是自然语言
_MARKDOWN_RE = re.compile(
    r"(\*\*[^*]+\*\*)"  # **bold**
    r"|(`[^`]+`)"  # `code`
    r"|(^#{1,6}\s+.+$)"  # ## heading
    r"|(\[RAG\])"  # [RAG] 前缀
    r"|(\[search\])"  # [search] 前缀
    r"|(\[工具[^\]]*\])",  # [工具调用已记录...]
    re.MULTILINE,
)


def _strip_markdown(text: str) -> str:
    """去除 Markdown 格式与技术标记，返回纯文本。"""

    def _replace(m):
        g = m.group()
        if g.startswith("**") or g.endswith("**"):
            return g.strip("*")
        if g.startswith("`") and g.endswith("`"):
            return g.strip("`")
        if g.startswith("#"):
            return ""
        if g.startswith("[") and g.endswith("]"):
            return ""
        return g

    text = _MARKDOWN_RE.sub(_replace, text)
    # 清理多余空行（>2 个连续换行压成 2 个）
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── 知识库直出：常见症状 → 结构化专业回复（千问风格）─────────────

# 从 search_health_info 的工具结果中提取结构化知识数据的正则
# 兼容知识库中所有字段标签变体（不同症状用的标签名不完全一致）
_KB_EXTRACTION = re.compile(
    r"症状：[^\n]+\n"
    r"简介：(?P<desc>[^\n]+)\n"
    r"常见原因：(?P<causes>[^\n]+)\n"
    r"(?P<care>[\s\S]*?)(?=建议就诊情况|何时就医|需就医|立即就医|建议就诊)"
    r"(?P<when>[\s\S]*?)(?=预后：|推荐就诊科室)",
    re.MULTILINE,
)


def _format_care_section(care_text: str) -> str:
    """把护理建议的编号列表转为简洁的分段要点（去掉编号前缀）。"""
    lines = care_text.strip().split("\n")
    points = []
    # 跳过的标题行（知识库中各症状用的不同标签）
    skip_prefixes = (
        "家庭护理建议",
        "护理建议",
        "暂时处理",
        "缓解建议",
        "改善建议",
        "护理",
        "缓解",
        "改善",
    )
    for line in lines:
        line = line.strip()
        if not line or any(line.startswith(p) for p in skip_prefixes):
            continue
        # 去掉 "1. " "2. " 等编号
        cleaned = re.sub(r"^\d+\.\s*", "", line).strip()
        if cleaned:
            points.append(cleaned)
    return "\n".join(f"· {p}" for p in points)


def _format_when_section(when_text: str) -> str:
    """把就医时机转为简洁列表。"""
    lines = when_text.strip().split("\n")
    points = []
    # 跳过的标题行
    skip_prefixes = (
        "建议就诊情况",
        "何时就医",
        "需就医",
        "立即就医",
        "建议就诊",
    )
    for line in lines:
        line = line.strip()
        if not line or any(line.startswith(p) for p in skip_prefixes):
            continue
        cleaned = re.sub(r"^[-*·]\s*", "", line).strip()
        if cleaned:
            points.append(cleaned)
    return "\n".join(f"· {p}" for p in points)


def try_format_knowledge_reply(tool_result: str, patient_id: str) -> str | None:
    """尝试从工具结果中提取知识库数据并格式化为千问风格的专业回复。

    返回格式化好的完整回复字符串；若工具结果不含知识库结构数据则返回 None（交由 LLM 组装）。
    """
    # 只处理含 [医学参考信息] 且有完整结构字段的工具结果
    if "[医学参考信息]" not in tool_result or "推荐就诊科室：" not in tool_result:
        return None

    m = _KB_EXTRACTION.search(tool_result)
    if not m:
        return None

    desc = m.group("desc").strip()
    causes_raw = m.group("causes").strip()
    care_raw = m.group("care").strip()
    when_raw = m.group("when").strip()

    # 清理 causes 字段中可能重复的"常见原因"前缀
    causes = re.sub(r"^常见原因[：:]\s*", "", causes_raw)
    causes = re.sub(r"^常见原因包括[：:]\s*", "", causes)

    # 提取推荐科室
    dept_m = re.search(r"推荐就诊科室：([^\n]+)", tool_result)
    dept = dept_m.group(1).strip() if dept_m else ""

    # 提取预后
    prog_m = re.search(r"预后：([^\n]+)", tool_result)
    prognosis = prog_m.group(1).strip() if prog_m else ""

    care_points = _format_care_section(care_raw)
    when_points = _format_when_section(when_raw)

    # 组装千问风格回复：概述（含原因）→ 护理要点 → 就医时机 → 科室推荐
    reply = f"{desc} 常见原因包括：{causes} {prognosis}\n\n"
    reply += f"【护理与调理建议】\n{care_points}\n\n"
    if when_points:
        reply += f"【以下情况建议就医】\n{when_points}\n\n"
    if dept:
        reply += f"📌 建议就诊科室：{dept}"
    reply += "\n\n（本回复供参考，不能替代医生诊断；如有不适请及时就医）"

    return reply


def try_format_hospital_reply(tool_result: str) -> str | None:
    """尝试从工具结果中提取院内资料 RAG 片段，格式化为「院内服务指引」回复。

    返回格式化好的完整回复字符串；若工具结果不含院内资料（[院内资料] 标记）则返回 None。
    """
    if "[院内资料]" not in tool_result:
        return None
    block = tool_result.split("[院内资料]", 1)[1]
    # 提取「· 标题：内容」条目行；过滤掉 search_department 的「建议科室」等无关片段
    items: list[str] = []
    for ln in block.split("\n"):
        ln = ln.strip()
        if ln.startswith("· "):
            items.append(ln)
    if not items:
        # 内存回退可能整段即内容（未命中条目格式），取非科室/非标记行
        for ln in block.split("\n"):
            ln = ln.strip()
            if ln and "建议科室" not in ln and not ln.startswith("["):
                items.append(ln)
    if not items:
        return None
    body = "\n".join(items)
    return body + "\n\n（以上为院内公开资料，具体以现场公示及工作人员告知为准）"


def compose_answer(intent: str, tool_result: str, redline: str, patient_id: str) -> str:
    # 红线优先（合规底线）：命中红线但工具无结果（极端边界）时，仍必须展示急症提示，绝不静默
    if redline and (
        not tool_result or "需要更多信息" in tool_result or "未提供具体症状" in tool_result
    ):
        return _strip_markdown(
            f"【红线提示】{redline}\n\n"
            "您描述的情况可能涉及紧急状况，请优先联系急救（120）或前往最近急诊；"
            "本提示不能替代专业医疗判断。"
        )
    # 空工具结果或模糊输入 → 友好引导回复（不显示分诊标题）
    if not tool_result or "需要更多信息" in tool_result or "未提供具体症状" in tool_result:
        return (
            "您好！我是康宁健康服务助手，很高兴为您服务。\n\n"
            "请问您哪里不舒服？可以详细说说您的症状，比如疼痛部位、持续时间、"
            "是否伴有其他不适等，我来帮您分析并推荐合适的科室。"
        )

    # ★ 院内资料直出：命中 hospital_rag 片段时生成「院内服务指引」回复（医院事务类问题）
    if intent == "triage":
        hosp = try_format_hospital_reply(tool_result)
        if hosp:
            return f"【院内服务指引】\n{hosp}"

    # ★ 常见症状知识库直出：命中结构化数据时直接生成千问风格专业回复（不依赖 LLM 自由发挥）
    if intent == "triage":
        direct = try_format_knowledge_reply(tool_result, patient_id)
        if direct:
            return f"【分诊建议】\n{direct}"

    head = INTENT_HEAD.get(intent, "【回复】")
    if redline:
        head = f"【红线提示】{redline}\n"
    return _strip_markdown(
        f"{head}\n{tool_result}\n\n"
        f"(患者 {patient_id}，本回复由 AI 辅助生成，关键医疗决策以医护为准)"
    )


def parse_compose_context(text: str):
    """从 final_answer 构造的 HumanMessage 文本里还原意图/工具结果/红线/患者。

    final_answer 把结构化上下文化进一行行 `key:value`，FakeLLM 在 fake 模式下据此
    复现同样的回复，保证流式输出与真实模型一致。工具结果可能含多行，需整体捕获。
    """
    intent = "triage"
    tool_result = ""
    redline = ""
    patient_id = "unknown"
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("意图:"):
            intent = line[3:].strip()
        elif line.startswith("红线:"):
            redline = line[3:].strip()
        elif line.startswith("患者:"):
            patient_id = line[3:].strip()
        elif line.startswith("工具结果:"):
            # 多行块：从本行冒号后开始，直到下一个已知 marker（红线/患者/意图）或文本结束
            tr = line[len("工具结果:") :].strip()
            j = i + 1
            while j < len(lines) and not (
                lines[j].startswith("红线:")
                or lines[j].startswith("患者:")
                or lines[j].startswith("意图:")
            ):
                tr += "\n" + lines[j]
                j += 1
            tool_result = tr
            i = j - 1
        i += 1
    return intent, tool_result, redline, patient_id
