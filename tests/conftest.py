"""测试全局 fixture：让全部测试跑在真实数据库上，验证真实落地路径。

两条运行路径（CI 各占一个 job，本地两者皆可）：

1. **默认 sqlite**：不设 ``DATABASE_URL``，conftest 每会话删除并重建 ``test.db``，
   天然干净、可无限重复执行，无需任何外部服务。
2. **Postgres**：设 ``DATABASE_URL`` 指向一个库（CI 的 integration job 用
   ``postgres:16`` service container，每次全新）。此时库不会被销毁重建，
   改由 :func:`_clean_volatile_state` 在每个测试前清理易变状态。

为什么需要 ``_clean_volatile_state``
------------------------------------
本项目有三类「运行时状态」会被上一个测试留下，从而让下一个测试假失败：

- ``idempotency_keys``：挂号/提醒的幂等缓存（TTL 1h）。同一句话第二次跑会命中缓存
  直接返回首次结果、不落库 → e2e「批准后未真实落库」；
- ``appointments`` + ``doctor_schedules.booked_slots``：号源被占满后锁号必然失败，
  且 ``MAX_APPOINTMENTS_PER_DAY`` 会拦住新预约；
- ``users.failed_attempts`` / ``locked_until``：防爆破锁定跨用例累积 → 登录返回 423；
- ``users.token_version`` / ``doctors.token_version``：登出或改密会 bump 它做全局吊销，
  而网关测试用 ``create_token()`` 签发的令牌固定带 ``tv=0``，库里若已 bump 过就会全部 401。

只清这四类**可重建的运行时状态**，绝不动核心资产（账号本身 / audit_logs /
knowledge_documents / PHI 患者私有库）。

安全护栏
--------
指向非 sqlite 库时必须显式设 ``MC_TEST_DB=1``，否则直接 fail-fast——
避免有人无意间把测试跑在含业务数据的库上（即便是上述"可重建"状态也不该被动）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 在所有测试模块导入 src 之前设置，确保 get_engine() 读到同一份 DATABASE_URL
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
# 强制 fake 模型：测试必须 hermetic、确定性，不依赖 .env 里的真实 LLM（openai 模式会烧 API）
# load_dotenv 默认不覆盖已存在的环境变量，故在此先钉死 LLM_MODE=fake
os.environ["LLM_MODE"] = "fake"

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from src.db import get_session, init_db  # noqa: E402
from src.seed import seed_all  # noqa: E402


def _clean_volatile_state() -> None:
    """清掉会被上一个测试污染的运行时状态（见模块 docstring）。"""
    with get_session() as s:
        s.execute(text("DELETE FROM idempotency_keys"))
        s.execute(text("DELETE FROM appointments"))
        s.execute(text("UPDATE doctor_schedules SET booked_slots = 0"))
        s.execute(
            text("UPDATE users SET failed_attempts = 0, locked_until = NULL, token_version = 0")
        )
        s.execute(
            text("UPDATE doctors SET failed_attempts = 0, locked_until = NULL, token_version = 0")
        )
        s.commit()


@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    url = os.environ.get("DATABASE_URL", "")
    if url and not url.startswith("sqlite") and os.getenv("MC_TEST_DB") != "1":
        pytest.exit(
            "\n[conftest] DATABASE_URL 指向非 sqlite 库："
            f"{url.split('@')[-1]}\n"
            "          测试会重置运行时状态（预约 / 幂等键 / 账号锁定 / token_version）。\n"
            "          确认这是专用测试库后，请设 MC_TEST_DB=1 再跑；\n"
            "          或去掉 DATABASE_URL，用默认临时 sqlite（推荐，无需任何外部服务）。",
            returncode=2,
        )
    if os.path.exists("test.db"):
        os.remove("test.db")
    init_db()
    seed_all()
    yield
    if os.path.exists("test.db"):
        os.remove("test.db")


@pytest.fixture(autouse=True)
def _reset_volatile_state():
    """每个测试前重置易变状态，保证测试可任意顺序、任意次数重复执行。"""
    _clean_volatile_state()
    yield
