#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V4.0-P1 网关流式计量回归测试（修 D-01）。

两层：
  A. SSEUsageTracker 单元用例（事件切分 / usage 位置 / CRLF / 任意字节切块 / 无 usage 估算 / 坏事件）
  B. 端到端：真网关(ThreadingHTTPServer + Handler) ⇄ 假上游(SSE)，走完整 HTTP 链路
     - 末块回报 usage           → reported，数值与上游一致
     - 上游不回 usage           → estimated，单列进 tokens_est_*
     - 上游拒收 stream_options  → 同 lane 去掉重发成功，记能力位；下一次不再注入
     - gzip 压缩的流            → 解压后照样拿到 usage
     - 非流式无 usage           → estimated
  另含 V3.2.1 旧逻辑复现：证明旧算法对同一条流只能得到 tin=0 / tout=2048。

用法：python -X utf8 test_gateway_usage.py      （stdlib only，退出码 0=全过）
"""
from __future__ import annotations

import gzip
import http.client
import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="gw_v4_test_")
_PRICES = os.path.join(_TMP, "prices.json")
with open(_PRICES, "w", encoding="utf-8") as f:
    json.dump({"models": {"fake-model": {"in": 1.0, "out": 2.0, "cache_read": 0.1}}}, f)
os.environ["STEWARD_PRICES"] = _PRICES
sys.path.insert(0, HERE)
import gateway  # noqa: E402


def sse(events, crlf=False) -> bytes:
    sep = "\r\n\r\n" if crlf else "\n\n"
    out = ""
    for e in events:
        out += "data: " + (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + sep
    return out.encode("utf-8")


def delta(text):
    return {"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}}]}


USAGE = {"prompt_tokens": 12345, "completion_tokens": 678,
         "prompt_tokens_details": {"cached_tokens": 1000}}


def stream_with_usage(n_chunks=40, text="施工中" * 20, usage_pos="end"):
    evs = [delta(text) for _ in range(n_chunks)]
    if usage_pos == "end":
        evs.append({"id": "x", "choices": [], "usage": USAGE})
    elif usage_pos == "middle":
        evs.insert(n_chunks // 2, {"id": "x", "choices": [], "usage": USAGE})
    evs.append("[DONE]")
    return sse(evs)


def v321_old_logic(raw: bytes):
    """V3.2.1 relay_stream 的原算法（逐字复刻），用于证明 bug。"""
    tail = b""
    for i in range(0, len(raw), 4096):
        tail = (tail + raw[i:i + 4096])[-8192:]
    tin, tout, tc = gateway.extract_usage(tail)
    if tin == 0 and tout == 0:
        tout = max(0, len(tail) // 4)
    return tin, tout


# ------------------------------------------------------------------ A. 单元


class TrackerUnit(unittest.TestCase):
    def run_tracker(self, raw, chunk=4096):
        t = gateway.SSEUsageTracker()
        for i in range(0, len(raw), chunk):
            t.feed(raw[i:i + chunk])
        t.close()
        return t

    def test_usage_at_end(self):
        t = self.run_tracker(stream_with_usage())
        self.assertEqual(t.result(0, 1.8), (12345, 678, 1000, "reported"))

    def test_usage_in_middle_kept(self):
        t = self.run_tracker(stream_with_usage(usage_pos="middle"))
        self.assertEqual(t.result(0, 1.8)[3], "reported")
        self.assertEqual(t.result(0, 1.8)[:2], (12345, 678))

    def test_last_usage_wins(self):
        raw = sse([delta("a"), {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                   delta("b"), {"choices": [], "usage": USAGE}, "[DONE]"])
        self.assertEqual(self.run_tracker(raw).result(0, 1.8)[:2], (12345, 678))

    def test_null_usage_chunks_ignored(self):
        evs = [dict(delta("x"), usage=None) for _ in range(5)] + [{"choices": [], "usage": USAGE}, "[DONE]"]
        self.assertEqual(self.run_tracker(sse(evs)).result(0, 1.8)[:2], (12345, 678))

    def test_crlf_and_random_byte_splits(self):
        raw = sse([delta("切块测试" * 7) for _ in range(30)] + [{"choices": [], "usage": USAGE}, "[DONE]"], crlf=True)
        rnd = random.Random(7)
        for _ in range(25):
            t = gateway.SSEUsageTracker()
            i = 0
            while i < len(raw):
                n = rnd.randint(1, 97)
                t.feed(raw[i:i + n])
                i += n
            t.close()
            self.assertEqual(t.result(0, 1.8), (12345, 678, 1000, "reported"))

    def test_no_usage_estimated_from_delta(self):
        raw = sse([delta("a" * 90) for _ in range(10)] + ["[DONE]"])
        tin, tout, tc, src = self.run_tracker(raw).result(500, 1.8)
        self.assertEqual(src, "estimated")
        self.assertEqual(tin, 500)
        self.assertEqual(tout, int(900 / 1.8))

    def test_tool_call_args_counted_in_estimate(self):
        ev = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "Edit", "arguments": "x" * 41}}]}}]}
        tin, tout, _, src = self.run_tracker(sse([ev, "[DONE]"])).result(0, 1.0)
        self.assertEqual((tout, src), (45, "estimated"))

    def test_bad_event_does_not_crash(self):
        raw = b"data: {not json}\n\n" + stream_with_usage(n_chunks=3)
        t = self.run_tracker(raw)
        self.assertEqual(t.parse_errors, 1)
        self.assertEqual(t.result(0, 1.8)[3], "reported")

    def test_long_stream_over_30kb(self):
        raw = stream_with_usage(n_chunks=400)
        self.assertGreater(len(raw), 30_000)
        self.assertEqual(self.run_tracker(raw).result(0, 1.8)[:2], (12345, 678))

    def test_v321_bug_reproduced(self):
        """旧算法：usage 明明在末块，也只能得到 tin=0, tout=2048（账本 daily_tokens_in=0 的根因）。"""
        raw = stream_with_usage(n_chunks=400)
        self.assertEqual(v321_old_logic(raw), (0, 2048))


# ------------------------------------------------------------------ B. 端到端


class FakeUpstream(BaseHTTPRequestHandler):
    mode = "usage_end"
    received: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        FakeUpstream.received.append(body)
        mode = FakeUpstream.mode
        if mode == "reject_stream_options" and "stream_options" in body:
            msg = b'{"error":{"message":"Unrecognized request argument supplied: stream_options"}}'
            self.send_response(400)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        if not body.get("stream"):
            obj = {"id": "x", "choices": [{"message": {"content": "ok" * 50}}]}
            if mode != "no_usage":
                obj["usage"] = USAGE
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        want_usage = (body.get("stream_options") or {}).get("include_usage") and mode in ("usage_end", "gzip")
        raw = stream_with_usage(n_chunks=200) if want_usage else sse([delta("a" * 90) for _ in range(10)] + ["[DONE]"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if mode == "gzip":
            raw = gzip.compress(raw)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        for i in range(0, len(raw), 1500):
            self.wfile.write(raw[i:i + 1500])
            self.wfile.flush()


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        up_url = "http://127.0.0.1:%d/v1" % cls.up.server_address[1]
        prov = os.path.join(_TMP, "provider_config.json")
        with open(prov, "w", encoding="utf-8") as f:
            json.dump({"config": {"providerConfigRules": {"providerRules": [{
                "providerId": "p-fake", "providerName": "Fake",
                "config": {"access": {"apiKey": "k"},
                           "api": {"type": "openai-chat-completions", "baseUrl": up_url},
                           "personalModelIds": ["fake-model"]}}]}}}, f)
        cls.cfg = {
            "state_dir": os.path.join(_TMP, "state"),
            "providers_config": prov,
            "policy": {"chars_per_token": 1.8, "timeout_sec": 20, "max_attempts": 2, "sticky": False},
            "aliases": {"auto": {"order": ["fake"]}},
            "lanes": [{"id": "fake", "providerId": "p-fake", "model": "fake-model", "tier": ["SIMPLE", "MEDIUM"]}],
        }

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def setUp(self):
        sd = self.cfg["state_dir"]
        if os.path.isdir(sd):
            for root, _, files in os.walk(sd):
                for fn in files:
                    os.remove(os.path.join(root, fn))
        self.st = gateway.State(self.cfg, self.cfg["state_dir"])
        self.gw = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        self.gw.state = self.st
        self.gw.cfg_path = os.path.join(_TMP, "routing.json")
        self.gw.base_dir = self.cfg["state_dir"]
        threading.Thread(target=self.gw.serve_forever, daemon=True).start()
        FakeUpstream.received = []

    def tearDown(self):
        self.gw.shutdown()
        self.gw.server_close()

    def call(self, stream=True):
        ok_before = self.st.total["ok"]
        code, data = self._post(stream)
        # 网关先回写响应再记账：等记账落地再断言（最多 3s），避免时序竞态
        deadline = time.time() + 3
        while code == 200 and self.st.total["ok"] <= ok_before and time.time() < deadline:
            time.sleep(0.02)
        return code, data

    def _post(self, stream):
        c = http.client.HTTPConnection("127.0.0.1", self.gw.server_address[1], timeout=20)
        body = json.dumps({"model": "auto", "stream": stream,
                           "messages": [{"role": "user", "content": "你好" * 90}]})
        c.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_e2e_reported(self):
        FakeUpstream.mode = "usage_end"
        code, data = self.call()
        self.assertEqual(code, 200)
        self.assertIn(b'"usage"', data)                                   # 客户端照样收到全量流
        self.assertEqual(FakeUpstream.received[0]["stream_options"], {"include_usage": True})
        tot = self.st.total
        self.assertEqual((tot["tokens_in"], tot["tokens_out"], tot["tokens_cached"]), (12345, 678, 1000))
        self.assertEqual(tot["estimated_requests"], 0)
        self.assertGreater(tot["cost_cny"], 0)

    def test_e2e_no_usage_estimated(self):
        FakeUpstream.mode = "no_usage"
        code, _ = self.call()
        self.assertEqual(code, 200)
        tot = self.st.total
        self.assertEqual(tot["estimated_requests"], 1)
        self.assertEqual(tot["tokens_est_out"], int(900 / 1.8))
        self.assertEqual(tot["tokens_est_in"], int(180 / 1.8))
        self.assertEqual(tot["tokens_out"], tot["tokens_est_out"])        # 估算已含在总数内，可拆出

    def test_e2e_reject_stream_options_then_capability(self):
        FakeUpstream.mode = "reject_stream_options"
        code, _ = self.call()
        self.assertEqual(code, 200)
        self.assertEqual(len(FakeUpstream.received), 2)                   # 带参被拒 → 去参重发
        self.assertNotIn("stream_options", FakeUpstream.received[1])
        self.assertIn("fake", self.st.no_stream_usage)
        self.assertEqual(self.st.stats["fake"]["fail"], 0)                 # 不计失败、不冷却
        self.assertEqual(self.st.cooldown_left("fake"), 0)
        self.call()
        self.assertEqual(len(FakeUpstream.received), 3)                   # 第二次直接不注入
        self.assertNotIn("stream_options", FakeUpstream.received[2])
        st2 = gateway.State(self.cfg, self.cfg["state_dir"])               # 能力位持久化
        self.assertIn("fake", st2.no_stream_usage)

    def test_e2e_gzip_stream(self):
        FakeUpstream.mode = "gzip"
        code, _ = self.call()
        self.assertEqual(code, 200)
        self.assertEqual(self.st.total["tokens_in"], 12345)

    def test_e2e_non_stream_estimated(self):
        FakeUpstream.mode = "no_usage"
        code, _ = self.call(stream=False)
        self.assertEqual(code, 200)
        self.assertEqual(self.st.total["estimated_requests"], 1)

    def test_e2e_non_stream_reported(self):
        FakeUpstream.mode = "usage_end"
        code, _ = self.call(stream=False)
        self.assertEqual(code, 200)
        self.assertEqual(self.st.total["tokens_in"], 12345)
        self.assertNotIn("stream_options", FakeUpstream.received[0])

    def test_snapshot_exposes_estimates(self):
        FakeUpstream.mode = "no_usage"
        self.call()
        snap = gateway.snapshot(self.st)
        lane = snap["lanes"][0]
        self.assertEqual(lane["estimated"], 1)
        self.assertEqual(lane["tokens_estimated"][1], int(900 / 1.8))
        self.assertEqual(snap["totals"]["usage_schema"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
