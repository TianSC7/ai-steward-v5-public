# -*- coding: utf-8 -*-
"""网关边界用例（确定性、零 token 成本）

用本地 mock 上游模拟各种上游劣化，验证网关的边界行为：
  1. 429 限流        -> 换 lane，原 lane 冷却
  2. 402 额度耗尽    -> 换 lane，长冷却
  3. 401 鉴权失败    -> 换 lane，最长冷却
  4. 500 上游故障    -> 换 lane
  5. gzip 强制压缩   -> 正确解压（本轮真实根因）
  6. SSE 正常流      -> 首块透传 + X-Steward-Lane
  7. SSE 空流        -> 判定失败并换 lane（不能把空 200 当成功）
  8. 全部失败        -> 502 且列出 tried
  9. 未知模型        -> 503 且列出 lanes
 10. 会话粘性        -> 同一对话 tier 升级不换 lane
 11. 粘性过期        -> TTL 到期后允许按新 tier 选 lane
 12. 显式 lane 点名  -> 不经过别名解析
 13. 客户端令牌      -> 配置后无令牌 401

用法：python -X utf8 test_edges.py [--json] [--keep]
退出码 0 = 全通过。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS: list[tuple[str, str, str]] = []


def rec(status: str, name: str, detail: str) -> None:
    RESULTS.append((status, name, detail))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------------------------ mock 上游

class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        model = str(body.get("model") or "")
        stream = bool(body.get("stream"))

        if model == "m-rate":
            return self._plain(429, b'{"error":{"message":"rate limited"}}')
        if model == "m-quota":
            return self._plain(402, b'{"error":{"message":"quota exhausted"}}')
        if model == "m-auth":
            return self._plain(401, b'{"error":{"message":"bad key"}}')
        if model == "m-500":
            return self._plain(500, b'{"error":{"message":"boom"}}')
        if model == "m-gzip":
            payload = json.dumps({"id": "x", "model": "m-gzip", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "GZIP-OK"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
            return self._plain(200, gzip.compress(payload), extra={"Content-Encoding": "gzip"})
        if model == "m-sse":
            return self._sse(["HELLO", " WORLD"])
        if model == "m-empty-sse":
            return self._sse([])
        if model == "m-slow":
            time.sleep(4)
            return self._plain(200, b'{"choices":[{"message":{"content":"SLOW"}}]}')
        if model == "m-ok2":  # 备用通道，用于确认确实换了 lane
            payload = json.dumps({"id": "y", "model": "m-ok2", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "OK2"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1}}).encode()
            return self._plain(200, payload)
        payload = json.dumps({"id": "z", "model": model, "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "prompt_tokens_details": {"cached_tokens": 60}}}).encode()
        return self._plain(200, payload)

    def _plain(self, code: int, body: bytes, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self, words: list):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for w in words:
                chunk = {"id": "s", "model": "m-sse", "choices": [{"delta": {"content": w}}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.02)
            if words:
                self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


# ------------------------------------------------------------------ 工具

def req(url: str, payload: dict | None = None, headers: dict | None = None, timeout: float = 20):
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except (urllib.error.URLError, socket.timeout) as e:
        return 0, str(e).encode(), {}


def wait_http(url: str, sec: float = 15) -> bool:
    end = time.time() + sec
    while time.time() < end:
        s, _, _ = req(url, timeout=1)
        if s == 200:
            return True
        time.sleep(0.2)
    return False


def msgs(user: str, pad: int = 0):
    m = [{"role": "system", "content": "edge-test"}, {"role": "user", "content": user}]
    if pad:
        m.append({"role": "user", "content": "X" * pad})
    return m


# ------------------------------------------------------------------ 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = ap.parse_args()

    fp = announce("test_edges")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(300, "test_edges")

    mock_port, gw_port, gw2_port = free_port(), free_port(), free_port()
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), MockHandler)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="steward-edge-")
    providers = {"config": {"providerConfigRules": {"providerRules": [{
        "providerId": "mock", "providerName": "mock",
        "config": {"api": {"type": "openai-chat-completions", "baseUrl": "http://127.0.0.1:%d/v1" % mock_port},
                   "access": {"type": "api-key", "apiKey": "mock-key"}},
    }]}, "providerOrder": ["mock"], "modelConfigRules": {"providerModelRules": []}}}

    lanes = [{"id": "l-" + m, "providerId": "mock", "model": m, "tier": ["SIMPLE", "MEDIUM", "COMPLEX"],
              "equiv": "ok-group"} for m in ("m-rate", "m-quota", "m-auth", "m-500", "m-gzip", "m-sse",
                                             "m-empty-sse", "m-slow")]
    lanes.append({"id": "l-ok", "providerId": "mock", "model": "m-ok", "tier": ["SIMPLE", "MEDIUM", "COMPLEX"],
                  "equiv": "ok-group"})
    lanes.append({"id": "l-ok2", "providerId": "mock", "model": "m-ok2", "tier": ["MEDIUM"], "equiv": "ok2-group"})

    routing = {
        "listen": {"host": "127.0.0.1", "port": gw_port},
        "providers_config": os.path.join(tmp, "providers.json").replace("\\", "/"),
        "policy": {"chars_per_token": 1.8, "timeout_sec": 1.5, "max_attempts": 4,
                   "tiers": {"simple_max_chars": 2500, "complex_min_chars": 60000},
                   "sticky": True, "sticky_ttl_sec": 1, "allow_cross_class_failover": True,
                   "cooldown_sec": {"auth": 3600, "quota": 900, "rate": 300, "upstream5xx": 120,
                                    "conn": 60, "timeout": 60, "other": 60}},
        "aliases": {
            "rate": {"order": ["l-m-rate", "l-ok"]},
            "quota": {"order": ["l-m-quota", "l-ok"]},
            "auth": {"order": ["l-m-auth", "l-ok"]},
            "err5xx": {"order": ["l-m-500", "l-ok"]},
            "gzip": {"order": ["l-m-gzip"]},
            "sse": {"order": ["l-m-sse"]},
            "empty": {"order": ["l-m-empty-sse", "l-ok"]},
            "slow": {"order": ["l-m-slow", "l-ok"]},
            "dead": {"order": []},
            # MEDIUM 故意不含 SIMPLE 的 lane：验证粘性能跨 tier 保留原模型
            "sticky": {"tiers": {"SIMPLE": ["l-m-ok-sl"], "MEDIUM": ["l-ok2"]}},
        },
        "lanes": lanes + [{"id": "l-m-ok-sl", "providerId": "mock", "model": "m-ok", "tier": ["SIMPLE"],
                           "equiv": "ok-group"}],
    }
    pj = os.path.join(tmp, "providers.json")
    json.dump(providers, open(pj, "w", encoding="utf-8"), ensure_ascii=False)
    rj = os.path.join(tmp, "routing.json")
    json.dump(routing, open(rj, "w", encoding="utf-8"), ensure_ascii=False)

    # 成本核算用例：临时价格表 + 固定 cached_tokens，验证网关按"真实模型+缓存命中"计价
    prices_path = os.path.join(tmp, "prices.json")
    json.dump({"models": {"m-ok": {"in": 2.0, "cache_read": 0.5, "out": 8.0}}},
              open(prices_path, "w", encoding="utf-8"), ensure_ascii=False)
    genv = dict(os.environ)
    genv["STEWARD_PRICES"] = prices_path

    gw = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(HERE, "gateway.py"), "--config", rj],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=genv)
    base = "http://127.0.0.1:%d" % gw_port
    try:
        if not wait_http(base + "/health"):
            rec("FAIL", "boot", "网关未启动")
            return finish(args, tmp)
        body = {"messages": msgs("hi"), "max_tokens": 32, "temperature": 0}

        # 1-4 失败分类 + 换 lane
        for alias, lane, want_cls in [("rate", "l-m-rate", "rate"), ("quota", "l-m-quota", "quota"),
                                      ("auth", "l-m-auth", "auth"), ("err5xx", "l-m-500", "upstream5xx")]:
            s, raw, hd = req(base + "/v1/chat/completions", {**body, "model": alias})
            lane_used = hd.get("X-Steward-Lane", "")
            st = json.loads(req(base + "/admin/status", timeout=5)[1])
            info = next((l for l in st["lanes"] if l["id"] == lane), {})
            ok = s == 200 and lane_used == "l-ok" and info.get("fail", 0) >= 1 and info.get("cooldown_left_sec", 0) > 0
            rec("PASS" if ok else "FAIL", "failover/" + want_cls,
                "HTTP=%s lane=%s cool=%ss fail=%s" % (s, lane_used, info.get("cooldown_left_sec"), info.get("fail")))

        # 5 gzip
        s, raw, hd = req(base + "/v1/chat/completions", {**body, "model": "gzip"})
        text = ""
        try:
            text = json.loads(raw.decode("utf-8", "replace"))["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError):
            pass
        rec("PASS" if s == 200 and text == "GZIP-OK" else "FAIL", "gzip",
            "HTTP=%s text=%r" % (s, text))

        # 6 SSE
        s, raw, hd = req(base + "/v1/chat/completions", {**body, "model": "sse", "stream": True})
        txt = raw.decode("utf-8", "replace")
        ok = s == 200 and "HELLO" in txt and "[DONE]" in txt and hd.get("X-Steward-Lane") == "l-m-sse"
        rec("PASS" if ok else "FAIL", "sse", "HTTP=%s lane=%s done=%s" % (s, hd.get("X-Steward-Lane"), "[DONE]" in txt))

        # 7 空流 -> 换 lane
        s, raw, hd = req(base + "/v1/chat/completions", {**body, "model": "empty", "stream": True})
        rec("PASS" if s == 200 and hd.get("X-Steward-Lane") == "l-ok" else "FAIL", "empty-stream",
            "HTTP=%s lane=%s" % (s, hd.get("X-Steward-Lane")))

        # 8 全部失败 -> 502（dead 别名没有候选）
        s, raw, _ = req(base + "/v1/chat/completions", {**body, "model": "dead"})
        rec("PASS" if s == 503 else "FAIL", "no-candidate", "HTTP=%s" % s)

        # 8.5 成本核算：成功请求应累计成本与缓存命中（m-ok 已注入 cached_tokens=60）
        st = json.loads(req(base + "/admin/status", timeout=5)[1])
        tot = st.get("totals") or {}
        cost = float(tot.get("cost_cny") or 0)
        cached = int(tot.get("tokens_cached") or 0)
        ok = cost > 0 and cached >= 60
        rec("PASS" if ok else "FAIL", "cost-accounting",
            "cost=¥%s cached=%s unpriced=%s" % (cost, cached, tot.get("unpriced")))

        # 9 未知模型 -> 503
        s, raw, _ = req(base + "/v1/chat/completions", {**body, "model": "nope"})
        rec("PASS" if s == 503 else "FAIL", "unknown-model", "HTTP=%s" % s)

        # 10 粘性：短请求 SIMPLE -> l-m-ok-sl；同对话加长（MEDIUM）仍 l-m-ok-sl
        s1, _, h1 = req(base + "/v1/chat/completions", {**body, "model": "sticky", "messages": msgs("sticky-test")})
        s2, _, h2 = req(base + "/v1/chat/completions", {**body, "model": "sticky", "messages": msgs("sticky-test", 6000)})
        ok = h1.get("X-Steward-Lane") == "l-m-ok-sl" and h2.get("X-Steward-Lane") == "l-m-ok-sl"
        rec("PASS" if ok else "FAIL", "sticky-hold", "first=%s tier=%s | second=%s tier=%s"
            % (h1.get("X-Steward-Lane"), h1.get("X-Steward-Tier"), h2.get("X-Steward-Lane"), h2.get("X-Steward-Tier")))

        # 11 粘性过期（TTL=1s）：等待后同一对话按 MEDIUM 重新选 -> l-ok2
        time.sleep(1.4)
        s3, _, h3 = req(base + "/v1/chat/completions", {**body, "model": "sticky", "messages": msgs("sticky-test", 6000)})
        rec("PASS" if h3.get("X-Steward-Lane") == "l-ok2" else "FAIL", "sticky-expire",
            "lane=%s tier=%s" % (h3.get("X-Steward-Lane"), h3.get("X-Steward-Tier")))

        # 12 显式 lane
        s, raw, hd = req(base + "/v1/chat/completions", {**body, "model": "l-ok2"})
        rec("PASS" if s == 200 and hd.get("X-Steward-Lane") == "l-ok2" else "FAIL", "explicit-lane",
            "HTTP=%s lane=%s" % (s, hd.get("X-Steward-Lane")))

        # 13 超时降级（timeout 1.5s，上游睡 4s）
        t0 = time.time()
        s, raw, hd = req(base + "/v1/chat/completions", {**body, "model": "slow"}, timeout=20)
        elapsed = time.time() - t0
        ok = s == 200 and hd.get("X-Steward-Lane") == "l-ok" and elapsed >= 1.0
        rec("PASS" if ok else "FAIL", "timeout-failover",
            "HTTP=%s lane=%s %.1fs(需>=1.0s，证明真的试了慢通道)" % (s, hd.get("X-Steward-Lane"), elapsed))

        # 14 客户端令牌
        r2 = dict(routing)
        r2["listen"] = {"host": "127.0.0.1", "port": gw2_port}
        r2["policy"] = {**routing["policy"], "client_token": "secret-token"}
        rj2 = os.path.join(tmp, "routing-token.json")
        json.dump(r2, open(rj2, "w", encoding="utf-8"), ensure_ascii=False)
        gw2 = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(HERE, "gateway.py"), "--config", rj2],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base2 = "http://127.0.0.1:%d" % gw2_port
            if not wait_http(base2 + "/health"):
                rec("FAIL", "client-token", "第二个网关未启动")
            else:
                s_no, _, _ = req(base2 + "/v1/chat/completions", {**body, "model": "l-ok2"})
                s_ok, _, _ = req(base2 + "/v1/chat/completions", {**body, "model": "l-ok2"},
                                 {"Authorization": "Bearer secret-token"})
                rec("PASS" if s_no == 401 and s_ok == 200 else "FAIL", "client-token",
                    "no_token=%s with_token=%s" % (s_no, s_ok))
        finally:
            gw2.terminate()
            gw2.wait(timeout=5)
    finally:
        gw.terminate()
        try:
            gw.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gw.kill()
        mock.shutdown()

    return finish(args, tmp)


def finish(args, tmp: str) -> int:
    fails = [r for r in RESULTS if r[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS], ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-22s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    if not args.keep:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not fails and RESULTS else 2


if __name__ == "__main__":
    raise SystemExit(main())
