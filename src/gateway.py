import json

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from .config import CORS_ORIGINS, TOKENS
from .graph import build_graph
from .store import STORE

app = FastAPI(title="医疗预约诊疗 Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

graph = build_graph()


def auth(authorization: str = Header(None)):
    t = authorization or ""
    if t.startswith("Bearer "):
        t = t[7:]
    if t not in TOKENS:
        raise HTTPException(status_code=401, detail="unauthorized")
    return TOKENS[t]


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


@app.post("/api/chat")
async def chat(req: Request, user: dict = Depends(auth)):
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
                # 仅推送 final_answer 节点的 token；子 Agent 内部的工具选择/中间推理不暴露给患者端
                node = ev.get("metadata", {}).get("langgraph_node")
                if node is not None and node != "final_answer":
                    continue
                tok = getattr(ev["data"]["chunk"], "content", "")
                if isinstance(tok, list):
                    tok = "".join(getattr(x, "text", str(x)) for x in tok)
                if tok:
                    yield _event({"type": "token", "text": tok})
                    emitted_tokens = True

        # astream_events 在 interrupt 处只停止流式、不抛异常，需读 state 判断人工门
        interrupt_value = _extract_interrupt_value(config)
        if interrupt_value is not None:
            aid = STORE.create(thread_id, interrupt_value)
            yield _event({"type": "interrupt", "approval_id": aid, "payload": interrupt_value})
            yield _event({"type": "done", "turn": "human"})
        else:
            yield _event({"type": "done", "turn": "ai" if emitted_tokens else "system"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/review/pending")
async def pending(user: dict = Depends(auth)):
    return {"pending": STORE.pending()}


@app.post("/api/review/resolve")
async def resolve(req: Request, user: dict = Depends(auth)):
    if user["role"] != "doctor":
        raise HTTPException(status_code=403, detail="doctor only")
    body = await req.json()
    aid = body.get("approval_id")
    decision = body.get("decision", {"approved": True})
    rec = STORE.get(aid)
    if not rec:
        raise HTTPException(status_code=404, detail="approval not found")
    thread_id = rec["thread_id"]
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(Command(resume=decision), config)
    STORE.resolve(aid, decision)
    final = result["messages"][-1].content
    return {"approval_id": aid, "result": final}


@app.get("/api/audit")
async def audit(user: dict = Depends(auth)):
    return {"audit": STORE.audit_log()}


@app.get("/health")
async def health():
    return {"status": "ok"}
