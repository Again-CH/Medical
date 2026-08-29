import asyncio
import hmac
import json
import secrets
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import (
    UserExistsError,
    authenticate,
    bump_token_version,
    change_password,
    consume_refresh_token,
    decode_token,
    get_current_user,
    record_audit,
    register_user,
    require_doctor,
    revoke_refresh_token,
    rotate_refresh_token,
    validate_password_strength,
)
from .config import (
    ACCOUNT_LOCKOUT_MINUTES,
    ADMIN_API_KEY,
    APP_ENV,
    CHAT_TIMEOUT_SECONDS,
    CORS_ORIGINS,
    CSP_STRICT,
    MAX_MESSAGE_LEN,
    METRICS_PUBLIC,
    MIN_PASSWORD_LEN,
    MIN_USERNAME_LEN,
    OTEL_ENABLED,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_SERVICE_NAME,
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_RULES,
    REGISTER_ENABLED,
    TRUST_PROXY,
)
from .cost import cost_breakdown, reset_ledger
from .db import (
    COMMON_EXAM_TYPES,
    EXAM_LOCATIONS,
    ChatLog,
    ConsentRecord,
    ConversationMemory,
    Department,
    Doctor,
    DoctorSchedule,
    ExamStep,
    LabReport,
    Reminder,
    Tenant,
    User,
    VitalSign,
    ensure_patient_db,
    get_patient_session,
    get_session,
    init_db,
    is_db_enabled,
    resolve_exam_location,
    utcnow,
)
from .graph import build_graph, build_pg_checkpointer
from .guard import SAFE_REPLY, check_output, should_flush
from .logging_config import get_logger, new_trace_id
from .masking import mask_ip, mask_phone, mask_pii_text
from .metrics import (
    APPROVAL_WAIT,
    APPROVALS_CREATED,
    APPROVALS_RESOLVED,
    CHAT_DURATION,
    CHAT_FIRST_TOKEN,
    CHAT_TIMEOUTS,
    CHAT_TURNS,
    GUARD_BLOCKS,
    SAFETY_GATE_HITS,
    observe_http,
    set_pending_approvals,
)
from .metrics import (
    render as render_metrics,
)
from .resilience import (
    KILL_SWITCH,
    RESILIENCE_ENABLED,
    all_breakers,
    reset_breakers,
)
from .retention import apply_retention, erase_patient
from .safety import (
    CONSENT_VERSION,
    DISCLAIMER_TEXT,
    SCOPE_STATEMENT,
    assess_emergency,
    assess_scope_violation,
)
from .seed import seed_all
from .store import get_store
from .supervisor import _keyword_intent  # 仅用于指标标签的兜底意图猜测（纯关键词，零成本）
from .tenant import resolve_tenant_id, set_tenant_context
from .tracing import hex_to_trace_id, span
from .tracing import shutdown as shutdown_tracing


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时幂等建表 + 播种：确保 pending_calls / appointments 等业务表存在，
    # 使 HITL 待审批缓存（pending_calls）持久化真正生效。
    if is_db_enabled():
        init_db()
        seed_all()
        # 把内置临床语料灌入 pgvector 知识库，使 RAG 检索走真实向量相似度。
        from . import kb

        seeded = kb.seed_knowledge()
        if seeded:
            log.warning("kb.knowledge_seeded", extra={"count": seeded})
    # 构建异步 Postgres checkpointer（绑定本事件循环），注入 graph 实现会话状态跨重启持久化；
    # 非 Postgres / 连接失败则保持默认内存 checkpointer。
    cp = await build_pg_checkpointer()
    if cp is not None:
        globals()["graph"] = build_graph(checkpointer=cp)
    if OTEL_ENABLED:
        log.warning(
            "tracing.enabled",
            extra={
                "service": OTEL_SERVICE_NAME,
                "endpoint": OTEL_EXPORTER_OTLP_ENDPOINT or "(仅内存，不外发)",
            },
        )
    yield
    shutdown_tracing()


app = FastAPI(title="医疗预约诊疗 Agent", lifespan=lifespan)

log = get_logger()


def _client_ip(request: Request) -> str:
    """解析客户端真实 IP。

    安全要点：``X-Forwarded-For`` 是**客户端可伪造**的请求头。只有在可信反向代理
    之后（``TRUST_PROXY=true``）才采信；否则一律使用 TCP 对端地址。
    无条件信任 XFF 的后果：攻击者每次请求换一个 XFF 即可重置配额令限流形同虚设，
    并可用随机 IP 污染审计与同意书记录。
    """
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if fwd:
            return fwd
    return request.client.host if request.client else "unknown"


# ---------------- 请求模型（统一输入校验） ----------------
class ChatRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=MAX_MESSAGE_LEN,
        description="患者/用户本轮输入，非空且不超过上限",
    )
    thread_id: str | None = Field(default=None, description="会话线程ID，缺省按用户隔离")
    service: str | None = Field(default=None, description="服务模块标识，如 health_chat")


class RegisterRequest(BaseModel):
    """患者自助注册。

    安全要点：**角色固定为 patient**。医护账号一律经 ``POST /admin/doctors``
    （管理员鉴权）开通——放任客户端自选 role 等于把医护权限（读取全部患者目录、
    查看全部对话审计、审批医保结算/120 呼叫）开放给匿名访客。
    """

    username: str = Field(..., min_length=MIN_USERNAME_LEN, max_length=50)
    password: str = Field(..., min_length=MIN_PASSWORD_LEN, max_length=128)
    role: Literal["patient"] = Field(default="patient")
    full_name: str = Field(default="", max_length=100)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=MIN_PASSWORD_LEN, max_length=128)


class CreateDoctorRequest(BaseModel):
    """管理员开通医护账号的请求体（仅 /admin/doctors 接受）。"""

    username: str = Field(..., min_length=MIN_USERNAME_LEN, max_length=50)
    password: str = Field(..., min_length=MIN_PASSWORD_LEN, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=100)
    title: str = Field(default="", max_length=100)


class KnowledgeRequest(BaseModel):
    """保存企业知识的请求体（POST /api/knowledge）。

    doc_id 省略时自动生成；相同 doc_id 为幂等覆盖更新。
    """

    doc_id: str | None = Field(default=None, max_length=128)
    doc_type: str = Field(default="enterprise", max_length=32)
    title: str = Field(..., min_length=1, max_length=256)
    content: str = Field(..., min_length=1, max_length=20000)
    tags: list[str] = Field(default_factory=list)
    source: str = Field(default="enterprise", max_length=256)


class ImportPatientDataRequest(BaseModel):
    """批量导入某患者档案数据的请求体（POST /api/import/patient-data）。

    仅接受已注册患者（不自动建档）。各列表项字段：
    - lab_reports[]: {item, result, ref_range?, abnormal?, report_date?}
    - vital_signs[]: {type, value, unit?, measured_at?}
    - case_summaries[]: {text, category?} 或纯字符串
    """

    patient: str = Field(..., min_length=1, max_length=50)
    lab_reports: list[dict] = Field(default_factory=list)
    vital_signs: list[dict] = Field(default_factory=list)
    case_summaries: list = Field(default_factory=list)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="patient", pattern="^(patient|doctor)$")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10, max_length=1024)


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"] if "*" in CORS_ORIGINS else ["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """轻量内存令牌桶限流（按 IP + 路由前缀）。

    - 单进程有效；多 worker / 多实例部署应替换为 Redis 集中式限流。
    - 规则来自 config.RATE_LIMIT_RULES：{路由前缀: (限额, 窗口秒)}。
    """

    def __init__(self, app, rules: dict, enabled: bool = True):
        super().__init__(app)
        self.rules = rules
        self.enabled = enabled
        self.hits: dict[str, list[float]] = defaultdict(list)
        # 追踪 key 上限：超过即回收过期桶，防止「随机 path/IP」打爆内存
        self.max_tracked_keys = 10_000

    def _evict(self, now: float) -> None:
        """回收已过期（窗口内无任何记录）的限流桶，避免内存无界增长。"""
        max_win = max((w for _, w in self.rules.values()), default=60)
        dead = [k for k, v in self.hits.items() if not v or now - v[-1] >= max_win]
        for k in dead:
            self.hits.pop(k, None)

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)
        path = request.url.path
        rule = next(
            ((lim, win) for pref, (lim, win) in self.rules.items() if path.startswith(pref)), None
        )
        if rule is None:
            return await call_next(request)
        limit, window = rule
        client = _client_ip(request)
        key = f"{client}:{path}"
        now = time.monotonic()
        # 清理窗口外的旧记录并判断是否超限
        bucket = self.hits[key]
        while bucket and now - bucket[0] >= window:
            bucket.pop(0)
        if len(bucket) >= limit:
            retry = int(window - (now - bucket[0])) + 1
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试"},
                headers={"Retry-After": str(retry)},
            )
        bucket.append(now)
        if len(self.hits) > self.max_tracked_keys:
            self._evict(now)
        return await call_next(request)


