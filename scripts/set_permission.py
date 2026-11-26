# -*- coding: utf-8 -*-
"""把管家派出的会话设为完全访问（yolo），避免 worker 弹权限卡住等人。

背景：ZCode 的 CreateSession 一律以 build 模式建会话；build 下部分工具
（CreateSession/CronCreate 等会话管理类）会弹授权，无人值守时直接卡死。
会话权限存在 ZCode 库 `session.permission`（JSON，如 {"mode":"build"}），
运行中的会话按该字段实时判定，因此派单后立刻改库即可生效（实测）。

项目级（关键兜底）：ZCode 新窗口/子智能体（sess_subagent_agent_*）不继承会话级权限，
只继承项目级 `local_setting`（scope=project, namespace=permission, key=mode）。
项目级没设 yolo 时，新窗口/子智能体一律弹授权（2026-11-18 事故根因）。

用法：
  python -X utf8 set_permission.py --session sess_xxx [--mode yolo|build|guarded]
  python -X utf8 set_permission.py --session sess_xxx --check
  python -X utf8 set_permission.py --from-registry        # 台账里所有 sessionId + dispatcher
  python -X utf8 set_permission.py --project proj_xxx     # 项目级（新窗口/子智能体继承）
  python -X utf8 set_permission.py --workspace C:/path    # 按工作区路径自动推 proj_* 再设
退出码：0 = 成功/无需变更；2 = 库不可读或会话不存在。
每次修改前把旧值备份到 ~/.zcode/cli/permission-backup-<ts>.json（可还原，不删任何会话）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.dont_write_bytecode = True

from fingerprint import announce  # noqa: E402

DB = os.environ.get("STEWARD_ZCODE_DB") or os.path.join(
    os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "state", "registry.json")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=5)
    con.execute("pragma busy_timeout=5000")
    return con


def _sessions_from_registry() -> list[str]:
    ids: list[str] = []
    try:
        reg = json.load(open(REGISTRY, encoding="utf-8"))
    except (OSError, ValueError):
        return ids
    for t in reg.get("tasks") or []:
        if isinstance(t, dict) and t.get("sessionId"):
            ids.append(str(t["sessionId"]))
    d = reg.get("dispatcher") or {}
    if isinstance(d, dict) and d.get("sessionId"):
        ids.append(str(d["sessionId"]))
    return sorted(set(ids))


def project_id_from_path(path: str) -> str:
    """ZCode 项目 id 规则：去掉盘符冒号，反斜杠/正斜杠转 '-'，全小写，加 proj_ 前缀。

    实测：C:\\Users\\TIAN\\Documents\\mate -> proj_c-users-tian-documents-mate
    """
    s = os.path.abspath(path).replace(":", "").replace("\\", "-").replace("/", "-").lower()
    return "proj_" + s


def _apply_projects(cur, con, pids: list[str], mode: str, check: bool,
                    allow_tools: list[str] | None = None) -> int:
    """项目级 mode / ruleset 读写；返回变更数。check=True 只打印。

    allow_tools 非空时，往项目 ruleset 追加 {"toolName": X} 通配条目（无 ruleContent=全部允许）。
    这条不依赖权限快照——子智能体出生自带的 build 快照也能命中，2026-11-18 事故的根治手段。
    """
    value = json.dumps({"mode": mode}, separators=(",", ":"))
    changed = []
    for pid in pids:
        r = cur.execute(
            "select value from local_setting where scope='project' and scope_id=? "
            "and namespace='permission' and key='mode'", (pid,)).fetchone()
        old = r[0] if r else None
        if check:
            print("%s  mode=%s" % (pid, old if old is not None else "<未设置>"))
        elif old == value:
            print("%s  已是 mode=%s" % (pid, mode))
        else:
            changed.append({"kind": "mode", "project": pid, "old": old, "new": value})
        if allow_tools:
            r2 = cur.execute(
                "select value from local_setting where scope='project' and scope_id=? "
                "and namespace='permission' and key='ruleset'", (pid,)).fetchone()
            rule_old = r2[0] if r2 else None
            ruleset = json.loads(rule_old) if rule_old else {"version": 1, "allow": []}
            allow = ruleset.setdefault("allow", [])
            have_wild = {e.get("toolName") for e in allow
                         if isinstance(e, dict) and not e.get("ruleContent")}
            added = [t for t in allow_tools if t not in have_wild]
            if check:
                print("%s  wildcard=%s" % (pid, sorted(have_wild) or "<无>"))
            elif added:
                for t in added:
                    allow.append({"toolName": t})
                changed.append({"kind": "ruleset", "project": pid, "old": rule_old,
                                "new": json.dumps(ruleset, ensure_ascii=False, separators=(",", ":")),
                                "added": added})
                print("%s  ruleset 追加通配 %s（共 %d 条）" % (pid, ",".join(added), len(allow)))
            else:
                print("%s  ruleset 已有通配 %s" % (pid, ",".join(sorted(have_wild))))
    if check or not changed:
        return 0
    bak = os.path.join(os.path.dirname(DB), "permission-backup-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    json.dump(changed, open(bak, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    now = int(time.time() * 1000)
    for item in changed:
        cur.execute(
            "insert or replace into local_setting"
            "(scope,scope_id,namespace,key,value,schema_version,time_created,time_updated) "
            "values('project',?,'permission',?,?,1,?,?)",
            (item["project"], item["kind"], item["new"], now, now))
    con.commit()
    print("已更新项目级权限：%d 条（备份 %s）" % (len(changed), bak))
    for item in changed:
        print("  %s [%s]" % (item["project"], item["kind"]))
    return len(changed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", action="append", default=[])
    ap.add_argument("--project", action="append", default=[],
                    help="项目 id（proj_*）：写 local_setting 项目级 mode，新窗口/子智能体继承")
    ap.add_argument("--workspace", action="append", default=[],
                    help="工作区绝对路径，自动推导 proj_* 项目 id")
    ap.add_argument("--allow-tool", action="append", default=[],
                    help="随 --project/--workspace 往规则集追加工具通配允许（如 WebSearch）")
    ap.add_argument("--from-registry", action="store_true")
    ap.add_argument("--mode", default="yolo", choices=["yolo", "build", "guarded", "edit", "plan"])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    fp = announce("set_permission")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    if not os.path.exists(DB):
        print("数据库不存在: %s" % DB)
        return 2

    pids = list(args.project) + [project_id_from_path(w) for w in args.workspace]
    pids = sorted(set(p for p in pids if p))

    ids = list(args.session)
    if args.from_registry:
        ids += _sessions_from_registry()
    ids = sorted(set(i for i in ids if i))
    if not ids and not pids:
        print("没有指定会话或项目（--session / --from-registry / --project / --workspace）")
        return 2

    con = _connect()
    cur = con.cursor()
    if pids:
        _apply_projects(cur, con, pids, args.mode, args.check, args.allow_tool)
    if not ids:
        con.close()
        return 0
    rows = []
    missing = []
    for sid in ids:
        r = cur.execute("select permission from session where id=?", (sid,)).fetchone()
        if r is None:
            missing.append(sid)
        else:
            rows.append((sid, r[0]))
    if args.check:
        for sid, perm in rows:
            print("%s  %s" % (sid, perm))
        for sid in missing:
            print("%s  <不存在>" % sid)
        con.close()
        return 0

    target = json.dumps({"mode": args.mode}, separators=(",", ":"))
    changed = []
    for sid, perm in rows:
        if perm == target:
            continue
        try:
            old = json.loads(perm) if perm else {}
        except ValueError:
            old = {"raw": perm}
        if old.get("mode") == args.mode:
            continue
        changed.append({"session": sid, "old": perm, "new": target})

    if not changed:
        print("无需变更：%d 个会话已是 mode=%s" % (len(rows), args.mode))
        for sid in missing:
            print("缺失: " + sid)
        con.close()
        return 0

    bak = os.path.join(os.path.dirname(DB), "permission-backup-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    json.dump(changed, open(bak, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for item in changed:
        cur.execute("update session set permission=? where id=?", (target, item["session"]))
    con.commit()
    con.close()

    print("已设为 mode=%s：%d 个会话（备份 %s）" % (args.mode, len(changed), bak))
    for item in changed:
        print("  %s  %s -> %s" % (item["session"], item["old"], item["new"]))
    for sid in missing:
        print("缺失: " + sid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
