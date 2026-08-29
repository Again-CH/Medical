"""本地告警接收端：把 Alertmanager 的 webhook 真正落地，让告警闭环可验证。

为什么需要它
------------
``observability/alertmanager/alertmanager.yml`` 原先配的是 ``null`` receiver 占位
——栈能起来，但告警**哪儿也去不了**，"可观测"并没有变成"有人响应"。
生产环境应换成企业 IM（企业微信 / 钉钉 / 飞书）或电话 webhook，但那需要真实
凭证，本地无法验证。于是补一个本地接收端：

- 让 Alertmanager → 接收端 这段链路在本地**可跑、可看、可测**；
- 收到的告警落成 JSONL 并打印可读摘要，作为"告警确实发出去了"的证据；
- 生产配置（``alertmanager.production.yml``）用 ``url_file`` 引用密钥文件，
  webhook URL 不进版本库 —— 本地用 sink，生产换真 IM，同一套告警规则不动。

用法::

    python scripts/alert_sink.py --port 9101 --log observability/alert-sink-data/alerts.jsonl

端到端验证（Alertmanager 起来后手工触发一条测试告警）::

    curl -XPOST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[
      {"labels":{"alertname":"SmokeTest","severity":"critical","instance":"local"},
       "annotations":{"summary":"告警闭环冒烟测试"}}
    ]'
    # 随后 docker compose logs alert-sink 应看到可读摘要，JSONL 应多一行
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 告警落盘字段顺序（JSONL 每行一条，便于 jq / ELK 消费）
FIELDS = (
    "received_at",
    "status",
    "alertname",
    "severity",
    "instance",
    "summary",
    "description",
    "starts_at",
    "ends_at",
    "generator_url",
    "labels",
)

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _label(labels: dict, *names: str, default: str = "") -> str:
    """按顺序取第一个存在的标签值（Alertmanager 各版本标签名略有差异）。"""
    for n in names:
        v = labels.get(n)
        if v:
            return str(v)
    return default


def normalize_payload(payload: dict) -> list[dict]:
    """把 Alertmanager webhook 载荷归一化成一组扁平告警记录。

    兼容 v4（``alerts`` 列表）与裸告警列表两种形态；缺字段时给安全默认值，
    绝不因单条告警格式异常而丢弃整个批次（告警系统本身不能成为故障点）。
    """
    if not isinstance(payload, dict):
        return []

    common_labels = payload.get("commonLabels") or {}
    common_annos = payload.get("commonAnnotations") or {}
    batch_status = payload.get("status") or "unknown"
    raw_alerts = payload.get("alerts")
    if not isinstance(raw_alerts, list):
        return []

    now = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []

    for raw in raw_alerts:
        if not isinstance(raw, dict):
            continue
        labels = {**common_labels, **(raw.get("labels") or {})}
        annos = {**common_annos, **(raw.get("annotations") or {})}
        out.append(
            {
                "received_at": now,
                "status": str(raw.get("status") or batch_status),
                "alertname": _label(labels, "alertname", default="unknown"),
                "severity": _label(labels, "severity", default="unknown"),
                "instance": _label(labels, "instance", "instance_name", default=""),
                "summary": str(annos.get("summary") or ""),
                "description": str(annos.get("description") or ""),
                "starts_at": str(raw.get("startsAt") or ""),
                "ends_at": str(raw.get("endsAt") or ""),
                "generator_url": str(raw.get("generatorURL") or ""),
                "labels": labels,
            }
        )
    return out


def persist(alerts: list[dict], log_path: str) -> int:
    """追加写入 JSONL；目录不存在时创建。返回成功写入条数。"""
    if not alerts:
        return 0
    directory = os.path.dirname(os.path.abspath(log_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    written = 0
    with open(log_path, "a", encoding="utf-8") as f:
        for a in alerts:
            row = {k: a.get(k) for k in FIELDS}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
        f.flush()
    return written


def format_line(a: dict) -> str:
    """人类可读摘要（供 docker compose logs 直接看）。"""
    sev = (a.get("severity") or "unknown").upper()
    return (
        f"[{sev:8s}] {a.get('status', '?')} {a.get('alertname', '?')} "
        f"@ {a.get('instance') or '-'} :: {a.get('summary') or a.get('description') or '(无摘要)'}"
    )


class _Handler(BaseHTTPRequestHandler):
    log_path = "alerts.jsonl"
    shared_token = ""

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def _route(self) -> str:
        """路径路由：必须剥掉 query string，否则 ``/webhook?token=x`` 会被判成未知路径。"""
        from urllib.parse import urlparse

        return urlparse(self.path).path.rstrip("/") or "/"

    def do_GET(self):  # noqa: N802
        if self._route == "/health":
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        if self._route not in ("/webhook", "/"):
            self._send(404, b'{"error":"not found"}')
            return
        # 可选共享密钥：配了 ALERT_SINK_TOKEN 则要求 ?token= 一致（防内网误发/扫端口）
        if self.shared_token:
            from urllib.parse import parse_qs, urlparse

            qs = parse_qs(urlparse(self.path).query)
            if (qs.get("token") or [""])[0] != self.shared_token:
                self._send(401, b'{"error":"bad token"}')
                return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._send(400, json.dumps({"error": f"invalid json: {e}"}).encode())
            return

        alerts = normalize_payload(payload)
        # 按严重度排序输出，critical 先看到
        alerts.sort(key=lambda a: SEVERITY_ORDER.get(a.get("severity"), 99))
        try:
            written = persist(alerts, self.log_path)
        except OSError as e:  # 落盘失败也要回 200：不能让告警把 Alertmanager 打重试风暴
            print(f"[alert-sink] 落盘失败：{e}", file=sys.stderr)
            self._send(200, b'{"ok":true,"persisted":0,"error":"log write failed"}')
            return

        for a in alerts:
            print(format_line(a), flush=True)
        self._send(200, json.dumps({"ok": True, "persisted": written}).encode())

    def log_message(self, fmt: str, *args) -> None:  # 压掉默认访问日志，保持输出干净
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Alertmanager webhook 本地接收端")
    ap.add_argument("--host", default=os.getenv("ALERT_SINK_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.getenv("ALERT_SINK_PORT", "9101")))
    ap.add_argument(
        "--log",
        default=os.getenv(
            "ALERT_SINK_LOG",
            os.path.join("observability", "alert-sink-data", "alerts.jsonl"),
        ),
        help="告警 JSONL 落盘路径",
    )
    args = ap.parse_args()

    _Handler.log_path = args.log
    _Handler.shared_token = os.getenv("ALERT_SINK_TOKEN", "")

    os.makedirs(os.path.dirname(os.path.abspath(args.log)) or ".", exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(
        f"[alert-sink] 监听 {args.host}:{args.port}  落盘={os.path.abspath(args.log)}  "
        f"token={'已启用' if _Handler.shared_token else '未启用'}",
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
