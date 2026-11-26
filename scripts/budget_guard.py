# -*- coding: utf-8 -*-
"""成本守卫 v1：把当日网关流水账按 task/project/lane 聚合成预算 verdict 与降档建议。

为什么要它：管家每天要在日报里回答"今天钱花到哪了、还能不能花、哪条 lane 该降档"。
现有 budget_report.py 走 ZCode usage 库（会话维度，按 session→task 映射摊成本），
网关侧流水账（logs/gateway-<date>.jsonl）只有 lane/tier 维度、实时带 cost_cny，
但没有一个脚本把它按预算阈值判 ok/warn/block。本脚本补这一格，且严守只读：
  输入  logs/gateway-<date>.jsonl + state/registry.json（均只读，不调用网关、不改路由）
  输出  stdout（人读）/ --json（机读）；退出码 0=ok 1=warn 2=block

聚合口径（与 budget_report.gateway_breakdown / DR-009 一致）：
  - 只认 event=ok/fail 为"请求"；switch/reload/traceback 等事件不计请求数；
  - 成本 = event=ok 的 cost_cny 合计（网关侧按上游真实模型结算，DR-009）；
  - 成功率 = ok / (ok+fail)，按桶四舍五入 4 位；
  - 时间窗 = 目标日期本地 [00:00, 次日00:00)，按记录 ts 过滤——
    文件是按天滚动的，但跨午夜的迟到记录 ts 仍属前一天，不能靠文件名归属。

为什么 by_task/by_project 大量落在 "unattributed"（v1 已知边界，不是 bug）：
  DR-009 明确"一个会话可能跨多个 lane，事后无法反推；必须在转发当下记账"，
  网关流水账因此不带 session/task 维度。本脚本仍按 task/project 建桶：
  记录带 task/task_id/taskId（或 project）字段时就归桶（前瞻兼容，如未来网关
  补记），否则进 "unattributed"；registry.json 提供当日任务清单（tasks_today）
  与项目分布，与成本桶并列输出，不强行把成本摊到任务上。

verdict 口径（v1，保守可回退）：
  - 总体 verdict 只看当日总成本 vs 预算阈值：>= block_at 记 block，>= warn_at 记 warn；
  - lane 健康度（成功率/集中度）与降档候选是 advisory：只进 warnings 与
    downgrade_candidates，不抬高总体 verdict——链路体检归 cron_health/mate_health，
    预算是本脚本的职责，两件事不混。

用法：
  python -X utf8 budget_guard.py [--json] [--date 2026-11-18|20261118|today|yesterday]
      [--logs-dir DIR] [--registry PATH] [--budget 20.0] [--warn-ratio 0.8]
      [--warn-at 16.0] [--block-at 20.0] [--min-success-rate 0.9]
      [--min-lane-requests 20] [--downgrade-margin 0.2] [--dominance-share 0.6]
退出码：0 ok / 1 warn / 2 block；日志或台账缺失不抛栈，按 0 元+warning 降级输出。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_LOGS = os.path.join(ROOT, "logs")
DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")

UNATTRIBUTED = "unattributed"
EXIT_FOR = {"ok": 0, "warn": 1, "block": 2}


# ---------------------------------------------------------------- 时间窗

def parse_date(s: str | None) -> dt.date:
    """目标日期：today/yesterday/ISO/紧凑格式；非法值退回今天（守卫不因参数错误罢工）。"""
    s = (s or "").strip().lower()
    today = dt.date.today()
    if s in ("", "today"):
        return today
    if s == "yesterday":
        return today - dt.timedelta(days=1)
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass
    try:
        return dt.datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return today


def parse_ts(v) -> dt.datetime | None:
    """epoch 秒 → 本地 datetime；缺失/脏值/越界返回 None（调用方按跳过计）。"""
    if isinstance(v, bool) or v is None:
        return None
    try:
        ts = float(v)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or ts > 4e10:  # 1970 前或 2096 年后都当脏数据
        return None
    try:
        return dt.datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def parse_registry_ts(s) -> dt.datetime | None:
    """台账时间戳（ISO，可带时区）→ 本地 naive datetime；解析不了返回 None。"""
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(t[:10]), dt.time.min)
        except ValueError:
            return None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d


# ---------------------------------------------------------------- 台账

def load_registry(path: str) -> tuple[dict, str]:
    """registry.json → (dict, 备注)；读不了/不是对象退化为 ({}, 原因)，不抛栈。"""
    try:
        reg = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {}, "registry 读取失败（%s），任务清单按空处理" % str(e)[:80]
    if not isinstance(reg, dict):
        return {}, "registry 根不是对象，任务清单按空处理"
    return reg, ""


def registry_tasks_today(reg: dict, day: dt.date, limit: int = 50) -> list[dict]:
    """当日活跃任务：created_at/last_check/updated_at/evidence_verified_at 任一命中目标日期。"""
    out = []
    for t in reg.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        stamps = [parse_registry_ts(t.get(k)) for k in
                  ("created_at", "last_check", "updated_at", "evidence_verified_at")]
        if not any(d is not None and d.date() == day for d in stamps):
            continue
        out.append({
            "id": t.get("id") or "?",
            "title": (t.get("title") or "")[:40],
            "project": t.get("project") or "",
            "state": t.get("state") or "?",
            "tier": t.get("tier") or "?",
            "model": t.get("model") or "",
            "last_check": t.get("last_check") or "",
        })
    return out[:limit]


def registry_by_state(reg: dict) -> dict:
    out: dict = {}
    for t in reg.get("tasks") or []:
        if isinstance(t, dict):
            k = t.get("state") or "?"
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- 聚合

def _new_bucket() -> dict:
    return {"requests": 0, "ok": 0, "fail": 0, "cost_cny": 0.0,
            "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
            "unpriced": 0, "tiers": {}}


def _finalize(buckets: dict, total_cost: float) -> dict:
    """桶集合补派生字段（成功率/占比/均价），按成本降序返回。"""
    out = {}
    for k, b in buckets.items():
        req = b["requests"]
        cost = round(b["cost_cny"], 4)
        out[k] = {
            "requests": req, "ok": b["ok"], "fail": b["fail"],
            "success_rate": round(b["ok"] / req, 4) if req else 0.0,
            "cost_cny": cost,
            "cost_share": round(b["cost_cny"] / total_cost, 4) if total_cost > 0 else 0.0,
            "avg_cost_per_req": round(b["cost_cny"] / b["ok"], 6) if b["ok"] else 0.0,
            "tokens_in": b["tokens_in"], "tokens_out": b["tokens_out"],
            "tokens_cached": b["tokens_cached"],
            "unpriced_requests": b["unpriced"],
            "tiers": dict(sorted(b["tiers"].items(), key=lambda kv: -kv[1])),
        }
    return dict(sorted(out.items(), key=lambda kv: (-kv[1]["cost_cny"], kv[0])))


def aggregate(log_path: str, day: dt.date) -> dict:
    """读网关流水账，按 lane/tier/task/project 聚合目标日期窗口内的请求。

    宽容到底：坏行/非对象/无 ts/窗外/非请求事件一律跳过并计数，绝不抛栈
    （与 board_view.today_cost 同一哲学：摘要坏一条不该拖垮整个守卫）。
    """
    res = {"log_file": os.path.basename(log_path), "log_exists": os.path.isfile(log_path),
           "skipped": {"bad_json": 0, "non_dict": 0, "no_ts": 0, "out_of_window": 0,
                       "not_request": 0},
           "by_lane": {}, "by_tier": {}, "by_task": {}, "by_project": {},
           "total_cny": 0.0, "requests": 0, "ok": 0, "fail": 0, "unpriced_requests": 0,
           "success_rate": 0.0}
    if not res["log_exists"]:
        return res

    lanes: dict = {}
    tiers: dict = {}
    tasks: dict = {}
    projects: dict = {}
    total, ok, fail, unpriced = 0.0, 0, 0, 0
    sk = res["skipped"]

    def bump(store: dict, key: str, row: dict, priced_ok: bool) -> None:
        b = store.setdefault(key, _new_bucket())
        b["requests"] += 1
        tier = row.get("tier") or "?"
        if row.get("event") == "ok":
            b["ok"] += 1
            b["tiers"][tier] = b["tiers"].get(tier, 0) + 1
            try:
                cost = float(row.get("cost_cny") or 0.0)
                if cost < 0:
                    cost = 0.0
            except (TypeError, ValueError):
                cost = 0.0
            b["cost_cny"] += cost
            for f in ("tokens_in", "tokens_out", "tokens_cached"):
                try:
                    b[f] += int(row.get(f) or 0)
                except (TypeError, ValueError):
                    pass
            if not priced_ok:
                b["unpriced"] += 1
        else:
            b["fail"] += 1

    try:
        f = open(log_path, encoding="utf-8", errors="replace")
    except OSError:
        return res
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                sk["bad_json"] += 1
                continue
            if not isinstance(row, dict):
                sk["non_dict"] += 1
                continue
            ev = row.get("event")
            if ev not in ("ok", "fail"):
                sk["not_request"] += 1
                continue
            when = parse_ts(row.get("ts"))
            if when is None:
                sk["no_ts"] += 1
                continue
            if when.date() != day:
                sk["out_of_window"] += 1
                continue
            lane = str(row.get("lane") or "?")
            tier = str(row.get("tier") or "?")
            task = (row.get("task") or row.get("task_id") or row.get("taskId")
                    or UNATTRIBUTED)
            project = row.get("project") or UNATTRIBUTED
            priced_ok = ev == "ok" and row.get("priced") is True
            for store, key in ((lanes, lane), (tiers, tier),
                               (tasks, str(task)), (projects, str(project))):
                bump(store, key, row, priced_ok)
            if ev == "ok":
                ok += 1
                if not priced_ok:
                    unpriced += 1
                try:
                    c = float(row.get("cost_cny") or 0.0)
                    total += c if c > 0 else 0.0
                except (TypeError, ValueError):
                    pass
            else:
                fail += 1

    res.update({
        "requests": ok + fail, "ok": ok, "fail": fail,
        "total_cny": round(total, 4), "unpriced_requests": unpriced,
        "success_rate": round(ok / (ok + fail), 4) if ok + fail else 0.0,
        "by_lane": _finalize(lanes, total), "by_tier": _finalize(tiers, total),
        "by_task": _finalize(tasks, total), "by_project": _finalize(projects, total),
    })
    return res


# ---------------------------------------------------------------- 判定

def lane_verdict(b: dict, th: dict) -> str:
    """单 lane 健康度（advisory）：样本不足只 warn 不 block，避免末位兜底 lane
    少量失败就刷 block（apigoto 类 lane 失败是设计内的兜底行为）。
    另设 ±0.05 模糊带：逼近成功率红线（差 5 个百分点内）先 warn 提醒。
    """
    sr = b["success_rate"]
    n = b["requests"]
    if n >= th["min_lane_requests"] and sr < th["min_success_rate"]:
        return "block"
    if n >= th["min_lane_requests"] and sr < th["min_success_rate"] + 0.05:
        return "warn"
    if sr < th["min_success_rate"]:
        return "warn"
    if b["cost_share"] >= th["dominance_share"]:
        return "warn"
    return "ok"


def downgrade_candidates(by_lane: dict, th: dict) -> list[dict]:
    """降档候选：高花费 lane 存在"同 tier 且显著更便宜"的有价替代时给出建议。

    条件（全部满足）：花费占比 >= dominance_share/6（默认 10%）、存在同 tier 的
    有价 lane 且其单请求均价低出 downgrade_margin（默认 20%）。两条排除：
    未定价 lane（cost 恒 0）不能当"更便宜"的证据——不知道价不等于便宜；
    成功率不达标的 lane 不当降档目标——省小钱换来不稳定链路不叫降档。
    """
    share_min = th["dominance_share"] / 6.0
    out = []
    for lane, b in sorted(by_lane.items(), key=lambda kv: -kv[1]["cost_cny"]):
        if b["cost_share"] < share_min or b["ok"] == 0 or b["avg_cost_per_req"] <= 0:
            continue
        if b["unpriced_requests"] >= b["ok"]:
            continue  # 自己都没价，比较无意义
        peers = []
        for other, ob in by_lane.items():
            if other == lane or ob["ok"] == 0 or ob["unpriced_requests"] >= ob["ok"]:
                continue
            if ob["success_rate"] < th["min_success_rate"]:
                continue  # 不健康的 lane 不当降档目标
            if not (set(ob["tiers"]) & set(b["tiers"])):
                continue
            if ob["avg_cost_per_req"] < b["avg_cost_per_req"] * (1 - th["downgrade_margin"]):
                peers.append({"lane": other, "avg_cost_per_req": ob["avg_cost_per_req"],
                              "success_rate": ob["success_rate"],
                              "saving_pct": round(
                                  (1 - ob["avg_cost_per_req"] / b["avg_cost_per_req"]) * 100, 1)})
        if not peers:
            continue
        peers.sort(key=lambda p: p["avg_cost_per_req"])
        out.append({
            "lane": lane, "cost_cny": b["cost_cny"], "cost_share": b["cost_share"],
            "avg_cost_per_req": b["avg_cost_per_req"], "cheaper_peers": peers,
            "suggest": peers[0]["lane"],
            "reason": "%s 占当日成本 %.1f%%，单请求均价 ¥%.6f；同 tier 有价 lane %s 均价 ¥%.6f（低 %.1f%%）"
                      % (lane, b["cost_share"] * 100, b["avg_cost_per_req"],
                         peers[0]["lane"], peers[0]["avg_cost_per_req"],
                         peers[0]["saving_pct"]),
        })
    return out


def verdict_for(total_cny: float, th: dict) -> str:
    if total_cny >= th["block_at_cny"]:
        return "block"
    if total_cny >= th["warn_at_cny"]:
        return "warn"
    return "ok"


def build_warnings(agg: dict, reg_note: str, th: dict, cands: list[dict]) -> list[str]:
    """advisory 清单：日志缺失、lane 健康度、集中度、未定价、降档、台账问题。"""
    w: list[str] = []
    if not agg["log_exists"]:
        w.append("网关日志缺失（%s），当日成本按 0 元计" % agg["log_file"])
    if reg_note:
        w.append(reg_note)
    for lane, b in agg["by_lane"].items():
        if b["success_rate"] < th["min_success_rate"]:
            small = "（样本 %d < %d，仅提示不判 block）" % (b["requests"], th["min_lane_requests"]) \
                if b["requests"] < th["min_lane_requests"] else ""
            w.append("lane %s 成功率 %.1f%% < %.0f%%%s"
                     % (lane, b["success_rate"] * 100, th["min_success_rate"] * 100, small))
    for lane, b in agg["by_lane"].items():
        if b["cost_share"] >= th["dominance_share"]:
            w.append("单 lane 集中度 %.1f%% >= %.0f%%: %s"
                     % (b["cost_share"] * 100, th["dominance_share"] * 100, lane))
    if agg["unpriced_requests"]:
        unpriced_lanes = [l for l, b in agg["by_lane"].items() if b["unpriced_requests"]]
        w.append("未定价请求 %d 次（成本按 0 计）: %s"
                 % (agg["unpriced_requests"], ", ".join(unpriced_lanes)))
    if agg["skipped"]["bad_json"] or agg["skipped"]["non_dict"]:
        w.append("跳过坏行 %d 条（bad_json=%d non_dict=%d）"
                 % (agg["skipped"]["bad_json"] + agg["skipped"]["non_dict"],
                    agg["skipped"]["bad_json"], agg["skipped"]["non_dict"]))
    if agg["skipped"]["out_of_window"]:
        w.append("窗外记录 %d 条已剔除（ts 不属于 %s）"
                 % (agg["skipped"]["out_of_window"], agg.get("date", "?")))
    for c in cands:
        w.append("降档候选: " + c["reason"])
    return w


def analyze(logs_dir: str, registry_path: str, day: dt.date, th: dict) -> dict:
    """守卫主口径：聚合 + 台账 + verdict + 降档候选 + warnings，全部只读。"""
    log_path = os.path.join(logs_dir, "gateway-%s.jsonl" % day.strftime("%Y%m%d"))
    agg = aggregate(log_path, day)
    agg["date"] = day.isoformat()
    reg, reg_note = load_registry(registry_path)
    cands = downgrade_candidates(agg["by_lane"], th)
    verdict = verdict_for(agg["total_cny"], th)
    for b in agg["by_lane"].values():
        b["verdict"] = lane_verdict(b, th)
    return {
        "available": True,
        "date": agg["date"],
        "window": {"from": dt.datetime.combine(day, dt.time.min).isoformat(),
                   "to": dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min).isoformat(),
                   "tz": "local"},
        "log_file": agg["log_file"],
        "log_exists": agg["log_exists"],
        "requests": agg["requests"], "ok": agg["ok"], "fail": agg["fail"],
        "success_rate": agg["success_rate"],
        "total_cny": agg["total_cny"],
        "unpriced_requests": agg["unpriced_requests"],
        "skipped": agg["skipped"],
        "thresholds": dict(th),
        "verdict": verdict,
        "by_lane": agg["by_lane"],
        "by_tier": agg["by_tier"],
        "by_task": agg["by_task"],
        "by_project": agg["by_project"],
        "downgrade_candidates": cands,
        "warnings": build_warnings(agg, reg_note, th, cands),
        "registry": {
            "available": bool(reg), "path": registry_path,
            "tasks_total": sum(1 for t in reg.get("tasks") or [] if isinstance(t, dict)),
            "by_state": registry_by_state(reg),
            "tasks_today": registry_tasks_today(reg, day),
            "dispatcher_session": (reg.get("dispatcher") or {}).get("sessionId") or "",
        },
    }


# ---------------------------------------------------------------- 输出

def render_text(r: dict) -> str:
    L = []
    L.append("== 成本守卫 %s（%s）==" % (r["date"], r["log_file"]))
    L.append("verdict=%s | 当日成本 ¥%.4f | 预算 ¥%.2f（warn≥¥%.2f block≥¥%.2f）"
             % (r["verdict"].upper(), r["total_cny"], r["thresholds"]["daily_budget_cny"],
                r["thresholds"]["warn_at_cny"], r["thresholds"]["block_at_cny"]))
    L.append("请求 %d（ok %d / fail %d，成功率 %.1f%%）| 未定价 %d"
             % (r["requests"], r["ok"], r["fail"], r["success_rate"] * 100,
                r["unpriced_requests"]))
    L.append("-- 按 lane --")
    L.append("%-24s %6s %6s %6s %9s %7s %9s  %s"
             % ("lane", "请求", "ok", "fail", "成本¥", "占比", "均价¥", "判定"))
    for lane, b in r["by_lane"].items():
        L.append("%-24s %6d %6d %6d %9.4f %6.1f%% %9.6f  %s"
                 % (lane[:24], b["requests"], b["ok"], b["fail"], b["cost_cny"],
                    b["cost_share"] * 100, b["avg_cost_per_req"], b.get("verdict", "?")))
    L.append("-- 按 tier --")
    for tier, b in r["by_tier"].items():
        L.append("%-12s 请求 %5d 成功率 %5.1f%% 成本 ¥%8.4f（%.1f%%）"
                 % (tier, b["requests"], b["success_rate"] * 100, b["cost_cny"],
                    b["cost_share"] * 100))
    L.append("-- 按 task/project（网关台账无 session 维度，未打标的进 %s）--" % UNATTRIBUTED)
    for k, b in r["by_task"].items():
        L.append("task %-20s 请求 %5d 成本 ¥%8.4f" % (k[:20], b["requests"], b["cost_cny"]))
    for k, b in r["by_project"].items():
        L.append("proj %-20s 请求 %5d 成本 ¥%8.4f" % (k[:20], b["requests"], b["cost_cny"]))
    if r["downgrade_candidates"]:
        L.append("-- 降档候选 --")
        for c in r["downgrade_candidates"]:
            L.append("  %s → 建议 %s（%s）" % (c["lane"], c["suggest"], c["reason"]))
    else:
        L.append("-- 降档候选：无 --")
    if r["warnings"]:
        L.append("-- 提示 --")
        for w in r["warnings"]:
            L.append("  ! " + w)
    rt = r["registry"]
    L.append("-- 台账（%s）--" % ("可用" if rt["available"] else "不可用"))
    L.append("任务总数 %d | 状态分布 %s | 当日活跃 %d"
             % (rt["tasks_total"],
                "/".join("%s:%d" % kv for kv in rt["by_state"].items()) or "-",
                len(rt["tasks_today"])))
    for t in rt["tasks_today"][:10]:
        L.append("  %s [%s] %s %s" % (t["id"], t["state"], t["tier"], t["title"]))
    sk = r["skipped"]
    if any(sk.values()):
        L.append("跳过: " + " ".join("%s=%d" % kv for kv in sk.items() if kv[1]))
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="成本守卫：当日网关成本聚合 + 预算 verdict + 降档候选（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（默认人读表格）")
    ap.add_argument("--date", default="today", help="目标日期 today/yesterday/YYYY-MM-DD/YYYYMMDD")
    ap.add_argument("--logs-dir", default=DEFAULT_LOGS)
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--budget", type=float, default=20.0, help="当日预算上限（元）")
    ap.add_argument("--warn-ratio", type=float, default=0.8, help="warn 阈值 = 预算×该比例")
    ap.add_argument("--warn-at", type=float, default=None, help="显式 warn 阈值（优先于 --warn-ratio）")
    ap.add_argument("--block-at", type=float, default=None, help="显式 block 阈值（默认=预算）")
    ap.add_argument("--min-success-rate", type=float, default=0.9)
    ap.add_argument("--min-lane-requests", type=int, default=20,
                    help="lane 成功率判 block 的最小样本数")
    ap.add_argument("--downgrade-margin", type=float, default=0.2,
                    help="替代 lane 需便宜的比例（0.2=低 20%%）")
    ap.add_argument("--dominance-share", type=float, default=0.6,
                    help="单 lane 成本占比提示线")
    a = ap.parse_args(argv)

    fp = announce("budget_guard")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "budget_guard")

    day = parse_date(a.date)
    budget = max(a.budget, 0.0)
    th = {
        "daily_budget_cny": round(budget, 4),
        "warn_at_cny": round(a.warn_at if a.warn_at is not None else budget * a.warn_ratio, 4),
        "block_at_cny": round(a.block_at if a.block_at is not None else budget, 4),
        "min_success_rate": a.min_success_rate,
        "min_lane_requests": a.min_lane_requests,
        "downgrade_margin": a.downgrade_margin,
        "dominance_share": a.dominance_share,
    }
    r = analyze(a.logs_dir, a.registry, day, th)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(render_text(r))
    return EXIT_FOR[r["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
