import os

from dotenv import load_dotenv

load_dotenv()

LLM_MODE = os.getenv("LLM_MODE", "fake").lower()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
_raw = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS = [o.strip() for o in _raw.split(",") if o.strip()]

# ---- LangSmith 链路追踪（可选；设了 LANGSMITH_TRACING 即自动上报 langgraph 运行） ----
LANGSMITH_TRACING = os.getenv("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes")
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY", "")
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "medical-agent")
if LANGSMITH_TRACING:
    # 兼容新旧环境变量名（LANGSMITH_TRACING / LANGCHAIN_TRACING_V2）
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ.setdefault("LANGCHAIN_PROJECT", LANGSMITH_PROJECT)
    if LANGSMITH_API_KEY:
        os.environ["LANGSMITH_API_KEY"] = LANGSMITH_API_KEY
        os.environ["LANGCHAIN_API_KEY"] = LANGSMITH_API_KEY

# 演示用静态 RBAC token（生产应换成 JWT 校验）
TOKENS = {
    "patient:alice": {"role": "patient", "sub": "alice"},
    "doctor:drwang": {"role": "doctor", "sub": "drwang"},
}
