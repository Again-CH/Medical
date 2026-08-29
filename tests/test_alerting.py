"""告警闭环测试：Alertmanager 配置有效性 + 本地接收端 alert_sink 的正确性。

验证三件事：
1. **配置不再有 null 占位** —— 告警必须发到真实 receiver（回归护栏，
   防止后人又把配置改回"能启动但哪儿也不发"的占位状态）。
2. **生产配置不内联密钥** —— webhook URL 必须用 ``url_file`` 引用密钥文件，
   绝不写进版本库。
3. **接收端能正确解析并落地告警** —— 含端到端 HTTP 回环（起真端口、发真 POST、
   验证落盘），而不只是测纯函数。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.alert_sink import (  # noqa: E402
    _Handler,
    format_line,
    normalize_payload,
    persist,
)

ALERTMANAGER_YML = os.path.join(ROOT, "observability", "alertmanager", "alertmanager.yml")
PRODUCTION_YML = os.path.join(ROOT, "observability", "alertmanager", "alertmanager.production.yml")


def _load_yaml(path: str) -> dict:
    import yaml  # 仅测试期依赖，运行期不引入

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# 一条贴近真实的 Alertmanager v4 webhook 载荷
REALISTIC_PAYLOAD = {
    "version": "4",
    "groupKey": '{}:{alertname="MedicalAgentSafetyGateSpike"}',
    "status": "firing",
    "receiver": "sink-critical",
    "groupLabels": {"alertname": "MedicalAgentSafetyGateSpike"},
    "commonLabels": {
        "alertname": "MedicalAgentSafetyGateSpike",
        "severity": "critical",
        "instance": "medical-agent:8000",
    },
    "commonAnnotations": {"summary": "安全闸命中数异常突增"},
    "externalURL": "http://localhost:9093",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "MedicalAgentSafetyGateSpike",
                "severity": "critical",
                "instance": "medical-agent:8000",
            },
            "annotations": {
                "summary": "安全闸命中数异常突增",
                "description": "10 分钟内安全闸命中 > 30，疑似攻击或提示词注入",
            },
            "startsAt": "2026-08-30T01:00:00.000Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://localhost:9090/graph?g0.expr=increase(...)",
        }
    ],
}


# ---------------- 1. 配置护栏 ----------------


def test_alertmanager_has_no_null_receiver():
    """回归护栏：本地配置不得再出现 null 占位 receiver。"""
    cfg = _load_yaml(ALERTMANAGER_YML)
    names = [r.get("name") for r in cfg.get("receivers", [])]
    assert names, "配置必须至少有一个 receiver"
    assert "null" not in names, f"不得再有 null 占位 receiver，当前：{names}"
    # 每个 receiver 都必须真的能发：至少配一个 webhook_configs
    for r in cfg["receivers"]:
        assert r.get("webhook_configs"), f"receiver {r.get('name')} 没有任何投递方式"


def test_alertmanager_routes_by_severity():
    """三个严重度分级路由，且 critical 的提醒节奏明显更快。"""
    cfg = _load_yaml(ALERTMANAGER_YML)
    routes = cfg["route"]["routes"]
    by_sev = {}
    for r in routes:
        for m in r.get("matchers", []):
            if "severity" in m:
                by_sev[m.split('"')[1]] = r
    assert set(by_sev) >= {"critical", "warning", "info"}, f"分级路由不全：{set(by_sev)}"
    # critical 应比 warning 更快触达、更频繁重复
    assert by_sev["critical"]["group_wait"] == "10s"
    assert by_sev["critical"]["repeat_interval"] == "30m"
    assert by_sev["warning"]["repeat_interval"] == "4h"
    # 抑制规则：critical 抑制同告警名的低级别，避免告警风暴
    assert cfg.get("inhibit_rules"), "缺抑制规则，一个故障会刷出一屏告警"


def test_production_config_does_not_inline_secrets():
    """生产配置必须用 url_file 引用密钥，禁止把含 key 的 URL 写进文件。"""
    cfg = _load_yaml(PRODUCTION_YML)
    for r in cfg["receivers"]:
        for w in r.get("webhook_configs", []):
            assert "url_file" in w, f"receiver {r.get('name')} 直接内联了 url（密钥会入库）"
            assert "url" not in w, "url 与 url_file 不可同时出现"


# ---------------- 2. 接收端纯函数 ----------------


def test_normalize_payload_realistic():
    rows = normalize_payload(REALISTIC_PAYLOAD)
    assert len(rows) == 1
    a = rows[0]
    assert a["alertname"] == "MedicalAgentSafetyGateSpike"
    assert a["severity"] == "critical"
    assert a["status"] == "firing"
    assert a["instance"] == "medical-agent:8000"
    assert a["summary"] == "安全闸命中数异常突增"
    assert a["starts_at"] == "2026-08-30T01:00:00.000Z"
    assert a["received_at"], "接收时间必须打上（用于算送达延迟）"


def test_malformed_alert_does_not_drop_batch():
    """单条告警格式异常不应丢掉整批 —— 告警系统本身不能成为故障点。"""
    payload = {
        "status": "firing",
        "alerts": [
            "不是字典",
            {
                "labels": {"alertname": "Good", "severity": "warning"},
                "annotations": {"summary": "这条应该被保留"},
            },
            {"labels": {"alertname": "NoSeverity"}},  # 缺 severity，应给默认值
        ],
    }
    rows = normalize_payload(payload)
    assert len(rows) == 2, f"坏数据不应影响同批好数据，实际解析出 {len(rows)} 条"
    assert rows[0]["alertname"] == "Good"
    assert rows[1]["severity"] == "unknown"


def test_normalize_rejects_garbage():
    assert normalize_payload({}) == []
    assert normalize_payload({"alerts": "不是列表"}) == []
    assert normalize_payload("完全不是 dict") == []


def test_persist_writes_jsonl(tmp_path):
    log = tmp_path / "alerts.jsonl"
    rows = normalize_payload(REALISTIC_PAYLOAD)
    assert persist(rows, str(log)) == 1
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["alertname"] == "MedicalAgentSafetyGateSpike"
    assert obj["severity"] == "critical"
    # 追加写：再写一条，文件应有两行
    persist(rows, str(log))
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_format_line_is_readable():
    a = normalize_payload(REALISTIC_PAYLOAD)[0]
    line = format_line(a)
    assert "CRITICAL" in line
    assert "MedicalAgentSafetyGateSpike" in line
    assert "安全闸命中数异常突增" in line


# ---------------- 3. 端到端 HTTP 回环 ----------------


def test_sink_http_roundtrip(tmp_path):
    """起真实端口 → POST 一条告警 → 200 且落盘，验证整条 HTTP 链路。"""
    log = tmp_path / "alerts.jsonl"

    class Handler(_Handler):
        pass

    Handler.log_path = str(log)
    Handler.shared_token = ""

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # 健康检查
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            assert r.status == 200

        # 投递告警
        body = json.dumps(REALISTIC_PAYLOAD).encode()
        req = Request(
            f"http://127.0.0.1:{port}/webhook",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=5) as r:
            assert r.status == 200
            assert json.loads(r.read().decode())["persisted"] == 1

        # 落盘校验
        obj = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[0])
        assert obj["alertname"] == "MedicalAgentSafetyGateSpike"
        assert obj["severity"] == "critical"
    finally:
        srv.shutdown()
        srv.server_close()


def test_sink_rejects_bad_token(tmp_path):
    """配了共享密钥时，token 不对应返回 401（防内网误发/扫端口）。"""
    log = tmp_path / "alerts.jsonl"

    class Handler(_Handler):
        pass

    Handler.log_path = str(log)
    Handler.shared_token = "s3cr3t"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        body = json.dumps(REALISTIC_PAYLOAD).encode()
        # 无 token
        req = Request(
            f"http://127.0.0.1:{port}/webhook",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(Exception) as ei:
            urlopen(req, timeout=5)  # noqa: S310 - 本地回环地址，测试专用
        assert "401" in str(ei.value)

        # 带正确 token → 通过
        req2 = Request(
            f"http://127.0.0.1:{port}/webhook?token=s3cr3t",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req2, timeout=5) as r:  # noqa: S310
            assert r.status == 200
    finally:
        srv.shutdown()
        srv.server_close()
