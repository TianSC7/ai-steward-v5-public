# -*- coding: utf-8 -*-
"""steward_scan 边界用例：证据判定不能误报。

覆盖：
  1. 路径不存在        -> exists=false，verdict=no-evidence
  2. 非 git + 无产物   -> no-evidence
  3. 产物新鲜（now）   -> fresh
  4. 产物陈旧（3天）   -> stale
  5. git 提交新鲜      -> fresh（E3 证据生效）
  6. git 提交陈旧      -> stale
  7. 配置非法 JSON     -> 退出码 2（不能静默当空配置跑）
  8. 输出结构           -> 含 projects/sessions/gateway，且 projects 数量一致

用法：python -X utf8 test_scan.py [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
RESULTS: list[tuple[str, str, str]] = []


def rec(ok: bool, name: str, detail: str) -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))


def git(cwd: str, *args: str, env: dict | None = None) -> None:
    e = dict(os.environ)
    e.update(env or {})
    subprocess.run(["git"] + list(args), cwd=cwd, env=e, capture_output=True, text=True)


def make_repo(path: str, days_ago: int) -> None:
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "edge@test.local")
    git(path, "config", "user.name", "edge")
    with open(os.path.join(path, "a.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    ts = "2026-11-14T10:00:00" if days_ago else "2026-11-17T22:00:00"
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "c", env={"GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    fp = announce("test_scan")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(180, "test_scan")

    tmp = tempfile.mkdtemp(prefix="steward-scan-edge-")
    try:
        fresh_dir = os.path.join(tmp, "proj_fresh")
        old_dir = os.path.join(tmp, "proj_old")
        miss_dir = os.path.join(tmp, "proj_missing")
        git_old = os.path.join(tmp, "git_old")
        os.makedirs(fresh_dir)
        os.makedirs(old_dir)
        open(os.path.join(fresh_dir, "PROGRESS.md"), "w", encoding="utf-8").write("fresh")
        stale_file = os.path.join(old_dir, "PROGRESS.md")
        open(stale_file, "w", encoding="utf-8").write("old")
        old_ts = time.time() - 3 * 86400
        os.utime(stale_file, (old_ts, old_ts))
        make_repo(git_old, days_ago=3)

        cfg = {
            "stale_sec": 1800,
            "projects": [
                {"name": "fresh", "path": fresh_dir, "artifacts": ["PROGRESS.md"]},
                {"name": "old", "path": old_dir, "artifacts": ["PROGRESS.md"]},
                {"name": "missing", "path": miss_dir, "artifacts": ["PROGRESS.md"]},
                {"name": "git_old", "path": git_old, "artifacts": []},
            ],
        }
        cfg_path = os.path.join(tmp, "steward.json")
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)

        r = subprocess.run([PY, "-X", "utf8", os.path.join(HERE, "steward_scan.py"),
                            "--config", cfg_path, "--json", "--no-gateway", "--no-usage"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        if r.returncode != 0:
            rec(False, "run", "退出码 %s: %s" % (r.returncode, (r.stderr or "")[:200]))
        else:
            try:
                d = json.loads(r.stdout)
            except ValueError:
                d = {}
                rec(False, "json", "输出不是合法 JSON")
            if not isinstance(d, dict) or not isinstance(d.get("projects"), list) or not d.get("projects"):
                rec(False, "shape", "输出为空对象或缺 projects（空 JSON 不得当通过）")
            else:
                rec(True, "json", "projects=%d sessions=%d" % (len(d.get("projects", [])), len(d.get("sessions", []))))
                if len(d["projects"]) != 4:
                    rec(False, "project-count", "期望 4 个项目，实际 %d" % len(d["projects"]))
                by = {p["name"]: p for p in d["projects"]}

                p = by.get("fresh", {})
                rec(p.get("verdict") == "fresh", "fresh-artifact", "verdict=%s age=%s" % (p.get("verdict"), p.get("freshest_evidence_age_sec")))

                p = by.get("old", {})
                rec(p.get("verdict") == "stale", "stale-artifact", "verdict=%s age=%s" % (p.get("verdict"), p.get("freshest_evidence_age_sec")))

                p = by.get("missing", {})
                rec(p.get("exists") is False and p.get("verdict") == "no-evidence", "missing-path",
                    "exists=%s verdict=%s" % (p.get("exists"), p.get("verdict")))

                p = by.get("git_old", {})
                g = p.get("git", {})
                rec(p.get("verdict") == "stale" and g.get("is_git") is True, "git-old",
                    "verdict=%s commits7d=%s age=%s" % (p.get("verdict"), g.get("commits_7d"), g.get("last_commit_age_sec")))

        bad_cfg = os.path.join(tmp, "bad.json")
        open(bad_cfg, "w", encoding="utf-8").write("{not json")
        r2 = subprocess.run([PY, "-X", "utf8", os.path.join(HERE, "steward_scan.py"),
                             "--config", bad_cfg, "--json", "--no-gateway"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        rec(r2.returncode == 2, "bad-config", "退出码=%s（期望2）" % r2.returncode)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if a.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS], ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-16s %s" % (s, n, d))
        print("\n%d passed / %d total" % (sum(1 for x in RESULTS if x[0] == "PASS"), len(RESULTS)))
    return 0 if RESULTS and all(x[0] == "PASS" for x in RESULTS) else 2


if __name__ == "__main__":
    raise SystemExit(main())
