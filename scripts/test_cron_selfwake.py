# -*- coding: utf-8 -*-
"""自理巡检闹钟与归档助手用例（cron_selfwake.py / archive_session.py）。

覆盖 18 个形态：
  1  ensure 建行（enabled=1/active/target/interval/next_run_at 在未来）
  2  ensure 幂等（二次 = updated，仍 1 行）
  3  ensure 拒死会话（rc=3，不落行）
  4  ensure 缺 --session（rc=2）
  5  ensure --db 不存在（rc=2）
  6  registry dispatcher 指针被更新且 tasks/decisions 不动
  7  --status 建后可查（rc=0）
  8  --status 无闹钟（rc=1）
  9  --status 只读（db/registry/备份 sha 全不变）
  10 schedule/next_run_at 计算（--interval 45：差值=45min、schedule_rule JSON）
  11 DB 首次写前备份生成，且二次 ensure 不覆写备份
  12 ensure 的 prompt 自带工作时段守卫与红线关键词
  13 archive cli 会话：time_archived 置位、幂等
  14 archive tasks 会话：archived=1
  15 archive 陌生会话 rc=4
  16 --list 输出含归档标记
  17 两个脚本 --help 可跑（py 编译级）
  18 全程不碰 ~/.zcode 真库（mtime 与 .bak-* 边文件集不变）

用法：python -X utf8 test_cron_selfwake.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS: list[tuple[str, str, str]] = []
CRON_ID = "automation-7c1e5a2b-4d3f-4a8e-9b6c-1d2e3f4a5b6c"
SESSION = "sess_fake-steward-0001"
WORKER = "sess_fake-worker-0002"

# 真库护栏：这两个库任何写入都必须先落 .bak-* 边文件（备份即写前哨兵）；
# 本测试只用临时库，故它们的 mtime 与边文件集必须全程不变。
REAL_DBS = (os.path.expanduser("~/.zcode/cli/db/db.sqlite"),
            os.path.expanduser("~/.zcode/v2/tasks-index.sqlite"))
SIDECAR_SUFFIXES = (".bak-cron-selfwake", ".bak-archive-session")

AUTOMATIONS_DDL = """
create table automations (
  automation_id text primary key, title text not null default '', cron_expr text not null,
  prompt text not null, model text, provider text, mode text, thought_level text,
  model_selection text, workspace_key text not null, workspace_path text not null,
  workspace_identity text, target_task_id text, bot_delivery_target text,
  location_kind text not null default 'local', recurring integer not null default 1,
  max_runs integer, end_at integer, schedule_rule text, schedule_edited_by_user integer
  not null default 0, run_count integer not null default 0,
  scheduled_run_count integer not null default 0, enabled integer not null default 1,
  lifecycle_status text not null default 'active', next_run_at integer, last_run_at integer,
  running integer not null default 0, claimed_at integer, dispatch_status text not null
  default 'idle', dispatch_attempts integer not null default 0, retry_at integer,
  last_error text, created_at integer not null, updated_at integer not null,
  project_id text, host_id text, additional_directories text)
