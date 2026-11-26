# -*- coding: utf-8 -*-
"""夜间排班助手：把 state/registry.json + config/steward.json 排成 24h 批次计划 JSON。

为什么需要它：
  夜班（尤其 step-5-preview 的超长批量/长文/扫描窗口）不该由调度台临时起意派单，
  而是在开夜前一次性排好班：哪些卡今晚跑、按什么顺序跑、建议开几路并发、每张卡
  大概多久、哪些卡今晚不跑以及为什么。调度台（cron_selfwake / steward_cycle）
  拿这份 JSON 就能按序派单，不必再自己解析台账。

输入（只读，绝不写）：
  state/registry.json    任务台账（tasks 列表；字段口径同 steward_board.py）
  config/steward.json    管家配置（dispatch.max_running / max_running_per_project 等）

输出（stdout 或 --out 落盘，原子写）：
  {
    "schema": 1, "tool": "night_shift", "generated_at": iso,
    "window":   {"start": iso, "end": iso, "hours": 24},
    "sources":  {"registry": 路径, "config": 路径,
                 "registry_sha256": hex, "config_sha256": hex},
    "concurrency": {"tier": "idle|serial|normal|batch", "suggested": int,
                    "max_running": int, "max_running_per_project": int,
                    "by_project": {...}, "by_lane": {...}, "reason": str},
    "plan":     [{"rank", "id", "title", "project", "tier", "state", "lane",
                  "model_hint", "est_minutes", "depends_on", "priority",
                  "reason"}, ...],
    "skipped":  [{"id", "title", "state", "reason"}, ...],
    "totals":   {"planned", "skipped", "est_minutes", "est_wall_minutes"},
    "reason":   str,          # 空队列/降级等全局说明，正常时为空串
    "warnings": [str, ...]    # 坏配置等非致命告警
  }

排序口径（适合 Step 的批量/长文/扫描卡，夜长话多先啃硬的）：
  泳道 lane 由标题/验收/下一步/备注关键词判定：
    batch     批量/全量/全库/回归/fixture/填槽/扩面/多版本/并行/批次
    long-text 长文/蒸馏/文档化/注释/日报/报告/写作/汇总/总结/交接/摘要
    scan      扫描/巡检/审计/审查/复核/检查/体检/盘点/统计/scan/probe/health
    standard  其余非 SIMPLE 卡（常规高级活）
    light     SIMPLE 档轻活（ nex 免费池，串行兜底 ）
  顺序：batch → long-text → scan → standard → light；
  泳道内按 tier（REASONING>COMPLEX>MEDIUM>SIMPLE，夜里额度管够先跑重的）、
  priority、created_at、id 稳定排序；依赖优先于泳道（见下）。

依赖口径（与 steward_cycle.py 派单闸对齐，保守默认）：
  depends_on 是 steward_board add --depends-on 写入的 id 列表。
    依赖 done            -> 满足；
    依赖 queued          -> 两张卡都进 plan，且依赖卡排在前（拓扑保序）；
    依赖 不存在/失败/弃案 -> 本卡 skipped，原因写清哪个依赖为什么不可用；
    依赖 进行中(dispatched/running)/受阻/失败 -> 本卡 skipped，待回轮重排；
    queued 卡之间成环     -> 环上各卡全部 skipped，原因"依赖成环"。

只读约束：
  本脚本只以读模式打开 registry/config，不写台账、不改配置、不碰引擎；
  跑前后 registry/config 的 sha256 不变（sources 字段即当次读数，可复核）。

容错口径（不抛栈是硬验收）：
  坏 registry（非 JSON / tasks 非列表）-> NightShiftError，CLI 退出码 2，
    stderr 打 [ERROR] 可读原因，无 Traceback；
  坏/缺 config -> 不阻塞：退默认并发档，原因进 warnings + reason，退出码 0；
  空队列 -> plan=[] 且 reason 明确（"今夜无可派单任务"不是错误），退出码 0。

用法：
  python -X utf8 scripts/night_shift.py
  python -X utf8 scripts/night_shift.py --registry state/registry.json \
      --config config/steward.json --out state/night_plan.json \
      --now 2026-11-18T22:00:00+08:00

退出码：0 计划生成成功（含空队列/坏配置降级）；2 台账或参数错误（stderr 可读，无堆栈）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")
DEFAULT_CONFIG = os.path.join(ROOT, "config", "steward.json")

SCHEMA = 1
TOOL = "night_shift"
WINDOW_HOURS = 24

#: 泳道顺序：Step 的批量/长文/扫描卡排最前（夜长、额度管够），轻活垫后。
LANE_ORDER = ("batch", "long-text", "scan", "standard", "light")
LANE_RANK = {lane: i for i, lane in enumerate(LANE_ORDER)}

BATCH_KEYWORDS = ("批量", "全量", "全库", "回归", "fixture", "填槽", "扩面",
                  "多版本", "并行", "批次")
LONGTEXT_KEYWORDS = ("长文", "蒸馏", "文档化", "注释", "日报", "报告", "写作",
                     "汇总", "总结", "交接", "摘要", "digest")
SCAN_KEYWORDS = ("扫描", "巡检", "审计", "审查", "复核", "检查", "体检", "盘点",
                 "统计", "scan", "probe", "health")

#: tier 权重：REASONING/COMPLEX 先跑（重的先啃），SIMPLE 垫后。
TIER_RANK = {"REASONING": 0, "COMPLEX": 1, "MEDIUM": 2, "SIMPLE": 3}

#: 预计时长基数（分钟）+ 泳道修正；确定性估算，不读时钟不猜随机。
EST_BASE_MIN = {"SIMPLE": 15, "MEDIUM": 45, "COMPLEX": 90, "REASONING": 120}
EST_DEFAULT_MIN = 30
EST_LANE_BONUS = {"batch": 30, "long-text": 30, "scan": 15,
                  "standard": 0, "light": 0}
EST_FLOOR_MIN = 5

#: 模型提示（口径见 docs/MODEL_SCORECARD.md：超长批量/长文/扫描 → step-5-preview；
#: 轻活 nex 免费优先，高级活 R4）。只作提示，调度台可自行覆盖。
STEP_MODEL = "new-provider-10/step-5-preview"
TIER_MODEL_HINT = {
    "SIMPLE": "new-provider-5/nex-n2.6",
    "MEDIUM": "new-provider-6/deepseek-v4.1-flash",
    "COMPLEX": "new-provider-6/deepseek-v4.1-flash",
    "REASONING": "new-provider-6/deepseek-v4.1-flash",
}
DEFAULT_MODEL_HINT = "new-provider-6/deepseek-v4.1-flash"

#: 并发档阈值（计划卡数 -> 档位）。上限仍受配置 max_running 约束。
CONC_SERIAL_MAX = 2      # <=2 张：串行更稳，免得起多进程互相踩
CONC_NORMAL_MAX = 6      # 3-6 张：常规小并发
CONC_BATCH_CAP = 8       # 大批次：建议并发不超过 8 路（step 池虽大也不铺张）

DEFAULT_MAX_RUNNING = 15
DEFAULT_MAX_PER_PROJECT = 4

#: 输出字段契约（test_night_shift 锁定，勿随意改名）。
PLAN_FIELDS = ("rank", "id", "title", "project", "tier", "state", "lane",
               "model_hint", "est_minutes", "depends_on", "priority", "reason")
CONCURRENCY_FIELDS = ("tier", "suggested", "max_running",
                      "max_running_per_project", "by_project", "by_lane",
                      "reason")
SKIPPED_FIELDS = ("id", "title", "state", "reason")
TOTALS_FIELDS = ("planned", "skipped", "est_minutes", "est_wall_minutes")


class NightShiftError(Exception):
    """台账结构性错误：调用方应打印可读原因并退出 2，不抛栈。"""


# ---------------------------------------------------------------- 读取（只读）


def sha256_of(path: str) -> str:
    """文件 sha256；读取失败返回空串（sources 字段降级，不抛栈）。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def load_registry(path: str) -> dict:
    """严格读取台账：坏 JSON / 非对象 / tasks 非列表都抛 NightShiftError。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise NightShiftError("台账读取失败 %s: %s" % (path, e))
    except ValueError as e:
        raise NightShiftError("台账不是合法 JSON %s: %s" % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise NightShiftError("台账根必须是对象且 tasks 必须是列表: %s" % path)
    return data


def load_config(path: str) -> tuple[dict, str]:
    """宽容读取配置：缺失/坏 JSON/非对象退默认值，返回 (配置, 提示)。

    坏配置不阻塞排班（默认并发档仍可派单），但提示必须让调度台看见。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return {}, "配置读取失败，使用默认并发档: %s" % e
    except ValueError as e:
        return {}, "配置不是合法 JSON，使用默认并发档: %s" % e
    if not isinstance(data, dict):
        return {}, "配置根不是对象，使用默认并发档"
    return data, ""


