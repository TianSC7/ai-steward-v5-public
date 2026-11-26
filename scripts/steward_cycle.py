# -*- coding: utf-8 -*-
"""调度引擎（只读）：把台账变成调度台下一轮要执行的动作清单。

设计原则：
  - 引擎不调用任何 ZCode 工具，只做确定性判定；执行由常驻调度台会话完成。
  - 「AI 停下来」不是等待信号，是升级信号：nudge → steer → replace → split。
  - 并发、依赖、预算、证据、重试全部在这里收口，调度台不需要自己算。

动作类型：
  dispatch      派一个新 worker（含完整 worker prompt 与三条硬纪律）
  dispatch-blocked 派单闸命中（主仓他人活跃/未提交改动/prompt 缺纪律），保持 queued
  verify        在办任务已出现非空证据，调度台核验收尾（done 或打回）
  check         在办任务超时，按 pulls 分级：nudge/steer/replace/split
  unblock       blocked 任务，给诊断清单；按 pulls 分级
  replace       failed/连续无进展，换新会话重派
  split         到达上限，拆子任务/换路线
  reject-done   done 但证据缺失/哈希不符，打回
  replan        依赖失败/不存在，重排
  report        失败/阻塞写日报（不打扰用户）
  budget        今日成本到顶，暂停派新单
  progress-alert 有活但长时间零进展，强制换路线
  idle          无在办、无排队

用法：
  python -X utf8 steward_cycle.py [--json] [--registry ...] [--config ...]
      [--now ISO] [--today-cost 12.3] [--no-kb]
退出码：0 正常；2 台账/配置读取失败。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")
DEFAULT_CONFIG = os.path.join(ROOT, "config", "steward.json")

sys.path.insert(0, HERE)
try:
    import steward_kb  # noqa: E402
except Exception:  # pragma: no cover - KB 坏了不能拖垮调度
    steward_kb = None

DEFAULTS = {
    "max_running": 3,
    "max_running_per_project": 1,
    "stale_sec": 1800,
    "max_attempts": 3,
    "max_pulls": 3,
    "daily_budget_cny": 0,
    "idle_alert_sec": 7200,
    "work_start": "08:00",
    "work_end": "20:00",
    "project_map": {},
    "worktree": {},
    # W-20261118-017 worker 纪律三条机械强制（可用配置关掉，默认全开）
    "check_main_repo": True,     # dispatch 前对 git 仓主仓做他人活跃/未提交改动自检
    "require_exit_marker": True, # verify 阶段证据必须含 EXIT= 或 [PASS]
    # INC-20261118-007 幽灵派单：派单超过该秒数仍无 tasks 行/无 assistant 输出 -> 直接 replace
    "dispatch_grace_sec": 300,
    # 空队列策略：report=只报告（默认，等厂长从日报/backlog 取活）；stop=不自动开新战线
    "queue_empty_policy": "report",
}
ACTIVE = ("dispatched", "running")
DISCIPLINE_MARKERS = ("【硬纪律1", "【硬纪律2", "【硬纪律3")


def now_dt(raw: str | None = None) -> datetime.datetime:
    if raw:
        dt = datetime.datetime.fromisoformat(raw.strip())
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.replace(microsecond=0)
    return datetime.datetime.now().astimezone().replace(microsecond=0)


def parse_iso(value) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def load_json(path: str) -> dict:
    data = json.load(open(path, encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("%s 根不是对象" % path)
    return data


def coerce(default, raw):
    """按默认值类型转换；bool("false") 是 True，必须单独处理。"""
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


def limits(cfg: dict) -> dict:
    raw = cfg.get("dispatch") if isinstance(cfg.get("dispatch"), dict) else {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw:
            out[k] = coerce(DEFAULTS[k], raw[k])
    if not isinstance(out.get("project_map"), dict):
        out["project_map"] = {}
    if not isinstance(out.get("worktree"), dict):
        out["worktree"] = {}
    return out


def state(t: dict) -> str:
    return str(t.get("state") or "queued")


def in_work_hours(now: datetime.datetime, lim: dict) -> bool:
    """工作时段判定；解析失败按非工作时段（宁可安静，不误报）。"""
    try:
        sh, sm = [int(x) for x in str(lim.get("work_start") or "08:00").split(":")]
        eh, em = [int(x) for x in str(lim.get("work_end") or "20:00").split(":")]
        cur = now.hour * 60 + now.minute
        start, end = sh * 60 + sm, eh * 60 + em
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end
    except (TypeError, ValueError):
        return False


def priority(t: dict) -> int:
    try:
        return int(t.get("priority", 1))
    except (TypeError, ValueError):
        return 1


def created(t: dict, now: datetime.datetime) -> datetime.datetime:
    return parse_iso(t.get("created_at")) or now


def progress_ref(t: dict, now: datetime.datetime) -> datetime.datetime:
    return (parse_iso(t.get("last_progress_at")) or parse_iso(t.get("last_check"))
            or parse_iso(t.get("updated_at")) or parse_iso(t.get("created_at")) or now)


def age_min(ref: datetime.datetime, now: datetime.datetime) -> float:
    return round((now - ref).total_seconds() / 60.0, 1)


def evidence_path(t: dict) -> str | None:
    ep = t.get("evidence_path")
    if not ep:
        # 约定路径：worker prompt 要求写到 reports/evidence/<id>.md，未回填台账也认
        return os.path.join(ROOT, "reports", "evidence", "%s.md" % t.get("id"))
    return ep if os.path.isabs(ep) else os.path.join(ROOT, ep)


def _evidence_sha(ep: str, normalize_lf: bool = False) -> str:
    """证据文件 sha256；normalize_lf=True 时先 CRLF→LF（与 board --sha-mode lf 同口径）。"""
    import hashlib
    with open(ep, "rb") as f:
        data = f.read()
    if normalize_lf:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def frozen_evidence_path(t: dict) -> str | None:
    fp = t.get("evidence_frozen_path")
    if not fp:
        return None
    return fp if os.path.isabs(fp) else os.path.join(ROOT, fp)


def evidence_problem(t: dict) -> str | None:
    """done 任务的证据复核；返回问题描述，None 表示没问题。

    冻结副本优先：done 时 board 会把证据复制到只读冻结区，冻结副本才是权威
    锚。工作树文件之后被追加、被格式化、行尾变化，都不该触发 reject-done。
    没有冻结副本的历史卡退回工作树复核：raw 不中再按 lf 口径复核一次
    （Windows 工作树常见 CRLF，只比 raw 会把“仅行尾不同”误判成被改过）。
    """
    sha = t.get("evidence_sha256")
    if not sha:
        return "证据未登记 sha256"
    fp = frozen_evidence_path(t)
    if fp:
        fsha = t.get("evidence_frozen_sha256") or sha
        if not os.path.isfile(fp):
            return "冻结证据缺失: %s" % fp
        if os.path.getsize(fp) <= 0:
            return "冻结证据为空: %s" % fp
        try:
            if _evidence_sha(fp) == fsha or _evidence_sha(fp, normalize_lf=True) == fsha:
                return None
            return "冻结证据哈希不匹配（冻结副本被改过）"
        except OSError as exc:
            return "冻结证据读取失败: %s" % exc
    ep = evidence_path(t)
    if not ep:
        return "done 但缺 evidence_path"
    if not os.path.isfile(ep):
        return "证据文件不存在: %s" % ep
    if os.path.getsize(ep) <= 0:
        return "证据文件为空: %s" % ep
    try:
        if _evidence_sha(ep) == sha:
            return None
        if _evidence_sha(ep, normalize_lf=True) == sha:
            return None
        return "证据哈希不匹配（文件被改过）"
    except OSError as exc:
        return "证据读取失败: %s" % exc


def evidence_drift(t: dict) -> str | None:
    """工作树证据相对冻结副本的漂移（只提示，不据此打回）。"""
    fp = frozen_evidence_path(t)
    if not fp:
        return None
    ep = evidence_path(t)
    if not ep or not os.path.isfile(ep):
        return "工作树证据不存在（冻结副本仍在）"
    sha = t.get("evidence_frozen_sha256") or t.get("evidence_sha256")
    if not sha:
        return None
    try:
        if _evidence_sha(ep) == sha or _evidence_sha(ep, normalize_lf=True) == sha:
            return None
        return "工作树证据已被改动（冻结副本未变，验收不受影响）"
    except OSError as exc:
        return "工作树证据读取失败: %s" % exc


def canon_path(path: str) -> str:
    """路径比较口径：绝对化 + realpath + Windows 忽略大小写 + 统一斜杠。"""
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        raw = os.path.realpath(raw)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(raw)).replace("\\", "/").rstrip("/")


def isolated_workspace(t: dict) -> str:
    """任务卡里登记的「真实隔离工作区」；path==workspace 或缺分支/仓不算。"""
    wt = t.get("worktree")
    if not isinstance(wt, dict):
        return ""
    path = str(wt.get("path") or "").strip()
    branch = str(wt.get("branch") or "").strip()
    repo = str(wt.get("repo") or "").strip()
    if not path or not branch or not repo:
        return ""
    ws = str(t.get("workspace") or "").strip()
    if ws and canon_path(path) == canon_path(ws):
        return ""
    return path


def task_workspace(t: dict, lim: dict) -> str:
    """任务实际会写的工作区：隔离工作区优先，否则任务卡 workspace / worktree.repos。"""
    iso = isolated_workspace(t)
    if iso:
        return iso
    ws = str(t.get("workspace") or "").strip()
    if ws:
        return ws
    repos = ((lim.get("worktree") or {}).get("repos") or {})
    return str(repos.get(str(t.get("project") or "")) or "").strip()


def git_probe(args: list, cwd: str) -> tuple[int, str]:
    """默认 git 探针；测试通过 dispatch_guard(git_runner=...) 注入替身。"""
    try:
        p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def exit_marker(path: str, limit: int = 65536) -> bool:
    """证据文件是否带离场标记：EXIT= 或 [PASS]（W-017 闸门3）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(limit)
    except OSError:
        return False
    return "EXIT=" in text or "[PASS]" in text


