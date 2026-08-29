# 生产镜像：基于 python:3.11-slim，安装依赖并启动 FastAPI 网关
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 以非 root 用户运行：容器被攻破时把影响限制在应用目录内
RUN useradd -m -u 10001 appuser && mkdir -p /app/data && chown -R appuser:appuser /app
COPY --chown=appuser:appuser . .

# 默认走本地 Ollama；生产可覆盖为 openai/qwen 并填 API key
# 注意：LLM_EGRESS_POLICY 默认 strict，外网端点会被拒绝启动（PHI 不出域）
ENV LLM_MODE=ollama \
    OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    OLLAMA_MODEL=qwen2.5:1.5b

# 安全默认值：DATABASE_URL / JWT_SECRET / ADMIN_API_KEY 一律不设默认值，
# 必须由部署方通过环境变量或 Docker Secret 注入（缺失即启动失败，杜绝弱口令）。
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/health'); sys.exit(0)" || exit 1

USER appuser

CMD ["sh", "-c", "python scripts/migrate.py && uvicorn src.gateway:app --host 0.0.0.0 --port 8000"]
