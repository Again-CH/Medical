"""把存量明文 PHI 行补齐加密（启用 PHI 静态加密后的一次性迁移）。

幂等：已加密（``enc:`` 前缀）的行直接跳过；只加密仍为明文的字段。
需同时设置 ``PHI_ENCRYPTION_ENABLED=1`` 与 ``PHI_ENCRYPTION_KEY``，否则拒绝执行
（fail-closed，避免无密钥空跑把数据写坏）。

适用场景：在某环境先以明文跑了一段时间、之后才开启列加密，用本脚本把历史数据
原地加密，使「落盘即加密」成为事实而非仅对新写入生效。

运行：
    PHI_ENCRYPTION_ENABLED=1 PHI_ENCRYPTION_KEY=<key> \\
        python scripts/encrypt_existing_phi.py
"""

import glob
import os
import re
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
load_dotenv()

from src import phi  # noqa: E402
from src.db import (  # noqa: E402
    Approval,
    ChatLog,
    ConversationMemory,
    EmergencyEvent,
    ExamStep,
    LabReport,
    Reminder,
    User,
    VitalSign,
    get_patient_engine,
    get_session,
    is_db_enabled,
)

_USER_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# (模型, [需加密的字段名]) —— 与 db.py 中标记为 PHI 的列一致
_MAIN_COLUMNS = [
    (User, ["phone", "full_name"]),
    (ChatLog, ["input_text", "output_text"]),
    (Approval, ["payload"]),
    (ExamStep, ["note"]),
]
_PATIENT_COLUMNS = [
    (ConversationMemory, ["value"]),
    (LabReport, ["result"]),
    (VitalSign, ["value"]),
    (Reminder, ["content"]),
    (EmergencyEvent, ["content"]),
]


def _encrypt_model_columns(s, model, fields):
    n = 0
    for row in s.query(model).all():
        dirty = False
        for f in fields:
            v = getattr(row, f)
            if v and not phi.is_encrypted(v):
                setattr(row, f, phi.encrypt_field(v))
                dirty = True
                n += 1
        if dirty:
            s.commit()
    return n


def main() -> int:
    if not phi.enabled() or not phi.is_configured():
        print("❌ 需设置 PHI_ENCRYPTION_ENABLED=1 与 PHI_ENCRYPTION_KEY 后才可加密存量数据")
        return 1
    if not is_db_enabled():
        print("❌ 未配置 DATABASE_URL，无法操作数据库")
        return 1

    total = 0
    # 主库
    with get_session() as s:
        for model, fields in _MAIN_COLUMNS:
            total += _encrypt_model_columns(s, model, fields)

    # 每患者私有库
    data_dir = os.path.join(ROOT, "data")
    if os.path.isdir(data_dir):
        for fn in glob.glob(os.path.join(data_dir, "*.db")):
            username = os.path.basename(fn)[:-3]
            if not _USER_RE.match(username):
                continue
            try:
                eng = get_patient_engine(username)
            except Exception:  # noqa: BLE001 - 损坏库跳过
                continue
            from sqlalchemy.orm import sessionmaker

            sess = sessionmaker(bind=eng, expire_on_commit=False)()
            try:
                for model, fields in _PATIENT_COLUMNS:
                    total += _encrypt_model_columns(sess, model, fields)
            finally:
                sess.close()

    print(f"✅ 已加密 {total} 处存量 PHI 字段（已加密的行自动跳过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
