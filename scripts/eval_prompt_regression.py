"""Prompt 版本管理与回归卡点。

用法::

    # 1) 检查当前 prompt 相对 baseline 是否发生变更（CI 卡点）
    python scripts/eval_prompt_regression.py --check

    # 2) 变更后跑真实 LLM 评测并更新 baseline（本地/回归机）
    python scripts/eval_prompt_regression.py --run-eval --backend ollama

    # 3) 强制重置 baseline（仅当确认无质量风险时）
    python scripts/eval_prompt_regression.py --reset-baseline

设计说明
--------
- 哈希对象：src/prompts/<version>/*.txt 的内容 SHA-256，不依赖 git；
- baseline 文件 .prompt_baseline.json 入版本控制，代表「已验证可用」的版本；
- CI 在 PR 阶段跑 --check：若哈希与 baseline 不一致，流程失败，
  强制开发者要么跑 --run-eval 通过并提交新 baseline，要么回滚 prompt；
- 评测阈值可配置，默认要求：接地准确率 >= 80%，幻觉率 <= 20%，
  越界拒答准确率 >= 80%，总通过率 >= 80%。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.prompts import load_prompt, validate_version, version_manifest  # noqa: E402

BASELINE_PATH = os.path.join(ROOT, ".prompt_baseline.json")
DEFAULT_THRESHOLDS = {
    "grounding_accuracy": 0.8,
    "hallucination_rate": 0.2,
    "refusal_accuracy": 0.8,
    "total_pass_rate": 0.8,
}


def _current_hashes(version: str | None = None) -> dict:
    """返回 {文件名: sha256}，基于 manifest 顺序。"""
    manifest = version_manifest(version)
    return {name: hashlib.sha256(load_prompt(name, version).encode("utf-8")).hexdigest() for name in manifest["prompts"]}


def _load_baseline() -> dict:
    if not os.path.exists(BASELINE_PATH):
        return {}
    with open(BASELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_baseline(data: dict) -> None:
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def check_baseline(version: str | None = None) -> tuple[bool, dict, dict]:
    """(是否有变更, 当前哈希, baseline 内容)。"""
    try:
        validate_version(version)
    except RuntimeError as e:
        print(f"ERROR: prompt 版本校验失败: {e}")
        return False, {}, {}

    current = _current_hashes(version)
    baseline = _load_baseline()
    changed = {k for k in current if current[k] != baseline.get("files", {}).get(k)}
    if baseline.get("version") != (version or os.getenv("PROMPT_VERSION", "v1")):
        changed.add("__version__")
    return bool(changed), current, baseline


def _thresholds_from_env() -> dict:
    t = dict(DEFAULT_THRESHOLDS)
    for key in t:
        env = os.getenv(f"PROMPT_EVAL_{key.upper()}")
        if env:
            t[key] = float(env)
    return t


def _passes(report: dict, thresholds: dict) -> tuple[bool, list[str]]:
    s = report["summary"]
    reasons = []
    ok = True
    if s.get("grounding_accuracy", 0) < thresholds["grounding_accuracy"]:
        ok = False
        reasons.append(
            f"接地准确率 {s['grounding_accuracy']} < {thresholds['grounding_accuracy']}"
        )
    if s.get("hallucination_rate", 1) > thresholds["hallucination_rate"]:
        ok = False
        reasons.append(
            f"幻觉率 {s['hallucination_rate']} > {thresholds['hallucination_rate']}"
        )
    if s.get("refusal_accuracy", 0) < thresholds["refusal_accuracy"]:
        ok = False
        reasons.append(
            f"越界拒答准确率 {s['refusal_accuracy']} < {thresholds['refusal_accuracy']}"
        )
    total_rate = s["passed"] / s["total"] if s.get("total") else 0
    if total_rate < thresholds["total_pass_rate"]:
        ok = False
        reasons.append(f"总通过率 {total_rate} < {thresholds['total_pass_rate']}")
    return ok, reasons


def _run_eval(backend: str, model: str) -> dict:
    """复用 scripts/eval_llm_quality.py 的评测逻辑。"""
    from scripts.eval_llm_quality import run_backend

    return run_backend(backend, model)


def main() -> int:
    ap = argparse.ArgumentParser(description="Prompt 版本管理与回归卡点")
    ap.add_argument(
        "--check",
        action="store_true",
        help="检查 prompt 是否相对 baseline 发生变更（有变更则 exit 1）",
    )
    ap.add_argument(
        "--run-eval",
        action="store_true",
        help="prompt 变更后跑真实 LLM 评测并更新 baseline",
    )
    ap.add_argument(
        "--reset-baseline",
        action="store_true",
        help="仅重置 baseline（危险，仅用于确认无质量风险时）",
    )
    ap.add_argument(
        "--backend",
        choices=["ollama", "deepseek"],
        default="ollama",
        help="真实评测后端",
    )
    ap.add_argument(
        "--model",
        default="",
        help="覆盖默认模型名",
    )
    ap.add_argument(
        "--version",
        default=None,
        help="指定 prompt 版本（默认 PROMPT_VERSION / v1）",
    )
    args = ap.parse_args()

    version = args.version or os.getenv("PROMPT_VERSION", "v1")

    if args.check:
        changed, current, baseline = check_baseline(version)
        if changed:
            print("PROMPT CHANGED relative to baseline:")
            print(f"  baseline version: {baseline.get('version')}")
            print(f"  current version : {version}")
            for name in current:
                old = baseline.get("files", {}).get(name, "<missing>")
                new = current[name]
                if old != new:
                    print(f"  - {name}: {old[:8]}... -> {new[:8]}...")
            print("\n请运行：python scripts/eval_prompt_regression.py --run-eval --backend <backend>")
            print("评测通过后，提交更新后的 .prompt_baseline.json。")
            return 1
        print(f"Prompt version={version} matches baseline. No regression required.")
        return 0

    if args.reset_baseline:
        current = _current_hashes(version)
        _save_baseline(
            {
                "version": version,
                "created_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "files": current,
                "note": "手动重置，未跑评测",
            }
        )
        print(f"Baseline reset for {version}.")
        return 0

    if args.run_eval:
        current = _current_hashes(version)
        print(f"Running LLM eval on backend={args.backend} ...")
        report = _run_eval(args.backend, args.model)
        s = report["summary"]
        print(
            f"Result: passed={s['passed']}/{s['total']}, "
            f"grounding={s['grounding_accuracy']}, hallucination_rate={s['hallucination_rate']}, "
            f"refusal={s['refusal_accuracy']}"
        )
        thresholds = _thresholds_from_env()
        ok, reasons = _passes(report, thresholds)
        if not ok:
            print("\n评测未通过阈值：")
            for r in reasons:
                print(f"  - {r}")
            print("\n请修改 prompt 或调整模型后重试。")
            return 2

        _save_baseline(
            {
                "version": version,
                "created_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "files": current,
                "eval": {
                    "backend": s["backend"],
                    "model": s["model"],
                    "grounding_accuracy": s["grounding_accuracy"],
                    "hallucination_rate": s["hallucination_rate"],
                    "refusal_accuracy": s["refusal_accuracy"],
                    "passed": s["passed"],
                    "total": s["total"],
                },
            }
        )
        print(f"\nBaseline updated: {BASELINE_PATH}")
        print("请提交 .prompt_baseline.json 到版本控制。")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
