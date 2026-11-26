# -*- coding: utf-8 -*-
"""定时任务健康检查边界用例（cron_health.py）。

覆盖 11 个形态：
  1 正常（next_run_at 在未来）-> OK / exit 0
  2 过期超过 grace -> STALE / exit 3
  3 enabled=0 -> DISABLED / exit 3
  4 last_error 非空 -> ERROR / exit 3
  5 next_run_at 为空 -> NO-NEXT / exit 3
  6 automation_id 不存在 -> NOTFOUND / exit 3（缺 cron 本身就是不健康）
  7 数据库不存在 -> exit 2
  8 --json 结构合法（ok/results/status）
  9 --grace-min 边界（过期 3 分钟：grace 5 = OK，grace 1 = STALE）
  10 --from-registry 目标会话不匹配 -> TARGET-MISMATCH / exit 3

用法：python -X utf8 test_cron_health.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS: list[tuple[str, str, str]] = []
NOW = int(datetime.datetime(2026, 11, 17, 23, 33, 20,
                             tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp() * 1000)
DISPATCHER = "sess_dispatcher"


def case(name: str, ok: bool, detail: str) -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))


def make_db(path: str, rows: list[dict]) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute(
        "create table automations (automation_id text primary key, title text, cron_expr text,"
        " enabled integer, lifecycle_status text, next_run_at integer, last_run_at integer,"
        " run_count integer, running integer, dispatch_status text, last_error text, target_task_id text)")
    base = {"title": "t", "cron_expr": "*/20 * * * *", "enabled": 1, "lifecycle_status": "active",
            "next_run_at": NOW + 300000, "last_run_at": NOW - 600000, "run_count": 1,
            "running": 0, "dispatch_status": "idle", "last_error": None,
            "target_task_id": DISPATCHER}
    for r in rows:
        v = dict(base, **r)
        con.execute("insert into automations values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (v["automation_id"], v["title"], v["cron_expr"], v["enabled"],
                     v["lifecycle_status"], v["next_run_at"], v["last_run_at"], v["run_count"],
                     v["running"], v["dispatch_status"], v["last_error"], v["target_task_id"]))
    con.commit()
    con.close()


def make_registry(path: str, cron_id: str, session_id: str = DISPATCHER) -> None:
    json.dump({"tasks": [], "dispatcher": {"sessionId": session_id, "cron_id": cron_id}},
              open(path, "w", encoding="utf-8"))


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-X", "utf8", os.path.join(HERE, "cron_health.py")] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def status_of(r: subprocess.CompletedProcess) -> str:
    try:
        return json.loads(r.stdout)["results"][0]["status"]
    except (ValueError, KeyError, IndexError):
        return "NO-JSON"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_cron_health")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_cron_health")

    tmp = tempfile.mkdtemp(prefix="steward-cron-")
    try:
        db = os.path.join(tmp, "tasks.sqlite")
        reg = os.path.join(tmp, "registry.json")
        now_arg = ["--now", str(NOW), "--db", db]

        make_db(db, [{"automation_id": "a-ok"}])
        r = run(now_arg + ["--id", "a-ok", "--json"])
        case("OK", r.returncode == 0 and status_of(r) == "OK",
             "rc=%s status=%s" % (r.returncode, status_of(r)))

        make_db(db, [{"automation_id": "a-stale", "next_run_at": NOW - 600000}])
        r = run(now_arg + ["--id", "a-stale"])
        case("STALE", r.returncode == 3 and "STALE" in r.stdout, "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:80]))

        make_db(db, [{"automation_id": "a-off", "enabled": 0}])
        r = run(now_arg + ["--id", "a-off"])
        case("DISABLED", r.returncode == 3 and "DISABLED" in r.stdout, "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:80]))

        make_db(db, [{"automation_id": "a-err", "last_error": "dispatch failed"}])
        r = run(now_arg + ["--id", "a-err"])
        case("ERROR", r.returncode == 3 and "ERROR" in r.stdout, "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:80]))

        make_db(db, [{"automation_id": "a-none", "next_run_at": None}])
        r = run(now_arg + ["--id", "a-none"])
        case("NO-NEXT", r.returncode == 3 and "NO-NEXT" in r.stdout, "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:80]))

        make_db(db, [{"automation_id": "a-1"}])
        r = run(now_arg + ["--id", "a-missing"])
        case("NOTFOUND", r.returncode == 3 and "NOTFOUND" in r.stdout, "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:80]))

        r = run(["--db", os.path.join(tmp, "nope.sqlite"), "--id", "a-1", "--now", str(NOW)])
        case("DB缺失", r.returncode == 2, "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:80]))

        make_db(db, [{"automation_id": "a-json"}])
        r = run(now_arg + ["--id", "a-json", "--json"])
        ok = False
        try:
            d = json.loads(r.stdout)
            ok = isinstance(d.get("ok"), bool) and isinstance(d.get("results"), list) \
                 and d["results"][0].get("status") == "OK"
        except (ValueError, KeyError, IndexError):
            pass
        case("JSON结构", r.returncode == 0 and ok, "rc=%s" % r.returncode)

        make_db(db, [{"automation_id": "a-grace", "next_run_at": NOW - 180000}])
        r5 = run(now_arg + ["--id", "a-grace", "--grace-min", "5"])
        r1 = run(now_arg + ["--id", "a-grace", "--grace-min", "1"])
        case("grace边界", r5.returncode == 0 and r1.returncode == 3,
             "grace5=%s grace1=%s" % (r5.returncode, r1.returncode))

        make_db(db, [{"automation_id": "a-reg", "target_task_id": "sess_other"}])
        make_registry(reg, "a-reg")
        r = run(now_arg + ["--from-registry", "--registry", reg])
        case("registry目标不匹配", r.returncode == 3 and "TARGET-MISMATCH" in r.stdout,
             "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:100]))

        make_registry(reg, "a-reg", "sess_other")
        r = run(now_arg + ["--from-registry", "--registry", reg])
        case("registry目标匹配", r.returncode == 0 and "OK" in r.stdout,
             "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:100]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [r for r in RESULTS if r[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-18s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails and RESULTS else 2


if __name__ == "__main__":
    raise SystemExit(main())
