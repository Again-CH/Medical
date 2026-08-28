import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from .auth import authenticate, get_current_user, register_user, require_doctor
from .db import is_db_enabled
from .graph import build_graph
from .store import get_store

app = FastAPI(title="医疗预约诊疗 Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

graph = build_graph()


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _extract_interrupt_value(config):
    """从暂停态里取出 interrupt 的载荷（兼容 langgraph 1.x 的 tasks.interrupts）。"""
    try:
        snap = graph.get_state(config)
        for task in getattr(snap, "tasks", ()) or ():
            interrupts = getattr(task, "interrupts", None)
            if interrupts:
                return interrupts[0].value
    except Exception:
        pass
    return None


# ---------------- 认证 ----------------
@app.post("/auth/register")
async def register(req: Request):
    body = await req.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role", "patient")
    if not username or not password:
        raise HTTPException(status_code=400, detail="username 与 password 必填")
    if role not in ("patient", "doctor"):
        raise HTTPException(status_code=400, detail="role 仅支持 patient/doctor")
    try:
        register_user(
            username,
            password,
            role=role,
            full_name=body.get("full_name", ""),
            title=body.get("title", ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail="用户已存在") from e
    return {"ok": True, "role": role}


@app.post("/auth/login")
async def login(req: Request):
    body = await req.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role", "patient")
    token = authenticate(username, password, role)
    if not token:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"access_token": token, "token_type": "bearer", "role": role}


# ---------------- 会话（患者） ----------------
@app.post("/api/chat")
async def chat(req: Request, user: dict = Depends(get_current_user)):
    body = await req.json()
    message = body.get("message", "")
    thread_id = body.get("thread_id") or f"thr-{user['sub']}"
    config = {"configurable": {"thread_id": thread_id}}
    input_state = {
        "messages": [HumanMessage(content=message)],
        "patient_id": user["sub"],
    }

    async def gen():
        emitted_tokens = False
        async for ev in graph.astream_events(input_state, config, version="v2"):
            if ev.get("event") == "on_chat_model_stream":
                # 仅推送 final_answer 节点的 token；子 Agent 内部推理不暴露给患者端
                node = ev.get("metadata", {}).get("langgraph_node")
                if node is not None and node != "final_answer":
                    continue
                tok = getattr(ev["data"]["chunk"], "content", "")
                if isinstance(tok, list):
                    tok = "".join(getattr(x, "text", str(x)) for x in tok)
                if tok:
                    yield _event({"type": "token", "text": tok})
                    emitted_tokens = True

        interrupt_value = _extract_interrupt_value(config)
        if interrupt_value is not None:
            aid = get_store().create(thread_id, interrupt_value)
            yield _event({"type": "interrupt", "approval_id": aid, "payload": interrupt_value})
            yield _event({"type": "done", "turn": "human"})
        else:
            yield _event({"type": "done", "turn": "ai" if emitted_tokens else "system"})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- 审批（医护） ----------------
@app.get("/api/review/pending")
async def pending(user: dict = Depends(require_doctor)):
    return {"pending": get_store().pending()}


@app.post("/api/review/resolve")
async def resolve(req: Request, user: dict = Depends(require_doctor)):
    body = await req.json()
    aid = body.get("approval_id")
    decision = body.get("decision", {"approved": True})
    rec = get_store().get(aid)
    if not rec:
        raise HTTPException(status_code=404, detail="approval not found")
    thread_id = rec["thread_id"]
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(Command(resume=decision), config)
    get_store().resolve(aid, decision)
    final = result["messages"][-1].content
    return {"approval_id": aid, "result": final}


@app.get("/api/audit")
async def audit(user: dict = Depends(require_doctor)):
    return {"audit": get_store().audit_log()}


@app.get("/health")
async def health():
    db_ok = False
    if is_db_enabled():
        try:
            from .db import get_session

            with get_session() as s:
                s.execute(__import__("sqlalchemy").text("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False
    return {"status": "ok", "db": "up" if db_ok else "memory"}


# ---------------- 前端静态托管（同源，供浏览器演示） ----------------
CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"


@app.get("/")
async def index():
    return FileResponse(CLIENT_DIR / "chat.html")


@app.get("/review")
async def review_page():
    return FileResponse(CLIENT_DIR / "review.html")
