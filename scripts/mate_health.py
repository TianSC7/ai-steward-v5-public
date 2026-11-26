# -*- coding: utf-8 -*-
"""mate 唯一真相源健康巡检：四大文件夹 git 状态 + 旧路径残骸 + 未提交告警。

用法：python -X utf8 mate_health.py [--json]
退出码 0=健康；2=有告警（未提交/残骸/缺远端）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True

MATE = r"C:/Users/Administrator/Documents/mate"
REPOS = {"dll": "源码仓", "newcad": "工作台", "lib": "实验室"}
# 风暴前遗物：mate 以外的旧副本，永远不该回来当参照（CHARTER 禁区）
REMNANTS = [
    r"D:/Github/dll",
    r"C:/Users/Administrator/Documents/GitHub/dll",
    r"C:/Users/Administrator/Documents/Mate/dll",
]


def g(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", os.path.join(MATE, repo)] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or r.stderr).strip()


def check() -> list[dict]:
    out = []
    for repo, role in REPOS.items():
        if not os.path.isdir(os.path.join(MATE, repo, ".git")):
            out.append({"item": repo, "level": "FATAL", "msg": "%s 不是 git 仓" % repo})
            continue
        dirty = len(g(repo, "status", "--porcelain").splitlines())
        remote = g(repo, "remote", "get-url", "origin") or ""
        ahead_r = subprocess.run(["git", "-C", os.path.join(MATE, repo),
                                  "rev-list", "--count", "@{u}..HEAD"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
        ahead = ahead_r.stdout.strip() if ahead_r.returncode == 0 else ""
        warns = []
        if dirty:
            warns.append("未提交 %d 项" % dirty)
        if not remote:
            warns.append("无远端（等用户给 GitHub 仓库清单后接线）")
        elif not ahead:
            warns.append("无上游分支（从未推送）")
        elif ahead != "0":
            warns.append("领先远端 %s 提交未推" % ahead)
        out.append({"item": repo, "role": role, "head": g(repo, "log", "--oneline", "-1")[:12],
                    "remote": remote or "无", "level": "WARN" if warns else "OK",
                    "msg": "；".join(warns) or "干净"})
    remnants = [p for p in REMNANTS if os.path.exists(p)]
    out.append({"item": "旧路径残骸", "level": "WARN" if remnants else "OK",
                "msg": ("存在 %d 处（待用户点头后清）：" % len(remnants)) + "；".join(remnants) if remnants else "已清净"})
    private = os.path.isdir(os.path.join(MATE, "Private"))
    out.append({"item": "Private", "level": "OK" if private else "WARN",
                "msg": "在位（用户自维护，Seafile 同步）" if private else "缺失"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows = check()
    if a.json:
        print(json.dumps({"rows": rows,
                          "warn": sum(1 for r in rows if r["level"] != "OK")},
                         ensure_ascii=False))
    else:
        for r in rows:
            print("[%s] %-8s %s" % (r["level"], r["item"], r.get("msg", "")))
        n = sum(1 for r in rows if r["level"] != "OK")
        print("\n%s" % ("健康" if not n else "告警 %d 项" % n))
    return 0 if not any(r["level"] != "OK" for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