class MetricsMiddleware(BaseHTTPMiddleware):
    """采集 HTTP 层指标（QPS / 延迟分布 / 状态码）。

    只埋路由模板（``/api/chat``）而非实际路径——带业务 ID 的路径会造成
    高基数（high cardinality），是拖垮 Prometheus 最常见的用法错误。
    """

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        status = 500
        try:
            resp = await call_next(request)
            status = resp.status_code
            return resp
        finally:
            route = request.scope.get("route")
            path_tpl = getattr(route, "path", None) or _normalize_path(request.url.path)
            observe_http(request.method, path_tpl, status, time.monotonic() - start)


def _normalize_path(path: str) -> str:
    """兜底：未匹配到路由时，把明显是 ID 的片段折叠成占位符，避免高基数。"""
    parts = []
    for seg in path.strip("/").split("/"):
        if seg.isdigit() or (len(seg) >= 8 and "-" in seg):
            parts.append("{id}")
        else:
            parts.append(seg)
    return "/" + "/".join(parts)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """统一注入安全响应头（纵深防御，不依赖反向代理也能生效）。

    - CSP：默认仅同源；严格模式下 script 仅放行 'self' 与带 nonce 的内联脚本，
      彻底杜绝 XSS 注入脚本（前端已把内联事件迁移为 data-action 事件委托）。
    - X-Frame-Options/Frame-Ancestors：禁止被 iframe 嵌套（防点击劫持）。
    - X-Content-Type-Options：禁止 MIME 嗅探。
    - Referrer-Policy / Permissions-Policy：最小化信息泄露与敏感 API 暴露。
    - HSTS：仅在 HTTPS 前终止时生效；明文 HTTP 下浏览器忽略，但声明即合规准备。
    - 移除 Server 头，减少指纹暴露。

    nonce 由本中间件按请求生成，供 HTML 页面注入到内联 <script> 标签
    （见 ``_serve_html``），静态 FileResponse 无法做到这一点。
    """

    _CSP = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        # 内联 style 属性迁移成本高且 CSS 注入风险远低于 JS，保留宽松策略
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'nonce-{nonce}'; "
        "connect-src *; "
        "frame-ancestors 'none'"
    )
    # 兼容模式：尚未完成事件委托迁移的前端需要放行内联脚本与内联事件处理器
    _CSP_LEGACY = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src *; "
        "frame-ancestors 'none'"
    )

    async def dispatch(self, request: Request, call_next):
        # 每请求一个 nonce：即使攻击者能注入 HTML，也无法预知 nonce
        request.state.csp_nonce = secrets.token_urlsafe(24)
        resp = await call_next(request)
        h = resp.headers
        nonce = getattr(request.state, "csp_nonce", "")
        csp = self._CSP.format(nonce=nonce) if CSP_STRICT else self._CSP_LEGACY
        h.setdefault("Content-Security-Policy", csp)
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        h.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        if "Server" in h:
            del h["Server"]
        return resp


app.add_middleware(MetricsMiddleware)
app.add_middleware(
    RateLimitMiddleware,
    rules=RATE_LIMIT_RULES,
    enabled=RATE_LIMIT_ENABLED,
)
app.add_middleware(SecurityHeadersMiddleware)


# ---------------- 全局异常处理 ----------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # 结构化上报（含 traceback）而非 print：便于接入告警与检索。
    # 响应体保持通用，绝不回显异常细节（防信息泄露与指纹探测）。
    log.error(
        "http.unhandled",
        extra={"path": request.url.path, "method": request.method, "error": repr(exc)},
        exc_info=True,
    )
    return JSONResponse(status_code=500, content={"detail": "服务内部错误，请稍后重试或联系管理员"})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "输入校验失败", "errors": exc.errors()})


graph = build_graph()


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _extract_interrupt_value(config):
    """从暂停态里取出 interrupt 的载荷（兼容 langgraph 1.x 的 tasks.interrupts）。"""
    try:
        snap = graph.get_state(config)
        for task in getattr(snap, "tasks", ()) or ():
            interrupts = getattr(task, "interrupts", None)
            if interrupts:
                return interrupts[0].value
    except Exception:
        pass
    return None


# ---------------- 审计落库（共享） ----------------


def record_chat_log(
    trace_id: str,
    sub: str,
    thread_id: str,
    message: str,
    output_text: str,
    intent: str,
    tool_used: str,
    fallback: bool,
    start: float,
) -> str:
    """落库本轮对话审计记录（ChatLog），任何异常都不应影响主流程。

    抽离为模块级函数，供正常 LLM 链路与三道硬闸（紧急/同意/定位）复用，
    保证无论走哪条路径，审计链路都完整可回放。
    """
    latency_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "chat.done",
        extra={
            "trace_id": trace_id,
            "user": sub,
            "thread_id": thread_id,
            "turn": intent,
            "tool_used": tool_used,
            "fallback": fallback,
            "latency_ms": latency_ms,
            "emitted_tokens": bool(output_text),
        },
    )
    if is_db_enabled():
        try:
            with get_session() as s:
                # 日志脱敏：落库前对输入输出文本做 PII 掩码，数据库不存原始直接标识符
                s.add(
                    ChatLog(
                        trace_id=trace_id,
                        patient_id=sub,
                        thread_id=thread_id,
                        intent=intent,
                        input_text=mask_pii_text(message),
                        output_text=mask_pii_text(output_text[:4000]),
                        tool_used=tool_used,
                        latency_ms=latency_ms,
                        fallback=fallback,
                    )
                )
                s.commit()
        except Exception:  # noqa: BLE001
            log.warning(
                "chat.log.fail",
                extra={"trace_id": trace_id, "user": sub, "thread_id": thread_id},
            )
    return intent


def _has_consent(sub: str) -> bool:
    """患者是否已签署当前版本的知情同意书。

    - 无 DATABASE_URL（内存演示模式）：无法持久化，视为已同意（演示可继续）。
    - 否则要求存在 username 且 consent_version == 当前版本 的记录。
    """
    if not is_db_enabled():
        return True
    try:
        with get_session() as s:
            return (
                s.query(ConsentRecord)
                .filter(
                    ConsentRecord.username == sub,
                    ConsentRecord.consent_version == CONSENT_VERSION,
                )
                .first()
                is not None
            )
    except Exception:  # noqa: BLE001
        return False


# ---------------- 登录探测防护（防批量锁定型 DoS）----------------
# 攻击者可对大量患者账号各试 5 次错误密码，借「失败锁定」机制把患者锁在系统外，
# 在医疗场景下这等同于拒绝服务。此处对「同一来源涉及过多不同账号」实施封禁。
_LOGIN_PROBE_WINDOW = 900  # 统计窗口 15 分钟
_LOGIN_PROBE_MAX_ACCOUNTS = 5  # 窗口内涉及账号数上限
_IP_BAN_SECONDS = 1800  # 触发后封禁 30 分钟
_login_probe: dict[str, dict] = {}


def _login_probe_ok(ip: str) -> bool:
    rec = _login_probe.get(ip)
    if not rec:
        return True
    return rec.get("banned_until", 0) <= time.time()


def _login_probe_record_failure(ip: str, username: str) -> None:
    now = time.time()
    rec = _login_probe.setdefault(ip, {"accounts": set(), "first": now, "banned_until": 0})
    if now - rec["first"] > _LOGIN_PROBE_WINDOW:
        rec.update({"accounts": set(), "first": now, "banned_until": 0})
    rec["accounts"].add(username)
    if len(rec["accounts"]) >= _LOGIN_PROBE_MAX_ACCOUNTS:
        rec["banned_until"] = now + _IP_BAN_SECONDS
        log.warning(
            "security.login_probe_ban",
            extra={"ip": ip, "distinct_accounts": len(rec["accounts"])},
        )
    # 顺带回收过期条目，防止内存无界增长
    if len(_login_probe) > 10_000:
        for k in [
            k
            for k, v in _login_probe.items()
            if now - v["first"] > _LOGIN_PROBE_WINDOW and v["banned_until"] <= now
        ]:
            _login_probe.pop(k, None)


# ---------------- 认证 ----------------
def _owned_thread_id(user: dict, client_tid: str | None) -> str:
    """服务端派生会话线程 ID：把归属信息固化进 ID。

    安全要点：若直接采信客户端传入的 thread_id，LangGraph checkpointer 会按 ID
    恢复**他人**的会话状态（含历史对话与检验报告），且待审批缓存同样以 thread_id
    为 key，可被抢占。派生后线程 ID 天然携带角色与用户，跨患者访问在寻址层即失效。
    """
    return f"{user['role']}:{user['sub']}:{client_tid or 'default'}"


