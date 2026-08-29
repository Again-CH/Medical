import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"

# 与网关进程共用同一份环境变量，确保查库指向同一 sqlite
os.environ["DATABASE_URL"] = "sqlite:///./demo.db"
os.environ["LLM_MODE"] = "fake"
sys.path.insert(0, "/Users/mac/Documents/Medical-care")

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def post(path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def get(path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(BASE + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def login(username, password, role):
    d = post("/auth/login", {"username": username, "password": password, "role": role})
    return "Bearer " + d["access_token"]


def chat_sse(message, token, thread_id):
    body = json.dumps({"message": message, "thread_id": thread_id}).encode()
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": token},
        method="POST",
    )
    tokens, approval_id, payload = [], None, None
    with urllib.request.urlopen(req, timeout=60) as r:
        buf = ""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "ignore")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                for line in block.splitlines():
                    if line.startswith("data:"):
                        p = json.loads(line[5:].strip())
                        if p["type"] == "token":
                            tokens.append(p["text"])
                        elif p["type"] == "interrupt":
                            approval_id = p["approval_id"]
                            payload = p["payload"]
    return "".join(tokens), approval_id, payload


def check(label, cond, extra=""):
    print(f"[{PASS if cond else FAIL}] {label}" + (f" — {extra}" if extra else ""))
    return cond


def main():
    ts = str(int(time.time()))
    print("=== 1) 健康检查 ===")
    h = get("/health")
    check("网关健康", h.get("status") == "ok", str(h))

    print("\n=== 2) 登录（患者 alice / 医护 drwang）===")
    tp = login("alice", "alice123", "patient")
    td = login("drwang", "dr123456", "doctor")
    check("患者登录拿 token", tp.startswith("Bearer "))
    check("医护登录拿 token", td.startswith("Bearer "))

    print("\n=== 3) 链路A：挂号（敏感动作 → HITL 中断）===")
    text_a, aid_a, pay_a = chat_sse(
        "我要挂神经内科今天的号，请锁定号源并办理医保结算", tp, f"thr-bk-{ts}"
    )
    print("  患者端流式回复:", text_a[:80].replace("\n", " ") or "(空/被中断)")
    check("触发中断 approval_id", aid_a is not None, f"aid={aid_a}")
    check(
        "中断载荷 action=lock_and_settle",
        pay_a and pay_a.get("action") == "lock_and_settle",
        str(pay_a),
    )
    check(
        "敏感工具含 lock_appointment+medicare_settle",
        pay_a and set(pay_a.get("tools", [])) >= {"lock_appointment", "medicare_settle"},
        str(pay_a.get("tools")),
    )

    print("\n=== 4) 医护端：待审列表 ===")
    pend = get("/api/review/pending", td)
    aids = [p["approval_id"] for p in pend["pending"]]
    check("待审含本单", aid_a in aids, f"待审={aids}")

    print("\n=== 5) 医护批准 → 图 resume → 真实落库 ===")
    res_a = post("/api/review/resolve", {"approval_id": aid_a, "decision": {"approved": True}}, td)
    print("  审批后回复:", str(res_a.get("result"))[:90].replace("\n", " "))

    from sqlalchemy import text
    from src.db import get_session

    with get_session() as s:
        row = s.execute(
            text("SELECT status, medicare_settled FROM appointments ORDER BY id DESC LIMIT 1")
        ).fetchone()
    check("Appointment 已落库(LOCKED)", row is not None and row[0] == "LOCKED", str(row))
    check("医保结算已标记 True", row is not None and bool(row[1]), str(row))

    print("\n=== 6) 链路B：急症（红线 → emergency_handoff 中断）===")
    text_b, aid_b, pay_b = chat_sse("我胸口剧痛喘不上气", tp, f"thr-em-{ts}")
    print("  患者端流式回复:", text_b[:80].replace("\n", " ") or "(空/被中断)")
    check("急症触发中断", aid_b is not None, f"aid={aid_b}")
    check(
        "中断 action=emergency_handoff",
        pay_b and pay_b.get("action") == "emergency_handoff",
        str(pay_b),
    )
    check(
        "敏感工具含 handoff+call_120",
        pay_b and set(pay_b.get("tools", [])) >= {"handoff", "call_120"},
        str(pay_b.get("tools")),
    )

    print("\n=== 7) 医护批准急症转诊 → 落 emergency_events ===")
    res_b = post("/api/review/resolve", {"approval_id": aid_b, "decision": {"approved": True}}, td)
    print("  审批后回复:", str(res_b.get("result"))[:90].replace("\n", " "))
    with get_session() as s:
        n = s.execute(text("SELECT count(*) FROM emergency_events")).scalar()
    check("emergency_events 已落库", n and n >= 1, f"count={n}")

    print("\n=== 8) 链路C：分诊（非敏感，直接返回 RAG 内容）===")
    text_c, aid_c, pay_c = chat_sse("我头痛应该挂哪个科", tp, f"thr-tr-{ts}")
    check("分诊未触发中断", aid_c is None)
    check("分诊返回科室建议(神经内科)", "神经内科" in text_c, text_c[:90].replace("\n", " "))

    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
