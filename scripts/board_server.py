#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""board_server.py —— MES 看板实时服务（V4.0-P4 安全收口版）

/            首屏 HTML（服务端 render）           /data.json     现场取数（客户端每 2 秒拉取）
/v2.html 等  静态看板页                          /api/v3/events 实况流
/lanes       并发数（跳转 v2#z14）                /api/lanes.json 并发数取数（客户端每 2 秒拉取）
/login       登录页（GET 表单 / POST 换会话）     /logout        退出（POST）
/healthz     探活（免鉴权）                       /api/v3/reject 24h 打回（仅 POST）  /api/v3/archive 归档（默认关闭）

V4.0-P4 安全模型（修 D-12；配置 config/board.json，缺省即最安全取值）：
  1. 只监听 127.0.0.1。对外访问走隧道（推荐 Tailscale Serve，HTTPS + 设备身份）；要监听 0.0.0.0 必须显式
     allow_public_bind=true（并会在日志里持续告警）。
  2. 口令不进网址：浏览器在 /login 提交一次口令 → 换取随机会话 cookie（HttpOnly · SameSite=Strict ·
     HTTPS 下自动 Secure · 默认 7 天），服务端只存会话的 sha256。旧版 ?k= 只对「本机直连」放行（工厂内核/脚本用），
     并立即 303 去掉网址里的口令；隧道/外网带 ?k= 一律拒绝。脚本可用请求头 X-Board-Token。
  3. 写操作（打回 / 归档 / 退出）只收 POST，且必须带 X-Board-Action: 1 头、Origin 与本站一致——跨站页面
     发不出带自定义头的请求（浏览器会先预检，本服务从不放行），即防跨站请求伪造。GET /api/v3/reject 返回 405。
  4. Host 白名单（防 DNS 重绑定）：只接受 127.0.0.1 / localhost / allowed_hosts（支持 *.ts.net 这类后缀）。
  5. 登录限速：同一来源 10 分钟内失败 5 次 → 锁 15 分钟。打回限速：30 秒 1 次。
  6. 审计日志 logs/board_access-YYYYMMDD.jsonl：登录成败、锁定、拒绝（原因/路径/来源）、写操作结果、旧口令使用；
     高频轮询（/data.json、/api/v3/events）成功请求不逐条记，按小时计数。口令与会话值一律不落盘。
  7. 所有响应带 nosniff / no-referrer / 禁止被嵌入（frame-ancestors 'none'）/ no-store。

命令行：
  python board_server.py [端口]                 启动（端口缺省取 board.json，再缺省 8900）
  python board_server.py rotate-token           生成新口令并作废全部会话（新口令只打印一次）
  python board_server.py show-config            打印生效配置（不含口令）