@app.post("/auth/register")
async def register(req: RegisterRequest):
    if not REGISTER_ENABLED:
        raise HTTPException(status_code=403, detail="注册通道已关闭，请联系管理员开通账号")
    try:
        register_user(req.username, req.password, role=req.role, full_name=req.full_name)
    except UserExistsError as e:
        # 账号冲突是资源状态问题，按 HTTP 语义回 409（客户端应换用户名重试）
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        # 口令强度不足等属客户端可修正问题，回 400
        raise HTTPException(status_code=400, detail=str(e)) from e
    # 为新注册患者建立独立健康档案库（物理隔离，杜绝跨患者串号）
    ensure_patient_db(req.username)
    return {"ok": True, "role": req.role}


@app.post("/auth/change-password")
async def change_pwd(req: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    """修改当前用户密码：校验旧密码 + 强度校验 + 全局吊销已发令牌。"""
    ok, why = change_password(user["sub"], user["role"], req.old_password, req.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=why)
    return {"ok": True, "detail": "密码已更新，其他设备的登录状态已失效"}


@app.post("/admin/doctors")
async def create_doctor(req: Request):
    """管理员开通医护账号（**医护不允许自助注册**）。

    鉴权方式：``X-Admin-Key`` 请求头，与环境变量 ``ADMIN_API_KEY`` 比对（常量时间）。
    生产环境必须显式配置该密钥，未配置则拒绝开通，避免默认口令导致提权。
    每次开通均落审计，可追责到「谁开通了哪个医护账号」。
    """
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="未配置 ADMIN_API_KEY，医护账号开通通道已关闭（生产环境必须显式设置）",
        )
    provided = req.headers.get("x-admin-key", "")
    if not hmac.compare_digest(provided, ADMIN_API_KEY):
        log.warning("security.admin_key_rejected", extra={"path": "/admin/doctors"})
        raise HTTPException(status_code=401, detail="管理员密钥无效")
    try:
        body = await req.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是合法 JSON") from e
    payload = CreateDoctorRequest(**body)

    ok, why = validate_password_strength(payload.password)
    if not ok:
        raise HTTPException(status_code=400, detail=why)
    try:
        register_user(
            payload.username,
            payload.password,
            role="doctor",
            full_name=payload.full_name,
            title=payload.title,
        )
    except UserExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    record_audit("admin", "create_doctor", {"username": payload.username, "env": APP_ENV})
    log.warning("admin.doctor_created", extra={"username": payload.username})
    return {"ok": True, "role": "doctor", "username": payload.username}


def _require_admin_key(req: Request) -> None:
    """校验管理员密钥，支持两种传法：``X-Admin-Key`` 或 ``Authorization: Bearer``。

    双通道的意义：Prometheus 抓取 ``/metrics`` 时无法发送任意自定义头，
    但原生支持 ``authorization.credentials_file``——把同一把 ADMIN_API_KEY 存成
    文件交给 Prometheus 即可安全抓取，不必为抓取而把 ``METRICS_PUBLIC=1`` 公开指标。
    """
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=503, detail="未配置 ADMIN_API_KEY，该通道已关闭（生产必须显式设置）"
        )
    provided = req.headers.get("x-admin-key", "").strip()
    if not provided:
        auth = req.headers.get("authorization", "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    if not hmac.compare_digest(provided, ADMIN_API_KEY):
        log.warning("security.admin_key_rejected", extra={"path": req.url.path})
        raise HTTPException(status_code=401, detail="管理员密钥无效")


# ---------------- 多租户上下文（X-Tenant-Id 头 → 上下文变量）----------------
async def require_tenant_context(request: Request) -> None:
    """把 ``X-Tenant-Id`` 请求头解析为当前租户上下文。

    租户解析优先级：X-Tenant-Id 头（显式）> 默认租户。工具与端点内统一经
    ``resolve_tenant_id()`` 取用，无需手工传参。非法值忽略（回退默认租户）。

    安全约束：租户标识只来自服务端上下文 / 受控请求头，工具 schema 不含
    ``tenant_id`` 入参——prompt injection 无法操纵跨租户读取科室主数据。
    """
    raw = request.headers.get("X-Tenant-Id")
    tid: Optional[int] = None
    if raw:
        try:
            tid = int(raw)
        except (ValueError, TypeError):
            tid = None
    set_tenant_context(tid)


@app.post("/api/admin/tenants")
async def create_tenant(req: Request, _: None = Depends(_require_admin_key)):
    """新建院区 / 租户。"""
    body = await req.json()
    code = (body.get("code") or "").strip()
    name = (body.get("name") or "").strip()
    if not code or not name:
        raise HTTPException(status_code=400, detail="code 与 name 必填")
    with get_session() as s:
        if s.query(Tenant).filter(Tenant.code == code).first():
            raise HTTPException(status_code=409, detail="租户 code 已存在")
        t = Tenant(code=code, name=name, is_default=False)
        s.add(t)
        s.flush()
        tid = t.id
        s.commit()
    return {"ok": True, "id": tid, "code": code, "name": name}


@app.get("/api/admin/tenants")
async def list_tenants(_: None = Depends(_require_admin_key)):
    """列出全部院区 / 租户。"""
    with get_session() as s:
        rows = s.query(Tenant).order_by(Tenant.id).all()
        return [
            {"id": t.id, "code": t.code, "name": t.name, "is_default": t.is_default} for t in rows
        ]


@app.post("/api/admin/departments")
async def create_department(req: Request, _: None = Depends(_require_admin_key)):
    """在指定租户下新建科室（tenant_id 缺省归属当前解析租户）。"""
    body = await req.json()
    code = (body.get("code") or "").strip()
    name = (body.get("name") or "").strip()
    desc = body.get("description") or ""
    tid = body.get("tenant_id")
    if not code or not name:
        raise HTTPException(status_code=400, detail="code 与 name 必填")
    if tid is not None:
        try:
            tid = int(tid)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="tenant_id 非法") from None
    else:
        tid = resolve_tenant_id()
    with get_session() as s:
        if s.query(Tenant).filter(Tenant.id == tid).first() is None:
            raise HTTPException(status_code=400, detail="tenant_id 不存在")
        if s.query(Department).filter(Department.code == code).first():
            raise HTTPException(status_code=409, detail="科室 code 已存在")
        d = Department(code=code, name=name, description=desc, tenant_id=tid)
        s.add(d)
        s.flush()
        did = d.id
        s.commit()
    return {"ok": True, "id": did, "code": code, "name": name, "tenant_id": tid}


@app.post("/api/knowledge")
async def save_knowledge(req: Request):
    """保存/覆盖一条企业知识到向量库（供 RAG 检索）。需 X-Admin-Key。

    请求体见 KnowledgeRequest：doc_id 省略自动生成；相同 doc_id 幂等覆盖。
    写入后 embedding 由 src/embeddings 生成并落库，后续 clinical_kb / dept_map_rag 即可检索命中。
    """
    _require_admin_key(req)
    try:
        body = KnowledgeRequest(**(await req.json()))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"请求体校验失败：{e}") from e
    doc_id = body.doc_id or f"ent-{uuid.uuid4().hex[:12]}"
    from . import kb

    try:
        kb.add_knowledge(doc_id, body.doc_type, body.title, body.content, body.tags, body.source)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    record_audit("admin", "knowledge_upsert", {"doc_id": doc_id, "doc_type": body.doc_type})
    return {"ok": True, "doc_id": doc_id, "doc_type": body.doc_type}


@app.get("/api/knowledge")
async def list_knowledge(req: Request, doc_type: str | None = None):
    """列出知识库条目（不含 embedding）。需 X-Admin-Key。"""
    _require_admin_key(req)
    from .db import KnowledgeDocument, get_session, is_db_enabled

    if not is_db_enabled():
        return {"items": [], "count": 0, "note": "DB 未启用，知识库不可用"}
    with get_session() as s:
        q = s.query(KnowledgeDocument)
        if doc_type:
            q = q.filter(KnowledgeDocument.doc_type == doc_type)
        rows = q.order_by(KnowledgeDocument.created_at).all()
        items = [
            {
                "doc_id": r.doc_id,
                "doc_type": r.doc_type,
                "title": r.title,
                "tags": r.tags,
                "source": r.source,
            }
            for r in rows
        ]
    return {"items": items, "count": len(items)}


