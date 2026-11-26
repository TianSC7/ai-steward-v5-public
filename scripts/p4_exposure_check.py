#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""p4_exposure_check.py —— V4.0-P4 暴露面验收（在云主机 AA 上运行，只读，不改任何东西）。

检查项（每项 PASS / FAIL / WARN，任一 FAIL 退出码 1）：
  监听   8900 / 8788 只监听 127.0.0.1（netstat）
  防火墙 Windows 防火墙没有放行 8900 / 8788 的入站规则（netsh；云安全组需你在云控制台另查）
  看板   未登录访问页面 → 403 或 303 到 /login；错口令 ?k= → 403；外部 Host → 421；GET 打回 → 405；
         POST 打回不带 X-Board-Action → 403；跨站 Origin 的 POST → 403；带正确请求头口令 → 200；
         响应带 nosniff / no-referrer / frame-ancestors；登录页可打开
  网关   不返回 Access-Control-Allow-Origin；外部 Host → 421；浏览器跨站 Origin 读 /admin/status → 403；
         本机脚本读 /admin/status → 200（内核记账依赖）；/v1/chat/completions 无令牌 → 401
  隧道   （可选 --tunnel-url https://aa.xxx.ts.net）隧道地址未登录 → 303/403；隧道上 ?k=正确口令 → 也必须被拒

用法：python -X utf8 p4_exposure_check.py [--steward-root …] [--tunnel-url https://…] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.parse

DEFAULT_STEWARD_ROOT = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"


class NoRedirect(Exception):
    pass


def req(method: str, url: str, headers: dict | None = None, body: bytes | None = None, host_override: str = "",
        timeout: float = 8) -> tuple[int, dict, bytes]:
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    c = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout)
    h = dict(headers or {})
    if host_override:
        h["Host"] = host_override
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    try:
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    except Exception as e:  # noqa: BLE001
        return 0, {"error": repr(e)}, b""
    finally:
        c.close()


def sh(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="gbk" if os.name == "nt" else "utf-8",
                           errors="replace", timeout=30)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return f"<{e!r}>"


def listeners(port: int) -> list[str]:
    out = sh(["netstat", "-ano", "-p", "TCP"]) + sh(["netstat", "-ano", "-p", "TCPv6"])
    addrs = set()
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 4 and parts[0].upper().startswith("TCP") and ("LISTEN" in ln.upper() or "侦听" in ln):
            if parts[1].rsplit(":", 1)[-1] == str(port):
                addrs.add(parts[1].rsplit(":", 1)[0])
    return sorted(addrs)


def firewall_rules(port: int) -> list[str]:
    out = sh(["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in", "verbose"])
    hits, cur = [], {}
    for ln in out.splitlines() + [""]:
        if not ln.strip():
            if cur and cur.get("enabled", "").lower() in ("yes", "是") and cur.get("action", "").lower() in ("allow", "允许"):
                lp = cur.get("localport", "")
                if lp.lower() in ("any", "任何") and cur.get("protocol", "").upper() in ("TCP", "ANY", "任何") and cur.get("program", "").lower().endswith("python.exe"):
                    hits.append(f"{cur.get('name')}（程序 python.exe，任意端口）")
                elif re.search(r"(^|,)\s*%d\s*(,|$|-)" % port, lp) or any(
                        a.isdigit() and b.isdigit() and int(a) <= port <= int(b) for a, b in re.findall(r"(\d+)-(\d+)", lp)):
                    hits.append(f"{cur.get('name')}（端口 {lp}）")
            cur = {}
            continue
        if ":" in ln:
            k, v = ln.split(":", 1)
            key = {"规则名称": "name", "rule name": "name", "已启用": "enabled", "enabled": "enabled", "操作": "action",
                   "action": "action", "本地端口": "localport", "localport": "localport", "协议": "protocol",
                   "protocol": "protocol", "程序": "program", "program": "program"}.get(k.strip().lower())
            if key:
                cur[key] = v.strip()
    return hits


