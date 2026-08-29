"""数据库层：SQLAlchemy 2.0 ORM。

设计原则（生产化核心）：
- 代码只认一个连接串 ``DATABASE_URL``；不设置 → 内存 demo 模式（get_hub 返回 MemoryHub）。
- 设置成 ``postgresql+psycopg2://...`` → 真实持久化（生产）。
- 本地开发可用 ``sqlite:///./dev.db`` 跑通同构 SQL，无需起服务即可验证。
- 所有模型集中在 Base 下，schema 版本由 Alembic 管理（``alembic/`` 目录 + ``init_db()``
  调用 ``alembic upgrade head``）；离线/单测可走 sqlite 同构 SQL，生产走 postgres。
"""

from __future__ import annotations

import json
import os
import re as _re
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    mapped_column,
    sessionmaker,
)

from .phi import EncryptedText  # 透明 PHI 列加密（底层仍是 TEXT）

# pgvector 向量类型：仅当 python 包可用时引入（未安装则 demo/sqlite 模式仍能正常 import）。
# 向量表（knowledge_documents）只在启用 Postgres 时由迁移创建，故缺失该包不影响无 DB 路径。
try:
    from pgvector.sqlalchemy import Vector  # type: ignore

    _PGVECTOR_OK = True
except Exception:  # pragma: no cover - 仅环境缺包时触发
    Vector = None
    _PGVECTOR_OK = False