@app.post("/api/import/patient-data")
async def import_patient_data(req: Request):
    """批量导入某患者的检验结果/生命体征/病例小结到其私有库。需 X-Admin-Key。

    请求体见 ImportPatientDataRequest。复用 integrations.bulk_import_patient，
    与 scripts/import_patient_data.py 离线脚本共用同一落库逻辑。患者必须已注册。
    """
    _require_admin_key(req)
    from .integrations import bulk_import_patient

    try:
        body = ImportPatientDataRequest(**(await req.json()))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"请求体校验失败：{e}") from e
    try:
        counts = bulk_import_patient(
            body.patient, body.lab_reports, body.vital_signs, body.case_summaries
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    record_audit("admin", "import_patient_data", {"patient": body.patient, **counts})
    return {"ok": True, "patient": body.patient, **counts}


@app.post("/auth/login")
async def login(req: LoginRequest, request: Request):
    src_ip = _client_ip(request)
    if not _login_probe_ok(src_ip):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    res = authenticate(req.username, req.password, req.role)
    if res.locked:
        raise HTTPException(
            status_code=423,
            detail=f"账户已锁定，请 {ACCOUNT_LOCKOUT_MINUTES} 分钟后再试，或联系管理员重置",
        )
    if not res.ok:
        # 记录来源维度的失败探测（防批量锁定患者账号）
        _login_probe_record_failure(src_ip, req.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return res.token_pair


@app.post("/admin/unlock")
async def unlock_account(req: Request):
    """管理员解锁被锁定的账号（缓解锁定型 DoS 的运营处置通道）。

    患者账号被恶意试密码锁定后，需要有带鉴权的自助解锁通道，
    否则只能等 15 分钟或改库——在就医场景下是不可接受的。
    """
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="未配置 ADMIN_API_KEY，解锁通道已关闭")
    provided = req.headers.get("x-admin-key", "")
    if not hmac.compare_digest(provided, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="管理员密钥无效")
    body = await req.json()
    username = (body or {}).get("username", "")
    role = (body or {}).get("role", "patient")
    if not username:
        raise HTTPException(status_code=400, detail="username 必填")
    try:
        with get_session() as s:
            rec = (
                s.query(Doctor).filter(Doctor.username == username).first()
                if role == "doctor"
                else s.query(User).filter(User.username == username).first()
            )
            if rec is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            was_locked = bool(rec.locked_until)
            rec.locked_until = None
            rec.failed_attempts = 0
            s.commit()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="解锁失败") from e
    record_audit("admin", "unlock_account", {"username": username, "was_locked": was_locked})
    return {"ok": True, "username": username, "was_locked": was_locked}


@app.post("/auth/refresh")
async def refresh(req: RefreshRequest):
    """用刷新令牌换取新令牌对（旋转：旧刷新令牌立即吊销）。

    加固点（见 ``auth.rotate_refresh_token``）：校验用户仍存在且未被锁定、
    token_version 未被 bump、以及绝对有效期——防止滑动续期导致令牌无限期有效。
    """
    try:
        payload = decode_token(req.refresh_token, expected_type="refresh")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail="无效的刷新令牌") from e
    if not consume_refresh_token(req.refresh_token, payload["sub"], payload["role"]):
        raise HTTPException(status_code=401, detail="刷新令牌已吊销或过期")
    ok, why, pair = rotate_refresh_token(payload)
    if not ok:
        raise HTTPException(status_code=401, detail=why)
    return pair


@app.post("/auth/logout")
async def logout(req: Request, user: dict = Depends(get_current_user)):
    """登出：全局吊销该用户所有访问令牌 + 吊销本次刷新令牌（并落审计）。"""
    bump_token_version(user["sub"], user["role"])
    try:
        body = await req.json()
        rt = (body or {}).get("refresh_token")
    except Exception:
        rt = None
    if rt:
        revoke_refresh_token(rt)
    record_audit(user["sub"], "logout", {"role": user["role"]})
    return {"ok": True}


