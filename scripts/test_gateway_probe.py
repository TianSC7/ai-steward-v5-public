# -*- coding: utf-8 -*-
"""gateway_probe 用例：本地假网关覆盖 健康/超时/坏JSON/冷却告警/降级/落盘/退出码。

假网关 = ThreadingHTTPServer + BaseHTTPRequestHandler，行为由类级开关控制：
  health/status 两个端点分别可设 ok / badjson / http500 / okfalse / slow。

覆盖：
  1  健康快照：health_ok/status_ok 为真，cooldowns/degraded 为空，退出码 0
  2  快照四字段齐备（验收口径：health_ok/status_ok/cooldowns/degraded）
  3  冷却告警：cooldown_left_sec>0 的 lane 进 cooldowns 清单，退出码 2
  4  全部可用 lane 冷却：degraded 出现汇总项，退出码 2
  5  不可用 lane：进 degraded 清单并带原因，退出码 2
  6  坏 JSON（/health 返回非 JSON）：health_ok=False 不抛栈，退出码 3
  7  坏 JSON（/admin/status 返回非 JSON）：status_ok=False，退出码 3
  8  /health 自报 ok=false：按 broken 处理，退出码 3
  9  /admin/status 返回 500：status_ok=False，退出码 3
 10  超时：服务端 sleep > 客户端短超时，快速失败不抛栈，退出码 3
 11  端口不通：子进程跑 main()，退出码 3 且 stderr 无 Traceback
 12  --write 落盘：原子写出的 JSON 含四字段，退出码 0

用法：python -X utf8 test_gateway_probe.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gateway_probe  # noqa: E402

# ---------------------------------------------------------------- 假网关

BEHAVIOR = {"health": "ok", "status": "ok"}   # ok | badjson | http500 | okfalse | slow
DELAY = {"health": 0.0, "status": 0.0}


def default_lanes() -> list:
    return [{"id": "lane-a", "provider": "P", "model": "m", "usable": True,
             "why_unusable": "", "cooldown_left_sec": 0, "last_error": ""},
            {"id": "lane-b", "provider": "P", "model": "m", "usable": True,
             "why_unusable": "", "cooldown_left_sec": 0, "last_error": ""}]


LANES = default_lanes()


def health_body() -> dict:
    return {"ok": True, "service": "steward-gateway", "version": "test", "uptime_sec": 7}


def status_body() -> dict:
    return {"service": "steward-gateway", "version": "test", "uptime_sec": 7,
            "lanes": LANES, "totals": {"requests": 3, "ok": 2, "fail": 1}}


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def _send(self, code: int, payload) -> None:
        body = payload if isinstance(payload, (bytes, bytearray)) \
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, key: str, payload) -> None:
        if DELAY[key]:
            time.sleep(DELAY[key])
        mode = BEHAVIOR[key]
        try:
            if mode == "badjson":
                self._send(200, b"not-a-json{")
            elif mode == "http500":
                self._send(500, {"error": "boom"})
            elif mode == "okfalse":
                self._send(200, {"ok": False})
            else:
                self._send(200, payload)
        except ConnectionError:
            pass  # 客户端已因超时断开，服务端别再炸栈

    def do_GET(self) -> None:
        p = urlparse(self.path).path
        if p == "/health":
            self._serve("health", health_body())
        elif p == "/admin/status":
            self._serve("status", status_body())
        else:
            self._send(404, {"error": "not found"})


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@contextmanager
def fake_gateway(health: str = "ok", status: str = "ok",
                 delay_health: float = 0.0, delay_status: float = 0.0,
                 lanes: list | None = None):
    """起一个本地假网关，退出时恢复类级开关。"""
    BEHAVIOR["health"], BEHAVIOR["status"] = health, status
    DELAY["health"], DELAY["status"] = delay_health, delay_status
    LANES[:] = default_lanes() if lanes is None else lanes
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        BEHAVIOR.update({"health": "ok", "status": "ok"})
        DELAY.update({"health": 0.0, "status": 0.0})
        LANES[:] = default_lanes()


# ---------------------------------------------------------------- 用例

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def cool_lane(lid: str, left: int) -> dict:
    return {"id": lid, "provider": "P", "model": "m", "usable": True,
            "why_unusable": "", "cooldown_left_sec": left, "last_error": "quota 402"}


def bad_lane(lid: str, why: str) -> dict:
    return {"id": lid, "provider": "P", "model": "m", "usable": False,
            "why_unusable": why, "cooldown_left_sec": 0, "last_error": ""}


@case
def case_healthy():
    with fake_gateway() as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        assert snap["health_ok"] is True, snap
        assert snap["status_ok"] is True, snap
        assert snap["cooldowns"] == [], snap
        assert snap["degraded"] == [], snap
        assert snap["service"] == "steward-gateway" and snap["version"] == "test", snap
        assert snap["lanes_total"] == 2 and snap["lanes_usable"] == 2, snap
        assert snap["totals"]["requests"] == 3, snap
        assert gateway_probe.verdict(snap) == 0, snap
        return "health=OK status=OK exit=0"


@case
def case_four_fields():
    with fake_gateway() as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        for key in ("health_ok", "status_ok", "cooldowns", "degraded"):
            assert key in snap, "缺字段 %s" % key
        return "health_ok/status_ok/cooldowns/degraded 齐备"


@case
def case_cooldown_alert():
    lanes = [cool_lane("lane-a", 300),
             {"id": "lane-b", "provider": "P", "model": "m", "usable": True,
              "why_unusable": "", "cooldown_left_sec": 0, "last_error": ""}]
    with fake_gateway(lanes=lanes) as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        assert len(snap["cooldowns"]) == 1, snap
        assert snap["cooldowns"][0]["lane"] == "lane-a", snap
        assert snap["cooldowns"][0]["left_sec"] == 300, snap
        assert any("lane-a" in a and "冷却" in a for a in snap["alerts"]), snap
        assert snap["degraded"] == [], snap
        assert gateway_probe.verdict(snap) == 2, snap
        return "cooldowns=[lane-a 300s] exit=2"


@case
def case_all_cooling_degraded():
    lanes = [cool_lane("lane-a", 120), cool_lane("lane-b", 60)]
    with fake_gateway(lanes=lanes) as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        assert len(snap["cooldowns"]) == 2, snap
        assert any(d["lane"] == "*" and "均在冷却" in d["reason"] for d in snap["degraded"]), snap
        assert gateway_probe.verdict(snap) == 2, snap
        return "degraded=[* 全部冷却] exit=2"


@case
def case_unusable_lane_degraded():
    lanes = [bad_lane("lane-a", "缺 baseUrl/apiKey"),
             {"id": "lane-b", "provider": "P", "model": "m", "usable": True,
              "why_unusable": "", "cooldown_left_sec": 0, "last_error": ""}]
    with fake_gateway(lanes=lanes) as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        assert snap["degraded"] == [{"lane": "lane-a", "reason": "缺 baseUrl/apiKey"}], snap
        assert any("lane-a" in a and "降级" in a for a in snap["alerts"]), snap
        assert gateway_probe.verdict(snap) == 2, snap
        return "degraded=[lane-a 缺 baseUrl/apiKey] exit=2"


@case
def case_bad_json_health():
    with fake_gateway(health="badjson") as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        assert snap["health_ok"] is False, snap
        assert snap["status_ok"] is True, snap
        assert snap["level"] == "broken", snap
        assert any("JSON" in e for e in snap["errors"]), snap
        assert gateway_probe.verdict(snap) == 3, snap
        return "health=FAIL(badjson) exit=3"


@case
def case_bad_json_status():
    with fake_gateway(status="badjson") as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        assert snap["health_ok"] is True, snap
        assert snap["status_ok"] is False, snap
        assert snap["level"] == "broken", snap
        assert snap["cooldowns"] == [] and snap["degraded"] == [], snap
        assert gateway_probe.verdict(snap) == 3, snap
        return "status=FAIL(badjson) exit=3"


@case
def case_health_ok_false():
    with fake_gateway(health="okfalse") as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        assert snap["health_ok"] is False, snap
        assert snap["level"] == "broken" and snap["unreachable"] is False, snap
        assert any("ok=False" in e for e in snap["errors"]), snap
        assert gateway_probe.verdict(snap) == 3, snap
        return "health ok=false exit=3"


@case
def case_status_http_500():
    with fake_gateway(status="http500") as port:
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        assert snap["status_ok"] is False, snap
        assert any("HTTP 500" in e for e in snap["errors"]), snap
        assert gateway_probe.verdict(snap) == 3, snap
        return "status=HTTP500 exit=3"


@case
def case_timeout():
    with fake_gateway(delay_health=3.0) as port:
        t0 = time.time()
        snap = gateway_probe.probe("127.0.0.1", port, timeout=0.5)
        elapsed = time.time() - t0
        assert snap["health_ok"] is False, snap
        assert snap["unreachable"] is True, snap
        assert any("timed out" in e or "timeout" in e.lower() for e in snap["errors"]), snap
        assert gateway_probe.verdict(snap) == 3, snap
        assert elapsed < 2.0, "短超时未生效: %.1fs" % elapsed
        return "timeout %.1fs 内失败 exit=3" % elapsed


@case
def case_unreachable_exit3_subprocess():
    port = free_port()  # 监听后立刻关闭 = 端口不通
    cmd = [sys.executable, "-X", "utf8", os.path.join(HERE, "gateway_probe.py"),
           "--port", str(port), "--timeout", "1"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 3, "rc=%s out=%s err=%s" % (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    assert "level=unreachable" in r.stdout, r.stdout
    return "rc=3 无栈 unreachable"


@case
def case_write_snapshot():
    with fake_gateway() as port, tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "nested", "gateway_health.json")
        snap = gateway_probe.probe("127.0.0.1", port, timeout=2.0)
        path = gateway_probe.write_snapshot(snap, out)
        assert path == out and os.path.exists(out), path
        with open(out, encoding="utf-8") as f:
            disk = json.load(f)
        for key in ("health_ok", "status_ok", "cooldowns", "degraded"):
            assert key in disk, "缺字段 %s" % key
        assert not os.path.exists(out + ".tmp"), "tmp 残留"
        # main() --write 裸参数（不带 PATH）：落 DEFAULT_STATE_PATH（打补丁到临时目录）
        patched = os.path.join(tmp, "state", "gateway_health.json")
        old_default = gateway_probe.DEFAULT_STATE_PATH
        gateway_probe.DEFAULT_STATE_PATH = patched
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf, \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = gateway_probe.main(["--port", str(port), "--timeout", "2",
                                         "--write", "--json"])
            assert rc == 0, buf.getvalue()
            assert os.path.exists(patched), "默认 state 路径未写"
            with open(patched, encoding="utf-8") as f:
                assert "health_ok" in json.load(f)
        finally:
            gateway_probe.DEFAULT_STATE_PATH = old_default
        return "落盘四字段齐备 + --write 裸参 OK"


def main() -> int:
    ok = 0
    for fn in CASES:
        try:
            detail = fn()
            print("[PASS] %-32s %s" % (fn.__name__[5:], detail))
            ok += 1
        except Exception as e:  # noqa: BLE001
            print("[FAIL] %-32s %s: %s" % (fn.__name__[5:], type(e).__name__, e))
    print("| %d passed / %d total" % (ok, len(CASES)))
    print("EXIT=%d" % (0 if ok == len(CASES) else 2))
    return 0 if ok == len(CASES) else 2


if __name__ == "__main__":
    sys.exit(main())
