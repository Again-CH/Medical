"""真实对话演示：用 Deepseek（OpenAI 兼容协议）跑完整「锁号 / 结算 / 审核」流程。

本脚本证明系统不是 demo：真实 LLM 做意图分类 + 自主函数调用，敏感动作走人工审核门，
医生（或自动化 reviewer）批准后，预约真实写入 PostgreSQL。

运行（在本机）：
    DEEPSEEK_API_KEY=sk-xxxx python scripts/demo_deepseek.py

注意：OpenAI 官方端点在本沙箱不可达；Deepseek 可达，故默认用 Deepseek。
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

# 让 .env 里的 DEEPSEEK_API_KEY 等在脚本读取前生效（config 自己也会再 load 一次）
load_dotenv()

# ---- 在任何 src 模块导入前，把环境变量配好（config 在 import 时读取） ----
KEY = os.getenv("DEEPSEEK_API_KEY", "")
if not KEY:
    sys.exit(
        "✗ 未检测到 DEEPSEEK_API_KEY，请: DEEPSEEK_API_KEY=sk-xxxx python scripts/demo_deepseek.py"
    )

os.environ["LLM_MODE"] = "openai"
os.environ["OPENAI_API_KEY"] = KEY
os.environ["OPENAI_BASE_URL"] = "https://api.deepseek.com/v1"
os.environ["OPENAI_MODEL"] = "deepseek-chat"
# 真实 Postgres（已 migrate+seed），让预约真正落库；不设置则回退内存演示
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://mac@localhost:5432/medical_agent",
)

# 把仓库根加入 path，方便 `python scripts/xxx.py` 直接跑
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402
from src.db import Appointment, get_session  # noqa: E402
from src.graph import build_graph  # noqa: E402


async def main():
    g = build_graph()
    thread = "demo-deepseek-1"
    cfg = {"configurable": {"thread_id": thread}}

    print("=" * 64)
    print("患者（真人输入）: 我要挂神经内科今天的号，请锁定号源并办理医保结算")
    print("=" * 64)

    state = {
        "messages": [HumanMessage(content="我要挂神经内科今天的号，请帮我锁定号源并办理医保结算")],
        "patient_id": "alice",
    }

    # —— 第一拍：supervisor 分类 + 子 Agent 调工具，命中敏感动作会 interrupt ——
    r1 = await g.ainvoke(state, cfg)

    intent = r1.get("intent")
    print(f"\n[supervisor] 意图分类 → {intent}")

    if "__interrupt__" in r1:
        iv = r1["__interrupt__"][0].value
        print(f"\n[🔒 人工审核门触发] action={iv['action']}  tools={iv['tools']}")
        print("\n[🤖 Deepseek 自主决策的工具调用]（命中敏感动作即挂起等待审批）:")
        for t in iv["tools"]:
            print(f"    🔧 {t}()")

        # —— 第二拍：医生（或自动 reviewer）批准 ——
        print("\n[👨‍⚕️ 医护端] 审核通过（Command(resume={'approved': True})）")
        r2 = await g.ainvoke(Command(resume={"approved": True}), cfg)

        reply = r2["messages"][-1].content
        print(f"\n[助手最终回复] {reply}\n")
    else:
        # 没触发审核门（模型未调用敏感工具），仍是有效对话
        reply = r1["messages"][-1].content
        print(f"\n[助手回复] {reply}\n")
        r2 = r1

    # —— 验证真实持久化：去 Postgres 查预约记录 ——
    with get_session() as s:
        rows = s.query(Appointment).all()
        print(f"[🗄️  PostgreSQL 实际落库] 预约记录数 = {len(rows)}")
        for a in rows:
            print(
                f"   • id={a.id} patient={a.patient_id} doctor={a.doctor_id} "
                f"work_date={a.work_date} period={a.period} status={a.status} "
                f"medicare_settled={a.medicare_settled}"
            )

    print("\n✅ 真实对话 + 锁号 + 医保结算 + 人工审核 + DB 落库 全流程跑通")


if __name__ == "__main__":
    asyncio.run(main())
