# -*- coding: utf-8 -*-
"""大管家巡检取证：一次收齐"到底有没有在干活"的物理证据。

为什么需要它：ZCode 的 rollout jsonl 会在会话重连时消失/重写（实测），单看会话状态会误判。
本脚本从文件系统侧取证，输出一份 JSON 给管家裁决，只读、零副作用。

用法：
  python -X utf8 steward_scan.py [--config ../config/steward.json] [--json]
  python -X utf8 steward_scan.py --json --no-gateway     # 网关没起时也能跑

证据分级：
  E1 会话遥测：rollout jsonl / agents transcript 的 mtime —— 最弱，可缺席
  E2 工作实物：项目产物（PROGRESS.md、debug log 等）mtime —— 在跑的铁证
  E3 提交记录：git log 最近提交时间 —— 阶段完成的铁证
裁决：E2/E3 任一新鲜 = fresh；仅 E1 新鲜 = 可能活跃；全静默超阈值 = stale
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402


def now() -> float:
    return time.time()


def mtime(p: str) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def run(cmd: list, cwd: str) -> str:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
        return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def git_facts(path: str) -> dict:
    if not os.path.isdir(os.path.join(path, ".git")) and not run(["git", "rev-parse", "--git-dir"], path):
        return {"is_git": False}
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    last = run(["git", "log", "-1", "--format=%ct|%h|%s"], path)
    ts, h, subj = (last.split("|", 2) + ["", "", ""])[:3] if last else (0, "", "")
    count7 = len([x for x in run(["git", "log", "--since=7.days", "--oneline"], path).splitlines() if x.strip()])
    dirty = len([x for x in run(["git", "status", "--porcelain"], path).splitlines() if x.strip()])
    return {"is_git": True, "branch": branch, "last_commit_ts": int(ts) if str(ts).isdigit() else 0,
            "last_commit_age_sec": round(now() - int(ts)) if str(ts).isdigit() and int(ts) > 0 else None,
            "last_commit": ("%s %s" % (h, subj))[:120], "commits_7d": count7, "dirty_files": dirty}


def project_facts(p: dict) -> dict:
    path = p.get("path", "")
    exists = os.path.isdir(path)
    arts = []
    if exists:
        for a in p.get("artifacts") or []:
            full = os.path.join(path, a)
            age = round(now() - mtime(full)) if mtime(full) > 0 else None
            arts.append({"path": a, "exists": age is not None, "age_sec": age})
    g = git_facts(path) if exists else {"is_git": False}
    return {"name": p.get("name"), "path": path, "exists": exists, "artifacts": arts, "git": g}


def session_facts() -> list:
    """E1：会话遥测。ZCode rollout 与 agents transcript 双源。"""
    rows: dict[str, dict] = {}
    for pat in (os.path.expanduser("~/.zcode/cli/rollout/model-io-sess_*.jsonl"),
                os.path.expanduser("~/.zcode/cli/agents/sess_*/**/transcript.jsonl")):
        for f in glob.glob(pat, recursive=True):
            base = os.path.basename(f)
            sid = ""
            for part in f.replace("\\", "/").split("/"):
                if part.startswith(("sess_", "session_")):
                    sid = part
                    break
            if not sid and base.startswith("model-io-"):
                core = base[len("model-io-"):]
                sid = core.split(".")[0]
            if not sid:
                continue
            age = round(now() - mtime(f)) if mtime(f) > 0 else None
            r = rows.setdefault(sid, {"session": sid, "sources": [], "age_sec": None})
            r["sources"].append("rollout" if base.startswith("model-io-") else "transcript")
            r["age_sec"] = min([x for x in [r["age_sec"], age] if x is not None], default=None)
    return sorted(rows.values(), key=lambda r: r["age_sec"] if r["age_sec"] is not None else 9e9)


def usage_facts() -> dict:
    """成本/速度取证：调用同目录 usage_facts.py（只读 usage-bar）。"""
    import subprocess
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_facts.py")
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", exe, "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=70)
        return json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"available": False, "reason": str(e)[:160]}


def gateway_facts(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/admin/ledger", timeout=4) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        rows = d.get("rows") or []
        cool = [x["lane"] for x in rows if (x.get("cooldown_left_sec") or 0) > 0]
        worst = sorted([x for x in rows if x.get("success_rate") is not None],
                       key=lambda x: x["success_rate"])[:3]
        return {"up": True, "lanes": len(rows), "cooling": cool,
                "worst": [{"lane": x["lane"], "rate": x["success_rate"], "avg_ms": x.get("avg_ms")} for x in worst]}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"up": False, "error": str(e)}


def verdict(proj: dict, stale: float) -> tuple[str, float | None]:
    ages = [a["age_sec"] for a in proj["artifacts"] if a.get("age_sec") is not None]
    if proj["git"].get("last_commit_age_sec") is not None:
        ages.append(proj["git"]["last_commit_age_sec"])
    if not ages:
        return ("no-evidence", None)
    fresh = min(ages)
    return (("fresh" if fresh <= stale else "idle") if fresh <= stale * 4 else "stale", fresh)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config", "steward.json"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-gateway", action="store_true")
    ap.add_argument("--no-usage", action="store_true", help="跳过 usage-bar 成本取证")
    ap.add_argument("--gateway", default="http://127.0.0.1:8788")
    args = ap.parse_args()

    fp = announce("steward_scan")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]), file=sys.stderr)
        return 2
    arm(240, "steward_scan")

    cfg = {}
    cfg_path = os.path.abspath(args.config)
    if os.path.exists(cfg_path):
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8"))
        except ValueError as e:
            print("配置解析失败: %s" % e, file=sys.stderr)
            return 2
    stale = float(cfg.get("stale_sec", 1800))

    projects = []
    for p in cfg.get("projects") or []:
        f = project_facts(p)
        v, age = verdict(f, stale)
        f["verdict"] = v
        f["freshest_evidence_age_sec"] = age
        projects.append(f)

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stale_sec": stale,
        "projects": projects,
        "sessions": session_facts()[:40],
        "gateway": None if args.no_gateway else gateway_facts(args.gateway),
        "usage": None if args.no_usage else usage_facts(),
        "registry": None,
    }
    reg = os.path.join(os.path.dirname(cfg_path), "..", "state", "registry.json")
    if os.path.exists(reg):
        try:
            out["registry"] = json.load(open(reg, encoding="utf-8"))
        except ValueError:
            pass

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("== 巡检 %s ==" % out["generated_at"])
        for p in projects:
            g = p["git"]
            print("[%-11s] %-14s 最新证据=%ss  %s" % (
                p["verdict"], p["name"],
                p["freshest_evidence_age_sec"] if p["freshest_evidence_age_sec"] is not None else "-",
                ("branch=%s commits7d=%s dirty=%s" % (g.get("branch"), g.get("commits_7d"), g.get("dirty_files"))) if g.get("is_git") else "(非 git)"))
        gw = out["gateway"]
        print("网关: %s" % ("在线 lanes=%s 冷却=%s" % (gw.get("lanes"), gw.get("cooling") or "无") if gw and gw.get("up") else "离线 %s" % (gw or {}).get("error")))
        u = out.get("usage") or {}
        t = u.get("today") or {}
        if t:
            print("今日成本: ¥%s | 请求 %s | 合计 %sM | 最贵: %s"
                  % (t.get("cost_cny"), t.get("requests"), t.get("total_m"),
                     ", ".join("%s ¥%s" % (m["model"], m["cost_cny"])
                               for m in sorted(t.get("models") or [], key=lambda x: -x.get("cost_cny", 0))[:2])))
        elif u:
            print("今日成本: 不可用（%s）" % u.get("reason", "usage-bar 缺席"))
        g2 = u.get("gateway") or {}
        if g2.get("up"):
            print("网关累计: 请求 %s | 成本 ¥%s | 未定价 %s 次（别名流量按上游真实模型计价）"
                  % (g2.get("requests"), g2.get("cost_cny"), g2.get("unpriced")))
        print("会话遥测: 最近 5 个 " + ", ".join("%s(%ss)" % (s["session"][:12], s["age_sec"]) for s in out["sessions"][:5]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