def prompt_discipline_problems(prompt: str, tid: str) -> list[str]:
    """dispatch 前自检：prompt 必须带三条硬纪律、离场标记与身份前缀。"""
    problems = []
    for marker in DISCIPLINE_MARKERS:
        if marker not in prompt:
            problems.append("worker prompt 缺 %s 段落" % marker)
    if "EXIT=" not in prompt or "[PASS]" not in prompt:
        problems.append("worker prompt 缺离场标记要求 EXIT= / [PASS]")
    if tid and ("%s: " % tid) not in prompt:
        problems.append("worker prompt 缺身份前缀提交命令 %s: ..." % tid)
    return problems


def dispatch_guard(t: dict, lim: dict, plan: dict | None, active: list,
                   prompt: str, git_runner=None) -> dict:
    """派单前机械自检（W-20261118-017）。

    三件事：
      1. prompt 自检：三条硬纪律 + EXIT=/[PASS] + 身份前缀提交命令，缺一拒派；
      2. 主仓他人活跃：同一 workspace 还有未隔离的在办任务 -> 拒派（按路径判，不按项目名）；
      3. 主仓脏：git 仓的已跟踪文件有未提交改动 -> 拒派（只认非 ?? 行，管家自己的
         未跟踪产物不误伤）。隔离工作区跳过 2/3，因为不写主仓。

    返回 {ok, problems, detail, remedy}；git 探针失败不拦派（环境问题不能当纪律问题），
    但 detail.git 会如实记录，供调度台排查。
    """
    tid = str(t.get("id") or "").strip()
    problems = prompt_discipline_problems(prompt, tid)
    detail = {"task_id": tid, "workspace": None, "isolated": False,
              "git": None, "others": []}
    plan = plan if isinstance(plan, dict) else None
    isolated = bool(plan and plan.get("path") and not plan.get("skipped") and not plan.get("error"))
    if isolated:
        detail["isolated"] = True
        detail["workspace"] = plan.get("path")
        return {"ok": not problems, "problems": problems, "detail": detail,
                "remedy": "" if not problems else "修复 worker_prompt 后重派"}

    ws = task_workspace(t, lim)
    detail["workspace"] = ws or None
    if not ws:
        return {"ok": not problems, "problems": problems, "detail": detail,
                "remedy": "" if not problems else "修复 worker_prompt 后重派"}

    ws_key = canon_path(ws)
    for other in active:
        oid = str(other.get("id") or "").strip()
        if not oid or oid == tid:
            continue
        if isolated_workspace(other):
            continue
        ows = task_workspace(other, lim)
        if ows and canon_path(ows) == ws_key:
            detail["others"].append(oid)
    if detail["others"]:
        problems.append("主仓他人活跃：%s 正在 %s 未隔离在办（checkin 拒主仓）"
                        % (",".join(detail["others"]), ws))

    if lim.get("check_main_repo", True):
        runner = git_runner or git_probe
        rc, out = runner(["rev-parse", "--is-inside-work-tree"], ws)
        probe: dict = {"inside": False}
        if rc == 0 and out.strip().lower() == "true":
            probe["inside"] = True
            rc2, out2 = runner(["status", "--porcelain"], ws)
            if rc2 == 0:
                lines = [ln for ln in out2.splitlines() if ln.strip()]
                tracked = [ln for ln in lines if not ln.startswith("??")]
                probe["dirty_tracked"] = len(tracked)
                probe["untracked"] = len(lines) - len(tracked)
                probe["sample"] = tracked[:3]
                if tracked:
                    problems.append(
                        "主仓有 %d 个未提交的已跟踪改动：%s（先让改动主收敛，或给本卡建隔离工作区）"
                        % (len(tracked), "；".join(tracked[:3])))
            else:
                probe["error"] = str(out2)[:120]
        elif rc == 127:
            probe["error"] = str(out)[:120]
        detail["git"] = probe

    remedy = ""
    if problems:
        remedy = ("保持 queued：让主仓改动先提交/收回、等他人任务收尾，或给任务卡建隔离工作区"
                  "（worker_worktree.py ensure）；下一轮自动复检，不要绕开闸门硬派")
    return {"ok": not problems, "problems": problems, "detail": detail, "remedy": remedy}


