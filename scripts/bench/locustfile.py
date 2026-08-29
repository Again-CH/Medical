"""Locust 压测场景：测「系统自身」的容量，而不是远程 LLM 的延迟。

为什么用 fake 模型压测
----------------------
真实模型下 p95 主要由 Deepseek 的往返延迟决定（数秒级），测出来的数字
反映的是供应商的网络，不是本系统的工程水平。用 ``LLM_MODE=fake`` 跑，
才能真正量出：LangGraph 编排开销、Postgres checkpointer 写放大、
SSE 序列化、Tier-0 闸门判定、脱敏与审计落库——这些才是我们能优化的部分。

场景
----
- ``/health``：纯 HTTP 基线，用来分离「框架+网络」的固定成本
- ``/auth/login``：PBKDF2 60 万轮哈希，CPU 密集，是最容易被打爆的端点
- ``/api/chat``：全链路（意图分类 → 子 Agent → 工具 → 流式），核心场景
- ``/api/review/pending``：医护端读接口

运行::

    locust -f scripts/bench/locustfile.py --headless \
        -u 20 -r 5 -t 60s --host http://127.0.0.1:8100 \
        --csv=bench/result --html=bench/report.html
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.request
from urllib.error import HTTPError

from locust import HttpUser, between, events, task

# 绕过系统代理：压测必须直连本机
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

PATIENT = os.getenv("BENCH_PATIENT", "alice")
PASSWORD = os.getenv("BENCH_PASSWORD", "alice123")

# 覆盖各意图的话术池，避免每次都走同一分支（缓存/短路会掩盖真实成本）
CHAT_MESSAGES = [
    "我最近头痛，应该挂什么科",
    "帮我预约神经内科下午的号",
    "帮我看看我的化验报告",
    "我有高血压平时怎么随访复查",
    "口腔溃疡怎么护理",
    "门诊几点开门",
]


@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    """预热：确保压测用户已存在且已签知情同意，否则全部请求都会撞在同意闸门上。"""
    host = environment.host or "http://127.0.0.1:8100"
    try:
        req = urllib.request.Request(
            f"{host}/auth/register",
            data=json.dumps(
                {"username": PATIENT, "password": PASSWORD, "role": "patient"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except HTTPError as e:
        if e.code not in (400, 409):  # 已存在属预期
            print(f"[bench] 预热注册异常: {e.code}")
    except Exception as e:  # noqa: BLE001
        print(f"[bench] 预热注册异常: {e}")


def _wait_time():
    """思考时间：默认模拟真人（0.5~2s）；设 BENCH_NO_WAIT=1 去掉，用于测极限吞吐。"""
    if os.getenv("BENCH_NO_WAIT") == "1":
        return between(0, 0)
    return between(0.5, 2)


class MedicalAgentUser(HttpUser):
    """模拟一个在线患者：登录后交替做健康检查、提问、查看报告。"""

    wait_time = _wait_time()

    def on_start(self):
        self.token = self._login()

    def _login(self) -> str:
        with self.client.post(
            "/auth/login",
            json={"username": PATIENT, "password": PASSWORD, "role": "patient"},
            name="/auth/login",
            catch_response=True,
        ) as r:
            if r.status_code == 200:
                return (r.json() or {}).get("access_token", "")
            r.failure(f"login failed: {r.status_code}")
            return ""

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @task(1)
    def health(self):
        self.client.get("/health", name="/health")

    @task(1)
    def login(self):
        """单独压登录：PBKDF2 60 万轮是 CPU 热点，值得单独测。"""
        self.client.post(
            "/auth/login",
            json={"username": PATIENT, "password": PASSWORD, "role": "patient"},
            name="/auth/login",
        )

    @task(4)
    def chat(self):
        """核心场景：SSE 全链路。"""
        msg = random.choice(CHAT_MESSAGES)
        start = time.monotonic()
        with self.client.post(
            "/api/chat",
            json={"message": msg},
            headers={**self._auth(), "Accept": "text/event-stream"},
            name="/api/chat",
            catch_response=True,
            stream=True,
        ) as r:
            if r.status_code != 200:
                r.failure(f"chat {r.status_code}")
                return
            # 必须真正消费流，否则服务端写缓冲阻塞，测出来的是假的高吞吐
            body = r.text  # noqa: F841 - 必须真正消费流，否则测出的是假吞吐
            elapsed = (time.monotonic() - start) * 1000
            if elapsed > 30_000:
                r.failure(f"chat too slow: {elapsed:.0f}ms")

    @task(2)
    def chat_redline(self):
        """红线场景：确定性闸门短路，应该极快——用来验证安全闸确实绕过了 LLM。"""
        with self.client.post(
            "/api/chat",
            json={"message": "我突然胸痛呼吸困难，快救我"},
            headers={**self._auth(), "Accept": "text/event-stream"},
            name="/api/chat [redline]",
            catch_response=True,
            stream=True,
        ) as r:
            if r.status_code == 200:
                _ = r.text  # 消费流，避免服务端写缓冲阻塞

    @task(1)
    def my_reports(self):
        self.client.get("/api/reports", headers=self._auth(), name="/api/reports")
