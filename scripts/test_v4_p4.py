#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V4.0-P4 安全收口回归测试：board_server（登录 / 会话 / 防伪造 / Host 白名单 / 限速 / 审计 / 轮换口令）、
gateway（去 CORS / Host 白名单 / admin 鉴权）、内核访问 8900 改请求头、看板前端补丁、暴露面验收工具。

全部在临时目录 + 随机端口上起真服务，走真实 HTTP；mes_board / v3_data / 内核 reject 用替身。
用法：python -X utf8 test_v4_p4.py
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TOOLS = os.path.join(HERE, "..", "tools") if os.path.isdir(os.path.join(HERE, "..", "tools")) else HERE
sys.path.insert(0, TOOLS)
TMP = tempfile.mkdtemp(prefix="v4_p4_test_")
TOKEN = "zc-TEST-token-123456"


class _Stub:
    pass


mb = _Stub()
mb.render = lambda: "<html><body>首屏</body></html>"
mb.seat_page = mb.card_page = mb.cards_page = lambda x: "<html>%s</html>" % x
vd = _Stub()
vd.collect_v3_data = lambda **k: {"orders": [], "v3_pipeline": {"cost_per_accepted_cny": 0.1}}
vd.collect_live_agent_stream = lambda: {"items": [], "ts": "12:00:00"}
sys.modules.setdefault("mes_board", mb)
sys.modules.setdefault("v3_data", vd)

import board_server  # noqa: E402
import gateway  # noqa: E402

BS = vars(board_server)
GW = vars(gateway)
REJECTS: list = []


def _fake_reject(order, reason):
    REJECTS.append((order, reason))
    return 0, json.dumps({"ok": True, "job": {"order_id": order + "-R1"}}).encode("utf-8")


BS["run_reject"] = _fake_reject
os.makedirs(os.path.join(TMP, "dashboard"), exist_ok=True)
with open(os.path.join(TMP, "dashboard", "v2.html"), "w", encoding="utf-8") as _f:
    _f.write("<html><body>v2</body></html>")
BS["DASH_DIR"] = os.path.join(TMP, "dashboard")
BS["TOKEN_FILE"] = os.path.join(TMP, "board_token.txt")
BS["SESSIONS_FILE"] = os.path.join(TMP, "board_sessions.json")
BS["LOG_DIR"] = os.path.join(TMP, "logs")
with open(BS["TOKEN_FILE"], "w", encoding="ascii") as _f:
    _f.write(TOKEN)


