import os
import secrets

from dotenv import load_dotenv

load_dotenv()

# 运行环境：production 下对密钥/安全配置做强校验，缺失即拒绝启动
APP_ENV = os.getenv("APP_ENV", "development").lower()
# JWT 密钥最小长度（字节），低于此值视为不安全
JWT_SECRET_MIN_BYTES = int(os.getenv("JWT_SECRET_MIN_BYTES", "32"))

LLM_MODE = os.getenv("LLM_MODE", "fake").lower()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")

JWT_SECRET = os.getenv("JWT_SECRET", "")

# ---- JWT 时效与身份声明（短时效访问令牌 + 可吊销刷新令牌）----
JWT_ACCESS_EXP_MINUTES = int(os.getenv("JWT_ACCESS_EXP_MINUTES", "15"))  # 访问令牌 15 分钟
JWT_REFRESH_EXP_DAYS = int(os.getenv("JWT_REFRESH_EXP_DAYS", "7"))  # 刷新令牌 7 天
JWT_ISSUER = os.getenv("JWT_ISSUER", "medical-agent")

# ---- 账号防爆破：连续失败锁定 ----
ACCOUNT_LOCKOUT_ATTEMPTS = int(os.getenv("ACCOUNT_LOCKOUT_ATTEMPTS", "5"))  # 5 次失败
ACCOUNT_LOCKOUT_MINUTES = int(os.getenv("ACCOUNT_LOCKOUT_MINUTES", "15"))  # 锁定 15 分钟

# ---- 密码哈希强度（PBKDF2-HMAC-SHA256，零原生依赖）----
PBKDF2_ROUNDS = int(os.getenv("PBKDF2_ROUNDS", "600000"))  # 建议 >= 60 万轮

# ---- CORS：生产环境应显式配置前端域名（逗号分隔），禁止放行 "*" ----
# 未配置时：开发环境宽松放行（便于本地 file:// / localhost 调试），生产环境收紧为空（必须显式配置）
_CORS_RAW = os.getenv("CORS_ORIGINS", "")
CORS_ORIGINS = (
    [o.strip() for o in _CORS_RAW.split(",") if o.strip()]
    if _CORS_RAW
    else (["*"] if APP_ENV != "production" else [])
)

# ---- 速率限制（内存令牌桶，按 IP+路由） ----
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() in ("1", "true", "yes")
# 各路由默认限额：{路由前缀: (最大请求数, 时间窗口秒)}
RATE_LIMIT_RULES = {
    "/auth/login": (10, 60),  # 登录：60秒最多10次，防爆破
    "/auth/register": (5, 60),  # 注册：60秒最多5次
    "/api/chat": (20, 60),  # 对话：60秒最多20次，控 LLM 成本
    "/api/review": (30, 60),  # 医护端：60秒最多30次
}

# ---- 注册开关：生产环境可关闭开放注册（改为邀请码/后台开通） ----
REGISTER_ENABLED = os.getenv("REGISTER_ENABLED", "true").lower() in ("1", "true", "yes")

# ---- 输入约束 ----
MAX_MESSAGE_LEN = int(os.getenv("MAX_MESSAGE_LEN", "2000"))  # 单条对话消息上限
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "8"))  # 密码最小长度
MIN_USERNAME_LEN = int(os.getenv("MIN_USERNAME_LEN", "3"))  # 用户名最小长度
# 密码复杂度：要求同时含字母与数字（弱口令是医疗系统最常见的入侵入口）
PASSWORD_REQUIRE_COMPLEXITY = os.getenv("PASSWORD_REQUIRE_COMPLEXITY", "true").lower() in (
    "1",
    "true",
    "yes",
)

# ---- 对话超时（秒）：单条事件等待超过此值视为 LLM/下游卡死，中止并返回友好错误 ----
CHAT_TIMEOUT_SECONDS = int(os.getenv("CHAT_TIMEOUT_SECONDS", "60"))

# ---- 管理员开通医护账号的共享密钥（bootstrap 方式）----
# 医护账号**绝不允许自助注册**（否则任何人可提权读取全部患者数据与审批敏感操作）。
# 生产环境必须显式设置，未设置时 /admin/doctors 直接拒绝开通。
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# ---- CSP 严格模式：使用 nonce 而非 'unsafe-inline' ----
# true （默认/推荐）：script-src 仅放行 'self' 与带 nonce 的内联脚本，彻底杜绝 XSS 注入脚本。
# false：回退到 'unsafe-inline'（仅用于尚未完成事件委托迁移的旧前端）。
CSP_STRICT = os.getenv("CSP_STRICT", "true").lower() in ("1", "true", "yes")

