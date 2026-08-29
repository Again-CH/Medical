"""备份脚本：主库 Postgres + 每患者 SQLite + 环境配置 + 校验和。

用法::

    # 默认备份到 backups/YYYY-MM-DD_HHMMSS/
    python scripts/backup.py

    # 指定输出目录
    python scripts/backup.py --out /mnt/nfs/medical-agent-backup

    # 仅做验证，不创建备份（检查 pg_dump / 目录权限）
    python scripts/backup.py --dry-run

输出结构::

    <out>/
      manifest.json          # 备份元数据、文件清单、sha256
      postgres.sql.gz        # 主库 SQL 转储
      data/                  # 每患者 SQLite 私有库
        alice.db
        bob.db
        ...
      env/                   # 环境配置（不含密钥明文时更安全，此处仅做存在性备份）
        .env.b64             # base64 后的 .env（提醒运维另行保管密钥）

设计说明
--------
- 患者私有 SQLite 必须随主库一起备份，否则恢复后只有公共表、没有 PHI；
- 校验和写入 manifest.json，恢复前先比对，防止静默损坏；
- 不在脚本里删旧备份（避免误删），建议用外部 retention policy / cron；
- 医疗数据建议加密落盘后再传对象存储，本脚本只负责本地打包。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_pg_dump() -> str:
    for cand in ("pg_dump", "/opt/homebrew/bin/pg_dump", "/usr/local/bin/pg_dump"):
        if shutil.which(cand):
            return cand
    raise RuntimeError("pg_dump 未找到，请安装 PostgreSQL 客户端")


def _pg_args_from_url(url: str) -> list[str]:
    """把 DATABASE_URL 解析为 pg_dump 命令行参数。"""
    parsed = urlparse(url)
    args = []
    if parsed.username:
        args += ["--username", parsed.username]
    if parsed.hostname:
        args += ["--host", parsed.hostname]
    if parsed.port:
        args += ["--port", str(parsed.port)]
    db = parsed.path.lstrip("/") if parsed.path else "postgres"
    args += ["--dbname", db]
    return args


def _backup_postgres(out_dir: Path, dry_run: bool) -> dict:
    url = os.getenv("DATABASE_URL", "")
    if not url or url.startswith("sqlite"):
        return {"skipped": True, "reason": "DATABASE_URL 未设置或为 sqlite"}

    pg_dump = _find_pg_dump()
    target = out_dir / "postgres.sql.gz"
    # 注意：不使用 --create，以便恢复时可以灵活指定目标库（演练/临时库）。
    # 如需完整重建原始库，运维先 drop/create 后再恢复即可。
    args = [pg_dump, "--clean", "--if-exists", * _pg_args_from_url(url)]

    env = os.environ.copy()
    parsed = urlparse(url)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    if dry_run:
        print(f"[dry-run] 将执行: {' '.join(args)} | gzip > {target}")
        return {"skipped": True, "reason": "dry-run"}

    with open(target, "wb") as f:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, env=env)
        gz = subprocess.Popen(["gzip"], stdin=p.stdout, stdout=f)
        p.stdout.close()
        rc = p.wait()
        gz.wait()
        if rc != 0:
            raise RuntimeError(f"pg_dump 失败，exit code={rc}")

    return {"file": str(target.relative_to(out_dir)), "sha256": _sha256_file(target)}


def _backup_sqlite(out_dir: Path, dry_run: bool) -> list[dict]:
    data_dir = Path(ROOT) / "data"
    files = sorted(data_dir.glob("*.db"))
    records = []
    target_dir = out_dir / "data"
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
    for src in files:
        target = target_dir / src.name
        if dry_run:
            print(f"[dry-run] 将复制: {src} -> {target}")
            continue
        shutil.copy2(src, target)
        records.append({"file": f"data/{src.name}", "sha256": _sha256_file(target)})
    return records


def _backup_env(out_dir: Path, dry_run: bool) -> dict:
    env_file = Path(ROOT) / ".env"
    if not env_file.exists():
        return {"skipped": True, "reason": ".env 不存在"}
    target = out_dir / "env" / ".env.b64"
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = env_file.read_bytes()
        target.write_text(base64.b64encode(raw).decode("ascii"))
    else:
        print(f"[dry-run] 将备份 .env -> {target} (base64)")
    return {"file": str(target.relative_to(out_dir))}


def main() -> int:
    ap = argparse.ArgumentParser(description="医疗 Agent 备份脚本")
    ap.add_argument("--out", default=os.path.join(ROOT, "backups"), help="备份输出目录")
    ap.add_argument("--dry-run", action="store_true", help="仅打印将执行的操作")
    args = ap.parse_args()

    out_root = Path(args.out)
    out_dir = out_root / _now_str()
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"备份目标: {out_dir}")
    postgres = _backup_postgres(out_dir, args.dry_run)
    sqlite_files = _backup_sqlite(out_dir, args.dry_run)
    env_info = _backup_env(out_dir, args.dry_run)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": ROOT,
        "database_url_masked": os.getenv("DATABASE_URL", "")[:20] + "..." if os.getenv("DATABASE_URL") else "",
        "postgres": postgres,
        "sqlite": sqlite_files,
        "env": env_info,
    }

    if not args.dry_run:
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"备份完成: {out_dir}")
        print(f"  Postgres: {postgres.get('file') or postgres.get('reason')}")
        print(f"  SQLite: {len(sqlite_files)} 个")
        print(f"  Env: {env_info.get('file') or env_info.get('reason')}")
    else:
        print("[dry-run] 未写入任何文件")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