"""
from __future__ import annotations

import hashlib
import hmac
import html as _html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

TOKEN_FILE = os.path.join(ROOT, "config", "board_token.txt")
CONFIG_FILE = os.path.join(ROOT, "config", "board.json")
SESSIONS_FILE = os.path.join(ROOT, "state", "board_sessions.json")
LOG_DIR = os.path.join(ROOT, "logs")
DASH_DIR = os.path.join(ROOT, "dashboard")
VERSION = "V5.0"

DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "port": 8900,
    "allow_public_bind": False,
    "allowed_hosts": [],                 # 隧道域名，如 "aa.tailxxxx.ts.net" 或 "*.ts.net"
    "legacy_query_token": "loopback",    # loopback：仅本机直连可用 ?k=；off：完全禁用；any：过渡期全放行（不推荐）
    "session_ttl_hours": 168,
    "cookie_secure": "auto",             # auto：经 HTTPS 隧道（X-Forwarded-Proto=https）时加 Secure；always / never
    "login_max_fails": 5,
    "login_fail_window_sec": 600,
    "login_lockout_sec": 900,
    "reject_min_interval_sec": 30,
    "enable_archive": False,             # 归档会改写 ZCode 的 tasks-index / cli db（steward.json 保护路径），默认关闭
    "audit_poll_requests": False,
}
POLL_PATHS = ("/data.json", "/api/v3/events", "/api/lanes.json")
STATIC_PAGES = {
    "/v2": "v2.html", "/v2.html": "v2.html",
    "/v3": "v3.html", "/v3.html": "v3.html",
    "/mes3": "mes_v3.html", "/mes3.html": "mes_v3.html",
    "/architecture": "architecture.html", "/architecture.html": "architecture.html",
    "/flow": "flow.html", "/flow.html": "flow.html",
    "/simple": "simple.html", "/simple.html": "simple.html",
    "/plan": "plans.html", "/plan.html": "plans.html",
    "/keepalive": "keepalive.html", "/keepalive.html": "keepalive.html",
    "/concurrency": "concurrency.html", "/concurrency.html": "concurrency.html",
    "/lanes": "lanes.html", "/lanes.html": "lanes.html",
}
FORWARD_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "X-Real-IP",
                   "Cf-Connecting-Ip", "Tailscale-User-Login", "True-Client-IP")
ORDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("Cache-Control", "no-store"),
)


# ------------------------------------------------------------------ 配置 / 口令


def load_config(path: str = "") -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(path or CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in DEFAULT_CONFIG:
                if k in d:
                    cfg[k] = d[k]
    except (OSError, ValueError):
        pass
    if cfg["legacy_query_token"] not in ("loopback", "off", "any"):
        cfg["legacy_query_token"] = "loopback"
    return cfg


def _load_token() -> str:
    try:
        with open(TOKEN_FILE, "r", encoding="ascii") as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    t = "zc-" + secrets.token_urlsafe(18)
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="ascii") as f:
        f.write(t)
    return t


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 审计日志


class Audit:
    def __init__(self, log_dir: str = LOG_DIR):
        self.dir = log_dir
        self.lock = threading.Lock()
        self.poll = {}      # (hour, path) -> count

    def write(self, event: str, **kw) -> None:
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **kw}
        try:
            os.makedirs(self.dir, exist_ok=True)
            with self.lock:
                with open(os.path.join(self.dir, "board_access-%s.jsonl" % time.strftime("%Y%m%d")), "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def count_poll(self, path: str) -> None:
        hour = time.strftime("%Y-%m-%d %H")
        flush = None
        with self.lock:
            stale = [k for k in self.poll if k[0] != hour]
            if stale:
                flush = {f"{k[0]} {k[1]}": self.poll.pop(k) for k in stale}
            self.poll[(hour, path)] = self.poll.get((hour, path), 0) + 1
        if flush:
            self.write("poll_summary", counts=flush)


# ------------------------------------------------------------------ 会话 / 限速


class Sessions:
    def __init__(self, path: str = SESSIONS_FILE, ttl_hours: float = 168):
        self.path = path
        self.ttl = float(ttl_hours) * 3600
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            now = time.time()
            self.items = {k: v for k, v in (d.get("sessions") or {}).items() if float(v.get("exp", 0)) > now}
        except (OSError, ValueError, AttributeError):
            self.items = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"sessions": self.items, "saved_at": time.time()}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def create(self, ip: str, ua: str) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.items[_sha(sid)] = {"created": now, "exp": now + self.ttl, "ip": ip, "ua": (ua or "")[:120]}
            self._save()
        return sid

    def valid(self, sid: str) -> bool:
        if not sid or len(sid) > 200:
            return False
        h = _sha(sid)
        with self.lock:
            v = self.items.get(h)
            if not v:
                return False
            if float(v.get("exp", 0)) <= time.time():
                self.items.pop(h, None)
                self._save()
                return False
            return True

    def drop(self, sid: str) -> None:
        with self.lock:
            if self.items.pop(_sha(sid or ""), None) is not None:
                self._save()

    def clear(self) -> int:
        with self.lock:
            n = len(self.items)
            self.items = {}
            self._save()
            return n


class Limiter:
    def __init__(self, max_fails: int, window: float, lockout: float):
        self.max, self.window, self.lockout = int(max_fails), float(window), float(lockout)
        self.lock = threading.Lock()
        self.fails: dict[str, list] = {}
        self.locked: dict[str, float] = {}

    def locked_for(self, key: str) -> float:
        with self.lock:
            until = self.locked.get(key, 0)
            return max(0.0, until - time.time())

    def fail(self, key: str) -> bool:
        """记一次失败；返回是否因此被锁。"""
        now = time.time()
        with self.lock:
            arr = [t for t in self.fails.get(key, []) if now - t < self.window] + [now]
            self.fails[key] = arr
            if len(arr) >= self.max:
                self.locked[key] = now + self.lockout
                self.fails[key] = []
                return True
            return False

    def ok(self, key: str) -> None:
        with self.lock:
            self.fails.pop(key, None)


# ------------------------------------------------------------------ 业务（保持 V3 行为）

CACHE_SEC = 1.0
_doc_lock = threading.Lock()
_doc_cache = {"ts": 0.0, "doc": None}


def get_doc() -> bytes:
    import mes_board  # 延迟导入：测试可替身
    with _doc_lock:
        if _doc_cache["doc"] is None or time.time() - _doc_cache["ts"] > CACHE_SEC:
            _doc_cache["doc"] = mes_board.render().encode("utf-8")
            _doc_cache["ts"] = time.time()
        return _doc_cache["doc"]


def get_data() -> bytes:
    import v3_data
    return json.dumps(v3_data.collect_v3_data(), ensure_ascii=False).encode("utf-8")


_lanes_lock = threading.Lock()
_lanes_cache = {"ts": 0.0, "body": None}


def get_lanes() -> bytes:
    """并发数卡取数：客户端 2 秒轮询，计算侧 1 秒缓存（只读 lane_caps/会话库）。"""
    import lane_status
    with _lanes_lock:
        if _lanes_cache["body"] is None or time.time() - _lanes_cache["ts"] > 1.0:
            _lanes_cache["body"] = json.dumps(lane_status.collect(), ensure_ascii=False).encode("utf-8")
            _lanes_cache["ts"] = time.time()
        return _lanes_cache["body"]


def get_events() -> bytes:
    import v3_data
    ev = v3_data.collect_live_agent_stream()
    try:
        d_now = json.loads(get_data().decode("utf-8"))
        ev["cost_per_accepted_cny"] = ((d_now.get("v3_pipeline") or {}).get("cost_per_accepted_cny", 0.0))
    except Exception:
        ev["cost_per_accepted_cny"] = 0.0
    return json.dumps(ev, ensure_ascii=False).encode("utf-8")


def mes_page(kind: str, arg: str) -> bytes:
    import mes_board
    fn = {"seat": mes_board.seat_page, "card": mes_board.card_page, "cards": mes_board.cards_page}[kind]
    return fn(arg).encode("utf-8")


def run_reject(order: str, reason: str) -> tuple[int, bytes]:
    """调用内核 reject，再后台触发一次 tick（与 V3 相同）。返回 (退出码, stdout)。"""
    import subprocess as _sp
    runner_p = os.path.join(SCRIPTS, "factory_runner.py")
    r = _sp.run([sys.executable, "-X", "utf8", runner_p, "reject", "--order", order, "--reason", reason],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    if r.returncode == 0:
        flags = getattr(_sp, "DETACHED_PROCESS", 0) | getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0)
        _sp.Popen([sys.executable, "-X", "utf8", runner_p, "tick"], cwd=ROOT, stdin=_sp.DEVNULL, stdout=_sp.DEVNULL,
                  stderr=_sp.DEVNULL, close_fds=True, creationflags=flags)
    return r.returncode, (r.stdout or "{}").encode("utf-8")


def archive_old_zcode_sessions() -> dict:
    import sqlite3
    cli_db = r"C:\Users\Administrator\.zcode\cli\db\db.sqlite"
    idx_db = r"C:\Users\Administrator\.zcode\v2\tasks-index.sqlite"
    now_ms = int(time.time() * 1000)
    archived_cnt = 0
    try:
        if os.path.exists(idx_db):
            conn = sqlite3.connect(idx_db, timeout=15)
            rows = [r[0] for r in conn.execute("SELECT session_id FROM tasks WHERE archived = 0 ORDER BY updated_at DESC").fetchall()]
            for sid in rows[2:]:
                conn.execute("UPDATE tasks SET archived = 1, updated_at = ? WHERE session_id = ?", (now_ms, sid))
                archived_cnt += 1
            conn.commit()
            conn.close()
        if os.path.exists(cli_db):
            conn2 = sqlite3.connect(cli_db, timeout=15)
            rows2 = [r[0] for r in conn2.execute("SELECT id FROM session WHERE time_archived IS NULL ORDER BY time_updated DESC").fetchall()]
            for sid in rows2[2:]:
                conn2.execute("UPDATE session SET time_archived = ? WHERE id = ?", (now_ms, sid))
            conn2.commit()
            conn2.close()
    except Exception as e:
        return {"ok": False, "error": str(e), "archived_count": archived_cnt}
    return {"ok": True, "archived_count": archived_cnt}


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>工厂面板 · 登录</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f8fafc;color:#0f172a;font:14px/1.5 system-ui,"Segoe UI","Microsoft YaHei",sans-serif}
main{width:min(360px,calc(100vw - 32px));background:#ffffff;border-radius:16px;padding:24px;box-shadow:0 1px 2px rgba(15,23,42,.05)}
h1{margin:0 0 4px;font-size:16px;font-weight:600}p{margin:0 0 16px;color:#64748b;font-size:12px}
label{display:block;margin:0 0 8px;font-size:12px;color:#475569;font-weight:600}
input{width:100%;height:40px;border:0;border-radius:8px;background:#f1f5f9;padding:0 12px;font:inherit;color:#0f172a}
input:focus-visible,button:focus-visible{outline:2px solid #2563eb;outline-offset:2px}
button{width:100%;height:40px;margin-top:16px;border:0;border-radius:8px;background:#2563eb;color:#ffffff;font:inherit;font-weight:600;cursor:pointer;transition:background 150ms}
.msg{margin-top:12px;font-size:12px;color:#dc2626;min-height:18px}
</style></head><body><main>
<h1>工厂面板</h1><p>输入访问口令后进入；本设备 __TTL__ 天内免登录。</p>
<form method="post" action="/login" autocomplete="off">
<label for="t">访问口令</label><input id="t" name="token" type="password" required autofocus>
<input type="hidden" name="next" value="__NEXT__">
<button type="submit">进入</button><div class="msg" role="alert">__MSG__</div>
</form></main></body></html>"""


