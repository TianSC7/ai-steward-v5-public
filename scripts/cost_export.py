# -*- coding: utf-8 -*-
"""成本导出 v1：把网关流水账按 日期/lane/tier 聚合成 CSV/JSON（纯只读）。

为什么要它：预算 guard 回答"今天还能不能花"，日报要回答"钱按天、按 lane 怎么走的"。
网关流水账 logs/gateway-<date>.jsonl 是唯一带 lane/tier/真实 cost_cny 的一手账本
（DR-009：成本在转发当下记账，事后不摊派）；本脚本把它导出成可进表格、可入库的
宽表，供日报/周报/对账直接消费。与 budget_guard.py 的分工：
  budget_guard  当日单日 + 阈值 verdict + 降档建议（退出码带语义）
  cost_export   跨日多文件 + 纯导出，不判预算、不给建议、退出码只表达成败

聚合口径（与 budget_guard.aggregate / DR-009 一致）：
  - 只认 event=ok/fail 为"请求"；switch/reload/traceback 等事件不计请求数；
  - 成本 = event=ok 的 cost_cny 合计；fail 行即便带成本也不计（失败不结算）；
    老格式缺 cost_cny/priced 的 ok 行按 0 元 + unpriced 计数，不抛栈；
  - 成功率 = ok / (ok+fail)，四舍五入 4 位；
  - 平均耗时 = 有合法 ms（数值、>=0、非 bool）的请求的 ms 均值，四舍五入 1 位；
  - 日期归属按记录 ts 的本地日期，不按文件名——文件按天滚动，跨午夜的迟到
    记录仍属前一天（与 budget_guard 同一理由）。

输出：
  CSV（--out 或默认 stdout）列固定为 date,lane,requests,success_rate,avg_ms,cost_cny，
  一行 = 一天 × 一个 lane（tier 维度在此汇总，保持验收约定的固定列）；
  JSON（--json）保留 tier 明细：meta/rows/by_day_lane/totals 四键恒定存在。
  --out 与 --json 可同时用：CSV 落文件，JSON 打 stdout（stdout 始终只有一种格式）。

只读与边界：
  - 输入只有 logs/gateway-*.jsonl 与 config/routing.json（读来登记 lane 清单、
    未知 lane 与 sha256 指纹），不写日志、不改路由、不调用网关、不读密钥；
  - routing.json 缺失/坏 JSON 不阻断导出，meta.routing.error 记原因；
  - 坏行（非 JSON、非对象、缺 ts、窗外、非请求事件）逐类计数跳过，绝不抛栈；
  - logs 目录/文件不存在 → 空结果 + 表头，退出码 0（导出岗位不该因无账罢工）。

用法：
  python -X utf8 cost_export.py [--logs-dir DIR] [--routing PATH]
      [--date 2026-11-18|20261118|today|yesterday]（可重复/逗号分隔）
      [--from D] [--to D] [--out CSV] [--json]

退出码：0 = 导出成功（含空账）；1 = --out 写入失败；2 = 参数错误。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import hashlib
import io
import json
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_LOGS = os.path.join(ROOT, "logs")
DEFAULT_ROUTING = os.path.join(ROOT, "config", "routing.json")

# 验收约定：CSV 列顺序固定，不得增删（tier 只进 JSON 明细）。
CSV_COLUMNS = ("date", "lane", "requests", "success_rate", "avg_ms", "cost_cny")
UNKNOWN = "?"
LOG_GLOB = "gateway-*.jsonl"


# ---------------------------------------------------------------- 时间

def parse_ts(v) -> dt.datetime | None:
    """epoch 秒 → 本地 datetime；缺失/脏值/越界返回 None（调用方按跳过计）。

    口径抄 budget_guard.parse_ts：bool 是 int 子类，必须先排除；
    0 <= ts <= 4e10 之外视为脏数据（1970 前 / 2096 年后）。
    """
    if isinstance(v, bool) or v is None:
        return None
    try:
        ts = float(v)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or ts > 4e10:
        return None
    try:
        return dt.datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def parse_day(s: str) -> dt.date:
    """单个日期：today/yesterday/YYYY-MM-DD/YYYYMMDD；非法抛 ValueError。

    与 budget_guard 不同，导出岗位不把非法日期静默退回今天——导出错一天
    比报错更贵，宁可让 CLI 以退出码 2 打出用法。
    """
    t = (s or "").strip().lower()
    today = dt.date.today()
    if t == "today":
        return today
    if t == "yesterday":
        return today - dt.timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    raise ValueError("非法日期: %r（支持 today/yesterday/YYYY-MM-DD/YYYYMMDD）" % s)


def day_selected(day: dt.date, dates: set[dt.date], lo: dt.date | None,
                 hi: dt.date | None) -> bool:
    """日期过滤：显式日期集合（为空不约束）与 [lo, hi] 闭区间同时满足。"""
    if dates and day not in dates:
        return False
    if lo and day < lo:
        return False
    if hi and day > hi:
        return False
    return True


# ---------------------------------------------------------------- 路由台账

def load_routing(path: str) -> dict:
    """读 routing.json 的 lane 清单与指纹（只读）；失败退化为 error 字段。"""
    info = {"path": path, "sha256": None, "lanes": [], "error": None}
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        info["error"] = "读取失败: %s" % str(e)[:120]
        return info
    info["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        cfg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        info["error"] = "坏 JSON: %s" % str(e)[:120]
        return info
    if not isinstance(cfg, dict):
        info["error"] = "根不是对象"
        return info
    lanes = cfg.get("lanes")
    if isinstance(lanes, list):
        for lane in lanes:
            if isinstance(lane, dict) and lane.get("id"):
                info["lanes"].append(str(lane["id"]))
    return info


# ---------------------------------------------------------------- 聚合

def _new_bucket() -> dict:
    return {"requests": 0, "ok": 0, "fail": 0, "ms_sum": 0.0, "ms_n": 0,
            "cost_cny": 0.0, "unpriced": 0}


def _as_float(v) -> float | None:
    """数值转 float；bool/None/非数值返回 None（不把 True 当 1）。"""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _as_ms(v) -> float | None:
    """毫秒：仅接受有限、>=0 的数值；脏值忽略但该请求仍计入 requests。"""
    f = _as_float(v)
    if f is None or f < 0:
        return None
    return f


def _finalize(b: dict) -> dict:
    """桶 → 对外的稳定数值（成功率 4 位、均值 1 位、成本 6 位）。"""
    n = b["requests"]
    return {
        "requests": n,
        "ok": b["ok"],
        "fail": b["fail"],
        "success_rate": round(b["ok"] / n, 4) if n else 0.0,
        "avg_ms": round(b["ms_sum"] / b["ms_n"], 1) if b["ms_n"] else 0.0,
        "cost_cny": round(b["cost_cny"], 6),
        "unpriced": b["unpriced"],
    }


def aggregate(logs_dir: str, dates: set[dt.date] | None = None,
              date_from: dt.date | None = None,
              date_to: dt.date | None = None) -> dict:
    """扫 logs/gateway-*.jsonl，按 (日期, lane, tier) 聚合并返回原始桶。

    宽容到底：坏行逐类计数跳过（bad_json/non_dict/not_request/no_ts/out_of_window），
    与 budget_guard.aggregate 同一哲学——账本坏一条不该让整份报表出不来。
    """
    dates = dates or set()
    res = {
        "logs_dir": logs_dir,
        "log_dir_exists": os.path.isdir(logs_dir),
        "log_files": [],
        "unreadable_files": [],
        "rows_read": 0,
        "skipped": {"bad_json": 0, "non_dict": 0, "not_request": 0,
                    "no_ts": 0, "out_of_window": 0},
        "unpriced_requests": 0,
        "buckets": {},
    }
    paths = sorted(glob.glob(os.path.join(logs_dir, LOG_GLOB)))
    res["log_files"] = [os.path.basename(p) for p in paths]
    buckets: dict[tuple[str, str, str], dict] = {}
    sk = res["skipped"]
    unpriced = 0

    for path in paths:
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            res["unreadable_files"].append(os.path.basename(path))
            continue
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                res["rows_read"] += 1
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
                if not day_selected(when.date(), dates, date_from, date_to):
                    sk["out_of_window"] += 1
                    continue
                lane = str(row.get("lane") or UNKNOWN)
                tier = str(row.get("tier") or UNKNOWN)
                b = buckets.setdefault((when.date().isoformat(), lane, tier),
                                       _new_bucket())
                b["requests"] += 1
                if ev == "ok":
                    b["ok"] += 1
                    cost = _as_float(row.get("cost_cny"))
                    if cost is not None and cost > 0:
                        b["cost_cny"] += cost
                    if row.get("priced") is not True:
                        b["unpriced"] += 1
                        unpriced += 1
                else:
                    b["fail"] += 1
                ms = _as_ms(row.get("ms"))
                if ms is not None:
                    b["ms_sum"] += ms
                    b["ms_n"] += 1

    res["buckets"] = buckets
    res["unpriced_requests"] = unpriced
    return res


def build_rows(buckets: dict) -> list[dict]:
    """桶 → 明细行（date/lane/tier + 指标），按 (date, lane, tier) 升序稳定输出。"""
    rows = []
    for (day, lane, tier) in sorted(buckets):
        item = {"date": day, "lane": lane, "tier": tier}
        item.update(_finalize(buckets[(day, lane, tier)]))
        rows.append(item)
    return rows


def rollup_day_lane(buckets: dict) -> list[dict]:
    """把 tier 维度折叠成 CSV 的一行一 (日期, lane)。

    成功率按 ok/requests 重算、avg_ms 按测得样本数加权——不能用已四舍五入的
    tier 行再平均，否则 ticks 少的 tier 会等权污染整车平均值。
    """
    merged: dict[tuple[str, str], dict] = {}
    for (day, lane, _tier), b in buckets.items():
        m = merged.setdefault((day, lane), _new_bucket())
        for k in ("requests", "ok", "fail", "ms_n", "unpriced"):
            m[k] += b[k]
        m["ms_sum"] += b["ms_sum"]
        m["cost_cny"] += b["cost_cny"]
    out = []
    for (day, lane) in sorted(merged):
        item = {"date": day, "lane": lane}
        item.update(_finalize(merged[(day, lane)]))
        out.append(item)
    return out


def build_totals(buckets: dict) -> dict:
    """全表合计：直接由桶求和（不在已四舍五入的行上二次平均）。

    avg_ms = 有效 ms 总和 / 有效样本数——与明细行同口径；缺 ms 的请求
    只进 requests 分母，不稀释毫秒均值。
    """
    n = ok = fail = ms_n = 0
    ms_sum = cost = 0.0
    for b in buckets.values():
        n += b["requests"]
        ok += b["ok"]
        fail += b["fail"]
        ms_sum += b["ms_sum"]
        ms_n += b["ms_n"]
        cost += b["cost_cny"]
    return {
        "requests": n,
        "ok": ok,
        "fail": fail,
        "success_rate": round(ok / n, 4) if n else 0.0,
        "avg_ms": round(ms_sum / ms_n, 1) if ms_n else 0.0,
        "cost_cny": round(cost, 6),
    }


# ---------------------------------------------------------------- 渲染

def render_csv(rows: list[dict]) -> str:
    """CSV 文本：固定表头 + 固定数值格式（成功率 4 位、毫秒 1 位、成本 6 位）。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([r["date"], r["lane"], r["requests"],
                    "%.4f" % r["success_rate"], "%.1f" % r["avg_ms"],
                    "%.6f" % r["cost_cny"]])
    return buf.getvalue()


