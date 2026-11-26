# -*- coding: utf-8 -*-
"""夜间摘要边界用例（night_digest.py）。

覆盖 10 个形态（任务卡点名的四类：空台账 / 有在办 / 坏日志行 / 日期滚动，均在内）：
  1 空台账        -> 五段齐全、占位符、exit 0
  2 有在办        -> 在办段列出 queued/dispatched/running，建议含派首单+催办
  3 坏日志行      -> 坏 JSONL 行跳过不崩，成本只算好行
  4 日期滚动      -> --date 换日：任务收口/网关日志/证据清单按日切换
  5 坏台账        -> exit 2，stderr 可读原因，不落半成品
  6 done 当日收口 -> 完成段 + 证据哈希 ✓/✗/· 三态
  7 失败三态      -> failed/abandoned 当日进失败段；昨日收口不进
  8 只读          -> registry/日志/证据 sha256 前后不变，无副产品
  9 五段标题      -> md 必须含 完成/失败/在办/明日建议/成本 五个标题
  10 --json       -> stdout 是合法 JSON 且计数与 md 一致
  11 默认 --now   -> 不传 --now 也能跑（argparse 会对字符串默认值套 type，曾在此翻车）

用法：python -X utf8 test_night_digest.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SCRIPT = os.path.join(HERE, "night_digest.py")
RESULTS: list[tuple[str, str, str]] = []
NOW = "2026-11-18T23:50:00"
DAY_A, DAY_B = "20261117", "20261118"
FIVE = ["## 【完成】", "## 【失败】", "## 【在办】", "## 【明日建议】", "## 【成本】"]


def case(name: str, ok: bool, detail: str) -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))


def iso(day: str, hm: str = "09:00:00") -> str:
    return "%s-%s-%sT%s" % (day[:4], day[4:6], day[6:], hm)


def sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def fresh(base: str, name: str) -> str:
    root = os.path.join(base, name)
    for sub in ("state", "logs", os.path.join("reports", "evidence")):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def put_registry(root: str, tasks: list) -> str:
    p = os.path.join(root, "state", "registry.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, ensure_ascii=False)
    return p


def put_log(root: str, day: str, lines: list[str]) -> str:
    p = os.path.join(root, "logs", "gateway-%s.jsonl" % day)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


def ok_row(cost: float, lane: str = "r4-dsv41") -> str:
    return json.dumps({"ts": 1794932012.0, "event": "ok", "lane": lane, "tier": "COMPLEX",
                       "req_model": "auto", "ms": 100, "tokens_in": 10, "tokens_out": 20,
                       "tokens_cached": 0, "cost_cny": cost, "priced": True, "stream": True},
                      ensure_ascii=False)


def task(tid: str, state: str, day: str, hm: str = "18:30:00", **kw) -> dict:
    t = {"id": tid, "title": "任务 " + tid, "tier": "MEDIUM", "state": state,
         "created_at": iso(day, "09:00:00"), "updated_at": iso(day, hm)}
    t.update(kw)
    return t


def run_digest(root: str, out: str, day: str, now: str = NOW, extra: list | None = None):
    args = [PY, "-X", "utf8", SCRIPT, "--root", root, "--out", out, "--date", day]
    if now:
        args += ["--now", now]
    args += extra or []
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=root)


def read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 用例

def case_empty(base: str) -> None:
    root = fresh(base, "empty")
    put_registry(root, [])
    out = os.path.join(root, "reports", "NIGHT_DIGEST_%s.md" % DAY_B)
    r = run_digest(root, out, DAY_B)
    md = read(out) if os.path.isfile(out) else ""
    case("空台账", r.returncode == 0 and "（当日无完成项）" in md and "（无在办任务" in md,
         "rc=%s err=%s" % (r.returncode, r.stderr.strip()[:80]))


def case_working(base: str) -> None:
    root = fresh(base, "working")
    put_registry(root, [
        task("W-A", "queued", DAY_B, next_action="先跑扫描"),
        task("W-B", "dispatched", DAY_B, last_check=iso(DAY_B, "20:00:00")),
        task("W-C", "running", DAY_B),
        task("W-D", "done", DAY_A),  # 昨日收口，不在今日完成段
    ])
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B)
    md = read(out) if os.path.isfile(out) else ""
    seg = md.split("## 【在办】")[1].split("## 【明日建议】")[0]
    ok = (r.returncode == 0 and all(i in seg for i in ("W-A", "W-B", "W-C"))
          and "W-D" not in seg and "派首单 W-A" in md and "催办" in md and "先跑扫描" in md)
    case("有在办", ok, "rc=%s working=3" % r.returncode)


def case_bad_log(base: str) -> None:
    root = fresh(base, "badlog")
    put_registry(root, [])
    put_log(root, DAY_B, [
        ok_row(0.001),
        "这不是 JSON",
        ok_row(0.002),
        "{坏了一半",
        json.dumps({"event": "fail", "lane": "r4-dsv41", "class": "timeout"}),
        ok_row(0.003),
        "",
    ])
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B, extra=["--json"])
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = {}
    cs = d.get("cost_summary", {})
    md = read(out) if os.path.isfile(out) else ""
    ok = (r.returncode == 0 and cs.get("ok") == 3 and cs.get("fail") == 1
          and cs.get("bad_lines") == 2 and abs(cs.get("cost_cny", 0) - 0.006) < 1e-9
          and "坏行跳过 2" in md)
    case("坏日志行", ok, "rc=%s cost=%s bad=%s" % (r.returncode, cs.get("cost_cny"), cs.get("bad_lines")))


def case_date_roll(base: str) -> None:
    root = fresh(base, "roll")
    put_registry(root, [
        task("W-OLD", "done", DAY_A, "20:00:00"),
        task("W-NEW", "done", DAY_B, "20:00:00"),
    ])
    put_log(root, DAY_A, [ok_row(0.5)])
    put_log(root, DAY_B, [ok_row(0.7)])
    ev_new = os.path.join(root, "reports", "evidence", "W-20261118-777.md")
    with open(ev_new, "w", encoding="utf-8") as f:
        f.write("证据\n")
    os.utime(ev_new, (datetime.datetime(2026, 11, 18, 12, 0).timestamp(),) * 2)
    ev_old = os.path.join(root, "reports", "evidence", "随手写的.md")
    with open(ev_old, "w", encoding="utf-8") as f:
        f.write("旧证据\n")
    os.utime(ev_old, (datetime.datetime(2026, 11, 17, 12, 0).timestamp(),) * 2)

    out_a = os.path.join(root, "reports", "NIGHT_DIGEST_%s.md" % DAY_A)
    out_b = os.path.join(root, "reports", "NIGHT_DIGEST_%s.md" % DAY_B)
    ra = run_digest(root, out_a, DAY_A)
    rb = run_digest(root, out_b, DAY_B)
    mda, mdb = read(out_a), read(out_b)
    ok = (ra.returncode == 0 and rb.returncode == 0
          and "W-OLD" in mda and "W-NEW" not in mda and "¥0.500000" in mda
          and "随手写的" in mda and "W-20261118-777" not in mda
          and "W-NEW" in mdb and "W-OLD" not in mdb and "¥0.700000" in mdb
          and "W-20261118-777" in mdb and "随手写的" not in mdb)
    case("日期滚动", ok, "A=%s B=%s" % (ra.returncode, rb.returncode))


def case_bad_registry(base: str) -> None:
    root = fresh(base, "badreg")
    with open(os.path.join(root, "state", "registry.json"), "w", encoding="utf-8") as f:
        f.write("{这不是 JSON")
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B)
    case("坏台账", r.returncode == 2 and "NIGHT-DIGEST-ERR" in r.stderr
         and not os.path.isfile(out), "rc=%s err=%s" % (r.returncode, r.stderr.strip()[:60]))


def case_done_evidence(base: str) -> None:
    root = fresh(base, "evidence")
    body = "证据正文\n"
    ev = os.path.join(root, "reports", "evidence", "W-20261118-900.md")
    with open(ev, "w", encoding="utf-8", newline="\n") as f:  # newline 固定，避免 Windows CRLF 使哈希对不上
        f.write(body)
    good = hashlib.sha256(body.encode("utf-8")).hexdigest()
    put_registry(root, [
        task("W-OK", "done", DAY_B, evidence_path="reports/evidence/W-20261118-900.md",
             evidence_sha256=good),
        task("W-BADSHA", "done", DAY_B, evidence_path="reports/evidence/W-20261118-900.md",
             evidence_sha256="0" * 64),
        task("W-NOFILE", "done", DAY_B, evidence_path="reports/evidence/不存在的.md"),
        task("W-BLANK", "done", DAY_B),
    ])
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B)
    md = read(out) if os.path.isfile(out) else ""
    seg = md.split("## 【完成】")[1].split("## 【失败】")[0]
    ok = (r.returncode == 0 and seg.count("| W-") == 4 and "✓ " + good[:8] in seg
          and "✗ 哈希不符" in seg and "✗ 缺文件" in seg and "· 未填" in seg)
    case("done当日收口", ok, "rc=%s" % r.returncode)


def case_fail_states(base: str) -> None:
    root = fresh(base, "failstates")
    put_registry(root, [
        task("W-F", "failed", DAY_B, note="超时三次"),
        task("W-AB", "abandoned", DAY_B, note="需求取消"),
        task("W-BLK", "blocked", DAY_A, note="等依赖"),
        task("W-DY", "done", DAY_A),
    ])
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B)
    md = read(out) if os.path.isfile(out) else ""
    seg = md.split("## 【失败】")[1].split("## 【在办】")[0]
    done_seg = md.split("## 【完成】")[1].split("## 【失败】")[0]
    ok = (r.returncode == 0 and "W-F" in seg and "W-AB" in seg and "W-BLK" not in seg
          and "超时三次" in seg and "需求取消" in seg and "（当日无完成项）" in done_seg
          and "复盘今日未收口 2 项" in md)
    case("失败三态", ok, "rc=%s" % r.returncode)


def case_readonly(base: str) -> None:
    root = fresh(base, "readonly")
    reg = put_registry(root, [task("W-RO", "done", DAY_B)])
    log = put_log(root, DAY_B, [ok_row(0.001)])
    ev = os.path.join(root, "reports", "evidence", "W-20261118-901.md")
    with open(ev, "w", encoding="utf-8") as f:
        f.write("x\n")
    before = {p: sha(p) for p in (reg, log, ev)}
    listdir_before = (sorted(os.listdir(os.path.join(root, "state"))),
                      sorted(os.listdir(os.path.join(root, "logs"))),
                      sorted(os.listdir(os.path.join(root, "reports", "evidence"))))
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B)
    after = {p: sha(p) for p in (reg, log, ev)}
    listdir_after = (sorted(os.listdir(os.path.join(root, "state"))),
                     sorted(os.listdir(os.path.join(root, "logs"))),
                     sorted(os.listdir(os.path.join(root, "reports", "evidence"))))
    case("只读", r.returncode == 0 and before == after and listdir_before == listdir_after,
         "rc=%s 哈希%s" % (r.returncode, "一致" if before == after else "被改"))


def case_five_sections(base: str) -> None:
    root = fresh(base, "sections")
    put_registry(root, [task("W-S", "queued", DAY_B)])
    put_log(root, DAY_B, [ok_row(0.001)])
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B)
    md = read(out) if os.path.isfile(out) else ""
    pos = [md.find(h) for h in FIVE]
    cost_seg = md.split("## 【成本】")[1].split("---")[0].strip().splitlines()
    case("五段标题", r.returncode == 0 and all(p >= 0 for p in pos)
         and pos == sorted(pos) and len([l for l in cost_seg if l.startswith("- ")]) <= 5,
         "rc=%s 成本%d行" % (r.returncode, len(cost_seg)))


def case_json(base: str) -> None:
    root = fresh(base, "jsonmode")
    put_registry(root, [task("W-J", "queued", DAY_B)])
    put_log(root, DAY_B, [ok_row(0.0025)])
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B, extra=["--json"])
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = {}
    ok = (r.returncode == 0 and d.get("day") == DAY_B
          and isinstance(d.get("done"), list) and isinstance(d.get("fail"), list)
          and isinstance(d.get("working"), list) and isinstance(d.get("evidence"), list)
          and isinstance(d.get("suggestions"), list) and d.get("out") == out
          and len(d.get("working", [])) == 1
          and d.get("cost_summary", {}).get("cost_cny") == 0.0025)
    case("--json", ok, "rc=%s keys=%s" % (r.returncode, sorted(d.keys())[:4]))


def case_default_now(base: str) -> None:
    root = fresh(base, "defaultnow")
    put_registry(root, [task("W-N", "queued", DAY_B)])
    out = os.path.join(root, "reports", "o.md")
    r = run_digest(root, out, DAY_B, now="")  # 不传 --now：默认取本机当前时间
    md = read(out) if os.path.isfile(out) else ""
    case("默认now", r.returncode == 0 and ("数据日 **%s**" % DAY_B) in md and "生成于 " in md,
         "rc=%s err=%s" % (r.returncode, r.stderr.strip()[:60]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fp = announce("test_night_digest")
    if not fp["ok"]:
        return 2
    arm(180, "test_night_digest")

    tmp = tempfile.mkdtemp(prefix="nd-test-")
    try:
        case_empty(tmp)
        case_working(tmp)
        case_bad_log(tmp)
        case_date_roll(tmp)
        case_bad_registry(tmp)
        case_done_evidence(tmp)
        case_fail_states(tmp)
        case_readonly(tmp)
        case_five_sections(tmp)
        case_json(tmp)
        case_default_now(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [r for r in RESULTS if r[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-14s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails and RESULTS else 2


if __name__ == "__main__":
    raise SystemExit(main())