def dispatch_cfg(config: dict) -> dict:
    d = config.get("dispatch") if isinstance(config, dict) else None
    return d if isinstance(d, dict) else {}


def parse_now(raw: str | None) -> datetime.datetime:
    if not raw:
        return datetime.datetime.now().astimezone().replace(microsecond=0)
    dt = datetime.datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.replace(microsecond=0)


# ---------------------------------------------------------------- 字段解析


def task_id(task: dict) -> str:
    return str(task.get("id") or "").strip()


def task_state(task: dict) -> str:
    return str(task.get("state") or "unknown").strip()


def task_tier(task: dict) -> str:
    return str(task.get("tier") or "").strip().upper()


def task_priority(task: dict) -> int:
    try:
        return int(task.get("priority"))
    except (TypeError, ValueError):
        return 5


def attempts_of(task: dict, default_limit: int) -> tuple[int, int]:
    """(已尝试, 上限)；脏值按 0 / 配置默认处理。"""
    try:
        attempts = int(task.get("attempts"))
    except (TypeError, ValueError):
        attempts = 0
    try:
        limit = int(task.get("max_attempts"))
    except (TypeError, ValueError):
        limit = default_limit
    return attempts, limit


def created_key(task: dict) -> str:
    """created_at 排序键：缺失/脏值排最后（空串最大），不抛栈。"""
    raw = str(task.get("created_at") or "").strip()
    return raw