# 向量维度：与 src/embeddings.EMBED_DIM 对齐（默认 384，匹配多语言 MiniLM）。
EMBED_DIM = int(os.getenv("EMBED_DIM", "384"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------- 模型 ----------
class User(Base):
    __tablename__ = "users"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash = mapped_column(String(160), nullable=False)
    full_name = mapped_column(EncryptedText)  # 患者真实姓名（PII，静态加密）
    phone = mapped_column(EncryptedText)  # 患者手机号（直接标识符，静态加密）
    # 安全字段：token_version 用于全局吊销（登出/改密即作废全部令牌）；锁用于防爆破
    token_version = mapped_column(Integer, default=0, nullable=False)
    failed_attempts = mapped_column(Integer, default=0, nullable=False)
    locked_until = mapped_column(DateTime, nullable=True)
    created_at = mapped_column(DateTime, default=utcnow)


class Doctor(Base):
    __tablename__ = "doctors"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash = mapped_column(String(160), nullable=False)
    full_name = mapped_column(String(128), nullable=False)
    title = mapped_column(String(64))  # 职称
    token_version = mapped_column(Integer, default=0, nullable=False)
    failed_attempts = mapped_column(Integer, default=0, nullable=False)
    locked_until = mapped_column(DateTime, nullable=True)
    dept_id = mapped_column(Integer, ForeignKey("departments.id"))
    # 医生归属院区：与其所在科室的租户一致（迁移中由 dept_id 派生回填）
    tenant_id = mapped_column(Integer, ForeignKey("tenants.id"), nullable=False)


class Tenant(Base):
    """多院区 / 租户注册表（见 docs/MULTI_TENANT.md）。

    租户维度覆盖范围（两阶段）：
    - 第一阶段（科室主数据）：``departments`` / ``symptom_dept_map``；
    - 第二阶段（业务主数据，本轮扩展）：``doctors`` / ``doctor_schedules``
      / ``appointments`` / ``exam_steps`` —— 让任何一张表都能直接按
      ``tenant_id`` 过滤，而不必层层 JOIN 推导归属。

    **``users`` 刻意不加 tenant_id**：患者可跨院区就诊，身份应当是集团内全局共享的
    （同一账号在 A 院区和 B 院区都能挂号），若按租户切分会导致跨院区重复建档、
    病历碎片化。预约归属哪個院区由 ``Appointment.tenant_id`` 表达，而非账号本身。
    这是建模判断，不是遗漏。
    """

    __tablename__ = "tenants"
    id = mapped_column(Integer, primary_key=True)
    code = mapped_column(String(32), unique=True, nullable=False)
    name = mapped_column(String(64), nullable=False)
    is_default = mapped_column(Boolean, default=False, nullable=False)
    created_at = mapped_column(DateTime, default=utcnow)


class Department(Base):
    __tablename__ = "departments"
    id = mapped_column(Integer, primary_key=True)
    code = mapped_column(String(32), unique=True, nullable=False)
    name = mapped_column(String(64), nullable=False)
    description = mapped_column(Text)
    tenant_id = mapped_column(Integer, ForeignKey("tenants.id"), nullable=False)


class SymptomDeptMap(Base):
    __tablename__ = "symptom_dept_map"
    id = mapped_column(Integer, primary_key=True)
    keyword = mapped_column(String(64), nullable=False, index=True)
    dept_id = mapped_column(Integer, ForeignKey("departments.id"), nullable=False)
    weight = mapped_column(Integer, default=1)
    tenant_id = mapped_column(Integer, ForeignKey("tenants.id"), nullable=False)


class DoctorSchedule(Base):
    __tablename__ = "doctor_schedules"
    id = mapped_column(Integer, primary_key=True)
    doctor_id = mapped_column(Integer, ForeignKey("doctors.id"), nullable=False)
    work_date = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    period = mapped_column(String(8), nullable=False)  # AM / PM
    total_slots = mapped_column(Integer, default=20)
    booked_slots = mapped_column(Integer, default=0)
    tenant_id = mapped_column(Integer, ForeignKey("tenants.id"), nullable=False)
    __table_args__ = (
        UniqueConstraint("doctor_id", "work_date", "period", name="uq_doc_date_period"),
    )


class Appointment(Base):
    __tablename__ = "appointments"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    doctor_id = mapped_column(Integer, ForeignKey("doctors.id"), nullable=False)
    schedule_id = mapped_column(Integer, ForeignKey("doctor_schedules.id"), nullable=False)
    work_date = mapped_column(String(10))
    period = mapped_column(String(8))
    slot_index = mapped_column(Integer)
    status = mapped_column(String(16), default="LOCKED")
    medicare_settled = mapped_column(Boolean, default=False)
    created_at = mapped_column(DateTime, default=utcnow)
    # 归属院区：本次挂号发生在哪个院区（患者账号本身是集团全局的，故此处显式记录）
    tenant_id = mapped_column(Integer, ForeignKey("tenants.id"), nullable=False)


class Approval(Base):
    __tablename__ = "approvals"
    id = mapped_column(String(64), primary_key=True)
    thread_id = mapped_column(String(128), index=True)
    action = mapped_column(String(64))
    payload = mapped_column(EncryptedText)  # JSON 文本（敏感动作参数，可能含 PHI）
    status = mapped_column(String(16), default="PENDING")
    created_at = mapped_column(DateTime, default=utcnow)
    resolved_at = mapped_column(DateTime)
    resolved_by = mapped_column(String(64))
    decision = mapped_column(Text)  # JSON 文本


# 患者私有档案基类：每患者一个独立 SQLite 库（data/<username>.db），物理隔离，
# 与共享主库（科室/医生/排班/预约）完全分离，从根上杜绝跨患者数据串号。
class PatientBase(DeclarativeBase):
    pass


class ConversationMemory(PatientBase):
    __tablename__ = "conversation_memory"
    id = mapped_column(Integer, primary_key=True)
    thread_id = mapped_column(String(64), index=True)
    patient_id = mapped_column(String(64), nullable=False, index=True)  # 存 username
    key = mapped_column(String(64))
    value = mapped_column(EncryptedText)  # 随访笔记/病例小结（自由文本 PHI）
    created_at = mapped_column(DateTime, default=utcnow)


class LabReport(PatientBase):
    __tablename__ = "lab_reports"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(String(64), nullable=False, index=True)  # 存 username
    item = mapped_column(String(64), nullable=False)
    result = mapped_column(EncryptedText)  # 检验数值（患者健康数据）
    ref_range = mapped_column(String(64))
    abnormal = mapped_column(Boolean, default=False)
    report_date = mapped_column(String(10))


class VitalSign(PatientBase):
    __tablename__ = "vital_signs"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(String(64), nullable=False, index=True)  # 存 username
    type = mapped_column(String(32), nullable=False)
    value = mapped_column(EncryptedText)  # 生命体征读数（患者健康数据）
    unit = mapped_column(String(16))
    measured_at = mapped_column(String(19))


class Reminder(PatientBase):
    __tablename__ = "reminders"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(String(64), nullable=False, index=True)  # 存 username
    content = mapped_column(EncryptedText, nullable=False)  # 提醒内容（可能含健康信息）
    remind_at = mapped_column(String(19))
    channel = mapped_column(String(16), default="APP")
    status = mapped_column(String(16), default="PENDING")
    created_at = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = mapped_column(Integer, primary_key=True)
    actor = mapped_column(String(64))
    action = mapped_column(String(64))
    detail = mapped_column(Text)
    created_at = mapped_column(DateTime, default=utcnow)


class ChatLog(Base):
    """每轮对话请求的审计 / 链路记录（支撑「可回滚查看执行流程」）。

    - ``trace_id`` 关联 LangSmith run，可在链路追踪平台回放完整 LangGraph 执行流程。
    - ``intent`` / ``tool_used`` 记录本轮命中路径：
      chat / human（人工审批中断）/ knowledge_base（知识库直出）/ llm（流式生成）/
      fallback（安全降级）/ timeout（超时兜底）。
    - 与业务表同库（postgres 生产、sqlite 本地/测试，同构 SQL），由 Alembic 版本化管理，
      可随迁移 ``downgrade`` 回滚。
    """

    __tablename__ = "chat_logs"
    id = mapped_column(Integer, primary_key=True)
    trace_id = mapped_column(String(64), index=True)  # 关联 LangSmith run
    patient_id = mapped_column(String(64), index=True)  # 存 username
    thread_id = mapped_column(String(128), index=True)
    intent = mapped_column(String(32), default="chat")  # chat/human/system
    input_text = mapped_column(EncryptedText)  # 患者本轮输入（PHI）
    output_text = mapped_column(EncryptedText, default="")  # 实际推送给患者的文本（可能含 PHI）
    tool_used = mapped_column(String(32), default="final_answer")  # 命中路径
    latency_ms = mapped_column(Integer, default=0)  # 端到端耗时
    fallback = mapped_column(Boolean, default=False)  # 是否安全降级
    created_at = mapped_column(DateTime, default=utcnow)


class Feedback(Base):
    """患者对某轮回答的评价（反馈驱动自我优化的输入）。

    - 只存「哪一轮、好/差、可选文字补充、命中意图」，**不复制对话正文**——
      正文已落在 ``chat_logs``（且经 PHI 脱敏/加密），此处不再二次留存 PHI。
    - ``comment`` 可能含健康信息，故用 ``EncryptedText`` 加密落盘。
    - ``proposed`` 标记该条反馈是否已被纳入知识更新提案，避免同一批反馈
      被反复提案（幂等）。
    - 用途：``src/feedback.py`` 聚合差评 → 生成知识库更新提案 → **走 HITL 审批门**
      → 批准后写入知识库。医疗场景下严禁无人监督的自我改写。
    """

    __tablename__ = "feedback"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(64), index=True, nullable=False)
    thread_id = mapped_column(String(128), index=True)
    trace_id = mapped_column(String(64), index=True)  # 关联 chat_logs / OTel trace
    intent = mapped_column(String(32), default="")  # 命中意图，用于归类知识缺口
    rating = mapped_column(String(8), nullable=False)  # up / down
    comment = mapped_column(EncryptedText)  # 患者补充（可能含健康信息）
    proposed = mapped_column(Boolean, default=False, nullable=False)
    created_at = mapped_column(DateTime, default=utcnow)


class ConsentRecord(Base):
    """用户知情同意书签署记录（Tier-0 法律责任红线）。

    - 首次使用对话服务前必须签署；未签署则网关拦截并返回 consent_required。
    - ``consent_version`` 关联文案版本，文案升级后旧版本用户需重新同意。
    - 与业务表同库、由 Alembic 版本化管理，可随迁移 downgrade 回滚。
    - 仅记录「谁、何时、签了哪版、签了哪些条款、经哪个渠道」，不存敏感健康数据。
    """

    __tablename__ = "consent_records"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(64), index=True, nullable=False)  # 患者账号
    consent_version = mapped_column(String(32), nullable=False)  # 文案版本号
    consent_types = mapped_column(String(256), default="service,scope,data")  # 已同意条款(逗号分隔)
    channel = mapped_column(String(32), default="web")  # 签署渠道
    ip = mapped_column(String(64), default="")  # 签署时客户端 IP（可选）
    agreed_at = mapped_column(DateTime, default=utcnow)


