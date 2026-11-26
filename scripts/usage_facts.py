# -*- coding: utf-8 -*-
"""成本/速度取证：读取用户在 ZCode 上装的 usage-bar（zusage.py）数据。

用途：管家汇报时必须能回答"今天花了多少、哪个模型最贵、哪个会话最烧、工具错误多少"。
本脚本只读调用，不修改 usage-bar 任何文件；usage-bar 不存在时返回 available=false，
由管家降级汇报（不阻塞其他工作）。

用法：
  python -X utf8 usage_facts.py [--json] [--sessions 5]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

ZUSAGE = os.path.expanduser("~/.zcode/plugins/usage-bar/zusage.py")


def call(args: list, timeout: float = 60) -> str:
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", ZUSAGE] + args,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return ""


def parse_today(text: str) -> dict:
    out = {"requests": None, "input_m": None, "cache_read_m": None, "output_m": None,
           "total_m": None, "cost_cny": None, "models": []}
    m = re.search(r"(\d+)\s*次请求\s*input\s*([\d.]+)M(?:\s*\(cache read\s*([\d.]+)M\))?\s*output\s*([\d.]+)M\s*合计\s*([\d.]+)M", text)
    if m:
        out.update(requests=int(m.group(1)), input_m=float(m.group(2)),
                   cache_read_m=float(m.group(3)) if m.group(3) else None,
                   output_m=float(m.group(4)), total_m=float(m.group(5)))
    c = re.search(r"目录价\s*¥([\d.]+)", text)
    if c:
        out["cost_cny"] = float(c.group(1))
    for line in text.splitlines():
        mm = re.match(r"^\s{2,}([^:：]+):\s*¥([\d.]+)(?:（(.+)）)?\s*$", line)
        if mm:
            out["models"].append({"model": mm.group(1).strip(), "cost_cny": float(mm.group(2)),
                                  "note": (mm.group(3) or "").strip()})
    return out


def gateway_totals() -> dict:
    """网关自己的成本账：累计请求/token/缓存命中/成本（按上游真实模型算）。

    usage-bar 按 ZCode 传来的 model_id 记账，别名 auto* 会被记成"未定价"；
    网关在转发时拿到上游真实模型与 cached_tokens，顺手把成本算准。
    """
    url = os.environ.get("STEWARD_GATEWAY_URL", "http://127.0.0.1:8788")
    try:
        with urllib.request.urlopen(url + "/admin/status", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        t = d.get("totals") or {}
        return {"up": True, "requests": t.get("requests"), "ok": t.get("ok"), "fail": t.get("fail"),
                "tokens_in": t.get("tokens_in"), "tokens_out": t.get("tokens_out"),
                "tokens_cached": t.get("tokens_cached"), "cost_cny": t.get("cost_cny"),
                "unpriced": t.get("unpriced")}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"up": False, "error": str(e)[:120]}


def current() -> dict:
    txt = call(["json"], timeout=30)
    try:
        d = json.loads(txt)
    except ValueError:
        return {}
    s = d.get("session") or {}
    tools = s.get("tools") or {}
    top = sorted((tools.get("list") or []), key=lambda t: -(t.get("count") or 0))[:5]
    return {"sid": s.get("sid"), "title": s.get("title"), "turns": s.get("turns"),
            "requests": s.get("requests"), "input": s.get("input"), "output": s.get("output"),
            "reasoning": s.get("reasoning"), "cache_read": s.get("cache_read"),
            "total": s.get("total"), "cost_cny": s.get("cost_cny"), "ctx": s.get("ctx"),
            "tool_calls": tools.get("total"), "tool_errors": tools.get("errors"),
            "top_tools": [{"name": t.get("name"), "count": t.get("count"),
                           "duration_ms": t.get("duration_ms"), "errors": t.get("errors")} for t in top]}


def sessions(n: int = 5) -> list:
    text = call(["sessions", str(n)], timeout=45)
    rows = []
    for line in text.splitlines():
        # 兼容 "会话名  requests ... ¥x.xx" 这类行；抓不到就忽略，不猜
        mm = re.search(r"(sess_[0-9a-f-]+|session_[0-9a-z-]+)", line)
        cm = re.search(r"¥([\d.]+)", line)
        if mm or cm:
            rows.append({"raw": line.strip()[:160], "sid": mm.group(1) if mm else None,
                         "cost_cny": float(cm.group(1)) if cm else None})
    return rows[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sessions", type=int, default=5)
    a = ap.parse_args()

    fp = announce("usage_facts")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(180, "usage_facts")

    if not os.path.exists(ZUSAGE):
        out = {"available": False, "reason": "未找到 usage-bar: %s" % ZUSAGE}
        print(json.dumps(out, ensure_ascii=False))
        return 0

    today_txt = call(["today"], timeout=45)
    out = {"available": bool(today_txt), "source": "usage-bar/zusage.py",
           "today": parse_today(today_txt) if today_txt else None,
           "current": current(), "sessions": sessions(a.sessions),
           "gateway": gateway_totals()}
    if not today_txt:
        out["reason"] = "zusage.py 无输出（可能数据库锁或未运行过）"

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        t = out.get("today") or {}
        print("== 今日成本 ==")
        if t:
            print("请求 %s 次 | input %sM (缓存 %sM) | output %sM | 合计 %sM | 目录价 ¥%s"
                  % (t.get("requests"), t.get("input_m"), t.get("cache_read_m"),
                     t.get("output_m"), t.get("total_m"), t.get("cost_cny")))
            for m in t.get("models") or []:
                print("  %-28s ¥%s %s" % (m["model"], m["cost_cny"], m.get("note") or ""))
        c = out.get("current") or {}
        if c:
            print("== 当前会话 ==")
            print("%s | 请求 %s | 输出 %s | 成本 ¥%s | 上下文 %s | 工具 %s(错 %s)"
                  % ((c.get("title") or "")[:30], c.get("requests"), c.get("output"),
                     c.get("cost_cny"), c.get("ctx"), c.get("tool_calls"), c.get("tool_errors")))
        g = out.get("gateway") or {}
        if g.get("up"):
            print("== 网关累计（按上游真实模型计价）==")
            print("请求 %s | input %s | output %s | 缓存 %s | 成本 ¥%s | 未定价 %s 次"
                  % (g.get("requests"), g.get("tokens_in"), g.get("tokens_out"),
                     g.get("tokens_cached"), g.get("cost_cny"), g.get("unpriced")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