class Report:
    def __init__(self):
        self.rows = []

    def add(self, group, name, ok, detail="", warn=False):
        self.rows.append({"group": group, "check": name, "status": "PASS" if ok else ("WARN" if warn else "FAIL"), "detail": str(detail)[:200]})

    def failed(self):
        return [r for r in self.rows if r["status"] == "FAIL"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steward-root", default=DEFAULT_STEWARD_ROOT)
    ap.add_argument("--board", default="http://127.0.0.1:8900")
    ap.add_argument("--gateway", default="http://127.0.0.1:8788")
    ap.add_argument("--tunnel-url", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-os", action="store_true", help="跳过 netstat / netsh（非 Windows 测试用）")
    a = ap.parse_args(argv)
    R = Report()
    try:
        tok = open(os.path.join(a.steward_root, "config", "board_token.txt"), encoding="ascii").read().strip()
    except OSError:
        tok = ""
    gw_tok = ""
    try:
        gw_tok = str((json.load(open(os.path.join(a.steward_root, "config", "routing.json"), encoding="utf-8")).get("policy") or {}).get("client_token") or "")
    except (OSError, ValueError):
        pass

    if not a.skip_os:
        for port, nm in ((8900, "看板"), (8788, "网关")):
            ls = listeners(port)
            R.add("监听", f"{nm} {port} 只听本机", bool(ls) and all(x in ("127.0.0.1", "[::1]") for x in ls),
                  ls or "未在监听（服务没起？）")
            fw = firewall_rules(port)
            R.add("防火墙", f"无放行 {port} 的入站规则", not fw, "；".join(fw) or "无", warn=bool(fw) and all(x in ("127.0.0.1", "[::1]") for x in ls))

    B = a.board.rstrip("/")
    c, hd, _ = req("GET", B + "/healthz")
    R.add("看板", "/healthz 可达", c == 200, c)
    c, hd, _ = req("GET", B + "/v2.html")
    R.add("看板", "未登录访问页面被拒（脚本）", c == 403, c)
    c, hd, _ = req("GET", B + "/v2.html", {"Accept": "text/html"})
    R.add("看板", "未登录浏览器跳登录页", c == 303 and hd.get("location", "").startswith("/login"), f"{c} {hd.get('location', '')}")
    c, hd, body = req("GET", B + "/login")
    R.add("看板", "登录页可打开", c == 200 and b"password" in body, c)
    R.add("看板", "安全响应头", all(k in hd for k in ("x-content-type-options", "referrer-policy", "content-security-policy")),
          {k: hd.get(k) for k in ("x-content-type-options", "referrer-policy", "content-security-policy")})
    c, _, _ = req("GET", B + "/v2.html?k=wrong-token")
    R.add("看板", "错口令 ?k= 被拒", c == 403, c)
    c, _, _ = req("GET", B + "/v2.html", {"X-Board-Token": tok}, host_override="evil.example.com")
    R.add("看板", "外部 Host 被拒（防 DNS 重绑定）", c == 421, c)
    c, _, _ = req("GET", B + "/v2.html", {"X-Board-Token": tok})
    R.add("看板", "请求头口令可访问（内核实拍用）", c == 200, c)
    c, _, _ = req("GET", B + "/api/v3/reject?order=C-X&reason=x", {"X-Board-Token": tok})
    R.add("看板", "GET 打回被拒（405）", c == 405, c)
    c, _, _ = req("POST", B + "/api/v3/reject", {"Content-Type": "application/json", "Origin": "https://evil.example.com"}, b'{"order":"C-X"}')
    R.add("看板", "跨站 Origin 的 POST 被拒", c == 403, c)
    c, _, _ = req("POST", B + "/api/v3/reject", {"Content-Type": "application/json", "Cookie": "board_k=" + tok}, b'{"order":"C-X"}')
    R.add("看板", "旧 cookie 不带 X-Board-Action 的 POST 被拒", c == 403, c)
    c, _, _ = req("POST", B + "/api/v3/archive", {"X-Board-Token": tok})
    R.add("看板", "归档接口默认关闭", c == 403, c, warn=(c == 200))

    G = a.gateway.rstrip("/")
    c, hd, _ = req("GET", G + "/health", {"Origin": "https://evil.example.com"})
    R.add("网关", "/health 可达", c == 200, c)
    R.add("网关", "不返回 CORS 放行头", "access-control-allow-origin" not in hd, hd.get("access-control-allow-origin", "无"))
    c, _, _ = req("GET", G + "/admin/status", host_override="evil.example.com")
    R.add("网关", "外部 Host 被拒（防 DNS 重绑定）", c == 421, c)
    c, _, _ = req("GET", G + "/admin/status", {"Origin": "https://evil.example.com"})
    R.add("网关", "浏览器跨站读 /admin/status 被拒", c == 403, c)
    c, _, _ = req("GET", G + "/admin/status")
    R.add("网关", "本机脚本读 /admin/status 正常（内核记账依赖）", c == 200, c)
    c, _, _ = req("POST", G + "/admin/reload", {"Origin": "https://evil.example.com"})
    R.add("网关", "浏览器跨站 POST /admin/reload 被拒", c == 403, c)
    c, _, _ = req("POST", G + "/v1/chat/completions", {"Content-Type": "application/json"}, b'{"model":"auto","messages":[]}')
    R.add("网关", "无令牌调用模型被拒", c in (401, 403) or not gw_tok, c, warn=not gw_tok)
    if not gw_tok:
        R.add("网关", "routing.json 已设 client_token", False, "未设置：本机任何进程都能花你的模型额度", warn=True)

    if a.tunnel_url:
        T = a.tunnel_url.rstrip("/")
        c, hd, _ = req("GET", T + "/v2.html", {"Accept": "text/html"})
        R.add("隧道", "隧道未登录跳登录页", c in (303, 403), f"{c} {hd.get('location', '')}")
        c, _, _ = req("GET", T + "/v2.html?k=" + urllib.parse.quote(tok))
        R.add("隧道", "隧道上 ?k=正确口令 也被拒", c == 403, c)
        c, hd, _ = req("GET", T + "/login")
        R.add("隧道", "隧道登录页可打开（HTTPS）", c == 200 and T.startswith("https://"), c)

    fails = R.failed()
    if a.json:
        print(json.dumps({"rows": R.rows, "failed": len(fails)}, ensure_ascii=False, indent=1))
    else:
        print(f"{'分组':<6}{'结果':<6}检查项 · 详情")
        for r in R.rows:
            print(f"{r['group']:<6}{r['status']:<6}{r['check']} · {r['detail']}")
        print(f"\n共 {len(R.rows)} 项：FAIL {len(fails)} / WARN {sum(1 for r in R.rows if r['status'] == 'WARN')}")
        print("云服务器安全组（控制台）里 8900 / 8788 的入站放行规则请一并删除——本工具看不到云侧配置。")
    try:
        d = os.path.join(a.steward_root, "logs", "v4_trace")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"trace-{dt.datetime.now().strftime('%Y%m%d')}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "kernel": "V4.0-P4", "event": "exposure_check",
                                "order_id": "", "failed": len(fails), "rows": R.rows}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