class RefreshToken(Base):
    """刷新令牌存储（仅存哈希值，原始令牌只返回一次给客户端）。

    - 用于 access token 过期后无感续期，且支持按条吊销（登出/盗用处置）。
    - ``token_hash`` = sha256(raw_refresh_token)，即使库被拖库也无法还原原始令牌。
    - 访问令牌本身通过用户 ``token_version`` 字段实现「全局一键吊销」（登出/改密即作废）。
    """

    __tablename__ = "refresh_tokens"
    id = mapped_column(Integer, primary_key=True)
    username = mapped_column(String(64), index=True, nullable=False)
    role = mapped_column(String(16), nullable=False)
    token_hash = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at = mapped_column(DateTime, nullable=False)
    revoked = mapped_column(Boolean, default=False, nullable=False)
    created_at = mapped_column(DateTime, default=utcnow)


class IdempotencyKey(Base):
    """幂等键表（防重试重复写）。

    - 写操作（创建预约 / 发送提醒）以「用户+动作+参数」生成稳定 key；
      同一 key 在 ``expires_at`` 之前重复调用只执行一次，返回首次结果。
    - 重试（网络抖动 / 上游重放）因此不会重复锁号、重复发信。
    - 过期后自动失效，避免无限增长（配合业务重试窗口，默认 1h 足够覆盖重试时序）。
    """

    __tablename__ = "idempotency_keys"
    key = mapped_column(String(255), primary_key=True)
    result_text = mapped_column(Text, nullable=False)  # 首次执行结果（供重复调用直接返回）
    created_at = mapped_column(DateTime, default=utcnow)
    expires_at = mapped_column(DateTime, nullable=False)


