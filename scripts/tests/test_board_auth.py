# -*- coding: utf-8 -*-
"""8900/8901 鉴权基线 6 条回归（C-20261120-002 T05 · C-20261120-004 扩到 6 条）

验法（契约验收 3 同口径）：
  1  /healthz                    免鉴权 → 200
  2  /                          裸访   → 403
  3  /?k=<暗号>                 暗号   → 200（并种 board_k cookie）
  4  /data.json                 裸访   → 403
  5  /?order=<code>             裸访   → 403（C-004 详情深链入口加测）
  6  /?order=<code>&k=<暗号>    暗号   → 200（C-004 详情深链入口加测）
附加：带 cookie 的 /data.json → 200（防"只测暗号漏种 cookie"）

暗号从 config/board_token.txt 读取，**只进命令行参数、不打印**（证据里只留状态码）。
本脚本不改任何文件、不重启服务；只对自己指定的 base 发 GET。

用法：python -X utf8 scripts/tests/test_board_auth.py [--base http://127.0.0.1:8901] [--json]
退出码 0 = 6 条（含附加项）全绿；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOKEN_FILE = os.path.join(ROOT, "config", "board_token.txt")
RESULTS: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def curl_code(url: str, extra: list[str] | None = None) -> str:
    """curl 只回状态码；不跟随重定向、不带 referer。"""
    cmd = ["curl", "-s", "-o", os.devnull, "-w", "%{http_code}", "--max-time", "8"]
    cmd += extra or []
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "ERR:%s" % type(exc).__name__
    return (p.stdout or p.stderr or "").strip()[-3:] or "ERR"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BOARD_TEST_URL", "http://127.0.0.1:8901"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    try:
        with open(TOKEN_FILE, "r", encoding="ascii") as f:
            token = f.read().strip()
    except OSError as exc:
        print("[FAIL] 读不到暗号文件 %s（%s）——不打印内容" % (TOKEN_FILE, type(exc).__name__))
        return 2
    if not token:
        print("[FAIL] 暗号文件为空——不起服务、不猜值")
        return 2

    tmp = tempfile.mkdtemp(prefix="board-auth-")
    jar = os.path.join(tmp, "cookies.txt")
    try:
        c1 = curl_code(base + "/healthz")
        check("healthz-200", c1 == "200", "code=%s" % c1)
        c2 = curl_code(base + "/")
        check("bare-root-403", c2 == "403", "code=%s" % c2)
        c3 = curl_code(base + "/?k=" + urllib.parse.quote(token), ["-c", jar])
        check("token-root-200", c3 == "200", "code=%s" % c3)
        c4 = curl_code(base + "/data.json")
        check("bare-data-403", c4 == "403", "code=%s" % c4)
        c5 = curl_code(base + "/data.json", ["-b", jar])
        check("cookie-data-200", c5 == "200", "code=%s" % c5)
        order = "/?order=C-20261120-002"
        c6 = curl_code(base + order)
        check("bare-order-403", c6 == "403", "code=%s" % c6)
        c7 = curl_code(base + order + "&k=" + urllib.parse.quote(token))
        check("token-order-200", c7 == "200", "code=%s" % c7)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-18s %s" % (s, n, d))
        print("\n%d passed / %d total（base=%s，暗号不打印）" % (len(RESULTS) - len(fails), len(RESULTS), base))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