def kb_hints(t: dict, limit: int = 3) -> list[dict]:
    if steward_kb is None:
        return []
    text = " ".join([str(t.get("title") or ""), str(t.get("goal") or ""),
                     str(t.get("note") or ""), " ".join(t.get("tags") or [])])
    try:
        return [{"id": e["id"], "title": e["title"], "confidence": e["confidence"],
                 "tags": e["tags"], "score": e.get("score", 0)}
                for e in steward_kb.search(text, limit)]
    except Exception:
        return []


def worktree_plan(t: dict, lim: dict, materialize: bool = True,
                  factory=None) -> dict | None:
    """写码任务是否要隔离工作区；返回给调度台的 worktree 说明（None=不启用）。

    三条规则：
      1. 配置 dispatch.worktree.enabled 才启用（默认关，测试与老台账零副作用）；
      2. 仓库按 project -> worktree.repos / 任务卡 workspace 解析，不是 git 仓就跳过；
      3. 启用且 materialize 时立刻建好（ensure 幂等），派单直接带上路径与分支。
    """
    cfg = lim.get("worktree") or {}
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    tid = str(t.get("id") or "").strip()
    if not tid:
        return None
    project = str(t.get("project") or "")
    repos = cfg.get("repos") or {}
    repo = str(repos.get(project) or t.get("workspace") or "").strip()
    prefix = str(cfg.get("branch_prefix") or "wt/")
    if not repo:
        return {"enabled": True, "skipped": "no-repo", "project": project,
                "hint": "任务卡没有 workspace，worktree.repos 也没有该 project"}
    if not materialize:
        return {"enabled": True, "planned": True, "repo": repo,
                "branch": prefix + tid, "task_id": tid}
    if factory is None:
        def factory(task_id: str, repo_path: str, wt_cfg: dict) -> dict:
            import worker_worktree as ww  # 延迟导入：不启用 worktree 时零开销
            return ww.ensure(task_id, repo_path, cfg=wt_cfg,
                             ledger_path=str(wt_cfg.get("ledger") or ww.DEFAULT_LEDGER))
    try:
        res = factory(tid, repo, cfg)
    except Exception as exc:  # 建工作区失败不能拖垮整轮调度
        return {"enabled": True, "error": "exception", "repo": repo,
                "detail": str(exc)[:160]}
    if not isinstance(res, dict):
        return {"enabled": True, "error": "bad-result", "repo": repo}
    if res.get("ok"):
        return {"enabled": True, "created": bool(res.get("created")),
                "reused": bool(res.get("reused")), "repo": res.get("repo") or repo,
                "path": res.get("path"), "branch": res.get("branch"),
                "base_branch": res.get("base_branch"),
                "base_commit": res.get("base_commit"), "task_id": tid}
    reason = res.get("reason") or "unknown"
    if res.get("skipped"):
        return {"enabled": True, "skipped": reason, "repo": repo,
                "hint": res.get("hint") or ""}
    return {"enabled": True, "error": reason, "repo": repo,
            "detail": res.get("error") or res.get("detail") or "",
            "hint": res.get("hint") or ""}