def build_json(agg: dict, routing: dict, rows: list[dict],
               day_lane: list[dict], dates: set[dt.date],
               date_from: dt.date | None, date_to: dt.date | None) -> dict:
    """JSON 结果：meta/rows/by_day_lane/totals 四键恒定存在，便于下游稳定消费。"""
    seen = {r["lane"] for r in rows}
    known = set(routing.get("lanes") or [])
    routing_meta = {
        "path": routing.get("path"),
        "sha256": routing.get("sha256"),
        "lanes": routing.get("lanes") or [],
        "unknown_lanes": sorted(seen - known) if known else [],
        "error": routing.get("error"),
    }
    return {
        "meta": {
            "logs_dir": agg["logs_dir"],
            "log_dir_exists": agg["log_dir_exists"],
            "log_files": agg["log_files"],
            "unreadable_files": agg["unreadable_files"],
            "rows_read": agg["rows_read"],
            "skipped": agg["skipped"],
            "unpriced_requests": agg["unpriced_requests"],
            "date_filter": {
                "dates": [d.isoformat() for d in sorted(dates)],
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None,
            },
            "routing": routing_meta,
        },
        "rows": rows,
        "by_day_lane": day_lane,
        "totals": build_totals(agg["buckets"]),
    }


# ---------------------------------------------------------------- CLI

