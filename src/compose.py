"""回复组装：把工具结果 + 红线提示合成为面向患者的自然语言回复。

移出 agents.py 是因为 fake 模式下的 LLM(FakeLLM) 也需要在不依赖 agents 模块的前提下
复现同样的组装逻辑，从而让「SSE 直连 LLM token 流式」在 fake 模式下也能真实流式。
"""

INTENT_HEAD = {
    "triage": "【分诊建议】",
    "booking": "【挂号结果】",
    "intake": "【诊前问诊】",
    "followup": "【慢病随访】",
    "emergency": "【应急转诊】",
}


def compose_answer(intent: str, tool_result: str, redline: str, patient_id: str) -> str:
    head = INTENT_HEAD.get(intent, "【回复】")
    if redline:
        head = f"【红线提示】{redline}\n"
    return (
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