def worker_prompt(t: dict, use_kb: bool = True, wt: dict | None = None) -> str:
    tid = t.get("id")
    wt = wt if wt is not None else (t.get("worktree") if isinstance(t.get("worktree"), dict) else None)
    isolated = bool(wt and wt.get("path") and not wt.get("skipped") and not wt.get("error"))
    workspace = (wt.get("path") if isolated else t.get("workspace"))
    lines = [
        "【%s】%s" % (tid, t.get("title")),
        "【任务】%s" % (t.get("goal") or t.get("title")),
        "【项目】%s%s" % (t.get("project") or "未指定",
                         ("（工作区 %s；cwd 不一定是工作区，命令一律用绝对路径）" % workspace)
                         if workspace else ""),
        "【边界】%s" % (t.get("boundary") or "不删除用户文件；不外传密钥；不擅自部署或外发消息"),
    ]
    if isolated:
        lines += [
            "【隔离工作区】%s（分支 %s，基准 %s）"
            % (wt.get("path"), wt.get("branch"),
               wt.get("base_branch") or "创建时的 HEAD"),
            "所有读写、构建、测试与提交都在该目录完成；禁止改动主仓 %s 里的文件。"
            % (wt.get("repo") or "（见上）"),
            "完成前必须提交，否则验收合并拿不到任何改动：",
            "  git -C \"%s\" add -A && git -C \"%s\" commit -m \"%s: 一句话摘要\""
            % (wt.get("path"), wt.get("path"), tid),
            "提交号写进【证据】；管家验收后会把分支合并回基准分支并清理该工作区。",
        ]
    elif wt and wt.get("error"):
        lines += [
            "【隔离工作区】创建失败（%s%s），本次按单写手模式在原工作区 %s 工作；"
            % (wt.get("error"), ("：" + wt.get("detail")) if wt.get("detail") else "",
               t.get("workspace") or "（未指定）"),
            "不要与他人并行改同一仓；完成后同样要 git commit 并报提交号。",
        ]
    # W-20261118-017：三条硬纪律落进每个 prompt，调度台派单前按段首标记自检（grep 可查）。
    ws_cmd = workspace or "（任务卡未给绝对工作区，命令一律用实际绝对路径）"
    lines += [
        "【硬纪律1·开工 checkin 拒主仓】",
        "  工作区是 git 仓时，开工前先跑：git -C \"%s\" rev-parse --is-inside-work-tree"
        " && git -C \"%s\" status --porcelain" % (ws_cmd, ws_cmd),
        "  非隔离工作区发现他人未提交改动/你不是唯一写手 -> 停手不写文件，回报 EXIT=BLOCKED-MAIN。",
        "  （本任务在隔离工作区时本条自动满足：只查自己的工作区，禁止改主仓。）",
        "【硬纪律2·claim 边界与身份前缀】",
        "  只改任务卡范围内的文件，不碰其他任务卡的文件和他人未提交改动；提交信息必须以任务 ID 开头：",
        "    git -C \"%s\" add -A && git -C \"%s\" commit -m \"%s: 一句话摘要\""
        % (ws_cmd, ws_cmd, tid),
        "  越界 -> 停手回报 EXIT=SCOPE，不要顺手替别人回滚或删除。",
        "【硬纪律3·离场三闸门】",
        "  闸门1 提交：git -C \"%s\" status --porcelain 必须为空（非 git 工作区跳过）；" % ws_cmd,
        "  闸门2 证据：reports/evidence/%s.md 非空，且必须含 EXIT= 或 [PASS] 行；" % tid,
        "  闸门3 回报：最后一条回复必须带 EXIT= 或 [PASS]，失败写 EXIT=FAIL 并说明原因。",
        "  三道闸门没全过，不许说“完成”。",
    ]
    lines += [
        "【验收】%s" % t.get("acceptance"),
        "【交付】完成后按以下格式回复：",
        "  结论: 成功/失败",
        "  证据: 命令输出摘要/文件路径/提交号",
        "  未决: 需要用户拍板的事项（没有写\"无\"）",
        "  标记: EXIT=PASS（失败写 EXIT=FAIL）",
        "并把可复核证据写到 %s/reports/evidence/%s.md（命令、关键输出、结论，"
        "且必须含 EXIT= 或 [PASS] 行）。"
        % (ROOT.replace("\\", "/"), tid),
        "【不停】不要以“需要用户确认/信息不足”为由停下：按最保守假设继续，把不确定项写进“未决”，",
        "但必须先把能做的部分做完并给出证据。只有删除用户文件、外发消息、上线部署才允许停。",
        "【纪律】不要向用户提问；不得删除用户文件；不得外传密钥；不得擅自部署或对外发消息。",
    ]
    hints = kb_hints(t) if use_kb else []
    if hints:
        lines.append("【管家知识】")
        for h in hints:
            lines.append("- %s（%s）：%s" % (h["id"], h["confidence"], h["title"]))
    if t.get("last_error"):
        lines.append("【上次失败】%s（换一种做法，不要重复同一条路）" % t["last_error"])
    return "\n".join(lines)


