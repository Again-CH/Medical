"""多租户（多院区）解析：科室主数据的租户隔离支撑。

隔离边界（本期范围，详见 docs/MULTI_TENANT.md）：
- ``departments`` 与 ``symptom_dept_map`` 按 ``tenant_id`` 隔离；
- 租户解析优先级：``X-Tenant-Id`` 请求头（显式覆盖）> 请求上下文变量 > 默认租户。

为何用 contextvars：工具函数由 LangGraph ToolNode 异步执行，无法从入参取租户。
在请求入口（FastAPI 依赖）把租户写入 ``tenant_ctx``，工具内部 ``resolve_tenant_id()``
无感读取，无需改动工具签名，FakeLLM / 评测路径也不受影响。

安全约束（与 PHI 一致）：租户标识**只来自服务端上下文 / 受控请求头**，
工具 schema 中绝不含 ``tenant_id`` 入参——prompt injection 无法操纵「跨租户读取科室」。
"""

from __future__ import annotations

from typing import Optional

from .context import tenant_ctx
from .db import Tenant, get_session, is_db_enabled

# 默认租户 code（种子与迁移约定一致）。所有历史 / 未指定租户的数据都归于此租户，
# 保证「加租户维度」对既有系统完全向后兼容（零改造即可继续跑）。
DEFAULT_TENANT_CODE = "DEFAULT"
DEFAULT_TENANT_NAME = "默认院区"

# DB 不可用（离线 / 纯内存 demo）时的兜底 id，与迁移写入的默认租户 id 保持一致。
_FALLBACK_DEFAULT_ID = 1


def set_tenant_context(tid: Optional[int]) -> None:
    """设置当前请求的租户上下文。``tid=None`` 表示「未指定 → 走默认租户」。"""
    tenant_ctx.set(tid)


def current_tenant_id() -> Optional[int]:
    """返回当前上下文变量中的租户 id（未设置则为 None）。"""
    v = tenant_ctx.get()
    return v if isinstance(v, int) else None


def default_tenant_id() -> int:
    """返回默认租户 id；DB 不可用时回退到约定值。"""
    if not is_db_enabled():
        return _FALLBACK_DEFAULT_ID
    try:
        with get_session() as s:
            row = s.query(Tenant).filter(Tenant.code == DEFAULT_TENANT_CODE).first()
            return row.id if row else _FALLBACK_DEFAULT_ID
    except Exception:
        return _FALLBACK_DEFAULT_ID


def resolve_tenant_id(override: Optional[int] = None) -> int:
    """决定本次科室读取所用的租户：显式覆盖 > 上下文 > 默认租户。

    所有触碰 ``Department`` / ``SymptomDeptMap`` 的读路径都经此函数，
    从而自动获得租户隔离，无需在每层调用处手工传参。
    """
    tid = override if override is not None else current_tenant_id()
    return tid if tid is not None else default_tenant_id()
