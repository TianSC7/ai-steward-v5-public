# -*- coding: utf-8 -*-
"""今日成本按任务分桶：ZCode model_usage × usage-bar 目录价 × 管家台账 session→task 映射。

为什么要它：usage_facts 只能给"今日总量/按模型"，管家的汇报格式要求"本轮任务成本"；
worker 会话 id 在 registry 里有任务归属，用它把每个请求的成本归到 W-* 任务上。

计价口径完全复用 usage-bar/zusage.py（不另立一套，避免两处对不上）：
  - input_tokens 是含缓存命中的总输入；真实未命中 = input - cache_read
  - 费用 = 未命中×输入价 + 输出×输出价 + cache_read×命中价（元/百万 tokens）
  - 峰谷：周六日全天谷；工作日 9-12/14-18 点为峰
价格表：~/.zcode/plugins/usage-bar/prices.json（热加载，只读）。

用法：
  python -X utf8 budget_report.py [--json] [--days 1] [--top 15]
退出码：0 正常（usage-bar 缺席也返回 0，available=false）；2 指纹/库不可读。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

DB = os.environ.get("STEWARD_ZCODE_DB") or os.path.join(
    os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
PRICES = os.path.expanduser("~/.zcode/plugins/usage-bar/prices.json")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "state", "registry.json")

# 与 zusage.py 的 _PEAK_SQL 同口径
_PEAK_SQL = """case when strftime('%w', {col}/1000, 'unixepoch', 'localtime') in ('0','6') then 0
     when strftime('%H', {col}/1000, 'unixepoch', 'localtime') between '09' and '11' then 1
     when strftime('%H', {col}/1000, 'unixepoch', 'localtime') between '14' and '17' then 1
     else 0 end"""


def price_entry(model_id: str, prices: dict):
    """模型 id → 目录价条目：精确 → 别名 → 互相包含（容忍 relay 前缀）；不中返回 None。"""
    m = str(model_id or "").strip().lower()
    if not m:
        return None
    models = prices.get("models", {})
    if m in models:
        return models[m]
    aliases = prices.get("aliases", {})
    if m in aliases and aliases[m] in models:
        return models[aliases[m]]
    for key, ent in models.items():
        if key in m or m in key:
            return ent
    return None


def row_cost(ent: dict, inp: int, outp: int, crd: int, peak: bool) -> float:
    r = ent.get("peak" if peak else "idle") if isinstance(ent.get("peak"), dict) else ent
    if not isinstance(r, dict):
        r = ent
    miss = inp - crd if inp > crd else 0
    return (miss * r.get("in", 0) + outp * r.get("out", 0) + crd * r.get("cache_read", 0)) / 1e6


def load_registry_map() -> tuple[dict, str]:
    """sessionId → 任务标签；dispatcher 会话单独标。返回 (map, dispatcher_sid)。"""
    try:
        reg = json.load(open(REGISTRY, encoding="utf-8"))
    except (OSError, ValueError):
        return {}, ""
    m = {}
    for t in reg.get("tasks") or []:
        sid = t.get("sessionId")
        if sid:
            label = "%s %s" % (t.get("id"), (t.get("title") or "")[:24])
            m.setdefault(sid, label)
    disp = (reg.get("dispatcher") or {}).get("sessionId") or ""
    if disp:
        m[disp] = "调度台"
    return m, disp


def gateway_breakdown(date_str: str):
    """网关台账按 tier/lane 分桶（真实上游模型成本；台账无 session 维度，故不能按任务摊）。"""
    p = os.path.join(ROOT, "logs", "gateway-%s.jsonl" % date_str.replace("-", ""))
    if not os.path.exists(p):
        return None
    by_tier, by_lane = {}, {}
    total, n = 0.0, 0
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") != "ok":
                continue
            c = float(r.get("cost_cny") or 0.0)
            n += 1
            total += c
            t = r.get("tier") or "?"
            lane = r.get("lane") or "?"
            by_tier[t] = by_tier.get(t, 0.0) + c
            by_lane[lane] = by_lane.get(lane, 0.0) + c
    return {"requests": n, "total_cny": round(total, 4),
            "by_tier": {k: round(v, 4) for k, v in sorted(by_tier.items(), key=lambda kv: -kv[1])},
            "by_lane": {k: round(v, 4) for k, v in sorted(by_lane.items(), key=lambda kv: -kv[1])}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()

    fp = announce("budget_report")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(180, "budget_report")

    if not os.path.exists(PRICES):
        print(json.dumps({"available": False, "reason": "未找到 prices.json: %s" % PRICES},
                         ensure_ascii=False))
        return 0
    prices = json.load(open(PRICES, encoding="utf-8"))
    smap, _ = load_registry_map()

    con = sqlite3.connect(DB, timeout=5)
    con.execute("pragma busy_timeout=5000")
    peak_sql = _PEAK_SQL.format(col="completed_at")
    start = dt.datetime.combine(dt.date.today() - dt.timedelta(days=a.days - 1), dt.time.min)
    end = dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time.min)
    rows = con.execute(
        "select session_id, model_id, %s as pk, count(*) n,"
        " coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0),"
        " coalesce(sum(cache_read_input_tokens),0)"
        " from model_usage where status='completed' and completed_at between ? and ?"
        " group by session_id, model_id, pk" % peak_sql,
        (int(start.timestamp() * 1000), int(end.timestamp() * 1000))).fetchall()
    con.close()

    tasks, models, unpriced, total_req = {}, {}, set(), 0
    for sid, model, pk, n, inp, outp, crd in rows:
        ent = price_entry(model, prices)
        cost = row_cost(ent, inp, outp, crd, bool(pk)) if ent else 0.0
        if ent is None:
            unpriced.add(model)
        label = smap.get(sid) or "非任务会话"
        agg = tasks.setdefault(label, {"cny": 0.0, "req": 0, "models": {}})
        agg["cny"] += cost
        agg["req"] += n
        agg["models"][model] = agg["models"].get(model, 0.0) + cost
        models[model] = models.get(model, 0.0) + cost
        total_req += n

    out_date = dt.date.today().isoformat()
    out = {
        "available": True, "date": out_date, "days": a.days,
        "total_cny": round(sum(v["cny"] for v in tasks.values()), 4), "requests": total_req,
        "unpriced_models": sorted(unpriced),
        "by_task": [
            {"task": k, "cny": round(v["cny"], 4), "requests": v["req"],
             "top_model": max(v["models"], key=v["models"].get) if v["models"] else ""}
            for k, v in sorted(tasks.items(), key=lambda kv: -kv[1]["cny"])[:a.top]
        ],
        "by_model": [{"model": k, "cny": round(v, 4)}
                     for k, v in sorted(models.items(), key=lambda kv: -kv[1])],
        "gateway": gateway_breakdown(out_date),
    }
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print("== 今日成本按任务（%s，近 %d 天）==" % (out["date"], a.days))
    print("%-34s %8s %6s  %s" % ("任务", "成本¥", "请求", "主力模型"))
    for r in out["by_task"]:
        print("%-34s %8.2f %6d  %s" % (r["task"][:34], r["cny"], r["requests"], r["top_model"]))
    print("%-34s %8.2f %6d" % ("合计", out["total_cny"], out["requests"]))
    if unpriced:
        print("未定价模型（按 0 元计）: " + ", ".join(sorted(unpriced)))
    g = out.get("gateway")
    if g:
        print("== 网关侧（真实上游模型成本，无 session 维度故不摊到任务）==")
        print("请求 %d | 合计 ¥%.2f" % (g["requests"], g["total_cny"]))
        print("按 tier: " + " / ".join("%s ¥%.2f" % kv for kv in g["by_tier"].items()))
        print("按 lane: " + " / ".join("%s ¥%.2f" % kv for kv in g["by_lane"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
