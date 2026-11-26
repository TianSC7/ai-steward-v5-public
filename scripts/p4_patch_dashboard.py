#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""p4_patch_dashboard.py —— V4.0-P4 看板前端最小补丁（只改 JS，视觉零变化）。

改什么（每处锚点必须恰好出现 1 次，否则整体放弃、不写文件）：
  1. 新增 boardPost()：写操作走主线程 fetch POST + JSON 体 + X-Board-Action: 1 头 + 同源 cookie（防跨站伪造）
  2. 24h 打回：GET /api/v3/reject?order=…&reason=…&k=口令  →  POST /api/v3/reject {order, reason}
  3. 归档旧会话：apiFetch('/api/v3/archive'+location.search,POST) → boardPost('/api/v3/archive')（服务端默认关闭，前端已有静默兜底）
  4. 取数遇 401/403（会话过期）→ 跳 /login?next=当前页（只跳一次）；404/断网等照旧显示「数据未就绪」横幅
  5. 两处提示文案：「需带访问口令打开本页」「403 时请用带口令的地址打开本页」→ 登录语义
不改：DOM、CSS、render、轮询节奏、data-zone 锚、只读接口的取数路径（仍兼容本机 ?k=）。

用法：
  python -X utf8 p4_patch_dashboard.py <现网 v2.html> --out <候选.html> [--check]
  然后：factory_runner.py probe-candidate --candidate <候选.html> --freeze-all --apply   （像素冻结全绿才提交）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

MARK = "V4.0-P4"

PATCHES = [
    {
        "id": "boardPost+rejectUrl",
        "old": "function rejectUrl(reason){\n  return '/api/v3/reject?order=C-20261121-001&reason='+encodeURIComponent(reason||'发起人24h一键打回重做')+\n    (K?('&k='+encodeURIComponent(K)):'');\n}",
        "new": ("/* V4.0-P4：写操作只走 POST + 同源会话 cookie + X-Board-Action 头（跨站页面发不出带自定义头的请求，即防伪造）；口令不再进网址 */\n"
                "function boardPost(u,obj){\n"
                "  return fetch(u,{method:'POST',credentials:'same-origin',cache:'no-store',\n"
                "    headers:{'Content-Type':'application/json','X-Board-Action':'1'},body:JSON.stringify(obj||{})});\n"
                "}\n"
                "function rejectUrl(reason){\n  return '/api/v3/reject';\n}"),
    },
    {
        "id": "doReject-call",
        "old": "return apiFetch(rejectUrl(String(reason||'').trim()),{cache:'no-store'}).then(function(r){",
        "new": "return boardPost(rejectUrl(),{order:'C-20261121-001',reason:String(reason||'').trim()||'发起人24h一键打回重做'}).then(function(r){",
    },
    {
        "id": "reject-error-text",
        "old": "'（需带访问口令打开本页）'",
        "new": "'（登录可能已过期，请刷新页面重新登录）'",
    },
    {
        "id": "archive-call",
        "old": "apiFetch('/api/v3/archive' + q, {method:'POST'})",
        "new": "boardPost('/api/v3/archive', {})",
    },
    {
        "id": "fetchNow-relogin",
        "old": "  }).catch(function(e){\n    FN_BUSY=0;\n    var er=$('err'); er.className='on';",
        "new": ("  }).catch(function(e){\n"
                "    /* V4.0-P4：会话过期（401/403）→ 跳登录页一次；其余失败照旧显示横幅 */\n"
                "    var em=String(e&&e.message||'');\n"
                "    if((em==='HTTP 401'||em==='HTTP 403')&&!window.__v4relogin){ window.__v4relogin=1; location.href='/login?next='+encodeURIComponent(location.pathname); return; }\n"
                "    FN_BUSY=0;\n    var er=$('err'); er.className='on';"),
    },
    {
        "id": "err-title",
        "old": "er.title='取数失败 '+(e&&e.message||e)+' · '+new Date().toLocaleTimeString()+'（403=请用带口令地址打开；反复出现请刷新本页）';",
        "new": "er.title='取数失败 '+(e&&e.message||e)+' · '+new Date().toLocaleTimeString()+'（会话失效会自动跳转登录页；反复出现请刷新本页）';",
    },
]


def patch(src: str) -> tuple[str, dict]:
    """AA 上的 v2.html 是 CRLF 换行（研究快照里被转成了 LF）：统一按 LF 打补丁，再还原原换行。"""
    crlf = "\r\n" in src
    if crlf:
        out, rep = _patch_lf(src.replace("\r\n", "\n"))
        rep["line_endings"] = "CRLF"
        return (out.replace("\n", "\r\n") if rep["ok"] and not rep.get("already_patched") else src), rep
    out, rep = _patch_lf(src)
    rep["line_endings"] = "LF"
    return out, rep


def _patch_lf(src: str) -> tuple[str, dict]:
    if MARK + "：写操作只走 POST" in src:
        return src, {"ok": True, "already_patched": True, "applied": []}
    out = src
    rep = {"ok": True, "already_patched": False, "applied": [], "errors": []}
    for p in PATCHES:
        n = out.count(p["old"])
        if n != 1:
            rep["errors"].append(f"{p['id']}: 锚点出现 {n} 次（应为 1）")
            continue
        out = out.replace(p["old"], p["new"], 1)
        rep["applied"].append(p["id"])
    if rep["errors"]:
        rep["ok"] = False
        return src, rep
    return out, rep


def verify(before: str, after: str) -> dict:
    """静态核对：CSS 与 HTML 结构逐字节不变；只有 <script> 变；新 JS 不再拼 &k= 与 GET 打回。"""
    before, after = before.replace("\r\n", "\n"), after.replace("\r\n", "\n")
    sty = lambda s: re.findall(r"<style[^>]*>.*?</style>", s, re.S)
    strip_js = lambda s: re.sub(r"<script[^>]*>.*?</script>", "<script></script>", s, flags=re.S)
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", after, re.S))
    checks = {
        "css_identical": sty(before) == sty(after),
        "markup_identical": strip_js(before) == strip_js(after),
        "no_token_in_reject_url": "reject?order=" not in js,
        "reject_is_post": "boardPost(rejectUrl()" in js,
        "csrf_header": "'X-Board-Action':'1'" in js,
        "relogin_on_403": "location.href='/login?next='" in js,
    }
    return {"ok": all(checks.values()), "checks": checks}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("--out", default="")
    ap.add_argument("--check", action="store_true", help="只检查锚点，不写文件")
    a = ap.parse_args(argv)
    src = open(a.html, encoding="utf-8").read()
    out, rep = patch(src)
    if rep["ok"] and not rep.get("already_patched"):
        rep["verify"] = verify(src, out)
        rep["ok"] = rep["verify"]["ok"]
    rep["sha16_before"] = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
    rep["sha16_after"] = hashlib.sha256(out.encode("utf-8")).hexdigest()[:16]
    if rep["ok"] and a.out and not a.check and not rep.get("already_patched"):
        with open(a.out, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        rep["written"] = a.out
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if rep["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