# ---- 反代信任：仅当部署在可信反向代理之后才置 true，才采信 X-Forwarded-For ----
# 置 false（默认）时限流/审计一律使用 TCP 对端 IP，杜绝伪造 XFF 绕过限流与污染审计。
TRUST_PROXY = os.getenv("TRUST_PROXY", "false").lower() in ("1", "true", "yes")

# ---- 鉴权失败模式：DB 抖动时是否放行 ----
# fail_closed（生产推荐）：吊销校验不可用即拒绝，宁可短暂不可用也不放行已作废令牌。
# fail_open：仅适用于可用性优先且无合规要求的演示环境。
AUTH_FAIL_MODE = os.getenv("AUTH_FAIL_MODE", "fail_closed").lower()

# ---- 刷新令牌绝对有效期（天）：超过后即便一直刷新也必须重新登录 ----
# 防止「滑动续期」导致令牌无限期有效（设备丢失/令牌泄露后无法收敛）。
REFRESH_ABSOLUTE_EXP_DAYS = int(os.getenv("REFRESH_ABSOLUTE_EXP_DAYS", "30"))

# ---- 单患者单日挂号上限：防 Agent 失控循环或恶意囤号耗尽号源 ----
MAX_APPOINTMENTS_PER_DAY = int(os.getenv("MAX_APPOINTMENTS_PER_DAY", "5"))

# ---- PHI 出境策略（医疗数据合规红线） ----
# strict：仅允许本地/私有化端点（ollama 或内网 base_url），配置外网端点即拒绝启动。
# masked：允许出境，但出境前对 prompt 做 PII/PHI 脱敏。
# allow ：原样出境（仅限无真实患者数据的本地开发）。
LLM_EGRESS_POLICY = os.getenv("LLM_EGRESS_POLICY", "strict").lower()
# 私有化/内网端点白名单（host 片段，逗号分隔），strict 模式下命中即视为合规出境
LLM_PRIVATE_HOSTS = [
    h.strip().lower()
    for h in os.getenv("LLM_PRIVATE_HOSTS", "localhost,127.0.0.1,host.docker.internal,::1").split(
        ","
    )
    if h.strip()
]


def _resolve_jwt_secret() -> str:
    """解析并校验 JWT 密钥。

    - 生产环境：密钥必须显式配置且长度达标，否则直接拒绝启动（安全风险）。
    - 非生产环境：未配置则临时生成一次性强随机密钥（仅本地/测试用，重启失效）；
      长度不足仅告警，不阻断，方便开发。
    """
    global JWT_SECRET
    if not JWT_SECRET:
        if APP_ENV == "production":
            raise RuntimeError(
                "安全启动校验失败：生产环境必须显式设置 JWT_SECRET（>=%d 字节）。"
                % JWT_SECRET_MIN_BYTES
            )
        # 开发/测试：生成临时强密钥，避免硬编码弱默认值被误用于生产
        JWT_SECRET = secrets.token_urlsafe(32)
        print("[config] 未检测到 JWT_SECRET，已生成临时开发密钥（重启失效，请勿用于生产）")
        return JWT_SECRET
    # 已显式配置：校验强度
    nbytes = len(JWT_SECRET.encode("utf-8"))
    if nbytes < JWT_SECRET_MIN_BYTES:
        if APP_ENV == "production":
            raise RuntimeError(
                "安全启动校验失败：JWT_SECRET 长度 %d < %d 字节，生产环境禁止启动。"
                % (nbytes, JWT_SECRET_MIN_BYTES)
            )
        print(
            "[config] ⚠️ 安全告警：JWT_SECRET 仅 %d 字节（< %d），存在被暴力破解风险，"
            "请在生产环境使用 secrets.token_urlsafe(32) 生成强密钥。"
            % (nbytes, JWT_SECRET_MIN_BYTES)
        )
    return JWT_SECRET


_resolve_jwt_secret()

# ---- LangSmith 链路追踪（可回滚查看执行流程的核心）----
# 设了 LANGSMITH_API_KEY 即自动开启（无需额外设 LANGSMITH_TRACING=true），
# 每次对话的 supervisor→子Agent→工具→final_answer 全链路自动上报，可在 LangSmith 回放。
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY", "")
LANGSMITH_TRACING = os.getenv("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes") or bool(
    LANGSMITH_API_KEY
)
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "medical-agent")
if LANGSMITH_TRACING:
    # 兼容新旧环境变量名（LANGSMITH_TRACING / LANGCHAIN_TRACING_V2）
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ.setdefault("LANGCHAIN_PROJECT", LANGSMITH_PROJECT)
    if LANGSMITH_API_KEY:
        os.environ["LANGSMITH_API_KEY"] = LANGSMITH_API_KEY
        os.environ["LANGCHAIN_API_KEY"] = LANGSMITH_API_KEY