def start_board(**over):
    cfg = dict(BS["DEFAULT_CONFIG"])
    cfg.update({"allowed_hosts": ["*.ts.net", "board.example.org"], "reject_min_interval_sec": 30})
    cfg.update(over)
    srv = BS["make_server"](cfg, port=0, host="127.0.0.1", token=None,
                            sessions=BS["Sessions"](BS["SESSIONS_FILE"], cfg["session_ttl_hours"]),
                            audit=BS["Audit"](BS["LOG_DIR"]))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(srv, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    h = {"Host": "127.0.0.1:%d" % srv.server_address[1]}
    h.update(headers or {})
    if isinstance(body, str):
        body = body.encode("utf-8")
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    hd = {k.lower(): v for k, v in r.getheaders()}
    sc = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
    c.close()
    return r.status, hd, data, sc


def login(srv, tok=TOKEN, headers=None):
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    h.update(headers or {})
    return call(srv, "POST", "/login", h, "token=%s&next=/v2.html" % tok)


def sid_of(set_cookies):
    for c in set_cookies:
        if c.startswith("board_s="):
            return c.split(";", 1)[0][len("board_s="):]
    return ""


def audit_text():
    d = BS["LOG_DIR"]
    if not os.path.isdir(d):
        return ""
    return "".join(open(os.path.join(d, f), encoding="utf-8").read() for f in sorted(os.listdir(d)))


class Board(unittest.TestCase):
    def setUp(self):
        for p in (BS["SESSIONS_FILE"],):
            if os.path.exists(p):
                os.remove(p)
        REJECTS.clear()
        self.srv = start_board()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_healthz_and_unauth(self):
        self.assertEqual(call(self.srv, "GET", "/healthz")[0], 200)
        self.assertEqual(call(self.srv, "GET", "/v2.html")[0], 403)                    # 脚本（工厂实拍的未授权检查）
        c, hd, _, _ = call(self.srv, "GET", "/v2.html", {"Accept": "text/html"})
        self.assertEqual((c, hd.get("location")), (303, "/login?next=/v2.html"))

    def test_login_flow_and_cookie_flags(self):
        c, hd, body, sc = login(self.srv, "wrong")
        self.assertEqual(c, 403)
        self.assertIn("口令不正确".encode("utf-8"), body)
        c, hd, _, sc = login(self.srv)
        self.assertEqual((c, hd.get("location")), (303, "/v2.html"))
        ck = [x for x in sc if x.startswith("board_s=")][0]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=604800"):
            self.assertIn(flag, ck)
        self.assertNotIn("Secure", ck)                                                   # 本机 http 不加
        c, hd, body, _ = call(self.srv, "GET", "/v2.html", {"Cookie": "board_s=" + sid_of(sc)})
        self.assertEqual((c, body), (200, b"<html><body>v2</body></html>"))
        for k in ("x-content-type-options", "referrer-policy", "content-security-policy", "x-frame-options"):
            self.assertIn(k, hd)
        self.assertEqual(call(self.srv, "GET", "/data.json", {"Cookie": "board_s=" + sid_of(sc)})[0], 200)

    def test_secure_cookie_behind_https_tunnel(self):
        _, _, _, sc = login(self.srv, headers={"X-Forwarded-Proto": "https", "Host": "aa.tail1234.ts.net",
                                               "X-Forwarded-For": "100.64.1.7"})
        self.assertIn("Secure", [x for x in sc if x.startswith("board_s=")][0])

    def test_lockout(self):
        for _ in range(4):
            self.assertEqual(login(self.srv, "bad")[0], 403)
        self.assertEqual(login(self.srv, "bad")[0], 429)
        self.assertEqual(login(self.srv)[0], 429)                                         # 锁定期内正确口令也不行
        self.assertEqual(login(self.srv, headers={"X-Forwarded-For": "100.64.1.9"})[0], 303)   # 不同来源不受牵连

    def test_legacy_query_loopback_only(self):
        c, _, _, _ = call(self.srv, "GET", "/data.json?k=" + TOKEN)
        self.assertEqual(c, 200)                                                          # 本机直连：内核/脚本兼容
        c, hd, _, sc = call(self.srv, "GET", "/v2.html?k=%s&x=1" % TOKEN, {"Accept": "text/html"})
        self.assertEqual((c, hd.get("location")), (303, "/v2.html?x=1"))                  # 去掉网址里的口令
        self.assertTrue(sid_of(sc))
        c, _, _, _ = call(self.srv, "GET", "/data.json?k=" + TOKEN, {"Host": "aa.tail1234.ts.net", "X-Forwarded-For": "100.64.1.7"})
        self.assertEqual(c, 403)                                                          # 隧道上 ?k= 一律拒
        c, _, _, _ = call(self.srv, "GET", "/data.json?k=" + TOKEN, {"Tailscale-User-Login": "x@y"})
        self.assertEqual(c, 403)                                                          # 带转发头即不算本机
        self.assertEqual(call(self.srv, "GET", "/data.json?k=nope")[0], 403)

    def test_legacy_modes(self):
        self.srv.cfg["legacy_query_token"] = "off"
        self.assertEqual(call(self.srv, "GET", "/data.json?k=" + TOKEN)[0], 403)
        self.srv.cfg["legacy_query_token"] = "any"
        self.assertEqual(call(self.srv, "GET", "/data.json?k=" + TOKEN, {"Host": "aa.x.ts.net", "X-Forwarded-For": "1.2.3.4"})[0], 200)

    def test_legacy_cookie_loopback_only(self):
        self.assertEqual(call(self.srv, "GET", "/data.json", {"Cookie": "board_k=" + TOKEN})[0], 200)
        self.assertEqual(call(self.srv, "GET", "/data.json", {"Cookie": "board_k=" + TOKEN, "Host": "aa.x.ts.net",
                                                              "X-Forwarded-For": "1.2.3.4"})[0], 403)

    def test_header_token(self):
        self.assertEqual(call(self.srv, "GET", "/v2.html", {"X-Board-Token": TOKEN})[0], 200)
        self.assertEqual(call(self.srv, "GET", "/v2.html", {"X-Board-Token": "bad"})[0], 403)

    def test_host_allowlist(self):
        self.assertEqual(call(self.srv, "GET", "/v2.html", {"X-Board-Token": TOKEN, "Host": "evil.com"})[0], 421)
        self.assertEqual(call(self.srv, "GET", "/v2.html", {"X-Board-Token": TOKEN, "Host": "aa.tail1234.ts.net"})[0], 200)
        self.assertEqual(call(self.srv, "GET", "/v2.html", {"X-Board-Token": TOKEN, "Host": "ts.net"})[0], 421)
        self.assertEqual(call(self.srv, "GET", "/v2.html", {"X-Board-Token": TOKEN, "Host": "board.example.org:443"})[0], 200)
        self.assertEqual(call(self.srv, "POST", "/login", {"Host": "evil.com"}, "token=" + TOKEN)[0], 421)

    def test_reject_csrf_and_rate(self):
        _, _, _, sc = login(self.srv)
        ck = {"Cookie": "board_s=" + sid_of(sc), "Content-Type": "application/json"}
        body = json.dumps({"order": "C-20261121-001", "reason": "导航太高"})
        self.assertEqual(call(self.srv, "GET", "/api/v3/reject?order=C-20261121-001", ck)[0], 405)
        self.assertEqual(call(self.srv, "POST", "/api/v3/reject", ck, body)[0], 403)               # 缺 X-Board-Action
        evil = dict(ck, **{"X-Board-Action": "1", "Origin": "https://evil.example.com"})
        self.assertEqual(call(self.srv, "POST", "/api/v3/reject", evil, body)[0], 403)            # 跨站 Origin
        good = dict(ck, **{"X-Board-Action": "1", "Origin": "http://127.0.0.1:%d" % self.srv.server_address[1]})
        c, _, data, _ = call(self.srv, "POST", "/api/v3/reject", good, body)
        self.assertEqual((c, json.loads(data)["job"]["order_id"]), (200, "C-20261121-001-R1"))
        self.assertEqual(REJECTS, [("C-20261121-001", "导航太高")])
        self.assertEqual(call(self.srv, "POST", "/api/v3/reject", good, body)[0], 429)            # 30 秒限速
        self.srv.last_reject = 0
        bad = json.dumps({"order": "../../etc", "reason": "x"})
        self.assertEqual(call(self.srv, "POST", "/api/v3/reject", good, bad)[0], 400)
        self.assertEqual(len(REJECTS), 1)

    def test_reject_with_header_token_script(self):
        c, _, _, _ = call(self.srv, "POST", "/api/v3/reject", {"X-Board-Token": TOKEN, "Content-Type": "application/json"},
                          json.dumps({"order": "C-1"}))
        self.assertEqual(c, 200)
        self.assertEqual(REJECTS[-1], ("C-1", "发起人24h一键打回重做"))

    def test_archive_disabled_by_default(self):
        self.assertEqual(call(self.srv, "POST", "/api/v3/archive", {"X-Board-Token": TOKEN})[0], 403)
        self.assertEqual(call(self.srv, "GET", "/api/v3/archive", {"X-Board-Token": TOKEN})[0], 405)

    def test_logout_and_persistence(self):
        _, _, _, sc = login(self.srv)
        sid = sid_of(sc)
        self.srv.shutdown()
        self.srv.server_close()
        self.srv = start_board()                                                          # 重启后会话仍有效
        self.assertEqual(call(self.srv, "GET", "/data.json", {"Cookie": "board_s=" + sid})[0], 200)
        c, _, _, sc2 = call(self.srv, "POST", "/logout", {"Cookie": "board_s=" + sid, "X-Board-Action": "1"})
        self.assertEqual(c, 200)
        self.assertTrue(any("Max-Age=0" in x for x in sc2))
        self.assertEqual(call(self.srv, "GET", "/data.json", {"Cookie": "board_s=" + sid})[0], 403)
        raw = open(BS["SESSIONS_FILE"], encoding="utf-8").read()
        self.assertNotIn(sid, raw)                                                        # 只存哈希

    def test_safe_next(self):
        sn = BS["safe_next"]
        self.assertEqual([sn(x) for x in ("/v2.html", "//evil.com", "http://evil.com", "/a\\b", "")], ["/v2.html", "/", "/", "/", "/"])
        c, _, body, _ = call(self.srv, "GET", "/login?next=%2F%2Fevil.com")
        self.assertIn(b'value="/"', body)

    def test_audit_has_events_no_secrets(self):
        login(self.srv, "wrong-guess")
        _, _, _, sc = login(self.srv)
        call(self.srv, "GET", "/v2.html", {"Host": "evil.com"})
        for _ in range(3):
            call(self.srv, "GET", "/data.json", {"Cookie": "board_s=" + sid_of(sc)})
        txt = audit_text()
        for ev in ('"login_fail"', '"login_ok"', '"denied"'):
            self.assertIn(ev, txt)
        self.assertNotIn(TOKEN, txt)
        self.assertNotIn("wrong-guess", txt)
        self.assertNotIn(sid_of(sc), txt)
        self.assertLessEqual(txt.count('"/data.json"'), 1)                                # 轮询不逐条记

    def test_refuses_public_bind(self):
        cfg = dict(BS["DEFAULT_CONFIG"])
        with self.assertRaises(SystemExit):
            BS["make_server"](cfg, port=0, host="0.0.0.0")


class Rotate(unittest.TestCase):
    def test_rotate(self):
        s = BS["Sessions"](BS["SESSIONS_FILE"])
        sid = s.create("x", "ua")
        new = BS["rotate_token"]()
        self.assertNotEqual(new, TOKEN)
        self.assertTrue(new.startswith("zc-") and len(new) >= 20)
        self.assertFalse(BS["Sessions"](BS["SESSIONS_FILE"]).valid(sid))
        with open(BS["TOKEN_FILE"], "w", encoding="ascii") as f:
            f.write(TOKEN)


class Gateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prov = os.path.join(TMP, "provider_config.json")
        json.dump({"config": {"providerConfigRules": {"providerRules": []}}}, open(prov, "w", encoding="utf-8"))
        cls.cfg = {"state_dir": os.path.join(TMP, "gwstate"), "providers_config": prov,
                   "policy": {"client_token": "gw-secret", "timeout_sec": 5}, "aliases": {}, "lanes": []}

    def setUp(self):
        st = GW["State"](self.cfg, self.cfg["state_dir"])
        self.srv = GW["ThreadingHTTPServer"](("127.0.0.1", 0), GW["Handler"])
        self.srv.state = st
        self.srv.cfg_path = os.path.join(TMP, "routing.json")
        json.dump(self.cfg, open(self.srv.cfg_path, "w", encoding="utf-8"))
        self.srv.base_dir = self.cfg["state_dir"]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def g(self, method, path, headers=None, body=None):
        return call(self.srv, method, path, headers, body)

    def test_no_cors(self):
        c, hd, _, _ = self.g("GET", "/health", {"Origin": "https://evil.com"})
        self.assertEqual(c, 200)
        self.assertNotIn("access-control-allow-origin", hd)

    def test_admin_status(self):
        self.assertEqual(self.g("GET", "/admin/status")[0], 200)                         # 内核本机读
        self.assertEqual(self.g("GET", "/admin/status", {"Origin": "https://evil.com"})[0], 403)
        self.assertEqual(self.g("GET", "/admin/status", {"Origin": "https://evil.com", "Authorization": "Bearer gw-secret"})[0], 200)
        self.assertEqual(self.g("GET", "/admin/status", {"Host": "evil.com"})[0], 421)
        self.assertEqual(self.g("GET", "/admin/ledger", {"Origin": "null"})[0], 403)

    def test_admin_reload(self):
        self.assertEqual(self.g("POST", "/admin/reload", {"Origin": "https://evil.com"})[0], 403)
        self.assertEqual(self.g("POST", "/admin/reload")[0], 200)

    def test_chat_auth(self):
        body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(self.g("POST", "/v1/chat/completions", {"Content-Type": "application/json"}, body)[0], 401)
        self.assertEqual(self.g("POST", "/v1/chat/completions", {"Authorization": "Bearer wrong"}, body)[0], 401)
        c = self.g("POST", "/v1/chat/completions", {"Authorization": "Bearer gw-secret"}, body)[0]
        self.assertNotIn(c, (401, 403, 421))                                              # 过了鉴权（无可用 lane → 503）
        self.assertEqual(self.g("POST", "/v1/chat/completions", {"x-api-key": "gw-secret", "Host": "evil.com"}, body)[0], 421)


class KernelHeader(unittest.TestCase):
    def test_board_request_and_http_get_status(self):
        try:
            import factory_runner
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"factory_runner 不可导入: {exc}")
        FR = vars(factory_runner)
        rq = FR["board_request"]("http://127.0.0.1:1/v2.html", "abc")
        self.assertEqual(rq.get_header("X-board-token"), "abc")
        self.assertNotIn("k=", rq.full_url)
        srv = start_board()
        try:
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            self.assertEqual(FR["http_get_status"](base + "/v2.html")[0], 403)
            self.assertEqual(FR["http_get_status"](base + "/v2.html", {"X-Board-Token": TOKEN})[0], 200)
        finally:
            srv.shutdown()
            srv.server_close()