def next_level(t: dict, lim: dict) -> str:
    pulls = int(t.get("pulls") or 0)
    if pulls <= 0:
        return "nudge"
    if pulls == 1:
        return "steer"
    if pulls < lim["max_pulls"]:
        return "replace"
    return "split"


def dag_analysis(tasks: list[dict]) -> dict:
    """依赖图视角：谁挡住最多下游（bottleneck）+ 最长未完成链路（critical_path）。

    只统计未完成（非 done/abandoned）任务；bottleneck 按“传递下游数”降序。
    病灶：厂长只看逐卡状态，不知道一张卡卡住一条链，于是先救会叫的卡。
    """
    by_id = {str(t.get("id")): t for t in tasks}
    children: dict[str, list[str]] = {}
    for t in tasks:
        tid = str(t.get("id"))
        for d in (t.get("depends_on") or []):
            d = str(d)
            if d in by_id:
                children.setdefault(d, []).append(tid)

    def is_open(tid: str) -> bool:
        return tid in by_id and state(by_id[tid]) not in ("done", "abandoned")

    def downstream(tid: str) -> list[str]:
        seen: set[str] = set()
        stack = list(children.get(tid, []))
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(children.get(x, []))
        return sorted(x for x in seen if is_open(x))

    bottlenecks = []
    for tid, t in by_id.items():
        if not is_open(tid):
            continue
        down = downstream(tid)
        if down:
            bottlenecks.append({"id": tid, "state": state(t), "title": t.get("title"),
                                "blocks": len(down), "downstream": down[:8]})
    bottlenecks.sort(key=lambda b: (-b["blocks"], b["id"]))

    # 最长未完成依赖链（memo DFS；环形依赖直接跳过，避免死循环）
    chain_memo: dict[str, list[str]] = {}
    visiting: set[str] = set()

    def chain(tid: str) -> list[str]:
        if tid in chain_memo:
            return chain_memo[tid]
        if tid in visiting:
            return [tid]
        visiting.add(tid)
        best = [tid]
        for d in (by_id[tid].get("depends_on") or []):
            d = str(d)
            if d in by_id and is_open(d):
                sub = chain(d)
                if len(sub) + 1 > len(best):
                    best = sub + [tid]
        visiting.discard(tid)
        chain_memo[tid] = best
        return best

    longest: list[str] = []
    for tid in by_id:
        if not is_open(tid):
            continue
        c = chain(tid)
        if len(c) > len(longest):
            longest = c
    return {
        "bottleneck": bottlenecks[:5],
        "critical_path": {"length": len(longest), "chain": longest} if len(longest) > 1 else None,
    }


