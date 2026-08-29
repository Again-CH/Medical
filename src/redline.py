"""红线引擎（兼容适配层）——**单一事实源为 ``safety.py``**。

历史问题：本文件与 ``safety.py`` 曾各自维护一套红线词库，口径分叉会导致
「网关已拦截、编排层却放行」（或反之），且本文件的旧词库缺少否定词守卫与
自伤危机类别，误报/漏报行为不一致。

现已收敛：本模块**不再维护独立词库**，全部委托 ``safety`` 的确定性判定，
仅保留 ``check_redline`` 签名以兼容评测集与既有调用方。

新代码请直接使用：
- ``safety.assess_emergency(text)``  —— 急症硬闸（含否定词守卫、分类急救要点）
- ``safety.assess_scope_violation(text)`` —— 诊断/开处方违规门
"""

from __future__ import annotations

from .safety import assess_emergency, assess_scope_violation


def check_redline(text: str) -> tuple[bool, str]:
    """红线判定：命中急症或违规请求则返回 (True, 原因)，否则 (False, "")."""
    emg = assess_emergency(text)
    if emg is not None:
        return True, f"命中急症关键词：{emg.keyword}"
    scope = assess_scope_violation(text)
    if scope is not None:
        return True, f"违规请求：{scope.keyword}（AI 不执行诊断/开药）"
    return False, ""
