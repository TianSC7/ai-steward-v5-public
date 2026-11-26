# -*- coding: utf-8 -*-
"""cost_export 用例：聚合口径/坏行/空账/日期过滤/CSV 固定列/JSON 稳定/只读/CLI。

覆盖（21 例）：
  1  aggregate-basic       按 (日,lane,tier) 桶的 requests/ok/fail/成功率/均值/成本
  2  rollup-day-lane      tier 折叠成 (日,lane)：成功率重算、avg_ms 按样本加权
  3  bad-lines-skipped    坏 JSON/非对象/非请求/缺 ts/脏 ts 逐类计数且不抛栈
  4  empty-logs-dir       空目录 -> 空结果 + 仅表头，退出码 0
  5  missing-logs-dir     目录不存在 -> 不崩，log_dir_exists=False
  6  empty-log-file       0 字节日志 -> 空结果，退出码 0
  7  date-filter-single   --date 单日只留当日桶，窗外请求计入 out_of_window
  8  date-filter-range    --from/--to 闭区间过滤
  9  date-filter-invalid  非法日期 -> 退出码 2（导出不静默退回今天）
  10 csv-schema-exact     表头与全部行内容逐字节比对（列序/格式固定）
  11 json-fields-stable   meta/rows/by_day_lane/totals 四键恒定 + totals 自洽
  12 cost-counts-ok-only  fail 行自带的 cost_cny 不计入成本
  13 tier-fallback        缺 tier -> "?" 桶，不丢请求
  14 avg-ms-robust        bool/字符串/负数/缺失 ms 不计入均值，请求仍计数
  15 ts-over-filename     记录 ts 归日优先于文件名（跨午夜/迟到记录）
  16 routing-meta         routing.json 只读读取：sha256/lane 清单/未知 lane
  17 routing-missing      routing 缺失 -> meta.routing.error，导出照常
  18 read-only-unchanged  跑前跑后 logs+routing sha256 不变、目录零新文件
  19 cli-out-csv          --out 写 CSV、stdout 为空、stderr 记 WROTE
  20 cli-out-plus-json    --out 与 --json 同用：文件是 CSV、stdout 是 JSON
  21 cli-stdout-csv       默认 stdout 直接吐 CSV，退出码 0

用法：python -X utf8 test_cost_export.py
退出码 0 = 全部符合预期；2 = 有偏差。

自包含说明：不 import fingerprint——隔离工作区上级无 marketplace.json，
fingerprint.selfcheck 必挂（同 test_review_gate 的处理）；本文件只需
cost_export 的确定性逻辑，故自搭临时夹具、自管退出码。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(HERE, "cost_export.py")
sys.path.insert(0, HERE)
import cost_export as ce  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []

D16 = dt.datetime(2026, 11, 16, 10, 0, 0).timestamp()
D17 = dt.datetime(2026, 11, 17, 10, 0, 0).timestamp()
D18 = dt.datetime(2026, 11, 18, 10, 0, 0).timestamp()

EXPECTED_CSV = (
    "date,lane,requests,success_rate,avg_ms,cost_cny\n"
    "2026-11-16,L1,4,0.7500,275.0,0.008000\n"
    "2026-11-16,L2,1,1.0000,0.0,0.000000\n"
    "2026-11-17,L1,1,1.0000,50.0,0.010000\n"
    "2026-11-18,L1,1,1.0000,10.0,0.020000\n"
)


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def sha256_of(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "<missing>"


def jline(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def make_logs(root: str) -> str:
    """标准夹具：2 天正常账 + 1 条迟到跨日账 + 5 类坏行。"""
    logs = os.path.join(root, "logs")
    os.makedirs(logs, exist_ok=True)
    rows16 = [
        {"ts": D16, "event": "ok", "lane": "L1", "tier": "SIMPLE",
         "ms": 100, "cost_cny": 0.001, "priced": True},
        {"ts": D16 + 1, "event": "ok", "lane": "L1", "tier": "SIMPLE",
         "ms": 300, "cost_cny": 0.002, "priced": True},
        # fail 行故意带 9.9 元：验证"失败不结算"
        {"ts": D16 + 2, "event": "fail", "lane": "L1", "tier": "SIMPLE",
         "ms": 500, "cost_cny": 9.9, "priced": True, "error": "boom"},
        {"ts": D16 + 3, "event": "ok", "lane": "L1", "tier": "COMPLEX",
         "ms": 200, "cost_cny": 0.005, "priced": True},
        # 老格式：无 cost_cny/priced/ms，按 0 元 + unpriced 计
        {"ts": D16 + 4, "event": "ok", "lane": "L2", "tier": "MEDIUM"},
    ]
    rows17 = [
        {"ts": D17, "event": "ok", "lane": "L1", "tier": "SIMPLE",
         "ms": 50, "cost_cny": 0.01, "priced": True},
        {"ts": D17 + 1, "event": "switch", "lane": "L1"},
        # 迟到记录：ts 属 11-18，却写在 11-17 的文件里
        {"ts": D18, "event": "ok", "lane": "L1", "tier": "SIMPLE",
         "ms": 10, "cost_cny": 0.02, "priced": True},
    ]
    lines16 = [jline(r) for r in rows16]
    lines17 = [jline(r) for r in rows17]
    # 坏行：非 JSON / 非对象 / 缺 ts / 脏 ts，各一条，另加空行
    lines17 += [
        "{not json",
        "[1,2,3]",
        jline({"event": "ok", "lane": "L1", "tier": "SIMPLE", "ms": 10}),
        jline({"ts": "oops", "event": "ok", "lane": "L1", "tier": "SIMPLE", "ms": 10}),
        "",
    ]
    with open(os.path.join(logs, "gateway-20261116.jsonl"), "w",
              encoding="utf-8", newline="") as f:
        f.write("\n".join(lines16) + "\n")
    with open(os.path.join(logs, "gateway-20261117.jsonl"), "w",
              encoding="utf-8", newline="") as f:
        f.write("\n".join(lines17) + "\n")
    return logs


def make_routing(root: str, lanes: list[str], name: str = "routing.json") -> str:
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump({"lanes": [{"id": x} for x in lanes]}, f)
    return path


def run_cli(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-X", "utf8", SCRIPT] + args,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=cwd or ROOT, timeout=120)


def pick(rows: list[dict], date: str, lane: str, tier: str | None = None) -> dict:
    for r in rows:
        if r["date"] == date and r["lane"] == lane and (tier is None or r["tier"] == tier):
            return r
    raise AssertionError("缺行: %s/%s/%s" % (date, lane, tier))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cost_export_test_")
    try:
        logs = make_logs(tmp)
        routing = make_routing(tmp, ["L1", "L2"])
        agg = ce.aggregate(logs)
        rows = ce.build_rows(agg["buckets"])
        day_lane = ce.rollup_day_lane(agg["buckets"])

        # 1 聚合基础口径
        detail = ""
        try:
            a = pick(rows, "2026-11-16", "L1", "SIMPLE")
            b = pick(rows, "2026-11-16", "L1", "COMPLEX")
            c = pick(rows, "2026-11-16", "L2", "MEDIUM")
            ok = (a["requests"] == 3 and a["ok"] == 2 and a["fail"] == 1
                  and a["success_rate"] == 0.6667 and a["avg_ms"] == 300.0
                  and a["cost_cny"] == 0.003
                  and b["requests"] == 1 and b["avg_ms"] == 200.0
                  and b["cost_cny"] == 0.005
                  and c["requests"] == 1 and c["avg_ms"] == 0.0
                  and c["cost_cny"] == 0.0 and c["unpriced"] == 1
                  and len(rows) == 5)
            detail = "rows=%d a=%s c=%s" % (len(rows), a, c)
        except AssertionError as e:
            ok, detail = False, str(e)
        check("aggregate-basic", ok, detail)

        # 2 (日,lane) 汇总：成功率重算 + 均值加权
        detail = ""
        try:
            x = [r for r in day_lane if r["date"] == "2026-11-16" and r["lane"] == "L1"][0]
            y = [r for r in day_lane if r["date"] == "2026-11-16" and r["lane"] == "L2"][0]
            ok = (len(day_lane) == 4
                  and x["requests"] == 4 and x["success_rate"] == 0.75
                  and x["avg_ms"] == 275.0 and x["cost_cny"] == 0.008
                  and y["avg_ms"] == 0.0 and y["cost_cny"] == 0.0)
            detail = "day_lane=%s" % day_lane
        except (AssertionError, IndexError) as e:
            ok, detail = False, str(e)
        check("rollup-day-lane", ok, detail)

        # 3 坏行逐类跳过
        sk = agg["skipped"]
        check("bad-lines-skipped",
              agg["rows_read"] == 12 and sk == {"bad_json": 1, "non_dict": 1,
                                               "not_request": 1, "no_ts": 2,
                                               "out_of_window": 0},
              "rows_read=%d skipped=%s" % (agg["rows_read"], sk))

        # 4 空目录
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        e_agg = ce.aggregate(empty)
        e_rows = ce.build_rows(e_agg["buckets"])
        p = run_cli(["--logs-dir", empty, "--routing", routing])
        check("empty-logs-dir",
              e_rows == [] and e_agg["log_dir_exists"] is True and e_agg["log_files"] == []
              and p.returncode == 0 and p.stdout == ce.render_csv([])
              and ce.render_csv([]) == "date,lane,requests,success_rate,avg_ms,cost_cny\n",
              "rc=%d stdout=%r" % (p.returncode, p.stdout[:60]))

        # 5 目录不存在
        gone = os.path.join(tmp, "no_such_dir")
        g_agg = ce.aggregate(gone)
        p = run_cli(["--logs-dir", gone, "--routing", routing])
        check("missing-logs-dir",
              g_agg["log_dir_exists"] is False and ce.build_rows(g_agg["buckets"]) == []
              and p.returncode == 0 and p.stdout.splitlines()[0] == ",".join(ce.CSV_COLUMNS),
              "rc=%d exists=%s" % (p.returncode, g_agg["log_dir_exists"]))

        # 6 0 字节日志
        zdir = os.path.join(tmp, "zero")
        os.makedirs(zdir)
        open(os.path.join(zdir, "gateway-20261117.jsonl"), "w").close()
        z_agg = ce.aggregate(zdir)
        p = run_cli(["--logs-dir", zdir, "--routing", routing])
        check("empty-log-file",
              z_agg["rows_read"] == 0 and ce.build_rows(z_agg["buckets"]) == []
              and p.returncode == 0,
              "rc=%d rows=%d" % (p.returncode, z_agg["rows_read"]))

        # 7 单日过滤
        d1 = ce.aggregate(logs, {dt.date(2026, 11, 17)})
        r1 = ce.build_rows(d1["buckets"])
        p = run_cli(["--logs-dir", logs, "--routing", routing,
                     "--date", "2026-11-17", "--json"])
        cli_rows = json.loads(p.stdout)["rows"]
        check("date-filter-single",
              len(r1) == 1 and r1[0]["date"] == "2026-11-17"
              and len(cli_rows) == 1 and cli_rows[0]["lane"] == "L1"
              and d1["skipped"]["out_of_window"] == 6,
              "rows=%d skipped=%s" % (len(r1), d1["skipped"]))

        # 8 区间过滤（闭区间）
        d2 = ce.aggregate(logs, set(), dt.date(2026, 11, 17), dt.date(2026, 11, 18))
        r2 = ce.build_rows(d2["buckets"])
        check("date-filter-range",
              sorted(x["date"] for x in r2) == ["2026-11-17", "2026-11-18"]
              and d2["skipped"]["out_of_window"] == 5,
              "dates=%s skipped=%s" % ([x["date"] for x in r2], d2["skipped"]))

        # 9 非法日期 -> 退出码 2
        p = run_cli(["--logs-dir", logs, "--routing", routing, "--date", "2026-13-01"])
        check("date-filter-invalid",
              p.returncode == 2 and "非法日期" in p.stderr,
              "rc=%d stderr=%r" % (p.returncode, p.stderr.strip().splitlines()[-1:]))

        # 10 CSV 逐字节
        p = run_cli(["--logs-dir", logs, "--routing", routing])
        parsed = list(csv.reader(io.StringIO(p.stdout)))
        check("csv-schema-exact",
              p.returncode == 0 and p.stdout == EXPECTED_CSV
              and parsed[0] == list(ce.CSV_COLUMNS)
              and all(len(x) == 6 for x in parsed),
              "rc=%d rows=%d" % (p.returncode, len(parsed) - 1))

        # 11 JSON 结构稳定 + totals 自洽
        p = run_cli(["--logs-dir", logs, "--routing", routing, "--json"])
        detail = ""
        try:
            obj = json.loads(p.stdout)
            t = obj["totals"]
            ok = (list(obj) == ["meta", "rows", "by_day_lane", "totals"]
                  and set(obj["meta"]["skipped"]) == {"bad_json", "non_dict",
                                                      "not_request", "no_ts",
                                                      "out_of_window"}
                  and obj["meta"]["unpriced_requests"] == 1
                  and t["requests"] == 7 and t["ok"] == 6 and t["fail"] == 1
                  and t["success_rate"] == 0.8571 and t["cost_cny"] == 0.038
                  # 6 条有合法 ms 的请求共 1160ms；L2 无 ms 不进均值分母
                  and t["avg_ms"] == 193.3
                  and len(obj["rows"]) == 5 and len(obj["by_day_lane"]) == 4)
            detail = "totals=%s" % t
        except (ValueError, KeyError, AssertionError) as e:
            ok, detail = False, "JSON 异常: %s" % e
        check("json-fields-stable", ok, detail)

        # 12 fail 行成本不计
        a = pick(rows, "2026-11-16", "L1", "SIMPLE")
        check("cost-counts-ok-only", a["cost_cny"] == 0.003,
              "cost=%s（fail 行 9.9 元应被排除）" % a["cost_cny"])

        # 13 缺 tier -> "?"
        tdir = os.path.join(tmp, "tier")
        os.makedirs(tdir)
        with open(os.path.join(tdir, "gateway-20261117.jsonl"), "w",
                  encoding="utf-8", newline="") as f:
            f.write(jline({"ts": D17, "event": "ok", "lane": "L9", "ms": 5,
                           "cost_cny": 0.001, "priced": True}) + "\n")
        t_rows = ce.build_rows(ce.aggregate(tdir)["buckets"])
        check("tier-fallback",
              len(t_rows) == 1 and t_rows[0]["tier"] == "?" and t_rows[0]["requests"] == 1,
              "rows=%s" % t_rows)

        # 14 avg_ms 只认合法数值
        mdir = os.path.join(tmp, "ms")
        os.makedirs(mdir)
        with open(os.path.join(mdir, "gateway-20261117.jsonl"), "w",
                  encoding="utf-8", newline="") as f:
            for ms in ("abc", True, -5, None, 40):
                f.write(jline({"ts": D17, "event": "ok", "lane": "LA",
                               "tier": "T", "ms": ms}) + "\n")
        m_rows = ce.build_rows(ce.aggregate(mdir)["buckets"])
        check("avg-ms-robust",
              len(m_rows) == 1 and m_rows[0]["requests"] == 5
              and m_rows[0]["avg_ms"] == 40.0,
              "rows=%s" % m_rows)

        # 15 ts 归日优先于文件名
        late = pick(rows, "2026-11-18", "L1", "SIMPLE")
        check("ts-over-filename",
              late["cost_cny"] == 0.02 and late["avg_ms"] == 10.0
              and "gateway-20261117.jsonl" in agg["log_files"],
              "late=%s files=%s" % (late, agg["log_files"]))

        # 16 routing 只读读取（第二份清单用独立文件，避免覆盖影响只读断言）
        rout_sha = sha256_of(routing)
        rout = ce.load_routing(routing)
        obj = ce.build_json(agg, rout, rows, day_lane, set(), None, None)
        routing_l1 = make_routing(tmp, ["L1"], "routing_l1.json")
        rout1 = ce.load_routing(routing_l1)
        obj1 = ce.build_json(agg, rout1, rows, day_lane, set(), None, None)
        check("routing-meta",
              rout["sha256"] == rout_sha
              and rout["lanes"] == ["L1", "L2"]
              and obj["meta"]["routing"]["unknown_lanes"] == []
              and obj1["meta"]["routing"]["unknown_lanes"] == ["L2"]
              and rout["error"] is None,
              "sha=%s unknown=%s" % ((rout["sha256"] or "")[:12],
                                     obj1["meta"]["routing"]["unknown_lanes"]))

        # 17 routing 缺失不阻断
        miss = ce.load_routing(os.path.join(tmp, "nope.json"))
        obj2 = ce.build_json(agg, miss, rows, day_lane, set(), None, None)
        p = run_cli(["--logs-dir", logs, "--routing", os.path.join(tmp, "nope.json"),
                     "--json"])
        check("routing-missing",
              miss["error"] and miss["sha256"] is None
              and obj2["meta"]["routing"]["error"]
              and p.returncode == 0 and len(json.loads(p.stdout)["rows"]) == 5,
              "rc=%d error=%s" % (p.returncode, miss["error"]))

        # 18 只读不变性：logs + routing sha256 与目录清单跑前跑后一致
        watched = [os.path.join(logs, x) for x in sorted(os.listdir(logs))]
        watched.append(routing)
        before = {x: sha256_of(x) for x in watched}
        listing_before = sorted(os.listdir(logs))
        ce.build_json(ce.aggregate(logs), ce.load_routing(routing),
                      ce.build_rows(agg["buckets"]), day_lane, set(), None, None)
        run_cli(["--logs-dir", logs, "--routing", routing, "--json"])
        out_csv = os.path.join(tmp, "out_readonly.csv")
        run_cli(["--logs-dir", logs, "--routing", routing, "--out", out_csv])
        after = {x: sha256_of(x) for x in watched}
        check("read-only-unchanged",
              before == after and sorted(os.listdir(logs)) == listing_before
              and set(os.listdir(logs)) == {"gateway-20261116.jsonl",
                                            "gateway-20261117.jsonl"},
              "changed=%s" % [x for x in before if before[x] != after.get(x)])

        # 19 --out 写 CSV
        out1 = os.path.join(tmp, "out1.csv")
        p = run_cli(["--logs-dir", logs, "--routing", routing, "--out", out1])
        body = open(out1, encoding="utf-8").read() if os.path.isfile(out1) else ""
        check("cli-out-csv",
              p.returncode == 0 and p.stdout == "" and body == EXPECTED_CSV
              and "WROTE 4 rows" in p.stderr,
              "rc=%d stdout=%r err=%r" % (p.returncode, p.stdout[:40], p.stderr.strip()))

        # 20 --out + --json 同用
        out2 = os.path.join(tmp, "out2.csv")
        p = run_cli(["--logs-dir", logs, "--routing", routing, "--out", out2, "--json"])
        detail = ""
        try:
            obj = json.loads(p.stdout)
            body = open(out2, encoding="utf-8").read()
            ok = (p.returncode == 0 and list(obj) == ["meta", "rows", "by_day_lane", "totals"]
                  and body == EXPECTED_CSV)
            detail = "rc=%d json_keys=%d" % (p.returncode, len(obj))
        except (ValueError, OSError) as e:
            ok, detail = False, str(e)
        check("cli-out-plus-json", ok, detail)

        # 21 默认 stdout 直接给 CSV
        p = run_cli(["--logs-dir", logs, "--routing", routing])
        check("cli-stdout-csv",
              p.returncode == 0 and p.stdout == EXPECTED_CSV and p.stderr == "",
              "rc=%d lines=%d" % (p.returncode, len(p.stdout.splitlines())))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [x for x in RESULTS if x[0] == "FAIL"]
    for status, name, detail in RESULTS:
        print("[%s] %-24s %s" % (status, name, detail))
    total = len(RESULTS)
    print("%d passed / %d total" % (total - len(fails), total))
    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())
