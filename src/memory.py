import threading

_profiles: dict = {}
# 用可重入锁：append_note 在持锁内调用 get_profile（同样需要加锁），
# 非重入的 Lock 会因同一线程二次加锁而永久死锁 → 改用 RLock。
_lock = threading.RLock()


def get_profile(patient_id: str) -> dict:
    with _lock:
        return _profiles.setdefault(patient_id, {"patient_id": patient_id, "notes": []})


def append_note(patient_id: str, note: str):
    """追加随访笔记：优先落库（ConversationMemory），DB 不可用时回退内存。"""
    try:
        from .integrations import get_hub

        if hasattr(get_hub(), "memory_append"):
            get_hub().memory_append("", patient_id, note)
    except Exception:
        # 离线/demo 模式：保留内存版长期记忆
        with _lock:
            get_profile(patient_id)["notes"].append(note)
