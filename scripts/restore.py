"""恢复与演练脚本：把 backup.py 产出的备份恢复到临时库并做健康查询验证。

用法::

    # 演练模式（默认）：恢复到临时 sqlite + temp Postgres，跑完后自动清理
    python scripts/restore.py --backup backups/2026-08-30_123456 --verify

    # 恢复到真实目标库（危险！会覆盖数据）
    python scripts/restore.py --backup backups/2026-08-30_123456 \
        --target-db postgresql+psycopg2://user:pass@host/db --yes

流程
----
1. 校验 manifest 中的 sha256；
2. 解压 postgres.sql.gz；
3. 用 psql 恢复到目标库；
4. 复制 SQLite 到目标 data 目录；
5. 执行健康查询（users/doctors/appointments 数量、关键表存在性）；
6. 演练模式直接退出并报告结果，不保留恢复后的库。

为什么必须演练
--------------
> 没演练过恢复的备份，在事故中等于没有备份。
> 本脚本把「恢复」变成可重复、可自动化的操作，并在每次备份后至少演练一次。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_tool(name: str) -> str:
    for cand in (name, f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if shutil.which(cand):
            return cand
    raise RuntimeError(f"{name} 未找到")


def _verify_manifest(backup_dir: Path) -> dict:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json 不存在: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    files_to_check = []
    if manifest.get("postgres", {}).get("sha256"):
        files_to_check.append(backup_dir / manifest["postgres"]["file"])
    for rec in manifest.get("sqlite", []):
        files_to_check.append(backup_dir / rec["file"])

    for f in files_to_check:
        actual = _sha256_file(f)
        expected = next(
            (r["sha256"] for r in ([manifest.get("postgres", {})] + manifest.get("sqlite", []))
             if r.get("file") == f.relative_to(backup_dir).as_posix()),
            None,
        )
        if expected and actual != expected:
            raise RuntimeError(f"校验和 mismatch: {f} (expected {expected[:16]}..., got {actual[:16]}...)")
    return manifest


def _unzip_postgres(backup_dir: Path) -> Path:
    gz = backup_dir / "postgres.sql.gz"
    if not gz.exists():
        raise FileNotFoundError(f"Postgres 备份不存在: {gz}")
    target = backup_dir / "postgres.sql"
    with gzip.open(gz, "rb") as f_in, open(target, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return target


def _restore_postgres(sql_file: Path, target_url: str) -> None:
    parsed = urlparse(target_url)
    psql = _find_tool("psql")
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    db = parsed.path.lstrip("/") or "postgres"
    args = [psql, "--quiet"]
    if parsed.username:
        args += ["--username", parsed.username]
    if parsed.hostname:
        args += ["--host", parsed.hostname]
    if parsed.port:
        args += ["--port", str(parsed.port)]
    args += ["--dbname", db, "--file", str(sql_file)]

    result = subprocess.run(args, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"psql 恢复失败: {result.stderr[:500]}")


def _health_check(db_url: str) -> dict:
    """用 SQLAlchemy 连目标库执行关键表查询。"""
    from sqlalchemy import create_engine, text

    engine = create_engine(db_url)
    with engine.connect() as conn:
        users = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        doctors = conn.execute(text("SELECT COUNT(*) FROM doctors")).scalar()
        appointments = conn.execute(text("SELECT COUNT(*) FROM appointments")).scalar()
        tenants = conn.execute(text("SELECT COUNT(*) FROM tenants")).scalar()
    return {"users": users, "doctors": doctors, "appointments": appointments, "tenants": tenants}


def main() -> int:
    ap = argparse.ArgumentParser(description="医疗 Agent 恢复演练脚本")
    ap.add_argument("--backup", required=True, help="备份目录路径")
    ap.add_argument("--target-db", default="", help="目标 Postgres 连接串（默认临时库）")
    ap.add_argument("--target-data", default=os.path.join(ROOT, "data"), help="SQLite 恢复目标目录")
    ap.add_argument("--verify", action="store_true", help="恢复后执行健康查询并退出")
    ap.add_argument("--yes", action="store_true", help="确认覆盖真实目标库")
    args = ap.parse_args()

    backup_dir = Path(args.backup)
    manifest = _verify_manifest(backup_dir)
    print(f"备份校验通过: {backup_dir}")

    # 演练模式：创建临时数据库
    temp_db_url = args.target_db
    temp_db_created = False
    if not temp_db_url:
        parsed = urlparse(os.getenv("DATABASE_URL", "postgresql://mac@localhost:5432/medical_agent"))
        temp_db_name = f"medical_agent_restore_drill_{os.getpid()}"
        temp_db_url = f"postgresql+psycopg2://{parsed.username or 'mac'}@{parsed.hostname or 'localhost'}:{parsed.port or 5432}/{temp_db_name}"
        createdb = _find_tool("createdb")
        env = os.environ.copy()
        if parsed.password:
            env["PGPASSWORD"] = parsed.password
        subprocess.run(
            [createdb, "--host", parsed.hostname or "localhost", "--port", str(parsed.port or 5432),
             "--username", parsed.username or "mac", temp_db_name],
            env=env, capture_output=True, check=False,
        )
        temp_db_created = True
        print(f"已创建临时演练库: {temp_db_name}")
    else:
        if not args.yes:
            print("ERROR: 恢复到真实目标库必须加 --yes")
            return 2

    try:
        sql_file = _unzip_postgres(backup_dir)
        print(f"开始恢复到: {temp_db_url}")
        _restore_postgres(sql_file, temp_db_url)

        if args.verify:
            stats = _health_check(temp_db_url)
            print("\n健康查询结果:")
            for k, v in stats.items():
                print(f"  {k}: {v}")
            if stats["users"] == 0:
                print("WARNING: users 表为空，恢复可能异常")

        # SQLite 恢复
        sqlite_target = Path(args.target_data)
        if not args.target_db:
            sqlite_target = backup_dir / "restored_data"
        sqlite_target.mkdir(parents=True, exist_ok=True)
        for rec in manifest.get("sqlite", []):
            src = backup_dir / rec["file"]
            dst = sqlite_target / Path(rec["file"]).name
            if args.yes or not args.target_db:
                shutil.copy2(src, dst)
                print(f"已恢复 SQLite: {dst}")

        print("\n恢复演练完成")
        if temp_db_created:
            print("临时库将在退出后清理")
        return 0
    finally:
        if temp_db_created:
            parsed = urlparse(temp_db_url)
            dropdb = _find_tool("dropdb")
            env = os.environ.copy()
            if parsed.password:
                env["PGPASSWORD"] = parsed.password
            subprocess.run(
                [dropdb, "--host", parsed.hostname or "localhost", "--port", str(parsed.port or 5432),
                 "--username", parsed.username or "mac", parsed.path.lstrip("/")],
                env=env, capture_output=True, check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())
