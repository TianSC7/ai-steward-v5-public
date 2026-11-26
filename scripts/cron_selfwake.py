#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自理巡检闹钟（steward self-wake cron）——管家到点唤醒自己跑巡检。

背景：ZCode 的 automations 存 ~/.zcode/v2/tasks-index.sqlite（schema 与字段口径见
knowledge/private-memory/zcode-automation-store.md）。部分环境（如 AA 云机）没有
CronCreate 类工具/CLI 通道，按该 schema 直接落行等价；CronCreate 会把 target 绑到
发起会话，这里用 --session 显式指定（管家本人会话）。

用法：
  python -X utf8 scripts/cron_selfwake.py --ensure --session <sess_id> [--interval 30] [--json]
  python -X utf8 scripts/cron_selfwake.py --status [--json]

行为：
  --ensure  幂等建/校闹钟：同 automation_id 已存在则只校正 schedule/enabled/target；
             目标会话必须存在于 cli 会话库或 tasks 表，否则拒建（防指向死会话）。
             成功后把 state/registry.json 的 dispatcher 指针改成 {sessionId, cron_id}
             （只改这一个字段，原子替换，不动 tasks/decisions）。
  --status  只读打印当前闹钟与 dispatcher 指针。
DB 首次写前备份 .bak-cron-selfwake；不删任何东西。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".zcode", "v2", "tasks-index.sqlite")
CLI_DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
REGISTRY = os.path.join(ROOT, "state", "registry.json")
# 固定 automation_id：幂等的锚（换机重建时同 id，cron_health 对得上）
CRON_ID = "automation-7c1e5a2b-4d3f-4a8e-9b6c-1d2e3f4a5b6c"
TITLE = "管家自理巡检（每30分钟·24小时）"
CRON_EXPR = "*/30 * * * *"

