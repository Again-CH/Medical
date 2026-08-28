import json
from typing import Any, ClassVar, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict

from .compose import compose_answer, parse_compose_context
from .config import (
    LLM_MODE,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    QWEN_MODEL,
)


def _messages_to_text(messages):
    parts = []
    for m in messages:
        if isinstance(m, BaseMessage):
            parts.append(f"{m.type}: {m.content}")
        elif isinstance(m, dict):
            parts.append(f"{m.get('role')}: {m.get('content')}")
        else:
            parts.append(str(m))
    return "\n".join(parts)


def _chunk_text(text: str, size: int = 4):
    """把一段文本切成小块，模拟 LLM 的 token 级流式输出。"""
    out = []
    for i in range(0, len(text), size):
        out.append(text[i : i + size])
    return out


class FakeLLM(BaseChatModel):
    """确定性假模型：无需任何外部 API，既支持 token 级流式，也支持 bind_tools 函数调用。

    通过继承 BaseChatModel，FakeLLM 在 fake 模式下也能被 graph.astream_events
    以 on_chat_model_stream 事件捕获，从而让 SSE 真的流式推送 token，而非事后切分。

    函数调用：bind_tools 后，FakeLLM 会根据 intent_hint 在「该子 Agent 命名空间」内
    确定性地返回 tool_calls（首轮）；工具结果回到上下文后，再走 compose 文本汇总。
    这让 fake 模式也能演示「LLM 自主选工具 → ToolNode 执行」的真实 ReAct 范式。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tools: List[Any] = []
    intent_hint: Optional[str] = None
    patient_id_hint: Optional[str] = None
    human_hint: Optional[str] = None

    # fake 模式下：各意图对应的「应调用的工具名」（即该命名空间全部工具）
    FAKE_INTENT_TOOLS: ClassVar[dict] = {
        "triage": ["search_department", "dept_map_rag"],
        "booking": ["query_availability", "lock_appointment", "medicare_settle"],
        "intake": ["read_lab_report", "clinical_kb"],
        "followup": ["read_vitals", "plan_reminder", "memory_append"],
        "emergency": ["handoff", "call_120"],
    }

    # 各工具参数的默认取值（callable 可引用运行时 hint）
    PROP_DEFAULTS: ClassVar[dict] = {
        "symptom": (lambda self: self.human_hint or "头痛"),
        "query": (lambda self: self.human_hint or ""),
        "department": "神经内科",
        "date": "today",
        "slot": "09:30",
        "appointment_id": "APT-1001",
        "patient_id": (lambda self: self.patient_id_hint or "alice"),
        "text": "按时服药、监测血压",
        "note": (lambda self: self.human_hint or ""),
    }

    @property
    def _llm_type(self) -> str:
        return "fake-medical"

    def bind_tools(self, tools, **kwargs):
        new = FakeLLM()
        new.tools = list(tools)
        new.intent_hint = self.intent_hint
        new.patient_id_hint = self.patient_id_hint
        new.human_hint = self.human_hint
        return new

    # ---- 工具选择（fake 模式确定性决策） ----
    def _has_tool_result(self, messages) -> bool:
        return any(getattr(m, "type", None) == "tool" for m in messages)

    def _select_tool_names(self):
        return self.FAKE_INTENT_TOOLS.get(
            self.intent_hint or "triage", [t.name for t in self.tools]
        )

    def _default_arg(self, name):
        v = self.PROP_DEFAULTS.get(name, "")
        return v(self) if callable(v) else v

    def _build_tool_calls(self):
        names = set(self._select_tool_names())
        calls = []
        for t in self.tools:
            if t.name in names:
                # LangChain @tool 的 .args 形如 {param_name: {type, description}}
                # （非 OpenAI 函数的 {properties: {...}} 嵌套），直接用即可
                schema = getattr(t, "args", {}) or {}
                props = schema.get("properties", schema) if isinstance(schema, dict) else {}
                args = {k: self._default_arg(k) for k in props}
                calls.append(
                    {"name": t.name, "args": args, "id": f"call_{t.name}", "type": "tool_call"}
                )
        return calls

    def _compose(self, messages) -> str:
        raw = ""
        for m in messages:
            if isinstance(m, HumanMessage):
                raw = m.content
        intent, tool_result, redline, pid = parse_compose_context(raw)
        return compose_answer(intent, tool_result, redline, pid)

    def _maybe_tool_calls(self, messages):
        """首轮（上下文无 ToolMessage 且已 bind 工具）返回 tool_calls，否则返回 None。"""
        if not self.tools or self._has_tool_result(messages):
            return None
        return self._build_tool_calls()

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        calls = self._maybe_tool_calls(messages)
        if calls:
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))]
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self._compose(messages)))]
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        calls = self._maybe_tool_calls(messages)
        if calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": c["name"], "args": "", "id": c["id"], "index": i}
                        for i, c in enumerate(calls)
                    ],
                )
            )
            return
        for piece in _chunk_text(self._compose(messages)):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        calls = self._maybe_tool_calls(messages)
        if calls:
            # 流式模式下 LangGraph 会用 tool_call_chunks 重建 tool_calls，
            # 因此 args 必须给出「完整 JSON 字符串」（单 chunk 即完整片段）
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": c["name"],
                            "args": json.dumps(c["args"], ensure_ascii=False),
                            "id": c["id"],
                            "index": i,
                        }
                        for i, c in enumerate(calls)
                    ],
                )
            )
            return
        for piece in _chunk_text(self._compose(messages)):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


def get_llm():
    if LLM_MODE == "fake":
        return FakeLLM()
    if LLM_MODE == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(base_url=OLLAMA_BASE_URL, model=OLLAMA_MODEL, streaming=True)
    if LLM_MODE in ("openai", "qwen"):
        from langchain_openai import ChatOpenAI

        base = OPENAI_BASE_URL or "https://api.openai.com/v1"
        model = QWEN_MODEL if LLM_MODE == "qwen" else OPENAI_MODEL
        return ChatOpenAI(model=model, api_key=OPENAI_API_KEY, base_url=base, streaming=True)
    raise ValueError(f"未知 LLM_MODE: {LLM_MODE}")


def _content_of(chunk) -> str:
    # 兼容 ChatGenerationChunk（.message.content）与裸 AIMessageChunk（.content）
    msg = getattr(chunk, "message", None)
    c = getattr(msg, "content", "") if msg is not None else getattr(chunk, "content", "")
    if isinstance(c, list):  # 部分模型返回 content part 列表
        return "".join(getattr(x, "text", str(x)) for x in c)
    return c or ""


async def acompose(llm, messages) -> str:
    """异步流式汇总 LLM 输出为完整文本（节点内使用，同时向 astream_events 暴露 token）。"""
    parts = []
    async for chunk in llm.astream(messages):
        parts.append(_content_of(chunk))
    return "".join(parts)


def compose_text(llm, messages) -> str:
    """同步版汇总（用于非 async 场景兜底）。"""
    parts = [getattr(c, "content", "") for c in llm.stream(messages)]
    return "".join(parts)