def _split_dates(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for chunk in values or []:
        out.extend(x.strip() for x in chunk.split(",") if x.strip())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="网关成本导出：按日/lane/tier 聚合 logs/gateway-*.jsonl（只读）")
    ap.add_argument("--logs-dir", default=DEFAULT_LOGS, help="日志目录（默认 <插件根>/logs）")
    ap.add_argument("--routing", default=DEFAULT_ROUTING,
                    help="routing.json 路径（只读，用于 lane 清单与指纹）")
    ap.add_argument("--date", action="append", default=None, metavar="D",
                    help="只导出指定日期，可重复或逗号分隔（today/yesterday/YYYY-MM-DD/YYYYMMDD）")
    ap.add_argument("--from", dest="date_from", default=None, metavar="D",
                    help="起始日期（含）")
    ap.add_argument("--to", dest="date_to", default=None, metavar="D",
                    help="结束日期（含）")
    ap.add_argument("--out", default=None, metavar="CSV",
                    help="CSV 输出路径（列固定 date,lane,requests,success_rate,avg_ms,cost_cny）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON（含 tier 明细）")
    args = ap.parse_args(argv)

    try:
        dates = {parse_day(s) for s in _split_dates(args.date)}
        date_from = parse_day(args.date_from) if args.date_from else None
        date_to = parse_day(args.date_to) if args.date_to else None
    except ValueError as e:
        ap.error(str(e))
    if date_from and date_to and date_from > date_to:
        ap.error("--from %s 晚于 --to %s" % (date_from, date_to))

    agg = aggregate(args.logs_dir, dates, date_from, date_to)
    rows = build_rows(agg["buckets"])
    day_lane = rollup_day_lane(agg["buckets"])
    routing = load_routing(args.routing)
    csv_text = render_csv(day_lane)

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8", newline="") as f:
                f.write(csv_text)
        except OSError as e:
            print("写出失败: %s: %s" % (args.out, e), file=sys.stderr)
            return 1
        print("WROTE %d rows -> %s" % (len(day_lane), args.out), file=sys.stderr)
    if args.json:
        payload = build_json(agg, routing, rows, day_lane, dates, date_from, date_to)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not args.out:
        sys.stdout.write(csv_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