def depends_of(task: dict) -> list[str]:
    """depends_on 归一化为 id 列表；支持列表或逗号分隔字符串。"""
    raw = task.get("depends_on")
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = []
    return [x.strip() for x in items if x and x.strip()]


def lane_of(task: dict) -> str:
    """泳道判定：标题/验收/下一步/备注命中关键词即归队；SIMPLE 兜底 light。"""
    text = " ".join(str(task.get(k) or "") for k in
                    ("title", "acceptance", "next_action", "note")).lower()
    for kw in BATCH_KEYWORDS:
        if kw in text:
            return "batch"
    for kw in LONGTEXT_KEYWORDS:
        if kw in text:
            return "long-text"
    for kw in SCAN_KEYWORDS:
        if kw in text:
            return "scan"
    if task_tier(task) == "SIMPLE":
        return "light"
    return "standard"


def est_minutes_of(task: dict, lane: str) -> int:
    """确定性预计时长（分钟）：tier 基数 + 泳道修正，下限 5。"""
    base = EST_BASE_MIN.get(task_tier(task), EST_DEFAULT_MIN)
    return max(EST_FLOOR_MIN, base + EST_LANE_BONUS.get(lane, 0))


def model_hint_of(task: dict, lane: str) -> str:
    """Step 三泳道（批量/长文/扫描）夜间一律提示 step-5-preview；其余看 tier。"""
    if lane in ("batch", "long-text", "scan"):
        return STEP_MODEL
    tier = task_tier(task)
    if tier in TIER_MODEL_HINT:
        return TIER_MODEL_HINT[tier]
    own = str(task.get("model") or "").strip()
    return own or DEFAULT_MODEL_HINT


