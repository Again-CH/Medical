#!/usr/bin/env python3
"""schema 漂移检查：确保 src/db.py 的 ORM 模型与 Alembic 迁移保持一致。

为什么需要它
------------
``alembic upgrade head`` 能建表，但无法阻止「有人改了 ORM 模型却忘了生成新迁移」这种
静默漂移——运行时不会报错，却会让「模型」与「迁移定义的 schema」长期分叉。
本脚本把这个问题变成 CI 门禁：

  1. 在一个库上执行 ``alembic upgrade head``，得到「迁移定义的 schema」；
  2. 运行 ``alembic check``，把 ORM 元数据与「迁移定义的 schema」做 autogenerate 对比；
  3. 若存在漂移（模型变了但没迁移），check 报
     "New upgrade operations detected" 并以非零退出 → CI 失败。

用法
----
    python scripts/check_migrations.py
        # 默认用临时 SQLite（无需外部数据库），适合本地快速校验

    DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/db \\
        python scripts/check_migrations.py
        # 指定方言校验（CI 的 integration 任务用真实 Postgres 验证方言一致性）

退出码：0 = 一致；非 0 = 存在漂移（CI 应判定失败）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"


def _run(args: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": db_url, "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _print_stream(proc: subprocess.CompletedProcess[str]) -> None:
    if proc.stdout.strip():
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr.strip():
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n", file=sys.stderr)


def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        tmp = Path(tempfile.gettempdir()) / "medagent_drift_check.db"
        if tmp.exists():
            tmp.unlink()
        db_url = f"sqlite:///{tmp}"
        print(f"[drift-check] 未设置 DATABASE_URL，使用临时 SQLite: {db_url}")
    else:
        masked = db_url.split("@")[0] + "@***"
        print(f"[drift-check] 使用 DATABASE_URL 指定的数据库校验（{masked}）")

    # 1) 应用全部迁移 → 「迁移定义的 schema」
    print("[drift-check] 执行 alembic upgrade head ...")
    up = _run(["upgrade", "head"], db_url)
    if up.returncode != 0:
        print("=== alembic upgrade head 失败 ===")
        _print_stream(up)
        return up.returncode

    # 2) 对比 ORM 元数据 vs 迁移定义的 schema
    print("[drift-check] 执行 alembic check ...")
    chk = _run(["check"], db_url)
    if chk.returncode == 0:
        print("[drift-check] ✅ ORM 模型与 Alembic 迁移一致，无漂移")
        return 0

    print("[drift-check] ❌ 检测到 schema 漂移：ORM 模型与迁移不一致！")
    print("------------------------------------------------------------")
    _print_stream(chk)
    print("------------------------------------------------------------")
    print("修复方法（二选一）：")
    print("  1) 若确有模型变更，生成新迁移：")
    print('       DATABASE_URL=<库> .venv/bin/alembic revision --autogenerate -m "描述"')
    print("  2) 若是误改，请还原 src/db.py 中的模型定义")
    return 1


if __name__ == "__main__":
    sys.exit(main())
