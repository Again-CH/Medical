import asyncio
import json
import os
import sys

# 让 tests 能 import src
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.redline import check_redline  # noqa: E402
from src.supervisor import classify_intent  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def test_redline():
    with open(os.path.join(HERE, "redline_cases.json"), encoding="utf-8") as f:
        cases = json.load(f)
    for c in cases:
        hit, reason = check_redline(c["text"])
        assert hit == c["expect_hit"], c["text"]
        if c["expect_hit"]:
            assert c["reason_contains"] in reason, c["text"]


def test_intent():
    with open(os.path.join(HERE, "intent_cases.json"), encoding="utf-8") as f:
        cases = json.load(f)
    for c in cases:
        # classify_intent 已为 async（真实模型走 LLM 分类），fake 模式亦统一 await
        got = asyncio.run(classify_intent(c["text"]))
        assert got == c["expect"], c["text"]
