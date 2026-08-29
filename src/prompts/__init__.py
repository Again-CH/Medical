"""Prompt 版本管理入口。

设计原则：
- 把 prompt 从 Python 代码字符串中抽出，变成可版本化的文本资产；
- 通过 PROMPT_VERSION 环境变量切换版本目录，默认 v1；
- 启动时校验 manifest：文件缺失或哈希不匹配直接失败，避免「代码与 prompt 不同步」导致线上行为漂移；
- 提供 list_versions / load_prompt / prompt_hash 工具函数，供回归脚本与运行时调用。

为什么不用 LLM 框架自带的 prompt hub？
- 医疗场景 prompt 含院内知识库格式、合规红线、拒绝策略，必须入版本控制；
- 本地/离线部署优先，不能依赖外部 hub 网络；
- 面试/审计需要可追溯：git diff 即可看出 prompt 变更。
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Dict

_PROMPTS_DIR = Path(__file__).parent


def _version_dir(version: str | None = None) -> Path:
    v = version or os.getenv("PROMPT_VERSION", "v1")
    return _PROMPTS_DIR / v


def list_versions() -> list[str]:
    """返回所有可用的 prompt 版本目录名（如 v1, v2）。"""
    return sorted(
        d.name
        for d in _PROMPTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_") and (d / "manifest.json").exists()
    )


def load_prompt(name: str, version: str | None = None) -> str:
    """加载指定版本的 prompt 文本。"""
    path = _version_dir(version) / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件缺失: {path}")
    return path.read_text(encoding="utf-8")


def load_system_prompts(version: str | None = None) -> Dict[str, str]:
    """加载一个版本目录下全部系统提示（按 manifest 顺序）。"""
    d = _version_dir(version)
    manifest_path = d / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest 缺失: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prompts = {}
    for name in manifest.get("prompts", []):
        prompts[name] = load_prompt(name, version=version or manifest.get("version"))
    return prompts


def prompt_hash(name: str, version: str | None = None) -> str:
    """计算单个 prompt 文件的 SHA-256。"""
    content = load_prompt(name, version).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def version_manifest(version: str | None = None) -> dict:
    """读取 manifest。"""
    d = _version_dir(version)
    path = d / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_version(version: str | None = None) -> dict:
    """校验版本目录：文件齐全且哈希与 manifest 一致。

    生产环境在启动时调用，校验失败直接抛异常（fail-closed），
    避免 prompt 被误改后服务带着错误指令启动。
    """
    manifest = version_manifest(version)
    expected = manifest.get("files", {})
    actual = {}
    for name in manifest.get("prompts", []):
        actual[name] = prompt_hash(name, version)
    missing = set(expected.keys()) - set(actual.keys())
    if missing:
        raise RuntimeError(f"Prompt 版本 {manifest['version']} 缺少文件: {missing}")
    mismatched = {k for k in expected if actual.get(k) != expected[k]}
    if mismatched:
        raise RuntimeError(
            f"Prompt 版本 {manifest['version']} 哈希不匹配: {mismatched}"
        )
    return {"version": manifest["version"], "prompts": actual, "valid": True}


# 注意：不在模块导入时自动 validate_version()，否则 CI 回归脚本还没机会比较 baseline 就会失败。
# fail-closed 校验由应用 lifespan（src/gateway.py）在启动前显式调用。