# ---------------------------------------------------------------- 排班核心


def _find_cycles(candidates: dict[str, list[str]]) -> set[str]:
    """候选 queued 卡之间的依赖环（只沿候选边找）。返回环上成员 id 集合。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in candidates}
    in_cycle: set[str] = set()
    for root in candidates:
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        while stack:
            node, idx = stack[-1]
            if idx == 0:
                color[node] = GRAY
                path.append(node)
            deps = [d for d in candidates[node] if d in candidates]
            if idx < len(deps):
                stack[-1] = (node, idx + 1)
                dep = deps[idx]
                if color[dep] == GRAY:
                    # 回到灰点：path 里从 dep 开始都是环上成员
                    if dep in path:
                        in_cycle.update(path[path.index(dep):])
                elif color[dep] == WHITE:
                    stack.append((dep, 0))
            else:
                color[node] = BLACK
                path.pop()
                stack.pop()
    return in_cycle


def build_plan(registry: dict, config: dict, now: datetime.datetime,
               registry_path: str = "", config_path: str = "") -> dict:
    """排班主入口：返回完整计划 dict（纯函数，不写盘不抛栈——坏台账在 load 层已拦）。"""
    cfg = dispatch_cfg(config)
    try:
        max_running = int(cfg.get("max_running"))
    except (TypeError, ValueError):
        max_running = DEFAULT_MAX_RUNNING
    try:
        max_per_project = int(cfg.get("max_running_per_project"))
    except (TypeError, ValueError):
        max_per_project = DEFAULT_MAX_PER_PROJECT
    default_max_attempts = 3
    try:
        default_max_attempts = int(cfg.get("max_attempts"))
    except (TypeError, ValueError):
        pass

    tasks = [t for t in registry.get("tasks") or [] if isinstance(t, dict)]
    by_id = {task_id(t): t for t in tasks if task_id(t)}

    plan: list[dict] = []
    skipped: list[dict] = []

    def skip(task: dict, reason: str) -> None:
        skipped.append({
            "id": task_id(task) or "(无 id)",
            "title": str(task.get("title") or "(无标题)"),
            "state": task_state(task),
            "reason": reason,
        })

    # 1) 只有 queued 是今晚可派单的候选；其余全部给出明确原因
    queued: list[dict] = []
    for t in tasks:
        state = task_state(t)
        if state == "queued":
            queued.append(t)
        elif state in ("done", "abandoned"):
            skip(t, "已完结（%s），不排班" % state)
        elif state in ("dispatched", "running"):
            skip(t, "进行中（%s），不重复派单" % state)
        elif state == "blocked":
            skip(t, "受阻（blocked），需人工解阻后再排")
        elif state == "failed":
            skip(t, "失败（failed），需换路线重派后再排")
        else:
            skip(t, "未知状态 %r，保守不排班" % state)

    # 2) 候选卡基础检查：缺 id / 重试耗尽
    candidates: dict[str, list[str]] = {}
    for t in queued:
        tid = task_id(t)
        if not tid:
            skip(t, "缺 id，无法派单")
            continue
        attempts, limit = attempts_of(t, default_max_attempts)
        if attempts >= limit:
            skip(t, "重试次数已耗尽（attempts=%d ≥ max_attempts=%d），需拆子任务"
                 % (attempts, limit))
            continue
        candidates[tid] = depends_of(t)

    # 3) 依赖成环：环上卡全部跳过（谁都不可能先完成）
    for tid in sorted(_find_cycles(candidates)):
        skip(by_id[tid], "依赖成环（queued 卡互相依赖），需人工拆解")
        del candidates[tid]

    # 4) 外部依赖闸：依赖不存在/失败/弃案/进行中/受阻 -> 本卡待回轮
    #    dropped 记录"queued 但本轮没进候选"的卡及其原因（重试耗尽/缺 id），
    #    依赖这种卡时如实转述，不笼统说成"进行中"。
    dropped: dict[str, str] = {}
    for entry in skipped:
        if entry["state"] == "queued":
            dropped[entry["id"]] = entry["reason"]
    ready: dict[str, list[str]] = {}
    for tid, deps in candidates.items():
        blockers = []
        for dep in deps:
            dt = by_id.get(dep)
            if dt is None:
                blockers.append("%s(不在台账)" % dep)
            elif task_state(dt) == "done":
                continue
            elif task_state(dt) in ("failed", "abandoned"):
                blockers.append("%s(%s，不会完成)" % (dep, task_state(dt)))
            elif dep in candidates:
                continue  # 同批 queued 依赖：拓扑保序，不拦
            elif dep in dropped:
                blockers.append("%s(queued 但本轮不可派：%s)" % (dep, dropped[dep]))
            else:
                blockers.append("%s(%s，待回轮后重排)" % (dep, task_state(dt)))
        if blockers:
            skip(by_id[tid], "依赖不可用: " + ", ".join(blockers))
        else:
            ready[tid] = [d for d in deps if d in candidates]

    # 5) 稳定拓扑排序：基础序（泳道 → tier → priority → created_at → id），
    #    同批依赖必须排在前面；DFS 保序，环已在第 3 步排除不会递归爆。
    def base_key(tid: str) -> tuple:
        t = by_id[tid]
        return (LANE_RANK.get(lane_of(t), len(LANE_ORDER)),
                TIER_RANK.get(task_tier(t), len(TIER_RANK)),
                task_priority(t),
                created_key(t) or "9999",
                tid)

    ordered: list[str] = []
    emitted: set[str] = set()

    def emit(tid: str) -> None:
        if tid in emitted:
            return
        for dep in sorted(ready.get(tid, []), key=base_key):
            emit(dep)
        emitted.add(tid)
        ordered.append(tid)

    for tid in sorted(ready, key=base_key):
        emit(tid)

    for rank, tid in enumerate(ordered, 1):
        t = by_id[tid]
        lane = lane_of(t)
        deps = depends_of(t)
        if lane == "light":
            lane_reason = "轻活垫后（nex 免费池）"
        elif lane == "batch":
            lane_reason = "Step 批量泳道优先"
        elif lane == "long-text":
            lane_reason = "Step 长文泳道"
        elif lane == "scan":
            lane_reason = "Step 扫描泳道"
        else:
            lane_reason = "常规高级活"
        if deps:
            lane_reason += "；依赖 %s 已排在前" % ",".join(deps)
        plan.append({
            "rank": rank,
            "id": tid,
            "title": str(t.get("title") or "(无标题)"),
            "project": str(t.get("project") or "(未标注项目)"),
            "tier": task_tier(t) or "(未标注)",
            "state": task_state(t),
            "lane": lane,
            "model_hint": model_hint_of(t, lane),
            "est_minutes": est_minutes_of(t, lane),
            "depends_on": deps,
            "priority": task_priority(t),
            "reason": lane_reason,
        })

    # 6) 并发档：计划量 + 配置上限 + 同项目上限，给出有效建议并发
    by_project: dict[str, int] = {}
    by_lane: dict[str, int] = {}
    for card in plan:
        by_project[card["project"]] = by_project.get(card["project"], 0) + 1
        by_lane[card["lane"]] = by_lane.get(card["lane"], 0) + 1

    n = len(plan)
    if n == 0:
        tier, suggested = "idle", 0
    elif n <= CONC_SERIAL_MAX:
        tier, suggested = "serial", 1
    elif n <= CONC_NORMAL_MAX:
        tier, suggested = "normal", min(n, 3)
    else:
        tier, suggested = "batch", min(n, max_running, CONC_BATCH_CAP)
    # 同项目并行上限会进一步压低有效并发（worktree 隔离前同仓不铺开）
    per_project_cap = sum(min(c, max_per_project) for c in by_project.values())
    effective = min(suggested, per_project_cap) if n else 0
    if n and effective < suggested:
        conc_reason = ("计划 %d 张卡，全局上限 %d、同项目上限 %d，"
                       "同项目上限压低有效并发至 %d" %
                       (n, max_running, max_per_project, effective))
    elif n:
        conc_reason = "计划 %d 张卡（%s 档），建议并发 %d 路（全局上限 %d）" % (
            n, tier, effective, max_running)
    else:
        conc_reason = "今夜无可派单卡，无需并发"
    concurrency = {
        "tier": tier,
        "suggested": effective,
        "max_running": max_running,
        "max_running_per_project": max_per_project,
        "by_project": by_project,
        "by_lane": by_lane,
        "reason": conc_reason,
    }

    total_est = sum(c["est_minutes"] for c in plan)
    wall = int(math.ceil(total_est / effective)) if effective else 0
    reason = ""
    if not plan:
        reason = ("夜间队列为空：台账中无 state=queued 的可派单任务"
                  "（%d 张卡全部给出跳过原因），今夜不派单"
                  % len([t for t in tasks if isinstance(t, dict)]))

    end = now + datetime.timedelta(hours=WINDOW_HOURS)
    plan_obj = {
        "schema": SCHEMA,
        "tool": TOOL,
        "generated_at": now.isoformat(),
        "window": {
            "start": now.isoformat(),
            "end": end.isoformat(),
            "hours": WINDOW_HOURS,
        },
        "sources": {
            "registry": registry_path,
            "config": config_path,
            "registry_sha256": sha256_of(registry_path) if registry_path else "",
            "config_sha256": sha256_of(config_path) if config_path else "",
        },
        "concurrency": concurrency,
        "plan": plan,
        "skipped": skipped,
        "totals": {
            "planned": n,
            "skipped": len(skipped),
            "est_minutes": total_est,
            "est_wall_minutes": wall,
        },
        "reason": reason,
        "warnings": [],
    }
    return plan_obj


def attach_notes(plan_obj: dict, config_warning: str) -> dict:
    """把坏配置降级等全局说明追加进 reason/warnings，字段保持稳定。

    空队列说明由 build_plan 写（排班语义），这里只补配置告警。
    """
    if config_warning:
        plan_obj["warnings"] = list(plan_obj["warnings"]) + [config_warning]
        plan_obj["reason"] = "；".join(
            [x for x in (plan_obj["reason"], config_warning) if x])
    return plan_obj


def write_atomic(path: str, text: str) -> None:
    """临时文件 + fsync + os.replace，不留下半截 JSON。"""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".night-shift-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render(plan_obj: dict) -> str:
    """计划 JSON 文本（stdout 与 --out 落盘同一份，保证可复核）。"""
    return json.dumps(plan_obj, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="夜间排班助手：只读 registry.json + config/steward.json，"
                    "输出 24h 批次计划 JSON（plan/concurrency/skipped）")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--out", default=None,
                    help="计划 JSON 落盘路径（默认只打 stdout）")
    ap.add_argument("--now", default=None, help="ISO 时间（测试用，固定排班起点）")
    args = ap.parse_args(argv)

    try:
        now = parse_now(args.now)
        registry = load_registry(args.registry)
    except (NightShiftError, ValueError) as e:
        print("[ERROR] %s" % (str(e) or e.__class__.__name__), file=sys.stderr)
        return 2

    config, config_warning = load_config(args.config)
    plan_obj = attach_notes(
        build_plan(registry, config, now, args.registry, args.config),
        config_warning)
    text = render(plan_obj)

    if args.out:
        try:
            write_atomic(args.out, text)
        except OSError as e:
            print("[ERROR] 计划落盘失败 %s: %s" % (args.out, e), file=sys.stderr)
            return 2

    conc = plan_obj["concurrency"]
    print("NIGHT plan=%d skipped=%d est=%dmin wall=%dmin conc=%s/%d -> %s"
          % (plan_obj["totals"]["planned"], plan_obj["totals"]["skipped"],
             plan_obj["totals"]["est_minutes"],
             plan_obj["totals"]["est_wall_minutes"],
             conc["tier"], conc["suggested"],
             args.out or "stdout"), file=sys.stderr)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