def build(reg: dict, cfg: dict, now: datetime.datetime, today_cost: float | None,
          use_kb: bool = True, materialize: bool = True, worktree_factory=None,
          git_runner=None, session_probe=None) -> dict:
    lim = limits(cfg)
    tasks = [t for t in reg.get("tasks", []) if isinstance(t, dict)]
    by_id = {str(t.get("id")): t for t in tasks}
    active = [t for t in tasks if state(t) in ACTIVE]
    queued = [t for t in tasks if state(t) == "queued"]
    counts: dict = {}
    for t in tasks:
        counts[state(t)] = counts.get(state(t), 0) + 1

    actions: list[dict] = []
    handled: set[str] = set()

    def add(kind: str, t: dict, reason: str, **extra) -> None:
        item = {"type": kind, "task_id": t.get("id"), "title": t.get("title"),
                "state": state(t), "reason": reason}
        item.update(extra)
        actions.append(item)
        handled.add(str(t.get("id")))

    # 1) done 证据复核：缺证据/哈希不符 → 打回
    for t in tasks:
        if state(t) == "done":
            problem = evidence_problem(t)
            if problem:
                add("reject-done", t, problem, sessionId=t.get("sessionId"))

    # 2) blocked：不是终点，先诊断解卡
    for t in tasks:
        if state(t) == "blocked" and str(t.get("id")) not in handled:
            level = next_level(t, lim)
            checklist = [
                "读 worker 最后一条消息，归类卡点：权限/缺信息/工具失败/路径错/验收不清",
                "权限问题 → 检查 set_permission.py 与 autoallow（state/autoallow.on）",
                "缺信息 → 管家按最保守假设代拍，写 decisions，再把决定发给 worker",
                "工具失败 → 换命令/换路径/换会话；同一手段失败 2 次必须换",
                "仍不动 → replace（新会话）或 split（拆小）",
            ]
            add("unblock", t, "blocked 已停，必须解卡而不是等用户",
                sessionId=t.get("sessionId"), level=level, checklist=checklist,
                prompt=worker_prompt(t, use_kb) if level in ("replace",) else "")

    # 3) failed：换路线或拆任务，不把失败当结论
    for t in tasks:
        if state(t) == "failed" and str(t.get("id")) not in handled:
            attempts = int(t.get("attempts") or 0)
            max_attempts = int(t.get("max_attempts") or lim["max_attempts"])
            if attempts < max_attempts:
                add("replace", t, "failed 第 %d/%d 次，换新会话/新路线" %
                    (attempts, max_attempts), prompt=worker_prompt(t))
            else:
                add("split", t, "已到 %d 次上限，必须拆子任务/换路线" % max_attempts,
                    prompt=worker_prompt(t))

    # 4) 依赖坏掉的 queued：重排
    for t in queued:
        deps = t.get("depends_on") or []
        bad = []
        for d in deps:
            dep = by_id.get(str(d))
            if dep is None:
                bad.append("%s(不存在)" % d)
            elif state(dep) in ("failed", "abandoned"):
                bad.append("%s(%s)" % (d, state(dep)))
        if bad and str(t.get("id")) not in handled:
            add("replan", t, "依赖不可用: " + ",".join(bad))

    # 5) 在办任务：先看有没有证据（可收尾），再判超时
    for t in active:
        if str(t.get("id")) in handled:
            continue
        ep = evidence_path(t)
        if ep and os.path.isfile(ep) and os.path.getsize(ep) > 0:
            if lim.get("require_exit_marker", True) and not exit_marker(ep):
                # W-017 闸门3：证据没带离场标记，不算可验收 -> 打回补标记
                add("reject-done", t,
                    "证据缺离场标记 EXIT= 或 [PASS]（W-017 闸门3，verify 打回）",
                    sessionId=t.get("sessionId"), evidence_path=ep,
                    marker_missing=True, stage="verify")
            else:
                add("verify", t, "发现证据文件，核验收尾",
                    sessionId=t.get("sessionId"), evidence_path=ep)
            continue
        # INC-20261118-007 幽灵派单：过了宽限期仍无 tasks 行（UI 不可见）或
        # 无任何 assistant token 输出 -> 会话根本没跑起来，nudge/steer 无效，直接 replace。
        sid = str(t.get("sessionId") or "")
        grace_min = float(lim.get("dispatch_grace_sec") or 0) / 60.0
        if session_probe and sid and grace_min > 0 and age_min(progress_ref(t, now), now) > grace_min:
            info = session_probe(sid) or {}
            if not info.get("error") and (int(info.get("tasks_row") or 0) == 0
                                          or int(info.get("assistant_with_output") or 0) == 0):
                add("check", t, "派单幽灵：会话无 tasks 行/无 assistant 输出（UI 不可见），"
                    "nudge/steer 无效，直接 replace",
                    sessionId=sid, level="replace", ghost=True, probe=info,
                    prompt=worker_prompt(t, use_kb))
                continue
            idle = info.get("idle_sec")
            if idle is not None and float(idle) > 600:
                # 有历史输出但最近 10 分钟没动静：UI 转圈已消失，按停滞升级（先 nudge）
                add("check", t, "疑似停滞：最近输出在 %.0f 秒前（蓝点不等于在跑）" % float(idle),
                    sessionId=sid, level=next_level(t, lim), idle_sec=idle, probe=info)
                continue
        ref = progress_ref(t, now)
        idle = age_min(ref, now)
        if idle > lim["stale_sec"] / 60.0:
            add("check", t, "在办 %.1f 分钟无进展（阈值 %.0f 分钟）" %
                (idle, lim["stale_sec"] / 60.0),
                sessionId=t.get("sessionId"), level=next_level(t, lim), idle_min=idle)

    # 6) 派新单：依赖完成 + 优先级 + 并发上限 + 预算
    slots = max(0, lim["max_running"] - len(active))
    budget_hit = bool(lim["daily_budget_cny"] and today_cost is not None
                      and today_cost >= lim["daily_budget_cny"])
    if budget_hit:
        actions.append({"type": "budget", "reason": "今日 ¥%.2f ≥ 预算 ¥%.2f，暂停派新单"
                        % (today_cost or 0, lim["daily_budget_cny"]),
                        "today_cost": today_cost, "budget": lim["daily_budget_cny"]})
    else:
        ready = []
        for t in queued:
            if str(t.get("id")) in handled:
                continue
            deps = t.get("depends_on") or []
            if all(by_id.get(str(d)) is not None and state(by_id[str(d)]) == "done" for d in deps):
                retry_at = parse_iso(t.get("next_retry_at"))
                if retry_at and retry_at > now:
                    continue
                ready.append(t)
        ready.sort(key=lambda t: (priority(t), created(t, now)))
        # 同项目并发默认 1：worker 共享工作区，两个写手会互相踩文件。
        # 要并行改代码，先拆 worktree/分支，或把任务 project 标成不同值。
        per_project: dict = {}
        for t in active:
            key = str(t.get("project") or "-")
            per_project[key] = per_project.get(key, 0) + 1
        max_per = int(lim.get("max_running_per_project") or 1)
        picked = 0
        spawned: list[dict] = []
        for t in ready:
            if picked >= slots:
                break
            key = str(t.get("project") or "-")
            if per_project.get(key, 0) >= max_per:
                continue
            # 写码任务先建隔离工作区：同仓并行才真正零共树冲突
            plan = worktree_plan(t, lim, materialize=materialize, factory=worktree_factory)
            prompt = worker_prompt(t, use_kb, wt=plan)
            # W-017 派单闸：prompt 三条硬纪律 + 主仓他人活跃 + 主仓未提交改动
            guard = dispatch_guard(t, lim, plan, active + spawned, prompt,
                                   git_runner=git_runner)
            if not guard["ok"]:
                add("dispatch-blocked", t, "；".join(guard["problems"]),
                    guard=guard["detail"], remedy=guard["remedy"],
                    project=t.get("project"),
                    zcode_project=(lim.get("project_map") or {}).get(
                        str(t.get("project") or ""), t.get("project") or "newcad"))
                continue  # 不占并发槽，下一轮自动复检
            per_project[key] = per_project.get(key, 0) + 1
            picked += 1
            extra = {}
            if plan:
                extra["worktree"] = plan
                if plan.get("path"):
                    extra["workspace"] = plan["path"]
            spawned.append({
                "id": t.get("id"), "project": t.get("project"),
                "workspace": t.get("workspace") or "",
                "worktree": ({"path": plan.get("path"), "branch": plan.get("branch"),
                              "repo": plan.get("repo")}
                             if plan and plan.get("path") and not plan.get("skipped")
                             and not plan.get("error") else None),
            })
            add("dispatch", t, "queued 且依赖已满足",
                model=t.get("model") or "new-provider-7/auto",
                project=t.get("project"),
                zcode_project=(lim.get("project_map") or {}).get(
                    str(t.get("project") or ""), t.get("project") or "newcad"),
                prompt=prompt, **extra)

    # 7) 失败/阻塞要落日报（不打扰用户）
    for t in tasks:
        if state(t) in ("failed", "blocked") and not t.get("reported_at"):
            actions.append({"type": "report", "task_id": t.get("id"), "title": t.get("title"),
                            "state": state(t), "reason": "需要进日报：%s" %
                            (t.get("last_error") or t.get("note") or "无详情")})

    # 7b) 已完成任务遗留的隔离工作区：合并 + 清理（不丢改动，执行交给调度台）
    wt_cfg = lim.get("worktree") or {}
    if isinstance(wt_cfg, dict) and wt_cfg.get("enabled"):
        ledger = str(wt_cfg.get("ledger") or os.path.join(ROOT, "state", "worktrees.json"))
        records: dict = {}
        try:
            raw = load_json(ledger)
            if isinstance(raw.get("worktrees"), dict):
                records = raw["worktrees"]
        except (OSError, ValueError):
            records = {}
        for t in tasks:
            if state(t) not in ("done", "abandoned"):
                continue
            rec = records.get(str(t.get("id")))
            if not isinstance(rec, dict) or rec.get("removed_at"):
                continue
            actions.append({
                "type": "worktree-cleanup", "task_id": t.get("id"),
                "title": t.get("title"), "state": state(t),
                "reason": "任务已%s，隔离工作区仍在：验收合并后清理" % state(t),
                "repo": rec.get("repo"), "path": rec.get("path"),
                "branch": rec.get("branch"),
            })

    # 8) 有活但长时间零进展：强制换路线
    if active or queued:
        refs = [progress_ref(t, now) for t in active + queued]
        if refs and age_min(min(refs), now) > lim["idle_alert_sec"] / 60.0:
            actions.append({"type": "progress-alert",
                            "reason": "有 %d 个在办/排队任务，但 %.0f 分钟无任何进展；换路线，不要等用户"
                            % (len(active) + len(queued),
                               age_min(min(refs), now))})

    if not active and not queued:
        policy = str(lim.get("queue_empty_policy") or "report")
        if policy == "stop":
            actions.append({"type": "idle",
                            "reason": "无在办、无排队任务（queue_empty_policy=stop，不自动开新战线）"})
        elif in_work_hours(now, lim):
            actions.append({
                "type": "queue-empty",
                "policy": policy,
                "reason": "工作时段队列为空：从日报“明日主线”或 ARCHITECTURE backlog 取一项落卡，避免零产出",
                "work_hours": "%s-%s" % (lim.get("work_start"), lim.get("work_end")),
            })
        else:
            actions.append({"type": "idle", "reason": "无在办、无排队任务（非工作时段）"})

    done_today = 0
    for t in tasks:
        if state(t) == "done":
            ref = parse_iso(t.get("updated_at")) or parse_iso(t.get("last_check"))
            if ref and ref.date() == now.date():
                done_today += 1
    refs = [progress_ref(t, now) for t in active + queued]
    dag = dag_analysis(tasks)
    drift = []
    for t in tasks:
        if state(t) != "done":
            continue
        why = evidence_drift(t)
        if why:
            drift.append({"id": t.get("id"), "detail": why})
    digest = {
        "counts": counts,
        "active": len(active),
        "queued": len(queued),
        "slots": slots,
        "done_today": done_today,
        "stale_active": sum(1 for t in active
                            if age_min(progress_ref(t, now), now) > lim["stale_sec"] / 60.0),
        "last_progress_min": age_min(min(refs), now) if refs else None,
        "today_cost": today_cost,
        "budget": lim["daily_budget_cny"] or None,
        "limits": lim,
        "bottleneck": dag["bottleneck"],
        "critical_path": dag["critical_path"],
        "evidence_drift": drift,
    }
    return {"ok": True, "generated_at": now.isoformat(), "digest": digest, "actions": actions}


