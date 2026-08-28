"""测试全局 fixture：让全部测试跑在真实数据库（sqlite 同构 SQL）上，验证真实落地路径。

- 统一设置 DATABASE_URL=sqlite:///./test.db，使 get_hub() 返回 DbHub、store 返回 ORM 存储，
  从而 graph / gateway 测试都覆盖到真实持久化逻辑，而非内存 demo。
- 每个测试会话重建库并播种演示数据（科室/医生/排班/患者/检验/生命体征）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 在所有测试模块导入 src 之前设置，确保 get_engine() 读到同一份 DATABASE_URL
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

import pytest  # noqa: E402
from src.db import init_db  # noqa: E402
from src.seed import seed_all  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    if os.path.exists("test.db"):
        os.remove("test.db")
    init_db()
    seed_all()
    yield
    if os.path.exists("test.db"):
        os.remove("test.db")
