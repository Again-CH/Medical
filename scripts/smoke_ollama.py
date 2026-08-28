"""Ollama 真实模型端到端冒烟：分诊路由+真实回答、挂号 interrupt+resume。

用法：
    LLM_MODE=ollama OLLAMA_MODEL=qwen2.5:1.5b python scripts/smoke_ollama.py

（需在 Ollama 守护进程已启动、对应模型已 pull 的前提下运行）
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402
from src.graph import build_graph  # noqa: E402


async def main():
    print(f"[config] LLM_MODE={os.getenv('LLM_MODE')} MODEL={os.getenv('OLLAMA_MODEL')}")
    g = build_graph()

    # 1) 分诊：验证真实模型意图分类 + 真实回答
    cfg1 = {"configurable": {"thread_id": "smoke-triage"}}
    r1 = await g.ainvoke(
        {"messages": [HumanMessage("我最近老是头痛，应该挂哪个科？")], "patient_id": "alice"},
        cfg1,
    )
    print("\n=== 分诊 ===")
    print("intent:", r1.get("intent"))
    print("reply :", r1["messages"][-1].content[:240])

    # 2) 挂号：验证真实模型触发敏感动作 interrupt + 人工 resume
    g2 = build_graph()
    cfg2 = {"configurable": {"thread_id": "smoke-booking"}}
    r2 = await g2.ainvoke(
        {"messages": [HumanMessage("我要挂号预约神经内科明天上午的号")], "patient_id": "alice"},
        cfg2,
    )
    print("\n=== 挂号 ===")
    print("intent:", r2.get("intent"))
    print("interrupted?", "__interrupt__" in r2)
    if "__interrupt__" in r2:
        payload = r2["__interrupt__"][0].value
        print("approval action:", payload.get("action"))
        r3 = await g2.ainvoke(Command(resume={"approved": True}), cfg2)
        print("after resume:", r3["messages"][-1].content[:240])
    else:
        print("（未触发 interrupt；真实模型未调用敏感工具，reply 见下）")
        print("reply:", r2["messages"][-1].content[:240])


if __name__ == "__main__":
    asyncio.run(main())
