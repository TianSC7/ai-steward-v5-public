# -*- coding: utf-8 -*-
"""任务台：管家自己的队列（唯一写入口，原子写 + 备份 + 状态机）。

为什么要它：
  管家不能靠聊天记录记任务。任务必须落盘成卡，状态可流转、可审计、可恢复；
  调度台每轮只做「读卡 → 派单/巡检/验收 → 回写」，进程重启也不丢。

状态机：
  queued -> dispatched -> running -> done
                    \\-> blocked -> queued/running
                    \\-> failed  -> queued（重试）或 abandoned
  done / abandoned 是终态；任何回退必须显式 --force。

用法：
  python -X utf8 steward_board.py add --title ... --goal ... --acceptance ... \
      --project mate-lib-ai --priority 1 --tier MEDIUM [--boundary ...] [--depends-on A,B]
  python -X utf8 steward_board.py claim --id W-... --session sess_... [--command app-control-...]
  python -X utf8 steward_board.py state --id W-... --state running [--note ...]
  python -X utf8 steward_board.py done  --id W-... --evidence reports/evidence/W-....md [--summary ...]
  python -X utf8 steward_board.py fail  --id W-... --reason "..." [--retry]
  python -X utf8 steward_board.py steer --id W-... --reason ... --interrupt applied --ack ... [--command ...]
  python -X utf8 steward_board.py reported --id W-...
  python -X utf8 steward_board.py list [--state queued] [--json]
  python -X utf8 steward_board.py get --id W-... [--json]

所有命令支持 --registry（默认 state/registry.json）、--json、--now（测试用 ISO 时间）。
退出码：0 成功；2 参数/状态/证据问题。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import shutil
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ai_store  # noqa: E402  同目录状态原语（原子写 + OS 文件锁）

DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")
DEFAULT_CONFIG = os.path.join(ROOT, "config", "steward.json")

LEGAL_STATES = ("queued", "dispatched", "running", "blocked", "done", "failed", "abandoned")
LEGAL_INTERRUPTS = ("applied", "queued", "failed")
TRANSITIONS = {
    "queued": {"dispatched", "abandoned"},
    "dispatched": {"running", "done", "blocked", "failed", "abandoned", "queued"},
    "running": {"done", "blocked", "failed", "abandoned", "queued"},
    "blocked": {"queued", "running", "done", "failed", "abandoned"},
    "failed": {"queued", "abandoned"},
    "done": set(),
    "abandoned": set(),
}
TIER_MODEL = {
    "SIMPLE": "new-provider-7/auto-cheap",
    "MEDIUM": "new-provider-7/auto",
    "COMPLEX": "new-provider-7/auto-heavy",
    "REASONING": "new-provider-7/auto-reason",
}
MAX_BACKOFF_SEC = 1800
# --extra 只用于锚点修复补字段；归属/状态/证据主字段不得经此改写
PROTECTED_FIELDS = {
    "id", "title", "state", "attempts", "max_attempts", "sessionId",
    "last_command_id", "steer_events", "created_at", "pulls",
    "evidence_path", "evidence_sha256", "worktree", "workspace",
    "evidence_sha_mode", "evidence_frozen_path", "evidence_frozen_sha256",
    "evidence_frozen_at", "reattest_history",
}


def now_dt(raw: str | None = None) -> datetime.datetime:
    if not raw:
        return datetime.datetime.now().astimezone().replace(microsecond=0)
    dt = datetime.datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.replace(microsecond=0)


def now_iso(raw: str | None = None) -> str:
    return now_dt(raw).isoformat()


def parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def load(path: str) -> dict:
    data = json.load(open(path, encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("registry 根必须是对象且 tasks 必须是列表")
    data.setdefault("decisions", [])
    return data


def save(path: str, reg: dict) -> None:
    """互斥前提下原子写台账：唯一临时名 + fsync + os.replace；写前留同秒备份。

    原子性/重试/孤儿回收由 ai_store.atomic_write_text 保证；并发读改写必须包在
    locked(path) 里（读-改-写三步不可被其它写者插入）。
    """
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, path + ".bak-" + stamp)
        except OSError:
            pass  # 备份是尽力而为：并发读占用不阻断主写（主写自身原子且可重试）
    ai_store.atomic_write_text(path, json.dumps(reg, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def locked(path: str, timeout: float = 10.0):
    """跨进程排他锁：台账/账本的读改写不能互相覆盖。

    锁由 OS 文件锁实现（状态目录 .locks/ 下，进程崩溃自动释放），锁名由台账绝对
    路径派生——同目录不同名字的台账互不阻塞。超时仍拿不到就报错，绝不无锁写入。
    """
    try:
        with ai_store.file_lock(ai_store.lock_name_for(path), timeout=timeout):
            yield
    except TimeoutError as exc:
        raise SystemExit("LOCK-TIMEOUT %s（另一进程正在写台账）" % path) from exc


def sha256(path: str, mode: str = "raw") -> str:
    """证据哈希。mode=raw 按工作区字节（历史口径）；mode=lf 先做 CRLF→LF 归一。

    lf 口径与 git blob（仓库入库内容）一致，跨平台/跨行尾稳定；2026-11-19
    M-AUDIT《AUDIT_EVIDENCE_ANCHORS_0719》建议证据锚以 git blob/LF 为准。
    """
    if mode == "lf":
        data = open(path, "rb").read().replace(b"\r\n", b"\n")
        return hashlib.sha256(data).hexdigest()
    if mode != "raw":
        raise SystemExit("BAD-SHA-MODE %r（raw|lf）" % mode)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_evidence(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def rel_to_root(full: str) -> str:
    try:
        inside = os.path.commonpath([os.path.abspath(full), ROOT]) == ROOT
    except ValueError:
        inside = False
    return os.path.relpath(full, ROOT).replace("\\", "/") if inside else full


FROZEN_DIR = os.path.join(ROOT, "state", "archive", "evidence")


def freeze_evidence(t: dict, full: str, now: datetime.datetime, sha_mode: str) -> str:
    """把验收证据复制到只读冻结区并登记哈希。

    done 之后工作树里的证据仍可能被审计追加；冻结副本才是验收对象。
    冻结文件按 <task-id>.<sha12>.md 命名，已存在则复用（幂等）。
    """
    digest = sha256(full, sha_mode)
    os.makedirs(FROZEN_DIR, exist_ok=True)
    name = "%s.%s.md" % (t.get("id"), digest[:12])
    frozen_full = os.path.join(FROZEN_DIR, name)
    if not os.path.exists(frozen_full):
        shutil.copy2(full, frozen_full)
        try:
            os.chmod(frozen_full, 0o444)  # 尽力而为；防误改，不防管理员
        except OSError:
            pass
    t["evidence_frozen_path"] = rel_to_root(frozen_full)
    t["evidence_frozen_sha256"] = digest
    t["evidence_frozen_at"] = now.isoformat()
    return t["evidence_frozen_path"]


def find(reg: dict, task_id: str) -> dict:
    for t in reg["tasks"]:
        if t.get("id") == task_id:
            return t
    raise SystemExit("NOTFOUND %s" % task_id)


def next_id(tasks: list[dict], now: datetime.datetime) -> str:
    day = now.strftime("%Y%m%d")
    prefix = "W-%s-" % day
    nums = []
    for t in tasks:
        tid = str(t.get("id") or "")
        if tid.startswith(prefix):
            try:
                nums.append(int(tid.rsplit("-", 1)[1]))
            except ValueError:
                pass
    return "%s%03d" % (prefix, (max(nums) + 1) if nums else 1)


def transition(task: dict, new_state: str, force: bool = False) -> None:
    old = task.get("state")
    if new_state not in LEGAL_STATES:
        raise SystemExit("BAD-STATE %r（合法：%s）" % (new_state, ",".join(LEGAL_STATES)))
    if new_state == old:
        return
    if not force and new_state not in TRANSITIONS.get(old, set()):
        raise SystemExit("BAD-TRANSITION %s: %s -> %s（需要 --force 才允许）"
                         % (task.get("id"), old, new_state))
    task["state"] = new_state


def workspace_for(project: str) -> str:
    """未显式给 workspace 时按 config/steward.json → dispatch.workspace_map 补全。

    worker 的 cwd 是 ZCode 壳项目（newcad），任务卡没有绝对工作区就会把文件建错地方
    （2026-11-18 W-20261118-003 因此废弃重建）。读不到配置就返回空，不让加卡失败。
    """
    try:
        cfg = json.load(open(DEFAULT_CONFIG, encoding="utf-8"))
        mapping = (cfg.get("dispatch") or {}).get("workspace_map") or {}
        return str(mapping.get(project) or "").strip()
    except Exception:
        return ""


def cmd_add(reg: dict, args, now: datetime.datetime) -> dict:
    title = (args.title or "").strip()
    goal = (args.goal or title).strip()
    acceptance = (args.acceptance or "").strip()
    if not title or not acceptance:
        raise SystemExit("add 需要 --title 与 --acceptance")
    tier = (args.tier or "MEDIUM").upper()
    if tier not in TIER_MODEL:
        raise SystemExit("BAD-TIER %r（%s）" % (tier, ",".join(TIER_MODEL)))
    deps = [x.strip() for x in (args.depends_on or "").split(",") if x.strip()]
    known = {t.get("id") for t in reg["tasks"]}
    missing = [d for d in deps if d not in known]
    if missing:
        raise SystemExit("BAD-DEPENDS 不存在：%s" % ",".join(missing))
    task = {
        "id": args.id or next_id(reg["tasks"], now),
        "title": title,
        "goal": goal,
        "acceptance": acceptance,
        "boundary": (args.boundary or "不删除用户文件；不外传密钥；不擅自部署或外发消息").strip(),
        "project": (args.project or "").strip(),
        "workspace": (args.workspace or "").strip() or workspace_for((args.project or "").strip()),
        "tier": tier,
        "model": (args.model or TIER_MODEL[tier]).strip(),
        "priority": int(args.priority),
        "depends_on": deps,
        "max_attempts": max(1, int(args.max_attempts)),
        "attempts": 0,
        "state": "queued",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "last_check": None,
        "next_retry_at": None,
        "pulls": 0,
        "sessionId": None,
        "last_command_id": None,
        "steer_events": [],
        "evidence_path": None,
        "evidence_sha256": None,
        "evidence_verified_at": None,
        "next_action": "",
        "note": (args.note or "").strip(),
    }
    if args.source:
        task["source"] = args.source
    reg["tasks"].append(task)
    return task


def cmd_claim(reg: dict, args, now: datetime.datetime) -> dict:
    t = find(reg, args.id)
    if t.get("state") != "queued":
        raise SystemExit("BAD-STATE %s 当前 %s，只有 queued 可领取" % (t.get("id"), t.get("state")))
    retry_at = parse_iso(t.get("next_retry_at"))
    if retry_at and retry_at > now:
        raise SystemExit("RETRY-BACKOFF %s 到 %s 之后才可重派" % (t.get("id"), t.get("next_retry_at")))
    attempts = int(t.get("attempts") or 0)
    max_attempts = int(t.get("max_attempts") or 3)
    if attempts >= max_attempts:
        raise SystemExit("MAX-ATTEMPTS %s 已派 %d/%d 次，先人工裁决" % (t.get("id"), attempts, max_attempts))
    t["attempts"] = attempts + 1
    t["sessionId"] = args.session
    if args.command:
        t["last_command_id"] = args.command
    wt_path = getattr(args, "worktree", "")
    if wt_path:
        wt_branch = (getattr(args, "worktree_branch", "") or "").strip()
        wt_repo = (getattr(args, "worktree_repo", "") or "").strip()
        # W-20261118-017 claim 边界：登记隔离工作区必须三件套齐全，且不能拿主 workspace 冒充。
        if not wt_branch or not wt_repo:
            raise SystemExit(
                "BAD-WORKTREE %s: --worktree 必须同时给 --worktree-branch/--worktree-repo；"
                "不是隔离工作区就不要传 --worktree" % t.get("id"))
        t_ws = str(t.get("workspace") or "").strip()
        if t_ws and os.path.normcase(os.path.abspath(wt_path)) == \
                os.path.normcase(os.path.abspath(t_ws)):
            raise SystemExit(
                "BAD-WORKTREE %s: --worktree 等于任务 workspace（%s），不是隔离工作区；"
                "去掉 --worktree 按单写手模式认领" % (t.get("id"), wt_path))
        # 隔离工作区随派单登记：验收/清理看这张卡就知道该合并哪条分支
        t["worktree"] = {
            "path": os.path.abspath(wt_path).replace("\\", "/"),
            "branch": (getattr(args, "worktree_branch", "") or "").strip(),
            "repo": (os.path.abspath(wt_repo).replace("\\", "/") if wt_repo else ""),
            "claimed_at": now.isoformat(),
        }
        if t.get("workspace"):
            t["worktree"]["source_workspace"] = t.get("workspace")
    t["last_check"] = now.isoformat()
    t["last_progress_at"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    t["next_action"] = ""
    if args.note:
        t["note"] = args.note
    transition(t, "dispatched")
    return t


def cmd_state(reg: dict, args, now: datetime.datetime) -> dict:
    t = find(reg, args.id)
    transition(t, args.state, force=args.force)
    t["last_check"] = now.isoformat()
    if args.state == "running":
        t["last_progress_at"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    if args.note:
        t["note"] = args.note
    if args.next_action:
        t["next_action"] = args.next_action
    return t


def cmd_done(reg: dict, args, now: datetime.datetime) -> dict:
    t = find(reg, args.id)
    if not args.evidence:
        raise SystemExit("done 需要 --evidence（没有证据不算完成）")
    full = args.evidence if os.path.isabs(args.evidence) else os.path.join(ROOT, args.evidence)
    if not os.path.isfile(full):
        raise SystemExit("EVIDENCE-MISSING %s" % full)
    if os.path.getsize(full) <= 0:
        raise SystemExit("EVIDENCE-EMPTY %s" % full)
    transition(t, "done", force=args.force)
    t["evidence_path"] = rel_to_root(full)
    t["evidence_sha_mode"] = getattr(args, "sha_mode", "raw")
    t["evidence_sha256"] = sha256(full, t["evidence_sha_mode"])
    freeze_evidence(t, full, now, t["evidence_sha_mode"])
    t["evidence_verified_at"] = now.isoformat()
    t["evidence_source"] = args.summary or ("本机 sha256 校验：" + os.path.basename(full))
    t["last_check"] = now.isoformat()
    t["last_progress_at"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    t["next_action"] = ""
    if args.note:
        t["note"] = args.note
    # 锚点修复补字段：只允许追加/覆盖非保护字段（如 canonical_sha256/merge_commit）
    for kv in getattr(args, "extra", []) or []:
        if "=" not in kv:
            raise SystemExit("BAD-EXTRA %r（需 KEY=VALUE）" % kv)
        key, val = kv.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("BAD-EXTRA %r（KEY 为空）" % kv)
        if key in PROTECTED_FIELDS:
            raise SystemExit("PROTECTED-FIELD %s 不可经 --extra 修改" % key)
        t[key] = val.strip()
    return t


def cmd_reattest(reg: dict, args, now: datetime.datetime) -> dict:
    """对 done 卡重新锚定证据（唯一合法的事后改锚入口）。

    审计追加/证据回流导致工作树文件变化时，不允许静默改文件；必须走本命令：
    旧哈希、原因、操作者全部进 reattest_history，并重新冻结一份副本。
    """
    t = find(reg, args.id)
    if t.get("state") != "done":
        raise SystemExit("reattest 只用于 done 卡（当前 %s）" % t.get("state"))
    reason = (args.reason or "").strip()
    if not reason:
        raise SystemExit("reattest 需要 --reason（记录为什么改锚）")
    ep = args.evidence or t.get("evidence_path")
    if not ep:
        raise SystemExit("reattest 需要 --evidence 或卡上已有 evidence_path")
    full = resolve_evidence(ep)
    if not os.path.isfile(full):
        raise SystemExit("EVIDENCE-MISSING %s" % full)
    if os.path.getsize(full) <= 0:
        raise SystemExit("EVIDENCE-EMPTY %s" % full)
    mode = args.sha_mode or t.get("evidence_sha_mode") or "raw"
    old_sha = t.get("evidence_sha256")
    new_sha = sha256(full, mode)
    t.setdefault("reattest_history", []).append({
        "ts": now.isoformat(),
        "actor": args.actor or "steward",
        "reason": reason,
        "old_sha256": old_sha,
        "new_sha256": new_sha,
        "sha_mode": mode,
        "evidence_path": rel_to_root(full),
        "previous_frozen_path": t.get("evidence_frozen_path"),
    })
    t["evidence_path"] = rel_to_root(full)
    t["evidence_sha_mode"] = mode
    t["evidence_sha256"] = new_sha
    t["evidence_verified_at"] = now.isoformat()
    t["last_check"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    freeze_evidence(t, full, now, mode)
    return t


def cmd_freeze(reg: dict, args, now: datetime.datetime) -> dict:
    """迁移命令：给历史 done 卡补冻结副本（幂等；已有冻结且未 --force 则跳过）。"""
    if args.all_done:
        targets = [t for t in reg["tasks"] if t.get("state") == "done"]
    elif args.id:
        targets = [find(reg, args.id)]
    else:
        raise SystemExit("freeze 需要 --id 或 --all-done")
    frozen, skipped = [], []
    for t in targets:
        if t.get("evidence_frozen_path") and not args.force:
            skipped.append(str(t.get("id")))
            continue
        ep = t.get("evidence_path")
        if not ep:
            skipped.append(str(t.get("id")))
            continue
        full = resolve_evidence(ep)
        if not os.path.isfile(full) or os.path.getsize(full) <= 0:
            skipped.append(str(t.get("id")))
            continue
        mode = t.get("evidence_sha_mode") or "raw"
        want = t.get("evidence_sha256")
        try:
            got = sha256(full, mode)
            got_alt = sha256(full, "lf" if mode == "raw" else "raw")
        except OSError:
            skipped.append("%s(读取失败)" % t.get("id"))
            continue
        if want and want not in (got, got_alt):
            # 冻结只能固化“已验收的那份”；工作树先漂移了必须显式 reattest，
            # 否则等于把漂移悄悄洗成新锚。
            skipped.append("%s(证据漂移，需先 reattest)" % t.get("id"))
            continue
        freeze_evidence(t, full, now, mode)
        t["updated_at"] = now.isoformat()
        frozen.append(str(t.get("id")))
    return {"frozen": frozen, "skipped": skipped, "total": len(targets)}


def cmd_progress(reg: dict, args, now: datetime.datetime) -> dict:
    t = find(reg, args.id)
    t["last_progress_at"] = now.isoformat()
    t["last_check"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    if args.note:
        t["note"] = args.note
    return t


def cmd_pull(reg: dict, args, now: datetime.datetime) -> dict:
    t = find(reg, args.id)
    t["pulls"] = int(t.get("pulls") or 0) + 1
    t["last_check"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    if args.note:
        t["note"] = args.note
    return t


def cmd_fail(reg: dict, args, now: datetime.datetime) -> dict:
    t = find(reg, args.id)
    reason = (args.reason or "").strip()
    if not reason:
        raise SystemExit("fail 需要 --reason")
    attempts = int(t.get("attempts") or 0)
    max_attempts = int(t.get("max_attempts") or 3)
    t["last_error"] = reason
    t["last_check"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    if args.retry and attempts < max_attempts:
        transition(t, "queued", force=args.force)
        backoff = min(MAX_BACKOFF_SEC, 60 * (2 ** max(0, attempts - 1)))
        t["next_retry_at"] = (now + datetime.timedelta(seconds=backoff)).isoformat()
        t["next_action"] = "retry"
    else:
        transition(t, "failed", force=args.force)
        t["next_action"] = "report"
    return t


def cmd_steer(reg: dict, args, now: datetime.datetime) -> dict:
    t = find(reg, args.id)
    if args.interrupt not in LEGAL_INTERRUPTS:
        raise SystemExit("BAD-INTERRUPT %r" % args.interrupt)
    event = {
        "ts": now.isoformat(),
        "reason": (args.reason or "").strip(),
        "interrupt": args.interrupt,
        "ack": (args.ack or "").strip(),
    }
    if args.command:
        event["commandId"] = args.command
    t.setdefault("steer_events", []).append(event)
    t["last_check"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    if args.note:
        t["note"] = args.note
    if args.next_action:
        t["next_action"] = args.next_action
    return t


def cmd_reported(reg: dict, args, now: datetime.datetime) -> dict:
    t = find(reg, args.id)
    t["reported_at"] = now.isoformat()
    t["updated_at"] = now.isoformat()
    if args.note:
        t["note"] = args.note
    return t


def cmd_list(reg: dict, args) -> list[dict]:
    tasks = reg["tasks"]
    if args.state:
        tasks = [t for t in tasks if t.get("state") == args.state]
    if args.project:
        tasks = [t for t in tasks if t.get("project") == args.project]
    tasks = sorted(tasks, key=lambda t: (int(t.get("priority") or 9), str(t.get("created_at") or "")))
    return tasks


def render(task: dict) -> str:
    return "%s [%s] P%s %s sess=%s att=%s/%s %s" % (
        task.get("id"), task.get("state"), task.get("priority"), task.get("title"),
        task.get("sessionId") or "-", task.get("attempts", 0), task.get("max_attempts", 3),
        ("err=" + str(task.get("last_error"))[:60]) if task.get("last_error") else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", default=None, help="测试用：ISO 时间（默认当前本地时间）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("--id")
    a.add_argument("--title", required=True)
    a.add_argument("--goal")
    a.add_argument("--acceptance", required=True)
    a.add_argument("--project", default="")
    a.add_argument("--workspace", default="")
    a.add_argument("--tier", default="MEDIUM")
    a.add_argument("--model")
    a.add_argument("--priority", type=int, default=1)
    a.add_argument("--depends-on", default="")
    a.add_argument("--max-attempts", type=int, default=3)
    a.add_argument("--boundary", default="")
    a.add_argument("--source", default="")
    a.add_argument("--note", default="")
    a.set_defaults(fn=cmd_add)

    c = sub.add_parser("claim")
    c.add_argument("--id", required=True)
    c.add_argument("--session", required=True)
    c.add_argument("--command", default="")
    c.add_argument("--worktree", default="", help="隔离工作区路径（worker_worktree.py ensure 的输出）")
    c.add_argument("--worktree-branch", default="")
    c.add_argument("--worktree-repo", default="")
    c.add_argument("--note", default="")
    c.set_defaults(fn=cmd_claim)

    s = sub.add_parser("state")
    s.add_argument("--id", required=True)
    s.add_argument("--state", required=True, choices=LEGAL_STATES)
    s.add_argument("--note", default="")
    s.add_argument("--next-action", default="")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_state)

    d = sub.add_parser("done")
    d.add_argument("--id", required=True)
    d.add_argument("--evidence", required=True)
    d.add_argument("--summary", default="")
    d.add_argument("--note", default="")
    d.add_argument("--sha-mode", choices=("raw", "lf"), default="raw",
                   help="证据哈希口径：raw=工作区字节（历史默认）；lf=CRLF→LF 归一（=git blob，跨行尾稳定）")
    d.add_argument("--extra", action="append", default=[], metavar="KEY=VALUE",
                   help="追加/覆盖非保护字段（如 canonical_sha256=…；merge_commit=…）；可重复")
    d.add_argument("--force", action="store_true")
    d.set_defaults(fn=cmd_done)

    rt = sub.add_parser("reattest")
    rt.add_argument("--id", required=True)
    rt.add_argument("--evidence", default="")
    rt.add_argument("--reason", required=True)
    rt.add_argument("--actor", default="steward")
    rt.add_argument("--sha-mode", choices=("raw", "lf"), default=None)
    rt.add_argument("--note", default="")
    rt.set_defaults(fn=cmd_reattest)

    fz = sub.add_parser("freeze")
    fz.add_argument("--id")
    fz.add_argument("--all-done", action="store_true")
    fz.add_argument("--force", action="store_true")
    fz.set_defaults(fn=cmd_freeze)

    f = sub.add_parser("fail")
    f.add_argument("--id", required=True)
    f.add_argument("--reason", required=True)
    f.add_argument("--retry", action="store_true")
    f.add_argument("--force", action="store_true")
    f.set_defaults(fn=cmd_fail)

    st = sub.add_parser("steer")
    st.add_argument("--id", required=True)
    st.add_argument("--reason", default="")
    st.add_argument("--interrupt", default="applied", choices=LEGAL_INTERRUPTS)
    st.add_argument("--ack", default="")
    st.add_argument("--command", default="")
    st.add_argument("--note", default="")
    st.add_argument("--next-action", default="")
    st.set_defaults(fn=cmd_steer)

    pg = sub.add_parser("progress")
    pg.add_argument("--id", required=True)
    pg.add_argument("--note", default="")
    pg.set_defaults(fn=cmd_progress)

    pl = sub.add_parser("pull")
    pl.add_argument("--id", required=True)
    pl.add_argument("--note", default="")
    pl.set_defaults(fn=cmd_pull)

    rp = sub.add_parser("reported")
    rp.add_argument("--id", required=True)
    rp.add_argument("--note", default="")
    rp.set_defaults(fn=cmd_reported)

    ls = sub.add_parser("list")
    ls.add_argument("--state", choices=LEGAL_STATES)
    ls.add_argument("--project")
    ls.set_defaults(fn=cmd_list)

    g = sub.add_parser("get")
    g.add_argument("--id", required=True)
    g.set_defaults(fn=None)

    args = ap.parse_args()
    now = now_dt(getattr(args, "now", None))

    if args.cmd in ("list", "get"):
        reg = load(args.registry)
        if args.cmd == "get":
            task = find(reg, args.id)
            print(json.dumps(task, ensure_ascii=False, indent=2) if args.json else render(task))
            return 0
        tasks = cmd_list(reg, args)
        if args.json:
            print(json.dumps(tasks, ensure_ascii=False, indent=2))
        else:
            for t in tasks:
                print(render(t))
        return 0

    with locked(args.registry):
        reg = load(args.registry)
        task = args.fn(reg, args, now)
        save(args.registry, reg)
    if args.cmd == "freeze":
        if args.json:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        else:
            print("FROZEN %d: %s | SKIP %d" % (
                len(task["frozen"]), ",".join(task["frozen"]) or "-", len(task["skipped"])))
            for item in task["skipped"]:
                print("  skip %s" % item)
        return 0
    print(json.dumps(task, ensure_ascii=False, indent=2) if args.json else render(task))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