# ------------------------------------------------------------------ HTTP


def _host_only(h: str) -> str:
    h = (h or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def host_allowed(host_header: str, allowed: list) -> bool:
    h = _host_only(host_header)
    if h in ("127.0.0.1", "localhost", "::1"):
        return True
    for pat in allowed or []:
        p = str(pat).strip().lower()
        if not p:
            continue
        if p.startswith("*."):
            if h.endswith(p[1:]) and len(h) > len(p) - 1:
                return True
        elif h == p:
            return True
    return False


def safe_next(n: str) -> str:
    n = (n or "/").strip()
    if not n.startswith("/") or n.startswith("//") or "\\" in n or "\n" in n or "\r" in n:
        return "/"
    return n[:300]


class BoardServer(ThreadingHTTPServer):
    """属性由 make_server 装配：cfg / token / sessions / audit / limiter / last_reject / reject_lock。"""
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "board"
    sys_version = ""

    # ---- 基础

    def log_message(self, fmt, *args):
        pass

    @property
    def S(self) -> BoardServer:
        return self.server  # type: ignore[return-value]

    def _ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _src(self) -> str:
        """限速/审计用的来源标识：隧道转发时用转发头里的原始地址（只作标识，不作信任依据）。"""
        fwd = self.headers.get("X-Forwarded-For") or self.headers.get("Cf-Connecting-Ip") or ""
        return (fwd.split(",")[0].strip() or self._ip())[:64]

    def _direct_loopback(self) -> bool:
        """本机直连：来源是回环地址、Host 是本机名、且没有任何代理转发头（隧道进来的请求一律不算）。"""
        if self._ip() not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return False
        if _host_only(self.headers.get("Host", "")) not in ("127.0.0.1", "localhost", "::1"):
            return False
        return not any(self.headers.get(h) for h in FORWARD_HEADERS)

    def _https(self) -> bool:
        mode = self.S.cfg.get("cookie_secure", "auto")
        if mode == "always":
            return True
        if mode == "never":
            return False
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _send(self, code: int, ctype: str, body: bytes, extra: list | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        if self._https():
            self.send_header("Strict-Transport-Security", "max-age=15552000")
        for k, v in extra or []:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict, extra: list | None = None) -> None:
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra)

    def _deny(self, code: int, reason: str, path: str) -> None:
        self.S.audit.write("denied", code=code, reason=reason, path=path, method=self.command, src=self._src(),
                           ua=(self.headers.get("User-Agent") or "")[:80])
        self._json(code, {"ok": False, "error": reason})

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v.strip()
        return ""

    def _session_cookie(self, sid: str, max_age: int) -> str:
        return ("board_s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s"
                % (sid, max_age, "; Secure" if self._https() else ""))

    # ---- 鉴权

    def _auth(self) -> tuple[str, str]:
        """→ (方式, 拒绝原因)。方式 ∈ session | header | legacy_query | legacy_cookie | ""。"""
        tok = self.S.token
        if self.S.sessions.valid(self._cookie("board_s")):
            return "session", ""
        ht = self.headers.get("X-Board-Token") or ""
        if ht:
            return ("header", "") if hmac.compare_digest(ht.encode(), tok.encode()) else ("", "bad header token")
        q = urllib.parse.urlsplit(self.path).query
        k = (urllib.parse.parse_qs(q).get("k") or [""])[0]
        if k:
            mode = self.S.cfg["legacy_query_token"]
            if mode == "off":
                return "", "legacy ?k= disabled"
            if mode == "loopback" and not self._direct_loopback():
                return "", "legacy ?k= only allowed from direct loopback"
            return ("legacy_query", "") if hmac.compare_digest(k.encode(), tok.encode()) else ("", "bad ?k= token")
        old = self._cookie("board_k")
        if old and self._direct_loopback() and hmac.compare_digest(old.encode(), tok.encode()):
            return "legacy_cookie", ""
        return "", "no credentials"

    def _origin_ok(self, allow_null: bool = False) -> bool:
        o = (self.headers.get("Origin") or "").strip()
        if not o:
            return True                      # 非浏览器客户端；浏览器跨站 POST 必带 Origin
        if o.lower() == "null":
            return allow_null                # 部分 App 内置 WebView 发 Origin: null
        try:
            host = urllib.parse.urlsplit(o).netloc
        except ValueError:
            return False
        return bool(host) and _host_only(host) == _host_only(self.headers.get("Host", "")) and \
            host_allowed(host, self.S.cfg.get("allowed_hosts"))

    def _wants_html(self) -> bool:
        return "text/html" in (self.headers.get("Accept") or "")

    # ---- GET

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        if path == "/healthz":
            self._send(200, "text/plain", b"ok")
            return
        if not host_allowed(self.headers.get("Host", ""), self.S.cfg.get("allowed_hosts")):
            self._deny(421, "host not allowed", path)
            return
        if path == "/login":
            self._login_page(parts.query)
            return
        if path in ("/api/v3/reject", "/api/v3/archive", "/logout"):
            self._send(405, "application/json; charset=utf-8", b'{"ok":false,"error":"POST only"}', [("Allow", "POST")])
            self.S.audit.write("denied", code=405, reason="write via GET", path=path, src=self._src())
            return
        how, why = self._auth()
        if not how:
            if self._wants_html():
                nxt = urllib.parse.quote(safe_next(path), safe="/")
                self.S.audit.write("login_redirect", path=path, reason=why, src=self._src())
                self._send(303, "text/plain; charset=utf-8", b"login required", [("Location", "/login?next=" + nxt)])
                return
            self._deny(403, why, path)
            return
        extra = []
        if how in ("legacy_query", "legacy_cookie"):
            self.S.audit.write("legacy_token_use", how=how, path=path, src=self._src())
            if how == "legacy_query" and self._wants_html():
                sid = self.S.sessions.create(self._src(), self.headers.get("User-Agent") or "")
                q2 = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "k"])
                self._send(303, "text/plain; charset=utf-8", b"", [
                    ("Location", path + ("?" + q2 if q2 else "")),
                    ("Set-Cookie", self._session_cookie(sid, int(self.S.sessions.ttl)))])
                return
        try:
            ctype, body = self._route_get(path)
        except FileNotFoundError:
            self._json(404, {"ok": False, "error": "not found"})
            return
        except Exception as exc:  # noqa: BLE001
            self.S.audit.write("error", path=path, error=repr(exc)[:300])
            self._json(500, {"ok": False, "error": "internal error"})
            return
        if path in POLL_PATHS and not self.S.cfg.get("audit_poll_requests"):
            self.S.audit.count_poll(path)
        elif ctype.startswith("text/html"):
            self.S.audit.write("page_view", path=path, how=how, src=self._src())
        self._send(200, ctype, body, extra)

    def _route_get(self, path: str) -> tuple[str, bytes]:
        if path in ("/", "/index.html"):
            # 2026-11-23: root serves the factory's latest panel (v2.html, 37th build,
            # client-side live data). Legacy server-rendered board kept at /legacy.
            with open(os.path.join(DASH_DIR, "v2.html"), "rb") as f:
                return "text/html; charset=utf-8", f.read()
        if path == "/legacy":
            return "text/html; charset=utf-8", get_doc()
        if path == "/data.json":
            return "application/json; charset=utf-8", get_data()
        if path == "/api/lanes.json":
            return "application/json; charset=utf-8", get_lanes()
        if path == "/api/v3/events":
            return "application/json; charset=utf-8", get_events()
        for pre, kind in (("/seat/", "seat"), ("/card/", "card"), ("/cards/", "cards")):
            if path.startswith(pre):
                return "text/html; charset=utf-8", mes_page(kind, path[len(pre):])
        name = STATIC_PAGES.get(path)
        if name:
            with open(os.path.join(DASH_DIR, name), "rb") as f:
                return "text/html; charset=utf-8", f.read()
        raise FileNotFoundError(path)

    def _login_page(self, query: str, msg: str = "", code: int = 200) -> None:
        nxt = safe_next((urllib.parse.parse_qs(query).get("next") or ["/v2.html"])[0])
        ttl_days = max(1, round(float(self.S.cfg["session_ttl_hours"]) / 24))
        body = (LOGIN_HTML.replace("__NEXT__", _html.escape(nxt, quote=True))
                .replace("__MSG__", _html.escape(msg)).replace("__TTL__", str(ttl_days)))
        self._send(code, "text/html; charset=utf-8", body.encode("utf-8"))

    # ---- POST

    def _read_body(self, limit: int = 65536) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(min(max(n, 0), limit)) if n > 0 else b""

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not host_allowed(self.headers.get("Host", ""), self.S.cfg.get("allowed_hosts")):
            self._deny(421, "host not allowed", path)
            return
        if not self._origin_ok(allow_null=(path == "/login")):
            self._deny(403, "cross-origin request origin=%s" % (self.headers.get("Origin") or "")[:80], path)
            return
        if path == "/login":
            self._login_post()
            return
        how, why = self._auth()
        if not how:
            self._deny(403, why, path)
            return
        if how in ("session", "legacy_cookie") and self.headers.get("X-Board-Action") != "1":
            self._deny(403, "missing X-Board-Action header (CSRF guard)", path)
            return
        body = self._read_body()
        if path == "/logout":
            self.S.sessions.drop(self._cookie("board_s"))
            self.S.audit.write("logout", src=self._src())
            self._json(200, {"ok": True}, [("Set-Cookie", self._session_cookie("", 0))])
            return
        if path == "/api/v3/reject":
            self._reject(body, how)
            return
        if path == "/api/v3/archive":
            if not self.S.cfg.get("enable_archive"):
                self._deny(403, "archive disabled (board.json enable_archive=false)", path)
                return
            res = archive_old_zcode_sessions()
            self.S.audit.write("write", action="archive", how=how, src=self._src(), result=res)
            self._json(200 if res.get("ok") else 500, res)
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _login_post(self) -> None:
        src = self._src()
        left = self.S.limiter.locked_for(src)
        if left > 0:
            self.S.audit.write("login_locked", src=src, left_sec=round(left))
            self._login_page("", f"尝试次数过多，请 {int(left // 60) + 1} 分钟后再试", 429)
            return
        form = urllib.parse.parse_qs(self._read_body(4096).decode("utf-8", "replace"))
        tok = (form.get("token") or [""])[0].strip()
        nxt = safe_next((form.get("next") or ["/v2.html"])[0])
        if tok and hmac.compare_digest(tok.encode(), self.S.token.encode()):
            self.S.limiter.ok(src)
            sid = self.S.sessions.create(src, self.headers.get("User-Agent") or "")
            self.S.audit.write("login_ok", src=src, ua=(self.headers.get("User-Agent") or "")[:80], https=self._https())
            self._send(303, "text/plain; charset=utf-8", b"", [("Location", nxt),
                                                               ("Set-Cookie", self._session_cookie(sid, int(self.S.sessions.ttl)))])
            return
        locked = self.S.limiter.fail(src)
        self.S.audit.write("login_fail", src=src, locked=locked)
        self._login_page("next=" + urllib.parse.quote(nxt), "口令不正确" if not locked else "尝试次数过多，已暂时锁定", 403 if not locked else 429)

    def _reject(self, body: bytes, how: str) -> None:
        try:
            jb = json.loads(body.decode("utf-8", "replace")) if body else {}
        except ValueError:
            jb = {}
        if not isinstance(jb, dict):
            jb = {}
        order = str(jb.get("order") or "").strip()
        reason = str(jb.get("reason") or "").strip()[:2000] or "发起人24h一键打回重做"
        if not ORDER_RE.match(order):
            self._deny(400, "invalid order id", "/api/v3/reject")
            return
        with self.S.reject_lock:
            gap = time.time() - self.S.last_reject
            if gap < float(self.S.cfg["reject_min_interval_sec"]):
                self._deny(429, f"reject rate limited ({int(self.S.cfg['reject_min_interval_sec'] - gap)}s)", "/api/v3/reject")
                return
            self.S.last_reject = time.time()
        rc, out = run_reject(order, reason)
        self.S.audit.write("write", action="reject", order=order, reason=reason[:300], how=how, src=self._src(), rc=rc)
        self._send(200 if rc == 0 else 400, "application/json; charset=utf-8", out)


