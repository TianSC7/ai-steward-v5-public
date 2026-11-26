# -*- coding: utf-8 -*-
"""会话归档助手边界用例（archive_session.py）。

覆盖 11 个形态：
  1  cli 会话归档：time_archived/time_updated 置位，archived_stores=["cli"]
  2  tasks 会话归档：archived=1、updated_at 刷新
  3  幂等：二次归档 skipped=已归档，time_archived 不变，备份不覆写
  4  陌生会话（两个库都没有）rc=4 且不产生任何备份
  5  已删除条目（deleted=1）跳过：archived 保持 0，不备份
  6  备份：首次写前生成 .bak-archive-session，内容=写前快照
  7  同会话双库命中：cli+tasks 两边都归档、两边备份均=写前快照
  8  --list：归档/活跃标记正确且只读（db sha 不变）
  9  无参数 rc=2
  10 --help rc=0
  11 全程不碰 ~/.zcode 真库（mtime 与 .bak-* 边文件集不变）

纪律：全部用例只建临时 sqlite 库，经 AI_STEWARD_CLI_DB / AI_STEWARD_TASK_DB
环境变量注入，绝不读写 ~/.zcode 真库。

用法：python -X utf8 test_archive_session.py [--json]
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
NOW0 = int(time.time() * 1000)
CLI_SESS = "sess_fake-cli-0003"
TASK_SESS = "sess_fake-task-0004"
BOTH_SESS = "sess_fake-both-0005"
DEAD_SESS = "sess_fake-dead-0006"
GONE_SESS = "sess_fake-gone-0007"
BAK_SESS = "sess_fake-bak-0009"
LIST_ACTIVE = "sess_fake-list-active-0008"

# 真库护栏：任何写库动作都必须先落 .bak-archive-session 边文件（备份即写前哨兵）；
# 本测试只用临时库，故真库主文件 mtime 与边文件集必须全程不变。
REAL_DBS = (os.path.expanduser("~/.zcode/cli/db/db.sqlite"),
            os.path.expanduser("~/.zcode/v2/tasks-index.sqlite"))
SIDECAR_SUFFIXES = (".bak-cron-selfwake", ".bak-archive-session")


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


def _drop(path: str) -> None:
    for suf in ("", "-wal", "-shm", ".bak-archive-session"):
        p = path + suf
        if os.path.exists(p):
            os.remove(p)


def make_cli_db(path: str, rows: list[tuple[str, str, int | None]]) -> None:
    """rows = [(session_id, title, time_archived|None)]。"""
    _drop(path)
    con = sqlite3.connect(path)
    con.execute("create table session (id text primary key, title text, directory text,"
                " time_created integer, time_updated integer, time_archived integer)")
    for sid, title, archived in rows:
        con.execute("insert into session (id, title, directory, time_created, time_updated,"
                    " time_archived) values (?,?,?,?,?,?)",
                    (sid, title, HERE, NOW0, NOW0, archived))
    con.commit()
    con.close()


def make_task_db(path: str, rows: list[tuple[str, str, int, int]]) -> None:
    """rows = [(task_id, title, archived, deleted)]。"""
    _drop(path)
    con = sqlite3.connect(path)
    con.execute("create table tasks (task_id text primary key, title text, archived integer"
                " not null default 0, deleted integer not null default 0, workspace_path text,"
                " updated_at integer)")
    for tid, title, archived, deleted in rows:
        con.execute("insert into tasks values (?,?,?,?,?,?)",
                    (tid, title, archived, deleted, HERE, NOW0))
    con.commit()
    con.close()


def env_for(cli: str, task: str) -> dict:
    return {"AI_STEWARD_CLI_DB": cli, "AI_STEWARD_TASK_DB": task}


def run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env)
    return subprocess.run([PY, "-X", "utf8", os.path.join(HERE, "archive_session.py")] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=90, env=e)


def jload(stdout: str) -> dict:
    try:
        return json.loads(stdout)
    except ValueError:
        return {}


def cli_row(path: str, sid: str):
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        return con.execute("select time_archived, time_updated from session where id=?",
                           (sid,)).fetchone()
    finally:
        con.close()


def task_row(path: str, sid: str):
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        return con.execute("select archived, updated_at from tasks where task_id=?",
                           (sid,)).fetchone()
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.parse_args()

    fp = announce("test_archive_session")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(180, "test_archive_session")

    real_before = real_snapshot()
    tmp = tempfile.mkdtemp(prefix="steward-archive-")
    try:
        none = lambda n: os.path.join(tmp, "none-%s.sqlite" % n)  # noqa: E731 故意不存在

        # 1 cli 会话归档
        cli_db = os.path.join(tmp, "cli.sqlite")
        make_cli_db(cli_db, [(CLI_SESS, "cli 会话", None)])
        env = env_for(cli_db, none("cli"))
        r = run(["--session", CLI_SESS, "--json"], env)
        out = jload(r.stdout)
        ta = cli_row(cli_db, CLI_SESS)
        case("cli-归档置位", r.returncode == 0 and out.get("ok") is True
             and out.get("archived_stores") == ["cli"] and out.get("hits") == ["cli"]
             and ta and ta[0] and ta[0] >= NOW0 and ta[1] and ta[1] >= NOW0,
             "rc=%s time_archived=%s" % (r.returncode, ta[0] if ta else None))

        # 2 tasks 会话归档
        task_db = os.path.join(tmp, "tasks.sqlite")
        make_task_db(task_db, [(TASK_SESS, "tasks 会话", 0, 0)])
        env2 = env_for(none("tasks"), task_db)
        r = run(["--session", TASK_SESS, "--json"], env2)
        out = jload(r.stdout)
        av = task_row(task_db, TASK_SESS)
        case("tasks-归档置位", r.returncode == 0 and out.get("archived_stores") == ["tasks"]
             and av and av[0] == 1 and av[1] and av[1] >= NOW0,
             "rc=%s archived=%s" % (r.returncode, av[0] if av else None))

        # 3 幂等：二次归档跳过，time_archived 不变，备份不覆写
        ta_before = cli_row(cli_db, CLI_SESS)[0]
        bakp = cli_db + ".bak-archive-session"
        bak_before = sha256(bakp) if os.path.exists(bakp) else None
        r = run(["--session", CLI_SESS, "--json"], env)
        out = jload(r.stdout)
        ta_after = cli_row(cli_db, CLI_SESS)[0]
        bak_after = sha256(bakp) if os.path.exists(bakp) else None
        case("幂等-二次归档", r.returncode == 0 and out.get("ok") is True
             and out.get("archived_stores") == []
             and out.get("skipped") == [{"store": "cli", "reason": "已归档"}]
             and out.get("hits") == ["cli"] and ta_after == ta_before
             and bak_after == bak_before,
             "skipped=%s ta_same=%s bak_same=%s" % (out.get("skipped"), ta_after == ta_before,
                                                    bak_after == bak_before))

        # 4 陌生会话：rc=4，不落任何备份
        miss_db = os.path.join(tmp, "miss.sqlite")
        make_cli_db(miss_db, [("sess_fake-other", "别人会话", None)])
        env3 = env_for(miss_db, none("miss"))
        r = run(["--session", GONE_SESS, "--json"], env3)
        out = jload(r.stdout)
        case("陌生会话-exit4", r.returncode == 4 and out.get("ok") is False
             and "不在任何会话库" in out.get("error", "")
             and not os.path.exists(miss_db + ".bak-archive-session")
             and not os.path.exists(none("miss") + ".bak-archive-session"),
             "rc=%s error=%s" % (r.returncode, out.get("error")))

        # 5 已删除条目：跳过且不备份
        del_db = os.path.join(tmp, "deleted.sqlite")
        make_task_db(del_db, [(DEAD_SESS, "已删除会话", 0, 1)])
        env4 = env_for(none("del"), del_db)
        r = run(["--session", DEAD_SESS, "--json"], env4)
        out = jload(r.stdout)
        av = task_row(del_db, DEAD_SESS)
        case("已删除条目跳过", r.returncode == 0 and out.get("ok") is True
             and out.get("archived_stores") == []
             and out.get("skipped") == [{"store": "tasks", "reason": "已删除"}]
             and av and av[0] == 0
             and not os.path.exists(del_db + ".bak-archive-session"),
             "rc=%s skipped=%s archived=%s" % (r.returncode, out.get("skipped"),
                                               av[0] if av else None))

        # 6 备份：首次写前生成，内容=写前快照
        bak_db = os.path.join(tmp, "bak.sqlite")
        bak_db_bak = bak_db + ".bak-archive-session"
        make_cli_db(bak_db, [(BAK_SESS, "备份用例", None)])
        sha_pre = sha256(bak_db)
        r = run(["--session", BAK_SESS, "--json"], env_for(bak_db, none("bak")))
        case("备份-写前生成", r.returncode == 0 and os.path.exists(bak_db_bak)
             and sha256(bak_db_bak) == sha_pre,
             "bak_exists=%s sha_match=%s" % (os.path.exists(bak_db_bak),
                                             os.path.exists(bak_db_bak)
                                             and sha256(bak_db_bak) == sha_pre))

        # 7 同会话双库命中：两边归档，两边备份均=写前快照
        d_cli = os.path.join(tmp, "dual-cli.sqlite")
        d_task = os.path.join(tmp, "dual-tasks.sqlite")
        make_cli_db(d_cli, [(BOTH_SESS, "双库会话", None)])
        make_task_db(d_task, [(BOTH_SESS, "双库会话", 0, 0)])
        sha_c, sha_t = sha256(d_cli), sha256(d_task)
        r = run(["--session", BOTH_SESS, "--json"], env_for(d_cli, d_task))
        out = jload(r.stdout)
        case("双库同会话归档", r.returncode == 0
             and out.get("archived_stores") == ["cli", "tasks"]
             and out.get("hits") == ["cli", "tasks"] and out.get("skipped") == []
             and cli_row(d_cli, BOTH_SESS)[0] and task_row(d_task, BOTH_SESS)[0] == 1
             and sha256(d_cli + ".bak-archive-session") == sha_c
             and sha256(d_task + ".bak-archive-session") == sha_t,
             "stores=%s" % out.get("archived_stores"))

        # 8 --list：标记正确且只读
        l_cli = os.path.join(tmp, "list-cli.sqlite")
        l_task = os.path.join(tmp, "list-tasks.sqlite")
        make_cli_db(l_cli, [(CLI_SESS + "-a", "已归档会话", NOW0), (LIST_ACTIVE, "活跃会话", None)])
        make_task_db(l_task, [(TASK_SESS + "-a", "gui 活跃", 0, 0)])
        env5 = env_for(l_cli, l_task)
        snap = {p: sha256(p) for p in (l_cli, l_task)}
        r = run(["--list", "--json"], env5)
        rows = jload(r.stdout) if isinstance(jload(r.stdout), list) else []
        by_id = {row.get("id"): row for row in rows}
        r2 = run(["--list"], env5)
        case("list-只读含标记", r.returncode == 0 and len(rows) == 3
             and by_id.get(CLI_SESS + "-a", {}).get("archived") is True
             and by_id.get(LIST_ACTIVE, {}).get("archived") is False
             and by_id.get(TASK_SESS + "-a", {}).get("store") == "tasks"
             and by_id.get(TASK_SESS + "-a", {}).get("archived") is False
             and r2.returncode == 0 and "ARCHIVED" in r2.stdout and "active" in r2.stdout
             and "共 3 个会话（归档 1）" in r2.stdout
             and all(sha256(p) == h for p, h in snap.items()),
             "json_rows=%d plain_tail=%s" % (len(rows), r2.stdout.strip().splitlines()[-1][:40]))

        # 9 无参数 rc=2 / --help rc=0
        r = run([], env5)
        case("无参数-rc2", r.returncode == 2 and "usage" in (r.stdout + r.stderr).lower(),
             "rc=%s" % r.returncode)
        r = run(["--help"], env5)
        case("help-可跑", r.returncode == 0, "rc=%s" % r.returncode)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # 10 真库护栏：mtime 与 .bak-* 边文件集全程不变
    real_after = real_snapshot()
    diff = [os.path.basename(k) for k in real_before if real_before[k] != real_after.get(k)]
    case("真库-mtime与备份集不变", not diff,
         "checked=%d changed=%s" % (len(real_before), diff or "无"))

    bad = [x for x in RESULTS if x[0] == "FAIL"]
    for st, name, detail in RESULTS:
        print("[%s] %-22s %s" % (st, name, detail[:110]))
    print("|  | %d passed / %d total" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