# ---------------- 会话（患者） ----------------
@app.post("/api/chat")
async def chat(
    req: ChatRequest,
    user: dict = Depends(get_current_user),
    _: None = Depends(require_tenant_context),
):
    message = req.message
    # 会话线程 ID 由服务端派生（内嵌 role:sub），杜绝跨患者会话越权
    thread_id = _owned_thread_id(user, req.thread_id)
    trace_id = new_trace_id()
    start = time.monotonic()
    sub = user["sub"]
    log.info(
        "chat.start",
        extra={
            "trace_id": trace_id,
            # W3C 32 位格式：可直接粘到 Jaeger / Grafana Tempo 里定位同一次调用
            "otel_trace_id": hex_to_trace_id(trace_id),
            "user": sub,
            "thread_id": thread_id,
            "msg_len": len(message),
        },
    )

    # ===== Tier-0 三道硬闸（确定性，先于一切 LLM 调用） =====

    # 闸门 1：紧急硬闸 —— 胸痛/卒中/大出血等，强制 120 + 就近急诊，绝不交 LLM 发挥
    emg = assess_emergency(message)
    if emg is not None:
        SAFETY_GATE_HITS.labels(gate="emergency").inc()
        log.warning(
            "safety.emergency_gate",
            extra={
                "trace_id": trace_id,
                "user": sub,
                "keyword": emg.keyword,
                "category": emg.category,
            },
        )

        async def _emg_stream():
            record_chat_log(
                trace_id,
                sub,
                thread_id,
                message,
                emg.response,
                "emergency",
                "emergency_gate",
                False,
                start,
            )
            yield _event({"type": "emergency", "text": emg.response})
            yield _event({"type": "disclaimer", "text": DISCLAIMER_TEXT})
            yield _event({"type": "done", "turn": "emergency"})

        return StreamingResponse(_emg_stream(), media_type="text/event-stream")

    # 闸门 2：知情同意 —— 仅患者；未签当前版本同意书则拦截，强制先同意
    if user.get("role") == "patient" and not _has_consent(sub):
        SAFETY_GATE_HITS.labels(gate="consent").inc()

        async def _consent_stream():
            record_chat_log(
                trace_id,
                sub,
                thread_id,
                message,
                "[consent_required]",
                "consent",
                "consent_gate",
                False,
                start,
            )
            yield _event(
                {"type": "consent_required", "detail": SCOPE_STATEMENT, "version": CONSENT_VERSION}
            )
            yield _event({"type": "done", "turn": "consent"})

        return StreamingResponse(_consent_stream(), media_type="text/event-stream")

    # 闸门 3：定位违规 —— 诊断/开处方请求，固定回复「不诊断不开方」
    scope = assess_scope_violation(message)
    if scope is not None:
        SAFETY_GATE_HITS.labels(gate="scope").inc()

        async def _scope_stream():
            record_chat_log(
                trace_id,
                sub,
                thread_id,
                message,
                scope.response,
                "scope",
                "scope_gate",
                False,
                start,
            )
            yield _event({"type": "scope", "text": scope.response})
            yield _event({"type": "disclaimer", "text": DISCLAIMER_TEXT})
            yield _event({"type": "done", "turn": "scope"})

        return StreamingResponse(_scope_stream(), media_type="text/event-stream")

    # metadata.trace_id 会透传到 LangSmith run，便于在链路追踪平台回放完整执行流程
    config = {"configurable": {"thread_id": thread_id}, "metadata": {"trace_id": trace_id}}
    input_state = {
        "messages": [HumanMessage(content=message)],
        "patient_id": sub,
        "tenant_id": resolve_tenant_id(),
    }

    async def _gen_impl():
        emitted_tokens = False
        first_token_at: Optional[float] = None  # 首字节时间（患者体感延迟）
        collected: list[str] = []  # 累积所有推送给患者的文本，用于审计落库
        final_state = None
        out_buf = ""  # 待检测的发送缓冲（句级冲刷，兼顾流式体验与输出安全）
        blocked = False  # 输出护栏是否已触发

        def _persist(turn: str, tool_used: str, fallback: bool) -> str:
            """落库本轮对话审计记录（ChatLog），任何异常都不应影响主流程。"""
            record_chat_log(
                trace_id,
                user["sub"],
                thread_id,
                message,
                "".join(collected),
                turn,
                tool_used,
                fallback,
                start,
            )
            # intent 优先取图执行后的真实结果（真实 LLM 分类），回退到关键词猜测
            intent = "unknown"
            if isinstance(final_state, dict):
                intent = final_state.get("intent") or intent
            if intent == "unknown":
                intent = _keyword_intent(message)
            CHAT_TURNS.labels(intent=intent, turn=turn, tool_used=tool_used or "none").inc()
            CHAT_DURATION.observe(time.monotonic() - start)
            if first_token_at is not None:
                CHAT_FIRST_TOKEN.observe(first_token_at - start)
            return turn

        def _guard(buf: str) -> Optional[str]:
            """输出侧护栏：命中越界模式返回 None（拦截），否则原样放行。"""
            nonlocal blocked
            hit = check_output(buf)
            if hit:
                blocked = True
                GUARD_BLOCKS.inc()
                log.warning(
                    "guard.output_blocked",
                    extra={
                        "trace_id": trace_id,
                        "user": sub,
                        "reason": hit.reason,
                        "sample": hit.sample,
                    },
                )
                return None
            return buf

        async def _emit_piece(piece: str):
            """把一段文本经护栏检测后推送；返回是否成功推送。"""
            nonlocal emitted_tokens
            safe = _guard(piece)
            if safe is None:
                yield _event({"type": "safety_override", "text": SAFE_REPLY, "reason": "guard"})
                return
            nonlocal first_token_at
            yield _event({"type": "token", "text": safe})
            if first_token_at is None:
                first_token_at = time.monotonic()
            emitted_tokens = True

        # 每次对话显著展示免责声明（Tier-0 要求），前端渲染于助手回复下方
        yield _event({"type": "disclaimer", "text": DISCLAIMER_TEXT})

        agen = graph.astream_events(input_state, config, version="v2")
        while True:
            try:
                ev = await asyncio.wait_for(agen.__anext__(), timeout=CHAT_TIMEOUT_SECONDS)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                # LLM/下游卡死：受控中止，返回友好错误而非永久挂起
                CHAT_TIMEOUTS.inc()
                timeout_msg = "⚠️ 响应超时，请稍后重试或联系人工客服。"
                collected.append(timeout_msg)
                log.warning(
                    "chat.timeout",
                    extra={
                        "trace_id": trace_id,
                        "user": user["sub"],
                        "thread_id": thread_id,
                        "latency_ms": int((time.monotonic() - start) * 1000),
                    },
                )
                yield _event({"type": "error", "text": timeout_msg})
                yield _event({"type": "done", "turn": _persist("system", "timeout", True)})
                return
            if ev.get("event") == "on_chat_model_stream":
                # 仅推送 final_answer 节点的 token；子 Agent 内部推理不暴露给患者端
                node = ev.get("metadata", {}).get("langgraph_node")
                if node is not None and node != "final_answer":
                    continue
                tok = getattr(ev["data"]["chunk"], "content", "")
                if isinstance(tok, list):
                    tok = "".join(getattr(x, "text", str(x)) for x in tok)
                if not tok or blocked:
                    continue
                out_buf += tok
                if not should_flush(out_buf):
                    continue
                piece, out_buf = out_buf, ""
                collected.append(piece)
                async for e in _emit_piece(piece):
                    yield e
            # 捕获最终状态（用于非流式直出回复的兜底）
            if ev.get("event") == "on_chain_end":
                node = ev.get("metadata", {}).get("langgraph_node")
                if node == "final_answer":
                    final_state = ev.get("data", {}).get("output")

        # 冲刷残留缓冲（同样必须过护栏，避免尾部内容绕过检测）
        if out_buf and not blocked:
            piece, out_buf = out_buf, ""
            collected.append(piece)
            async for e in _emit_piece(piece):
                yield e

        interrupt_value = _extract_interrupt_value(config)
        if interrupt_value is not None:
            APPROVALS_CREATED.labels(
                action=(interrupt_value or {}).get("action") or "unknown"
            ).inc()
            aid = get_store().create(thread_id, interrupt_value)
            collected.append(json.dumps(interrupt_value, ensure_ascii=False))
            yield _event({"type": "interrupt", "approval_id": aid, "payload": interrupt_value})
            yield _event({"type": "done", "turn": _persist("human", "human_approval", False)})
        elif not emitted_tokens and final_state:
            # 兜底：final_answer 绕过了 LLM 流式（如知识库直出），从状态中提取完整回复推送
            msgs = final_state.get("messages") or []
            ai_content = ""
            for m in reversed(msgs):
                if getattr(m, "type", "") == "ai" and getattr(m, "content", ""):
                    ai_content = m.content
                    break
            if ai_content:
                collected.append(ai_content)
                async for e in _emit_piece(ai_content):
                    yield e
                yield _event(
                    {
                        "type": "done",
                        "turn": _persist(
                            "ai" if not blocked else "system",
                            "knowledge_base" if not blocked else "guard_blocked",
                            False,
                        ),
                    }
                )
            else:
                yield _event({"type": "done", "turn": _persist("system", "fallback", True)})
        else:
            yield _event(
                {
                    "type": "done",
                    "turn": _persist(
                        "ai" if emitted_tokens else "system",
                        "llm" if emitted_tokens else "fallback",
                        not emitted_tokens,
                    ),
                }
            )

    async def gen():
        """外层包一个根 span，把本轮对话的所有子 span 串成一棵树。

        没有这一层，supervisor / agent / tool / llm 的 span 会各自成为孤立的根，
        在追踪平台里看到的是四段互不相干的调用，而不是一次完整问诊。
        """
        with span(
            "chat.turn",
            {"user": sub, "thread_id": thread_id, "trace_id": trace_id},
        ):
            async for ev in _gen_impl():
                yield ev

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- 知情同意（Tier-0 法律责任红线） ----------------
@app.get("/api/consent/status")
async def consent_status(user: dict = Depends(get_current_user)):
    """返回当前用户是否已签署当前版本知情同意书（前端据此决定是否弹强制同意框）。"""
    if user.get("role") != "patient":
        # 医护工作台不在对话服务范围内，无需同意书
        return {"required": False, "version": CONSENT_VERSION}
    return {"required": not _has_consent(user["sub"]), "version": CONSENT_VERSION}


@app.post("/api/consent")
async def consent_agree(req: Request, user: dict = Depends(get_current_user)):
    """患者签署知情同意书（仅患者）。body 可选 {types, channel}。"""
    if user.get("role") != "patient":
        raise HTTPException(status_code=403, detail="仅患者需签署知情同意")
    body: dict = {}
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = {}
    types = body.get("types") or "service,scope,data"
    channel = body.get("channel") or "web"
    # 真实 IP（不再无条件信任 XFF）+ 落库前掩码（最小化个人信息留存）
    ip = mask_ip(_client_ip(req))
    if not is_db_enabled():
        # 内存演示模式无法持久化：仍返回成功，但提示生产必须开启数据库
        return {
            "ok": True,
            "persisted": False,
            "version": CONSENT_VERSION,
            "note": "内存演示模式未持久化同意记录，生产环境请配置 DATABASE_URL",
        }
    with get_session() as s:
        s.add(
            ConsentRecord(
                username=user["sub"],
                consent_version=CONSENT_VERSION,
                consent_types=types,
                channel=channel,
                ip=ip,
            )
        )
        s.commit()
    return {"ok": True, "persisted": True, "version": CONSENT_VERSION}


# ---------------- 审批（医护） ----------------
class ResolveRequest(BaseModel):
    """审批决策：结构化校验，杜绝任意 JSON 透传进 ``Command(resume=...)``。"""

    approval_id: str = Field(..., min_length=1, max_length=64)
    decision: Literal["approve", "reject"] = Field(default="approve")
    comment: str = Field(default="", max_length=500)


@app.get("/api/review/pending")
async def pending(user: dict = Depends(require_doctor)):
    return {"pending": get_store().pending()}


@app.post("/api/review/resolve")
async def resolve(req: ResolveRequest, user: dict = Depends(require_doctor)):
    """医护审批敏感操作（医保结算 / 转诊 / 120 呼叫）。

    安全要点：
    - ``decision`` 经 Pydantic 枚举校验，不再把客户端任意 JSON 直接当作 resume 载荷。
    - 审批人身份（``user['sub']``）写入 ``resolved_by`` 与审计日志，敏感操作可追责。
    - 审批前后完整落审计：谁批准了哪一单、涉及哪些工具与参数。
    """
    rec = get_store().get(req.approval_id)
    if not rec:
        raise HTTPException(status_code=404, detail="approval not found")
    action = (rec.get("payload") or {}).get("action") or "unknown"
    # 审批等待时长：从审批单创建算起，直接反映患者干等多久
    created_at = rec.get("created_at")
    if created_at:
        try:
            APPROVAL_WAIT.observe(max((utcnow() - created_at).total_seconds(), 0))
        except Exception:  # noqa: BLE001 - 指标不得影响主流程
            pass
    APPROVALS_RESOLVED.labels(
        action=action, decision="approve" if req.decision == "approve" else "reject"
    ).inc()
    decision = {"approved": req.decision == "approve", "reason": req.comment}
    thread_id = rec["thread_id"]
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(Command(resume=decision), config)
    get_store().resolve(req.approval_id, decision, actor=user["sub"])
    record_audit(
        user["sub"],
        "approval_resolve",
        {
            "approval_id": req.approval_id,
            "thread_id": thread_id,
            "approved": decision["approved"],
            "action": (rec.get("payload") or {}).get("action"),
            "calls": (rec.get("payload") or {}).get("calls"),
        },
    )
    log.warning(
        "approval.resolved",
        extra={
            "approval_id": req.approval_id,
            "actor": user["sub"],
            "approved": decision["approved"],
        },
    )
    final = result["messages"][-1].content
    return {"approval_id": req.approval_id, "approved": decision["approved"], "result": final}