# ------------------------------------------------------------------ 启动 / CLI


def make_server(cfg: dict, port: int | None = None, host: str | None = None, token: str | None = None,
                sessions: Sessions | None = None, audit: Audit | None = None) -> BoardServer:
    h = host if host is not None else str(cfg.get("listen_host") or "127.0.0.1")
    if h not in ("127.0.0.1", "localhost", "::1") and not cfg.get("allow_public_bind"):
        raise SystemExit(f"拒绝监听 {h}：对外访问请走隧道（Tailscale Serve 等）。确需直接监听请在 board.json 设 allow_public_bind=true")
    p = int(port if port is not None else cfg.get("port") or 8900)
    srv = BoardServer((h, p), Handler)
    srv.cfg = cfg
    srv.token = token or _load_token()
    srv.sessions = sessions or Sessions(SESSIONS_FILE, cfg["session_ttl_hours"])
    srv.audit = audit or Audit(LOG_DIR)
    srv.limiter = Limiter(cfg["login_max_fails"], cfg["login_fail_window_sec"], cfg["login_lockout_sec"])
    srv.last_reject = 0.0
    srv.reject_lock = threading.Lock()
    return srv


def rotate_token() -> str:
    t = "zc-" + secrets.token_urlsafe(18)
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(t)
    os.replace(tmp, TOKEN_FILE)
    n = Sessions(SESSIONS_FILE).clear()
    Audit(LOG_DIR).write("token_rotated", sessions_cleared=n)
    return t


def main(argv: list[str]) -> int:
    if argv[:1] == ["rotate-token"]:
        t = rotate_token()
        print("新访问口令（只显示这一次，已写入 config/board_token.txt，旧口令与全部会话已作废）：")
        print(t)
        print("注意：board_server 正在运行时需重启后才使用新口令。")
        return 0
    cfg = load_config()
    if argv[:1] == ["show-config"]:
        print(json.dumps({**cfg, "version": VERSION, "token_file": TOKEN_FILE, "sessions_file": SESSIONS_FILE}, ensure_ascii=False, indent=2))
        return 0
    port = int(argv[0]) if argv and argv[0].isdigit() else None
    srv = make_server(cfg, port)
    host, p = srv.server_address[:2]
    if host not in ("127.0.0.1", "::1"):
        srv.audit.write("public_bind_warning", host=host, port=p)
        print(f"[警告] board_server 监听 {host}:{p}（allow_public_bind=true）——建议改用隧道", flush=True)
    srv.audit.write("start", version=VERSION, host=host, port=p, legacy_query_token=cfg["legacy_query_token"],
                    allowed_hosts=cfg.get("allowed_hosts"))
    print(f"board_server {VERSION} on {host}:{p} (session-auth, csrf-guard, host-allowlist)", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
