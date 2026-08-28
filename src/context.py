"""请求级上下文：用 contextvars 在图节点 → 工具调用间透传患者/会话标识。

为何用 contextvars：工具函数由 LangGraph 的 ToolNode 异步执行，无法从 state 直接取
patient_id 入参。在 agent 节点进入时把 state["patient_id"] 写入上下文变量，工具内部即可
无感读取，无需改动工具签名（从而 FakeLLM 与评测完全不受影响）。
"""

from contextvars import ContextVar

# 当前请求的患者标识（来自 JWT sub / graph state patient_id）
patient_ctx: ContextVar[str] = ContextVar("patient_ctx", default="anonymous")
# 当前会话/线程标识
thread_ctx: ContextVar[str] = ContextVar("thread_ctx", default="")