@app.get("/api/audit")
async def audit(user: dict = Depends(require_doctor)):
    return {"audit": get_store().audit_log()}


@app.get("/api/chat-history")
async def chat_history(thread_id: str = "", user: dict = Depends(get_current_user)):
    """患者端拉取本线程历史对话，用于刷新页面后重渲染聊天框。

    - 仅返回 human/ai 可读气泡（不暴露 tool 内部消息）。
    - thread_id 仍经服务端安全派生（_owned_thread_id），杜绝越权恢复他人会话。
    """
    tid = _owned_thread_id(user, thread_id or None)
    try:
        snap = await graph.aget_state({"configurable": {"thread_id": tid}})
    except Exception:
        return {"messages": []}
    if not snap or not getattr(snap, "values", None):
        return {"messages": []}
    raw = snap.values.get("messages", []) or []
    out = []
    for m in raw:
        cls = type(m).__name__
        if cls == "HumanMessage":
            out.append({"role": "user", "text": _msg_text(m.content)})
        elif cls == "AIMessage":
            if getattr(m, "tool_calls", None):
                continue  # 内部工具调用不展示给患者
            txt = _msg_text(m.content)
            if txt:
                out.append({"role": "bot", "text": txt})
    return {"messages": out}


def _msg_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c)


@app.get("/api/chat-logs")
async def chat_logs(
    patient_id: str = "",
    thread_id: str = "",
    limit: int = 50,
    user: dict = Depends(require_doctor),
):
    """查看对话执行链路（审计回放）。仅医护可访问。

    - 可按 patient_id / thread_id 过滤；按时间倒序返回最近 limit 条。
    - 每条含 trace_id（关联 LangSmith 完整 LangGraph 链路）、intent、tool_used、
      fallback 标志、latency_ms 与输入输出文本，支撑「可回滚查看执行流程」。
    """
    if not is_db_enabled():
        raise HTTPException(status_code=503, detail="审计库未启用（未配置 DATABASE_URL）")
    limit = max(1, min(int(limit), 200))
    from sqlalchemy import desc

    with get_session() as s:
        q = s.query(ChatLog)
        if patient_id:
            q = q.filter(ChatLog.patient_id == patient_id)
        if thread_id:
            q = q.filter(ChatLog.thread_id == thread_id)
        rows = q.order_by(desc(ChatLog.created_at), desc(ChatLog.id)).limit(limit).all()
        return {
            "logs": [
                {
                    "id": r.id,
                    "trace_id": r.trace_id,
                    "patient_id": r.patient_id,
                    "thread_id": r.thread_id,
                    "intent": r.intent,
                    "tool_used": r.tool_used,
                    "fallback": bool(r.fallback),
                    "latency_ms": r.latency_ms,
                    # 数据脱敏：展示层再次脱敏（幂等），覆盖历史未脱敏记录
                    "input_text": mask_pii_text(r.input_text) if r.input_text else r.input_text,
                    "output_text": mask_pii_text(r.output_text) if r.output_text else r.output_text,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        }


@app.get("/health")
async def health():
    db_ok = False
    if is_db_enabled():
        try:
            from sqlalchemy import text

            from .db import get_session

            with get_session() as s:
                s.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False
    return {"status": "ok", "db": "up" if db_ok else "memory"}


@app.get("/metrics")
async def metrics(req: Request):
    """Prometheus 抓取端点。

    默认需 ``X-Admin-Key``：指标会暴露路由清单、版本与流量特征，
    匿名可读等于给攻击者送一份系统地图。内网 / sidecar 抓取场景
    可设 ``METRICS_PUBLIC=1`` 关闭鉴权。
    """
    if not METRICS_PUBLIC:
        _require_admin_key(req)
    set_pending_approvals(_count_pending_approvals())
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


@app.get("/api/admin/resilience")
async def resilience_status(req: Request):
    """查看熔断器状态与 kill switch 清单（运维排障）。需 X-Admin-Key。"""
    _require_admin_key(req)
    breakers = [b.snapshot() for b in all_breakers().values()]
    breakers.sort(key=lambda x: x["name"])
    return {
        "resilience_enabled": RESILIENCE_ENABLED,
        "breakers": breakers,
        "killswitch": {
            "disabled": KILL_SWITCH.list_disabled(),
            "active": len(KILL_SWITCH.list_disabled()),
        },
    }


@app.post("/api/admin/killswitch")
async def killswitch_toggle(req: Request):
    """运行时停用/启用某个工具或意图（``agent:<intent>``）。需 X-Admin-Key。

    请求体示例::

        {"target": "query_availability", "disabled": true}
        {"target": "agent:triage", "disabled": false}

    下游 HIS/短信网关宕机时，运维无需发版即可把流量从故障依赖上摘掉，系统走安全降级。
    """
    _require_admin_key(req)
    try:
        body = await req.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"请求体校验失败：{e}") from e
    target = body.get("target")
    disabled = body.get("disabled")
    if not isinstance(target, str) or not target:
        raise HTTPException(status_code=400, detail="target 必须为非空字符串")
    if not isinstance(disabled, bool):
        raise HTTPException(status_code=400, detail="disabled 必须为布尔值")
    KILL_SWITCH.toggle(target, disabled)
    from .resilience import _sync_killswitch_metric

    _sync_killswitch_metric()
    record_audit("admin", "killswitch_toggle", {"target": target, "disabled": disabled})
    log.warning("resilience.killswitch_toggled", extra={"target": target, "disabled": disabled})
    return {"ok": True, "target": target, "disabled": KILL_SWITCH.is_disabled(target)}


@app.post("/api/admin/breaker/reset")
async def breaker_reset(req: Request):
    """手动复位熔断器（运维确认依赖已恢复后）。需 X-Admin-Key。

    请求体 ``{"name": "llm"}`` 复位指定熔断器；省略 name 则复位全部。
    """
    _require_admin_key(req)
    try:
        body = await req.json()
    except Exception:
        body = {}
    name = body.get("name")
    if name:
        b = all_breakers().get(name)
        if b:
            b.reset()
        result = {"ok": True, "reset": [name]}
    else:
        reset_breakers()
        result = {"ok": True, "reset": "all"}
    return result


@app.get("/api/admin/cost")
async def llm_cost(req: Request, reset: bool = False):
    """LLM 成本归因快照：按患者 / Agent / 模型三维聚合 token 与估算费用。需 X-Admin-Key。

    查询参数 ``?reset=1`` 清空进程内分账 ledger（演示复位用，不影响 Prometheus TSDB 累计）。
    返回结构见 ``src/cost.cost_breakdown``：总量 + by_patient / by_agent / by_model 明细。
    """
    _require_admin_key(req)
    if reset:
        reset_ledger()
    return cost_breakdown()


@app.post("/api/admin/retention")
async def admin_retention(req: Request, dry_run: bool = False):
    """运行 PHI 留存策略（需 X-Admin-Key）。

    默认执行清理；``?dry_run=1`` 只统计不改写（合规审计前的安全预览）。
    返回各作用域处理计数，便于接入 ``phi_purged_total`` 指标与告警。
    """
    _require_admin_key(req)
    body = {}
    try:
        body = await req.json() or {}
    except Exception:  # noqa: BLE001 - 空 body 也允许
        body = {}
    do_dry = dry_run or bool(body.get("dry_run"))
    return apply_retention(dry_run=do_dry)


@app.post("/api/admin/erase")
async def admin_erase(req: Request):
    """管理员触发患者删除权（需 X-Admin-Key）：整体抹除某患者全部可定位数据。

    请求体：``{"username": "...", "confirm": true}``。``confirm`` 必须为 true，
    防止误触。等价于患者自助 ``DELETE /api/patient/me`` 的管理员版本，共享同一完整路径。
    """
    _require_admin_key(req)
    try:
        body = await req.json() or {}
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体需为 JSON") from None
    username = (body.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username 必填")
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="需显式 confirm=true 确认删除")
    try:
        result = erase_patient(username, actor="admin")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {"ok": True, "result": result}


def _count_pending_approvals() -> Optional[int]:
    """待审批积压：DB 不可用时返回 None（Gauge 保持上次值，不写 0 造成假象）。"""
    if not is_db_enabled():
        return None
    try:
        from sqlalchemy import text

        from .db import get_session

        with get_session() as s:
            return (
                s.execute(text("SELECT count(*) FROM approvals WHERE status = 'PENDING'")).scalar()
                or 0
            )
    except Exception:
        return None


# ---------------- 业务数据只读端点（患者端真实数据展示） ----------------
@app.get("/api/departments")
async def departments(tenant: Optional[int] = None, _: None = Depends(require_tenant_context)):
    """科室主数据（按租户隔离，用于智能分诊导航）。?tenant= 可显式覆盖。"""
    tid = resolve_tenant_id(tenant)
    with get_session() as s:
        rows = s.query(Department).filter(Department.tenant_id == tid).order_by(Department.id).all()
        return [
            {
                "code": d.code,
                "name": d.name,
                "description": d.description,
                "tenant_id": d.tenant_id,
            }
            for d in rows
        ]


@app.get("/api/appointments/available")
async def available(
    department: str = "",
    date: str = "",
    tenant: Optional[int] = None,
    user: dict = Depends(get_current_user),
    _: None = Depends(require_tenant_context),
):
    """今日可约号源（真实排班数据）。可传 department 过滤；不传返回全部科室。"""
    from datetime import date as _d

    target = date or _d.today().isoformat()
    tid = resolve_tenant_id(tenant)
    with get_session() as s:
        q = (
            s.query(DoctorSchedule)
            .join(Doctor)
            .join(Department)
            .filter(DoctorSchedule.work_date == target, Department.tenant_id == tid)
        )
        if department:
            q = q.filter(Department.name == department)
        schs = q.order_by(Department.id, DoctorSchedule.period).all()
        slots = []
        for sch in schs:
            doc = s.get(Doctor, sch.doctor_id)
            dept = s.get(Department, doc.dept_id) if doc else None
            remaining = max(0, sch.total_slots - sch.booked_slots)
            slots.append(
                {
                    "doctor": doc.full_name if doc else "",
                    "title": doc.title if doc else "",
                    "department": dept.name if dept else "",
                    "date": sch.work_date,
                    "period": sch.period,
                    "remaining": remaining,
                    "total": sch.total_slots,
                }
            )
        return {"date": target, "slots": slots}


# ---------------- 医护号源管理（仅医生角色） ----------------


@app.get("/api/admin/schedules")
async def list_schedules(
    date: str = "",
    tenant: Optional[int] = None,
    user: dict = Depends(require_doctor),
    _: None = Depends(require_tenant_context),
):
    """获取全部排班（含 ID，供医护编辑名额）。可传 date 过滤，默认今天。"""
    from datetime import date as _d

    target = date or _d.today().isoformat()
    tid = resolve_tenant_id(tenant)
    with get_session() as s:
        q = (
            s.query(DoctorSchedule)
            .join(Doctor)
            .join(Department)
            .filter(DoctorSchedule.work_date >= target, Department.tenant_id == tid)
        )
        if date:
            q = q.filter(DoctorSchedule.work_date == date)
        schs = q.order_by(DoctorSchedule.work_date, Department.id, DoctorSchedule.period).all()
        result = []
        for sch in schs:
            doc = s.get(Doctor, sch.doctor_id)
            dept = s.get(Department, doc.dept_id) if doc else None
            result.append(
                {
                    "id": sch.id,
                    "doctor": doc.full_name if doc else "",
                    "doctor_username": doc.username if doc else "",
                    "title": doc.title if doc else "",
                    "department": dept.name if dept else "",
                    "dept_code": dept.code if dept else "",
                    "date": sch.work_date,
                    "period": sch.period,
                    "total_slots": sch.total_slots,
                    "booked_slots": sch.booked_slots,
                    "remaining": max(0, sch.total_slots - sch.booked_slots),
                }
            )
        return {"schedules": result}


@app.put("/api/admin/schedules")
async def update_schedules(req: Request, user: dict = Depends(require_doctor)):
    """批量更新排班名额。body: { updates: [{id, total_slots}] }"""
    body = await req.json()
    updates = body.get("updates", [])
    if not isinstance(updates, list) or len(updates) == 0:
        raise HTTPException(status_code=400, detail="updates 不能为空")
    with get_session() as s:
        for u in updates:
            sid = u.get("id")
            total = u.get("total_slots")
            if sid is None or total is None:
                continue
            sch = s.query(DoctorSchedule).filter(DoctorSchedule.id == sid).first()
            if sch:
                # 不允许设为低于已预约数
                new_total = max(int(total), sch.booked_slots)
                sch.total_slots = new_total
        s.commit()
    return {"ok": True, "updated": len(updates)}


# ---------------- 医护开具检查流程单（体检详细流程） ----------------


@app.get("/api/doctor/patients")
async def doctor_patients(user: dict = Depends(require_doctor)):
    """医生可开单的患者列表（排除医护账号）。"""
    with get_session() as s:
        doc_usernames = {d.username for d in s.query(Doctor.username).all()}
        q = s.query(User)
        if doc_usernames:
            q = q.filter(~User.username.in_(doc_usernames))
        rows = q.order_by(User.username).all()
        # 数据脱敏：患者目录仅暴露掩码后的手机号，绝不返回明文联系方式
        return [
            {
                "username": u.username,
                "full_name": u.full_name or u.username,
                "phone_masked": mask_phone(u.phone or ""),
            }
            for u in rows
        ]


@app.get("/api/doctor/patient-record")
async def doctor_patient_record(patient: str = "", user: dict = Depends(require_doctor)):
    """医生查看指定患者的病历档案（检验报告 / 生命体征 / 病例小结 / 随访提醒）。

    仅接受本机构已注册、且非医护账号的患者；患者不存在或属医护账号返回 404。
    数据来自该患者独立 SQLite（物理隔离），与共享主库分离。
    """
    patient = (patient or "").strip()
    if not patient:
        raise HTTPException(status_code=400, detail="缺少 patient 参数")
    with get_session() as s:
        row = s.query(User).filter(User.username == patient).first()
        if row is None:
            raise HTTPException(status_code=404, detail="患者不存在或无权限查看")
        # 区分患者与医护账号：在 Doctor 表中即视为医护，不可作为病历查看对象
        doc_usernames = {d.username for d in s.query(Doctor.username).all()}
        if patient in doc_usernames:
            raise HTTPException(status_code=404, detail="患者不存在或无权限查看")

    # 检验结果（按日期升序，便于趋势呈现）
    lab_reports = []
    vital_signs = []
    case_summaries = []
    reminders = []
    try:
        with get_patient_session(patient) as ps:
            lab_rows = (
                ps.query(LabReport)
                .filter(LabReport.patient_id == patient)
                .order_by(LabReport.report_date.asc(), LabReport.id.asc())
                .all()
            )
            lab_reports = [
                {
                    "item": r.item,
                    "result": r.result,
                    "ref_range": r.ref_range,
                    "abnormal": bool(r.abnormal),
                    "report_date": r.report_date,
                }
                for r in lab_rows
            ]
            vital_rows = (
                ps.query(VitalSign)
                .filter(VitalSign.patient_id == patient)
                .order_by(VitalSign.id.desc())
                .all()
            )
            vital_signs = [
                {
                    "type": r.type,
                    "value": r.value,
                    "unit": r.unit,
                    "measured_at": r.measured_at,
                }
                for r in vital_rows
            ]
            case_rows = (
                ps.query(ConversationMemory)
                .filter(
                    ConversationMemory.patient_id == patient,
                    ConversationMemory.key.like("case_summary:%"),
                )
                .order_by(ConversationMemory.id.asc())
                .all()
            )
            case_summaries = [
                {
                    "category": (r.key.split(":", 1)[1] if ":" in r.key else "general"),
                    "text": r.value,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in case_rows
            ]
            rem_rows = (
                ps.query(Reminder)
                .filter(Reminder.patient_id == patient)
                .order_by(Reminder.id.desc())
                .all()
            )
            reminders = [
                {
                    "id": r.id,
                    "content": r.content,
                    "remind_at": r.remind_at,
                    "status": r.status,
                }
                for r in rem_rows
            ]
    except Exception as ex:  # 独立库异常不应拖垮主流程
        log.warning(
            "doctor.patient_record.read_failed", extra={"patient": patient, "err": str(ex)[:160]}
        )

    return {
        "patient": {
            "username": patient,
            "full_name": row.full_name or patient,
            "phone_masked": mask_phone(row.phone or ""),
        },
        "lab_reports": lab_reports,
        "vital_signs": vital_signs,
        "case_summaries": case_summaries,
        "reminders": reminders,
    }


@app.get("/api/exam-types")
async def exam_types(user: dict = Depends(get_current_user)):
    """常用检查项 + 名称→位置映射，供前端下拉与自动补全。"""
    return {"types": COMMON_EXAM_TYPES, "locations": EXAM_LOCATIONS}


@app.post("/api/doctor/exam-orders")
async def create_exam_orders(req: Request, user: dict = Depends(require_doctor)):
    """医生为患者开具检查流程单。body: {patient_username, appointment_id?, steps:[{name,location?,note?}]}。

    每一步自动补全院区楼宇位置（如 验血→B栋2楼 检验科），生成「体检详细流程报表」。
    """
    body = await req.json()
    patient = (body.get("patient_username") or "").strip()
    steps = body.get("steps") or []
    if not patient:
        raise HTTPException(status_code=400, detail="patient_username 必填")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=400, detail="steps 不能为空")
    with get_session() as s:
        u = s.query(User).filter(User.username == patient).first()
        if not u:
            raise HTTPException(status_code=404, detail="患者不存在")
        created = []
        for i, st in enumerate(steps):
            name = (st.get("name") or "").strip()
            if not name:
                continue
            loc = (st.get("location") or "").strip() or resolve_exam_location(name)
            note = st.get("note")
            row = ExamStep(
                patient_username=patient,
                appointment_id=body.get("appointment_id"),
                seq=i,
                step_name=name,
                location=loc,
                note=note,
                status="PENDING",
                created_by=user["sub"],
            )
            s.add(row)
            s.flush()
            created.append(
                {
                    "id": row.id,
                    "seq": row.seq,
                    "name": row.step_name,
                    "location": row.location,
                    "note": row.note,
                    "status": row.status,
                }
            )
        s.commit()
    return {"ok": True, "created": created}


@app.get("/api/doctor/exam-orders")
async def list_exam_orders(patient: str = "", user: dict = Depends(require_doctor)):
    """医生查看某患者的检查流程（按流程顺序返回）。"""
    if not patient:
        raise HTTPException(status_code=400, detail="patient 必填")
    with get_session() as s:
        rows = (
            s.query(ExamStep)
            .filter(ExamStep.patient_username == patient)
            .order_by(ExamStep.seq, ExamStep.id)
            .all()
        )
        return {
            "patient": patient,
            "steps": [
                {
                    "id": r.id,
                    "seq": r.seq,
                    "name": r.step_name,
                    "location": r.location,
                    "note": r.note,
                    "status": r.status,
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "done_at": r.done_at.isoformat() if r.done_at else None,
                }
                for r in rows
            ],
        }


@app.put("/api/doctor/exam-steps/{step_id}")
async def update_exam_step(step_id: int, req: Request, user: dict = Depends(require_doctor)):
    """医生标记某检查步骤完成 / 撤销完成。body: {status: 'PENDING'|'DONE'}。"""
    body = await req.json()
    status = (body.get("status") or "PENDING").upper()
    if status not in ("PENDING", "DONE"):
        raise HTTPException(status_code=400, detail="status 仅支持 PENDING/DONE")
    with get_session() as s:
        r = s.query(ExamStep).filter(ExamStep.id == step_id).first()
        if not r:
            raise HTTPException(status_code=404, detail="step not found")
        r.status = status
        r.done_at = utcnow() if status == "DONE" else None
        s.commit()
        return {
            "ok": True,
            "id": r.id,
            "status": r.status,
            "done_at": r.done_at.isoformat() if r.done_at else None,
        }


@app.get("/api/patient/exam-flow")
async def patient_exam_flow(user: dict = Depends(get_current_user)):
    """当前患者的体检详细流程（按流程顺序），供患者端生成流程报表。"""
    with get_session() as s:
        rows = (
            s.query(ExamStep)
            .filter(ExamStep.patient_username == user["sub"])
            .order_by(ExamStep.seq, ExamStep.id)
            .all()
        )
        steps = [
            {
                "id": r.id,
                "seq": r.seq,
                "name": r.step_name,
                "location": r.location,
                "note": r.note,
                "status": r.status,
                "created_by": r.created_by,
                "done_at": r.done_at.isoformat() if r.done_at else None,
            }
            for r in rows
        ]
        done = sum(1 for x in steps if x["status"] == "DONE")
        return {"patient": user["sub"], "total": len(steps), "done": done, "steps": steps}


# ---------------- 患者数据主体权利（删除权 / 被遗忘权） ----------------
@app.delete("/api/patient/me")
async def delete_my_data(user: dict = Depends(get_current_user)):
    """患者行使删除权（个人信息保护法第 47 条 / GDPR 第 17 条）：整体抹除本人数据。

    委托给 ``src/retention.erase_patient`` 执行与管理员擦除完全一致的完整路径：
    删除独立私有库、主库一切可定位记录（账号/令牌/预约/检查单/审批/对话/知情同意），
    并对含标识符的历史审计日志做盐哈希假名化，保留可追溯性而不留存直接标识符。
    """
    sub = user["sub"]
    result = erase_patient(sub, actor=sub)
    record_audit(
        sub, "patient_data_erased", {"scopes": "all", "pseudonym": result.get("pseudonym")}
    )
    log.warning("privacy.erased", extra={"user": sub, "pseudonym": result.get("pseudonym")})
    return {
        "ok": True,
        "removed_private_db": bool(result.get("patient_db_file")),
        "audit_anonymized_as": result.get("pseudonym"),
        "details": result,
    }


@app.get("/api/reports")
async def reports(user: dict = Depends(get_current_user)):
    """当前患者的检验报告（来自 LIS），仅读取该用户独立库。"""
    with get_patient_session(user["sub"]) as s:
        rows = (
            s.query(LabReport)
            .filter(LabReport.patient_id == user["sub"])
            .order_by(LabReport.id.desc())
            .all()
        )
        return [
            {
                "item": r.item,
                "result": r.result,
                "ref_range": r.ref_range,
                "abnormal": bool(r.abnormal),
                "report_date": r.report_date,
            }
            for r in rows
        ]


@app.get("/api/vitals")
async def vitals(user: dict = Depends(get_current_user)):
    """当前患者的生命体征（随访档案），仅读取该用户独立库。"""
    with get_patient_session(user["sub"]) as s:
        rows = (
            s.query(VitalSign)
            .filter(VitalSign.patient_id == user["sub"])
            .order_by(VitalSign.id.desc())
            .all()
        )
        return [
            {"type": r.type, "value": r.value, "unit": r.unit, "measured_at": r.measured_at}
            for r in rows
        ]


@app.get("/api/reminders")
async def reminders(user: dict = Depends(get_current_user)):
    """当前患者的随访提醒，仅读取该用户独立库。"""
    with get_patient_session(user["sub"]) as s:
        rows = (
            s.query(Reminder)
            .filter(Reminder.patient_id == user["sub"])
            .order_by(Reminder.id.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "content": r.content,
                "remind_at": r.remind_at,
                "channel": r.channel,
                "status": r.status,
            }
            for r in rows
        ]


# ---------------- 前端静态托管（同源，供浏览器演示） ----------------
CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
DIST_DIR = CLIENT_DIR / "dist"
# 是否使用 Vite 构建产物：dist 存在则优先 serving 构建产物，否则回退源码（未构建的开发态）。
USE_DIST = DIST_DIR.exists()


def _client_html(name: str) -> Path:
    """解析前端页面文件：优先 dist 构建产物，回退 client 源码。"""
    dist = DIST_DIR / name
    if USE_DIST and dist.exists():
        return dist
    src = CLIENT_DIR / name
    if not src.exists():
        raise HTTPException(status_code=404, detail="page not found")
    return src


def _serve_html(request: Request, name: str):
    """托管前端页面并注入 CSP nonce。

    严格 CSP（``script-src 'self' 'nonce-xxx'``）要求内联脚本携带 nonce，
    而静态 FileResponse 无法动态改写内容，故在此读取后注入再返回。
    页面名固定为字面量，不接受用户输入，无路径穿越风险。
    构建产物中的入口脚本为同源 ``<script type="module" src="/assets/...">``，
    已被 ``'self'`` 放行，无需 nonce（仅内联 ``<script>`` 需要）。
    """
    path = _client_html(name)
    html = path.read_text(encoding="utf-8")
    nonce = getattr(request.state, "csp_nonce", "")
    if nonce:
        html = html.replace("<script>", f'<script nonce="{nonce}">')
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


@app.get("/")
async def index(request: Request):
    return _serve_html(request, "chat.html")


@app.get("/review")
async def review_page(request: Request):
    return _serve_html(request, "review.html")


# 前端静态资源挂载：构建后由 /assets 提供打包产物；未构建时挂载 /src 以支持源码直跑（本地开发）。
# 二者互斥：生产部署总是先 `npm run build`，故只挂载 /assets。
if USE_DIST and (DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="assets")
elif not USE_DIST and (CLIENT_DIR / "src").exists():
    app.mount("/src", StaticFiles(directory=str(CLIENT_DIR / "src")), name="src")
