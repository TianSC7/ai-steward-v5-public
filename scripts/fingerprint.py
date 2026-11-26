# -*- coding: utf-8 -*-
"""测试/回归脚本的版本指纹、环境自检与看门狗（被各脚本 import，不单独运行）。

为什么需要：
  测试脚本最危险的失败不是 FAIL，而是"跑的根本不是这份代码"——旧副本、旧进程、
  旧市场版本，都会给出漂亮的假 PASS。指纹解决三件事：
  1. 每次运行在 stderr 打一行 `FP ai-steward@<版本> script=<sha12> schema=1 env=OK`，
     外层总闸据此断言"这轮子进程确实带指纹跑过"。
  2. 启动时自检插件清单/市场版本/路由配置，任何漂移直接非零退出，不进入测试。
  3. 整体看门狗：超过预算秒数直接退出码 3，避免挂死拖垮 CI 或巡检。

用法：
  from fingerprint import announce, arm
  fp = announce("test_scan")
  if not fp["ok"]: return 2
  arm(180, "test_scan")
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading

SCHEMA = 1
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANIFEST = os.path.join(ROOT, ".zcode-plugin", "plugin.json")
MARKET = os.path.join(os.path.dirname(ROOT), "marketplace.json")


def _sha12(path: str | None = None) -> str:
    p = path or (sys.argv[0] if sys.argv else "")
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "unknown"


def _manifest() -> dict:
    try:
        return json.load(open(MANIFEST, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def selfcheck() -> dict:
    """返回 {ok, problems, plugin, version, schema, sha, script}；problems 为空才算通过。"""
    problems: list[str] = []
    mf = _manifest()
    name = mf.get("name") or ""
    version = mf.get("version") or ""
    if name != "ai-steward":
        problems.append("manifest.name=%r" % name)
    if not re.fullmatch(r"\d+\.\d+\.\d+([-.+][\w.-]+)?", version):
        problems.append("manifest.version=%r" % version)
    script = os.path.abspath(sys.argv[0]) if sys.argv else ""
    if script and os.path.commonpath([script, HERE]) != HERE:
        problems.append("script 不在插件 scripts/ 内: %s" % script)
    try:
        mk = json.load(open(MARKET, encoding="utf-8"))
        entry = next((p for p in mk.get("plugins", []) if p.get("name") == name), None)
        if not entry:
            problems.append("marketplace 缺 %s 条目" % (name or "?"))
        elif entry.get("version") != version:
            problems.append("marketplace.version=%r != manifest %r" % (entry.get("version"), version))
    except (OSError, ValueError) as e:
        problems.append("marketplace 读取失败: %s" % str(e)[:80])
    try:
        json.load(open(os.path.join(ROOT, "config", "routing.json"), encoding="utf-8"))
    except (OSError, ValueError) as e:
        problems.append("routing.json 读取失败: %s" % str(e)[:80])
    return {"ok": not problems, "problems": problems, "plugin": name or "?",
            "version": version or "?", "schema": SCHEMA, "sha": _sha12(), "script": script}


def announce(label: str = "") -> dict:
    """stderr 打指纹行（stdout 留给 --json），返回自检结果。"""
    fp = selfcheck()
    state = "OK" if fp["ok"] else "BAD:" + ";".join(fp["problems"])
    print("FP %s@%s script=%s schema=%d env=%s%s" % (
        fp["plugin"], fp["version"], fp["sha"], fp["schema"], state,
        (" label=" + label) if label else ""), file=sys.stderr, flush=True)
    return fp


def arm(seconds: float, label: str = "script") -> threading.Timer:
    """整体看门狗：超时打印并 os._exit(3)，不做清理（测试进程无副作用需要保留时自行清理）。"""
    def fire() -> None:
        print("[TIMEOUT] %s 超过 %.0fs 预算，强制退出 3" % (label, seconds), file=sys.stderr, flush=True)
        os._exit(3)

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    return t