class KnowledgeDocument(Base):
    """企业知识库文档（RAG 语料），由 pgvector 做余弦相似度检索。

    - 仅 Postgres 主库生效（embedding 为 ``vector`` 类型）；sqlite/demo 模式不建此表。
    - ``doc_type`` 区分知识类别：dept（科室画像）/ guideline（临床指引）/ enterprise（企业自有知识）。
    - ``embedding`` 由 ``src/embeddings.embed()`` 生成，余弦距离用 pgvector ``<=>`` 算子排序。
    - ``doc_id`` 为业务稳定主键（如 CORPUS id 或企业自定义编码），支持幂等 upsert（重复保存覆盖）。
    """

    __tablename__ = "knowledge_documents"
    doc_id = mapped_column(String(128), primary_key=True)
    doc_type = mapped_column(String(32), nullable=False, default="enterprise", index=True)
    title = mapped_column(String(256), nullable=False)
    tags = mapped_column(Text, nullable=False, default="")  # 逗号分隔关键词
    content = mapped_column(Text, nullable=False)
    source = mapped_column(String(256), nullable=False, default="")
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    created_at = mapped_column(DateTime, default=utcnow)


# 检查项目 → 院区楼宇位置（用于流程报表自动标注「去哪栋楼几楼」）
class ExamStep(Base):
    """主诊医生开具的检查/体检流程单（一张挂号对应一串有序步骤）。

    与预约同属共享主库：由医护开具（写），患者端只读展示。每个步骤自动映射到
    院区楼宇位置（如 验血→B栋2楼 检验科，彩超/CT→A栋3楼 影像科），用于生成
    「体检详细流程报表」，让患者清楚每一步去哪栋楼几楼。
    """

    __tablename__ = "exam_steps"
    id = mapped_column(Integer, primary_key=True)
    patient_username = mapped_column(String(64), nullable=False, index=True)
    appointment_id = mapped_column(Integer, nullable=True)
    seq = mapped_column(Integer, default=0)  # 流程顺序
    step_name = mapped_column(String(64), nullable=False)  # 验血 / 彩超 / CT …
    location = mapped_column(String(64), nullable=False, default="")  # A栋3楼 影像科
    note = mapped_column(EncryptedText)  # 医生备注 / 注意事项（可能含 PHI）
    status = mapped_column(String(16), default="PENDING")  # PENDING / DONE
    created_by = mapped_column(String(64))  # 开单医生 username
    created_at = mapped_column(DateTime, default=utcnow)
    done_at = mapped_column(DateTime)
    tenant_id = mapped_column(Integer, ForeignKey("tenants.id"), nullable=False)


# 检查项目 → 院区楼宇位置（用于流程报表自动标注「去哪栋楼几楼」）
EXAM_LOCATIONS = {
    "验血": "B栋2楼 · 检验科",
    "抽血": "B栋2楼 · 检验科",
    "血常规": "B栋2楼 · 检验科",
    "血生化": "B栋2楼 · 检验科",
    "彩超": "A栋3楼 · 超声科",
    "B超": "A栋3楼 · 超声科",
    "超声": "A栋3楼 · 超声科",
    "CT": "A栋3楼 · 影像科(CT室)",
    "CT平扫": "A栋3楼 · 影像科(CT室)",
    "X光": "A栋3楼 · 影像科(X光室)",
    "DR": "A栋3楼 · 影像科(X光室)",
    "磁共振": "A栋3楼 · 影像科(MRI室)",
    "MRI": "A栋3楼 · 影像科(MRI室)",
    "心电图": "C栋1楼 · 心功能室",
    "动态心电图": "C栋1楼 · 心功能室",
    "核酸": "B栋1楼 · 采样点",
    "胃肠镜": "D栋2楼 · 内镜中心",
    "骨密度": "B栋2楼 · 检验科(骨密度室)",
}

