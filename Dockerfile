# 生产镜像：基于 python:3.11-slim，安装依赖并启动 FastAPI 网关
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 默认走本地 Ollama；生产可覆盖为 openai/qwen 并填 API key
ENV LLM_MODE=ollama \
    OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    OLLAMA_MODEL=qwen2.5:1.5b \
    DATABASE_URL=postgresql+psycopg2://med_user:med_pass@postgres:5432/med_agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/health'); sys.exit(0)" || exit 1

CMD ["sh", "-c", "python scripts/migrate.py && uvicorn src.gateway:app --host 0.0.0.0 --port 8000"]