PROMPT_TEMPLATE = """【管家自理巡检】你是 AI 大管家（工作机当前宿主，插件根 {root}）。本闹钟每 {interval} 分钟唤醒你一次，24 小时运行。全程不向用户提问；技术路线自决；取保守可回退默认值。按序执行：

1. 任何时候被唤醒都执行巡检，不再区分工作日/工作时段；夜间批次优先派 Step 长文/批量/扫描类任务。
2. 跑巡检：cd {root} && python -X utf8 scripts\\steward_cycle.py --json，读 digest 与 actions。queued=0 且 actions 含 queue-empty 时，从 reports/ 最新日报"明日主线"或 docs  backlog 取一项落卡（steward_board.py add，优先级/模型自定），避免零产出。
3. 派单后必须跑 python -X utf8 scripts\\verify_dispatch.py --id <id> --session <sid> --grace 45；ghost 立即回 queued 重派（INC-20261118-007）。
4. 对 dispatched/running 卡：ReadSession(view="summary") 看状态；python -X utf8 scripts\\steward_scan.py --json 取 git/产物证据；静默超 stale_sec(1800s) 发一次拉起话术（同一卡上限 3 次，超过上报不重试）。
5. worker 报完成的卡：管家独立抽验（重跑测试/读证据文件/核对 sha256），达标才 python -X utf8 scripts\\steward_board.py done --id <id> --evidence <绝对路径> --summary ...；建过 worktree 的随后 worker_worktree.py finish；同一轮跑 python -X utf8 scripts\\archive_session.py --session <worker会话id> 归档该会话（cron target 与本管家会话永不归档）。
6. 派单纪律：新活只走 CreateSession；创建后立刻 RenameSession 为 "W-<任务号> <短标题>（模型简称）"，否则用户在任务列表认不出（用户令：看不到=任务失败）；再跑 verify_dispatch.py 核 tasks 行。AA 供应商实况（2026-11-18 17:00 重编号后）：重活 new-provider-6/deepseek-v4.1-flash（备份 new-provider-11/deepseek-v4.1-flash）、长文/夜班 new-provider-10/step-5-preview（批量可用 step-3.7-flash）、轻活 new-provider-5/nex-n2.6、默认聚合 new-provider-7/auto；apigoto 只做末位兜底不日常派；官方 GLM 不进网关。派后即跑 scripts\\set_permission.py --session <id> 与 --workspace <项目路径>。
7. 红线：绝不删用户文件（先移 _trash）；不外传密钥；不擅自部署/对外发消息。
8. 汇报只报事实（做了什么/证据/还剩什么在跑），不报巡检间隔数字；无需要用户拍板的事只报两行。只有删用户文件/上线/外发/花钱级取舍才停下来详述请用户拍板。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _session_exists(session_id: str, db: str) -> bool:
    for path in (CLI_DB, db):
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=5)
        except sqlite3.Error:
            continue
        try:
            cur = con.cursor()
            for table, col in (("session", "id"), ("tasks", "task_id")):
                try:
                    cur.execute("SELECT 1 FROM %s WHERE %s=?" % (table, col), (session_id,))
                    if cur.fetchone():
                        return True
                except sqlite3.Error:
                    continue  # 该库没有这张表，换下一张
        finally:
            con.close()
    return False


def _project_of(session_id: str, db: str):
    if os.path.exists(CLI_DB):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % CLI_DB.replace("\\", "/"), uri=True, timeout=5)
            try:
                cur = con.cursor()
                cur.execute("SELECT project_id FROM session WHERE id=?", (session_id,))
                row = cur.fetchone()
                if row:
                    return row[0]
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return None


def _backup_db(db: str) -> str:
    bak = db + ".bak-cron-selfwake"
    if not os.path.exists(bak):
        shutil.copy2(db, bak)
    return bak


def ensure(db: str, session_id: str, interval: int, registry: str, as_json: bool) -> int:
    if not os.path.exists(db):
        print("DB 不存在:", db)
        return 2
    if not _session_exists(session_id, db):
        print("目标会话不在会话库，拒建闹钟（防指向死会话）:", session_id)
        return 3
    now = _now_ms()
    prompt = PROMPT_TEMPLATE.format(root=ROOT, interval=interval)
    rule = json.dumps({
        "unit": "minute", "interval": interval,
        "hour": time.gmtime(now / 1000 + 8 * 3600).tm_hour,
        "minute": time.gmtime(now / 1000 + 8 * 3600).tm_min,
        "anchorAt": now,
    }, ensure_ascii=False)
    project_id = _project_of(session_id, db)
    _backup_db(db)
    con = sqlite3.connect(db, timeout=15)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        cur = con.cursor()
        cur.execute("SELECT automation_id FROM automations WHERE automation_id=?", (CRON_ID,))
        existed = cur.fetchone() is not None
        if existed:
            cur.execute(
                "UPDATE automations SET title=?, cron_expr=?, prompt=?, schedule_rule=?, "
                "recurring=1, enabled=1, lifecycle_status='active', next_run_at=?, running=0, "
                "dispatch_status='idle', last_error=NULL, target_task_id=?, workspace_key=?, "
                "workspace_path=?, project_id=?, updated_at=? WHERE automation_id=?",
                (TITLE, CRON_EXPR, prompt, rule, now + interval * 60000, session_id,
                 ROOT, ROOT, project_id, now, CRON_ID))
            action = "updated"
        else:
            cur.execute(
                "INSERT INTO automations (automation_id, title, cron_expr, prompt, model, provider, "
                "mode, thought_level, model_selection, workspace_key, workspace_path, "
                "workspace_identity, target_task_id, bot_delivery_target, location_kind, recurring, "
                "max_runs, end_at, schedule_rule, schedule_edited_by_user, run_count, "
                "scheduled_run_count, enabled, lifecycle_status, next_run_at, last_run_at, running, "
                "claimed_at, dispatch_status, dispatch_attempts, retry_at, last_error, created_at, "
                "updated_at, project_id, host_id, additional_directories) "
                "VALUES (?,?,?,?,NULL,NULL,'yolo',NULL,NULL,?,?,NULL,?,NULL,'local',1,NULL,NULL,?,0,"
                "0,0,1,'active',?,NULL,0,NULL,'idle',0,NULL,NULL,?,?,?,'local','[]')",
                (CRON_ID, TITLE, CRON_EXPR, prompt, ROOT, ROOT, session_id, rule,
                 now + interval * 60000, now, now, project_id))
            action = "created"
        con.commit()
    finally:
        con.close()

    # dispatcher 指针（只改这一个字段；任务台账不动）
    disp = {"sessionId": session_id, "cron_id": CRON_ID,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.gmtime(now / 1000 + 8 * 3600))}
    reg_changed = False
    if os.path.exists(registry):
        with open(registry, encoding="utf-8") as f:
            reg = json.load(f)
        if reg.get("dispatcher") != disp:
            reg["dispatcher"] = disp
            tmp = registry + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                json.dump(reg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, registry)
            reg_changed = True
    out = {"ok": True, "action": action, "automation_id": CRON_ID, "title": TITLE,
           "target_task_id": session_id, "interval_min": interval, "cron_expr": CRON_EXPR,
           "next_run_at": now + interval * 60000, "registry_dispatcher_updated": reg_changed,
           "db_backup": db + ".bak-cron-selfwake"}
    print(json.dumps(out, ensure_ascii=False, indent=1) if as_json else
          "[%s] %s -> %s（每 %d 分钟；dispatcher 指针%s）" % (
              action, CRON_ID, session_id, interval, "已更新" if reg_changed else "无需变更"))
    return 0


def status(db: str, registry: str, as_json: bool) -> int:
    row = None
    if os.path.exists(db):
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True, timeout=5)
        try:
            cur = con.cursor()
            cur.execute("SELECT automation_id, title, enabled, lifecycle_status, next_run_at, "
                        "last_run_at, run_count, target_task_id, last_error FROM automations "
                        "WHERE automation_id=?", (CRON_ID,))
            row = cur.fetchone()
        finally:
            con.close()
    disp = None
    if os.path.exists(registry):
        with open(registry, encoding="utf-8") as f:
            disp = json.load(f).get("dispatcher")
    out = {"cron": dict(zip(
        ["automation_id", "title", "enabled", "lifecycle_status", "next_run_at", "last_run_at",
         "run_count", "target_task_id", "last_error"], row)) if row else None,
        "dispatcher": disp}
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if row else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="管家自理巡检闹钟：幂等建/校/查")
    ap.add_argument("--ensure", action="store_true", help="建/校闹钟（需 --session）")
    ap.add_argument("--status", action="store_true", help="只读查状态")
    ap.add_argument("--session", help="目标会话 id（管家本人会话）")
    ap.add_argument("--interval", type=int, default=30, help="分钟，默认 30")
    ap.add_argument("--db", default=DEFAULT_DB, help="tasks-index.sqlite 路径")
    ap.add_argument("--registry", default=REGISTRY, help="registry.json 路径")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.ensure:
        if not args.session:
            print("--ensure 需要 --session <会话id>")
            return 2
        return ensure(args.db, args.session, args.interval, args.registry, args.json)
    if args.status:
        return status(args.db, args.registry, args.json)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