# 常用检查项（供医护工作台下拉与自动补全）
COMMON_EXAM_TYPES = [
    "验血",
    "抽血",
    "血常规",
    "血生化",
    "彩超",
    "B超",
    "超声",
    "CT",
    "X光",
    "DR",
    "磁共振",
    "MRI",
    "心电图",
    "动态心电图",
    "核酸",
    "胃肠镜",
    "骨密度",
]


def resolve_exam_location(name: str) -> str:
    """根据检查项名称解析院区楼宇位置。

    精确匹配 → 子串匹配（如「头颅CT」「空腹抽血」）→ 兜底提示。
    """
    name = (name or "").strip()
    if not name:
        return "位置待定（请联系接诊医生）"
    if name in EXAM_LOCATIONS:
        return EXAM_LOCATIONS[name]
    for key, loc in EXAM_LOCATIONS.items():
        if key in name or name in key:
            return loc
    return "位置待定（请联系接诊医生）"


class EmergencyEvent(PatientBase):
    __tablename__ = "emergency_events"
    id = mapped_column(Integer, primary_key=True)
    patient_id = mapped_column(String(64), index=True)  # 存 username
    content = mapped_column(EncryptedText)  # 紧急事件内容（敏感）
    created_at = mapped_column(DateTime, default=utcnow)


# ---------- 引擎 / 会话（懒加载，导入不建连） ----------
_engines: dict[str, "object"] = {}
_sessions: dict[str, sessionmaker] = {}


def _connect_args(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def get_engine():
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    if url not in _engines:
        _engines[url] = create_engine(url, pool_pre_ping=True, connect_args=_connect_args(url))
    return _engines[url]


def is_db_enabled() -> bool:
    return get_engine() is not None


def get_session() -> Session:
    """返回一个会话上下文管理器（with 使用）。未配置 DATABASE_URL 时抛错。"""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未设置，无法使用数据库（请设置以启用真实持久化，离线 demo 走 MemoryHub）"
        )
    if url not in _sessions:
        _sessions[url] = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _sessions[url]()