"""


def case(name: str, ok: bool, detail: str) -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def real_snapshot() -> dict:
    """真库主文件 + 两类备份边文件的 mtime_ns 快照（None = 不存在）。"""
    snap = {}
    for p in REAL_DBS:
        snap[p] = os.stat(p).st_mtime_ns if os.path.exists(p) else None
        for suf in SIDECAR_SUFFIXES:
            q = p + suf
            snap[q] = os.stat(q).st_mtime_ns if os.path.exists(q) else None
    return snap


def make_db(path: str, with_session: bool) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute(AUTOMATIONS_DDL)
    con.execute("create table tasks (task_id text primary key, title text, archived integer"
                " not null default 0, deleted integer not null default 0)")
    if with_session:
        con.execute("insert into tasks values (?,?,0,0)", (SESSION, "管家会话"))
    con.commit()
    con.close()


def make_registry(path: str) -> None:
    json.dump({"tasks": [{"id": "W-1", "state": "done"}], "decisions": [],
               "dispatcher": {"sessionId": "sess_old", "cron_id": "automation-old"}},
              open(path, "w", encoding="utf-8"))


def run(script: str, args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([PY, "-X", "utf8", os.path.join(HERE, script)] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=90, env=env)


def row_of(db: str, cron_id: str = CRON_ID):
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        cur = con.cursor()
        cur.execute("select * from automations where automation_id=?", (cron_id,))
        cols = [d[0] for d in cur.description]
        r = cur.fetchone()
        return dict(zip(cols, r)) if r else None
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_cron_selfwake")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(180, "test_cron_selfwake")

    real_before = real_snapshot()
    tmp = tempfile.mkdtemp(prefix="steward-selfwake-")
    try:
        db = os.path.join(tmp, "tasks.sqlite")
        reg = os.path.join(tmp, "registry.json")
        cli_db = os.path.join(tmp, "cli.sqlite")
        bak = db + ".bak-cron-selfwake"
        now_ms = int(time.time() * 1000)

        # 1 建行
        make_db(db, with_session=True)
        make_registry(reg)
        r = run("cron_selfwake.py", ["--ensure", "--session", SESSION, "--db", db,
                                     "--registry", reg, "--json"])
        row = row_of(db)
        ok = (r.returncode == 0 and row and row["enabled"] == 1
              and row["lifecycle_status"] == "active" and row["target_task_id"] == SESSION
              and row["next_run_at"] > now_ms and row["recurring"] == 1
              and row["mode"] == "yolo" and row["cron_expr"] == "*/30 * * * *")
        case("ensure-建行", ok, "rc=%s row=%s" % (r.returncode, bool(row)))

        # 2 幂等
        r2 = run("cron_selfwake.py", ["--ensure", "--session", SESSION, "--db", db,
                                      "--registry", reg, "--json"])
        con = sqlite3.connect(db)
        n = con.execute("select count(*) from automations").fetchone()[0]
        con.close()
        case("ensure-幂等", r2.returncode == 0 and n == 1 and '"action": "updated"' in r2.stdout,
             "rc=%s rows=%s out=%s" % (r2.returncode, n, r2.stdout.strip()[:60]))

        # 3 拒死会话
        make_db(db, with_session=False)
        r3 = run("cron_selfwake.py", ["--ensure", "--session", "sess_dead", "--db", db,
                                      "--registry", reg])
        case("ensure-拒死会话", r3.returncode == 3 and row_of(db) is None,
             "rc=%s out=%s" % (r3.returncode, r3.stdout.strip()[:60]))

        # 4 缺 session
        r4 = run("cron_selfwake.py", ["--ensure", "--db", db, "--registry", reg])
        case("ensure-缺session", r4.returncode == 2, "rc=%s" % r4.returncode)

        # 5 DB 不存在
        r5 = run("cron_selfwake.py", ["--ensure", "--session", SESSION,
                                      "--db", os.path.join(tmp, "none.sqlite"),
                                      "--registry", reg])
        case("ensure-DB不存在", r5.returncode == 2, "rc=%s" % r5.returncode)

        # 6 registry 指针（只改 dispatcher，tasks/decisions 与键集不动）
        make_db(db, with_session=True)
        make_registry(reg)
        before_reg = json.load(open(reg, encoding="utf-8"))
        run("cron_selfwake.py", ["--ensure", "--session", SESSION, "--db", db, "--registry", reg])
        reg_d = json.load(open(reg, encoding="utf-8"))
        case("registry-指针", reg_d.get("dispatcher", {}).get("cron_id") == CRON_ID
             and reg_d["dispatcher"]["sessionId"] == SESSION
             and reg_d.get("tasks") == before_reg["tasks"]
             and reg_d.get("decisions") == before_reg["decisions"]
             and set(reg_d) == set(before_reg),
             json.dumps(reg_d.get("dispatcher"), ensure_ascii=False))

        # 7 status 建后可查
        r6 = run("cron_selfwake.py", ["--status", "--db", db, "--registry", reg, "--json"])
        ok6 = r6.returncode == 0 and json.loads(r6.stdout)["cron"]["automation_id"] == CRON_ID
        case("status-建后可查", ok6, "rc=%s" % r6.returncode)

        # 8 status 无闹钟 -> rc=1
        make_db(db, with_session=True)
        make_registry(reg)
        r8 = run("cron_selfwake.py", ["--status", "--db", db, "--registry", reg])
        case("status-无闹钟", r8.returncode == 1 and row_of(db) is None, "rc=%s" % r8.returncode)

        # 9 status 只读：db/registry/备份 sha 与备份边文件集全不变
        make_db(db, with_session=True)
        make_registry(reg)
        run("cron_selfwake.py", ["--ensure", "--session", SESSION, "--db", db, "--registry", reg])
        snap = {p: sha256(p) for p in (db, reg, bak) if os.path.exists(p)}
        side_before = sorted(f for f in os.listdir(tmp) if f.endswith(".bak-cron-selfwake"))
        r9 = run("cron_selfwake.py", ["--status", "--db", db, "--registry", reg, "--json"])
        side_after = sorted(f for f in os.listdir(tmp) if f.endswith(".bak-cron-selfwake"))
        ok9 = (r9.returncode == 0 and side_before == side_after
               and all(sha256(p) == h for p, h in snap.items()))
        case("status-只读", ok9, "rc=%s files=%s" % (r9.returncode, list(snap)))

        # 10 schedule/next_run_at 计算（interval=45）
        if os.path.exists(bak):
            os.remove(bak)
        make_db(db, with_session=True)
        make_registry(reg)
        r10 = run("cron_selfwake.py", ["--ensure", "--session", SESSION, "--interval", "45",
                                       "--db", db, "--registry", reg, "--json"])
        row10 = row_of(db) or {}
        try:
            rule10 = json.loads(row10.get("schedule_rule") or "{}")
        except ValueError:
            rule10 = {}
        out10 = json.loads(r10.stdout) if r10.stdout.strip().startswith("{") else {}
        ok10 = (r10.returncode == 0
                and row10.get("next_run_at", 0) - row10.get("created_at", 0) == 45 * 60000
                and rule10.get("unit") == "minute" and rule10.get("interval") == 45
                and rule10.get("anchorAt") == row10.get("created_at")
                and isinstance(rule10.get("hour"), int) and 0 <= rule10["hour"] <= 23
                and isinstance(rule10.get("minute"), int) and 0 <= rule10["minute"] <= 59
                and out10.get("next_run_at") == row10.get("next_run_at")
                and out10.get("interval_min") == 45)
        case("schedule-next_run_at", ok10,
             "delta=%s rule=%s" % (row10.get("next_run_at", 0) - row10.get("created_at", 0),
                                   row10.get("schedule_rule")))

        # 11 备份：首次写前生成且内容=写前快照；二次 ensure 不覆写
        if os.path.exists(bak):
            os.remove(bak)
        make_db(db, with_session=True)
        make_registry(reg)
        sha_pre = sha256(db)
        r11 = run("cron_selfwake.py", ["--ensure", "--session", SESSION, "--interval", "45",
                                       "--db", db, "--registry", reg, "--json"])
        run("cron_selfwake.py", ["--ensure", "--session", SESSION, "--interval", "45",
                                 "--db", db, "--registry", reg, "--json"])
        n11 = sqlite3.connect(db).execute("select count(*) from automations").fetchone()[0]
        case("备份-写前生成不覆写", r11.returncode == 0 and os.path.exists(bak)
             and sha256(bak) == sha_pre and n11 == 1,
             "bak=%s sha_match=%s rows=%s" % (os.path.exists(bak),
                                              sha256(bak) == sha_pre if os.path.exists(bak) else None,
                                              n11))

        # 12 prompt 守卫
        p = (row_of(db) or {}).get("prompt", "")
        case("prompt-守卫红线", all(k in p for k in (
            "24 小时", "steward_cycle.py", "verify_dispatch.py", "绝不删用户文件", "不外传密钥",
            "不擅自部署", "apigoto", "CreateSession")),
             "len=%d" % len(p))

        # 13-16 archive_session（env 覆盖库路径隔离）
        for suffix in ("", "-wal", "-shm"):
            for p2 in (cli_db, cli_db + suffix):
                if os.path.exists(p2):
                    os.remove(p2)
        con = sqlite3.connect(cli_db)
        con.execute("create table session (id text primary key, project_id text, workspace_id text,"
                    " parent_id text, slug text, directory text, path text, title text, version text,"
                    " share_url text, summary_additions integer, summary_deletions integer,"
                    " summary_files integer, summary_diffs text, revert text, permission text,"
                    " time_created integer, time_updated integer, time_compacting integer,"
                    " time_archived integer)")
        con.execute("insert into session (id, title, directory, time_created, time_updated)"
                    " values (?,?,?,?,?)", (WORKER, "worker 会话", tmp, now_ms, now_ms))
        con.commit()
        con.close()
        env = {"AI_STEWARD_CLI_DB": cli_db, "AI_STEWARD_TASK_DB": os.path.join(tmp, "none.sqlite")}
        ra = run("archive_session.py", ["--session", WORKER, "--json"], env)
        con = sqlite3.connect(cli_db)
        ta = con.execute("select time_archived from session where id=?", (WORKER,)).fetchone()[0]
        con.close()
        case("archive-cli", ra.returncode == 0 and bool(ta), "rc=%s time_archived=%s" % (ra.returncode, ta))

        ra2 = run("archive_session.py", ["--session", WORKER], env)
        case("archive-幂等", ra2.returncode == 0 and "无需变更" in ra2.stdout,
             "out=%s" % ra2.stdout.strip()[:50])

        # tasks 库归档
        task_db = os.path.join(tmp, "tasks2.sqlite")
        con = sqlite3.connect(task_db)
        con.execute("create table tasks (task_id text primary key, title text, archived integer"
                    " not null default 0, deleted integer not null default 0, workspace_path text,"
                    " updated_at integer)")
        con.execute("insert into tasks values (?,?,0,0,?,?)", (WORKER, "gui 会话", tmp, now_ms))
        con.commit()
        con.close()
        env2 = {"AI_STEWARD_CLI_DB": os.path.join(tmp, "none.sqlite"), "AI_STEWARD_TASK_DB": task_db}
        rb = run("archive_session.py", ["--session", WORKER, "--json"], env2)
        con = sqlite3.connect(task_db)
        av = con.execute("select archived from tasks where task_id=?", (WORKER,)).fetchone()[0]
        con.close()
        case("archive-tasks", rb.returncode == 0 and av == 1, "rc=%s archived=%s" % (rb.returncode, av))

        rc9 = run("archive_session.py", ["--session", "sess_nope"], env)
        case("archive-陌生会话", rc9.returncode == 4, "rc=%s" % rc9.returncode)

        rl = run("archive_session.py", ["--list"], env)
        case("list-含归档标记", rl.returncode == 0 and "ARCHIVED" in rl.stdout,
             "out=%s" % rl.stdout.strip().splitlines()[-1][:60])

        rh = run("cron_selfwake.py", ["--help"])
        case("help-可跑", rh.returncode == 0, "rc=%s" % rh.returncode)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # 18 真库护栏：mtime 与 .bak-* 边文件集全程不变
    real_after = real_snapshot()
    diff = [os.path.basename(k) for k in real_before if real_before[k] != real_after.get(k)]
    case("真库-mtime与备份集不变", not diff,
         "checked=%d changed=%s" % (len(real_before), diff or "无"))

    bad = [r for r in RESULTS if r[0] == "FAIL"]
    for st, name, detail in RESULTS:
        print("[%s] %-22s %s" % (st, name, detail[:110]))
    print("|  | %d passed / %d total" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