def probe_session(session_id: str) -> dict:
    """生产用会话探针（只读）；导入/读库失败返回 error，由调用方跳过（不误判）。"""
    try:
        import session_probe as sp
    except Exception as exc:  # noqa: BLE001
        return {"error": "probe-unavailable: %s" % exc}
    return sp.probe(session_id)


def render(result: dict) -> None:
    d = result["digest"]
    c = d["counts"]
    print("STEWARD %s | active=%s queued=%s slots=%s done_today=%s stale=%s" % (
        result["generated_at"], d["active"], d["queued"], d["slots"],
        d["done_today"], d["stale_active"]))
    print("COUNTS " + " ".join("%s=%s" % (k, c[k]) for k in sorted(c)))
    for b in (d.get("bottleneck") or [])[:3]:
        print("BOTTLENECK %s 挡住 %d 个下游（%s）%s" % (
            b["id"], b["blocks"], b["state"], b.get("title") or ""))
    cp = d.get("critical_path")
    if cp:
        print("CRITICAL len=%d %s" % (cp["length"], " -> ".join(cp["chain"])))
    if d.get("evidence_drift"):
        print("DRIFT %d 张 done 卡的工作树证据已改（冻结副本未变，仅提示）: %s" % (
            len(d["evidence_drift"]),
            ",".join(str(x.get("id")) for x in d["evidence_drift"][:8])))
    for a in result["actions"]:
        print("ACTION %-13s %s %s" % (a["type"], a.get("task_id") or "-", a.get("reason") or ""))
        if a.get("level"):
            print("  level=%s session=%s" % (a["level"], a.get("sessionId") or "-"))
        wt = a.get("worktree") if isinstance(a.get("worktree"), dict) else None
        if wt:
            print("  worktree=%s branch=%s%s" % (
                wt.get("path") or "-", wt.get("branch") or "-",
                (" skip=%s" % (wt.get("skipped") or wt.get("error"))) if
                (wt.get("skipped") or wt.get("error")) else ""))
        guard = a.get("guard") if isinstance(a.get("guard"), dict) else None
        if guard:
            print("  guard workspace=%s others=%s git=%s" % (
                guard.get("workspace") or "-",
                ",".join(guard.get("others") or []) or "-",
                (guard.get("git") or {})))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--now", default=None, help="测试用 ISO 时间")
    ap.add_argument("--today-cost", type=float, default=None)
    ap.add_argument("--no-kb", action="store_true")
    ap.add_argument("--no-worktree", action="store_true",
                    help="只出计划不建隔离工作区（dry-run/排查用）")
    ap.add_argument("--no-probe", action="store_true",
                    help="关闭幽灵派单探针（离线/测试用；默认开启）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        reg = load_json(args.registry)
        cfg = load_json(args.config)
    except (OSError, ValueError) as exc:
        print("ERROR %s" % exc)
        return 2
    result = build(reg, cfg, now_dt(args.now), args.today_cost, use_kb=not args.no_kb,
                   materialize=not args.no_worktree,
                   session_probe=None if args.no_probe else probe_session)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