# ---------- 幂等键（防重试重复写） ----------
def _naive_now() -> "datetime":
    """naive UTC 当前时间（与 SQLite DateTime 列一致，避免 tz-aware/naive 比较报错）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run_idempotent(key: str, func: "callable", ttl_seconds: int = 3600):
    """带幂等保护的执行：同一 key 在 ttl 内仅执行一次 ``func``，重复调用直接返回首次结果。

    - 未配置数据库（离线 demo）时退化为直接执行 ``func``（无去重，但 MemoryHub 本就不持久化）。
    - ``func`` 执行成功才登记 key；抛异常则不登记，允许上层重试。
    - 返回值为 ``func`` 的返回值（字符串化后落库，重复调用时原样返回）。

    适用：创建预约、发送提醒/短信等副作用写操作，确保网络重试/上游重放不重复生效。
    """
    if not is_db_enabled():
        return func()
    now = _naive_now()
    with get_session() as s:
        row = s.get(IdempotencyKey, key)
        if row is not None and row.expires_at > now:
            return row.result_text
        result = func()
        s.merge(
            IdempotencyKey(
                key=key,
                result_text=str(result),
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
        )
        s.commit()
        return result


# ---------- 每患者独立 SQLite 库（物理隔离，杜绝跨患者串号） ----------

_USER_RE = _re.compile(r"^[A-Za-z0-9_]{1,64}$")
_patient_engines: dict[str, "object"] = {}
_patient_sessions: dict[str, sessionmaker] = {}


def _data_dir() -> str:
    """患者私有库统一存放目录：<项目根>/data/。"""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(repo_root, "data")
    os.makedirs(d, exist_ok=True)
    return d


def _patient_db_path(username: str) -> str:
    if not _USER_RE.match(username or ""):
        raise ValueError(f"非法用户名（仅允许字母/数字/下划线，长度 1-64）：{username!r}")
    return os.path.join(_data_dir(), f"{username}.db")


def get_patient_engine(username: str):
    """懒创建并返回该患者的独立 SQLite 引擎（data/<username>.db）。"""
    if not _USER_RE.match(username or ""):
        raise ValueError(f"非法用户名：{username!r}")
    url = f"sqlite:///{_patient_db_path(username)}"
    if url not in _patient_engines:
        _patient_engines[url] = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
    return _patient_engines[url]


def ensure_patient_db(username: str) -> None:
    """确保该患者的独立库与表结构存在（注册时调用，幂等）。"""
    if not _USER_RE.match(username or ""):
        raise ValueError(f"非法用户名：{username!r}")
    PatientBase.metadata.create_all(get_patient_engine(username))


def get_patient_session(username: str) -> Session:
    """返回绑定到该用户独立库的会话（with 使用）。库/表不存在时自动建。"""
    if not _USER_RE.match(username or ""):
        raise ValueError(f"非法用户名：{username!r}")
    ensure_patient_db(username)
    url = f"sqlite:///{_patient_db_path(username)}"
    if url not in _patient_sessions:
        _patient_sessions[url] = sessionmaker(
            bind=get_patient_engine(username), expire_on_commit=False
        )
    return _patient_sessions[url]()


def _alembic_config() -> "object":
    """构造指向仓库内 alembic.ini 的 Alembic Config。

    真正的连接串由 alembic/env.py 从环境变量 DATABASE_URL 读取，
    因此同一份迁移既能跑 sqlite（本地/测试）也能跑 postgres（生产）。
    """
    from alembic.config import Config

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(repo_root, "alembic.ini"))
    return cfg


def migrate_db() -> None:
    """执行 Alembic 迁移到最新版本（生产级 schema 版本管理）。

    取代原先的 ``Base.metadata.create_all``：现在 schema 有版本号、可回滚、可演进。
    兼容两类库：
    - 全新库：``alembic upgrade head`` 按迁移文件建全部表。
    - 旧库（曾用 create_all 建表、但无 alembic_version 登记）：首次 upgrade 会因
      “表已存在” 报错，此时自动 ``stamp head`` 标记为最新版本（schema 与初始迁移一致），
      后续启动即变为幂等的无操作。
    """
    eng = get_engine()
    if eng is None:
        raise RuntimeError("DATABASE_URL 未设置，无法迁移")

    from alembic import command

    cfg = _alembic_config()
    try:
        command.upgrade(cfg, "head")
    except Exception as e:  # noqa: BLE001
        # 旧库已用 create_all 建过表但未登记版本 → 直接 stamp head，避免重复建表报错
        err = str(e)
        if "already exists" in err or type(e).__name__ == "ProgrammingError":
            try:
                command.stamp(cfg, "head")
                return
            except Exception:  # noqa: BLE001
                pass
        raise


def init_db() -> None:
    """建表（幂等）：执行 Alembic 迁移到最新版本。"""
    migrate_db()


class PendingCall(Base):
    """待人工审批的敏感工具调用缓存（HITL 持久化）。

    LangGraph 的 interrupt() 在 resume 时会把节点从头重跑；若重跑时真实 LLM
    不再生成敏感工具调用，会导致「已批准却没执行」。故把待审批的 tool_calls
    落地到本表，resume 重跑直接读取执行，保证落库确定性，且跨进程/重启不丢。
    """

    __tablename__ = "pending_calls"
    cache_key = mapped_column(String(128), primary_key=True)
    calls = mapped_column(Text, nullable=False)  # 敏感 tool_calls 的 JSON
    created_at = mapped_column(DateTime, default=utcnow)


def set_pending(cache_key: str, calls: list) -> None:
    """持久化待审批的敏感工具调用（覆盖写）。"""
    from sqlalchemy import delete

    eng = get_engine()
    if eng is None:
        raise RuntimeError("DATABASE_URL 未设置，无法持久化 pending")
    with Session(eng) as s:
        s.execute(delete(PendingCall).where(PendingCall.cache_key == cache_key))
        s.add(PendingCall(cache_key=cache_key, calls=json.dumps(calls, ensure_ascii=False)))
        s.commit()


def pop_pending(cache_key: str):
    """取出并删除待审批调用；不存在返回 None。"""
    eng = get_engine()
    if eng is None:
        return None
    with Session(eng) as s:
        row = s.get(PendingCall, cache_key)
        if row is None:
            return None
        calls = json.loads(row.calls)
        s.delete(row)
        s.commit()
        return calls


def clear_pending() -> None:
    eng = get_engine()
    if eng is None:
        return
    from sqlalchemy import delete

    with Session(eng) as s:
        s.execute(delete(PendingCall))
        s.commit()