class DashboardPatch(unittest.TestCase):
    FIX = ("<html><head><style>.a{color:#0f172a}</style></head><body><div id=x></div><script>\n'use strict';\n"
           "var K = new URLSearchParams(location.search).get('k') || '';\n"
           "function rejectUrl(reason){\n  return '/api/v3/reject?order=C-20261121-001&reason='+encodeURIComponent(reason||'发起人24h一键打回重做')+\n    (K?('&k='+encodeURIComponent(K)):'');\n}\n"
           "function doReject(){ var reason='x';\n  return apiFetch(rejectUrl(String(reason||'').trim()),{cache:'no-store'}).then(function(r){ return r; })"
           ".catch(function(e){ showReject('打回未受理：'+'（需带访问口令打开本页）',false); }); }\n"
           "window._v3ArchiveOldSessions=function(){ var q=location.search||''; apiFetch('/api/v3/archive' + q, {method:'POST'}); };\n"
           "function fetchNow(){ return apiFetch('/data.json').then(function(r){ return r; })\n  }).catch(function(e){\n    var er=$('err'); er.className='on';\n"
           "    er.title='403 时请用带口令的地址打开本页'; });\n}\n</script></body></html>")

    def setUp(self):
        try:
            import p4_patch_dashboard
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"p4_patch_dashboard 不可导入: {exc}")
        self.PD = vars(p4_patch_dashboard)

    def test_patch_fixture(self):
        out, rep = self.PD["patch"](self.FIX)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(len(rep["applied"]), 6)
        v = self.PD["verify"](self.FIX, out)
        self.assertTrue(v["ok"], v)
        self.assertNotIn("reject?order=", out)
        out2, rep2 = self.PD["patch"](out)
        self.assertTrue(rep2["already_patched"])
        self.assertEqual(out2, out)

    def test_crlf_file(self):
        crlf = self.FIX.replace("\n", "\r\n")
        out, rep = self.PD["patch"](crlf)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["line_endings"], "CRLF")
        self.assertNotIn("\n", out.replace("\r\n", ""))                                  # 换行全部还原为 CRLF
        self.assertTrue(self.PD["verify"](crlf, out)["ok"])

    def test_missing_anchor_aborts(self):
        out, rep = self.PD["patch"](self.FIX.replace("function rejectUrl", "function rejectURL"))
        self.assertFalse(rep["ok"])
        self.assertEqual(out, self.FIX.replace("function rejectUrl", "function rejectURL"))


class Exposure(unittest.TestCase):
    def test_exposure_check_against_live_services(self):
        try:
            import p4_exposure_check
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"p4_exposure_check 不可导入: {exc}")
        EC = vars(p4_exposure_check)
        root = os.path.join(TMP, "steward")
        os.makedirs(os.path.join(root, "config"), exist_ok=True)
        open(os.path.join(root, "config", "board_token.txt"), "w", encoding="ascii").write(TOKEN)
        json.dump({"policy": {"client_token": "gw-secret"}}, open(os.path.join(root, "config", "routing.json"), "w", encoding="utf-8"))
        b = start_board()
        g = Gateway("test_no_cors")
        Gateway.setUpClass()
        g.setUp()
        try:
            rc = EC["main"](["--steward-root", root, "--board", "http://127.0.0.1:%d" % b.server_address[1],
                             "--gateway", "http://127.0.0.1:%d" % g.srv.server_address[1], "--skip-os", "--json"])
            self.assertEqual(rc, 0)
        finally:
            g.tearDown()
            b.shutdown()
            b.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
