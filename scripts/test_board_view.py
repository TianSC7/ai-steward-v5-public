# -*- coding: utf-8 -*-
"""看板生成器边界用例：空台账/正常台账/坏 JSON/转义/证据缺失/只读约束/实时化。

覆盖（编号延续 W-20261118-004 版的 1-20，21 起为 W-20261118-022 实时化新增）：
  1 空台账 -> 三栏齐全、各栏“无任务”
  2 正常台账 -> 七种状态落对栏
  3 各栏 data-count 与页头总数一致
  4 标题中的 HTML 标签被转义（不产生 <script>）
  5 属性注入（引号/&）被转义
  6 证据未登记 -> 缺失标记
  7 证据路径指向不存在文件 -> 缺失标记 + 保留路径
  8 证据文件存在 -> 已登记
  9 尝试次数显示 attempts/max_attempts
  10 任务无 attempts -> 0/配置上限
  11 台账坏 JSON -> BoardError
  12 tasks 不是列表 -> BoardError
  13 坏配置 -> 宽容降级且不崩
  14 未知状态 -> 归入 Blocked 并标 unknown
  15 中文编码（charset/lang/标题）
  16 无外部资源（无 script/无 http 链接）
  17 原子写：内容一致、无残留临时文件
  18 CLI 成功路径：退出码 0、产物 >2KB
  19 CLI 坏台账：退出码 2、不抛栈、落可读错误页（W-022 起契约变更）
  20 静默标记：超过 stale_sec 才出现
  21 meta refresh=60 内嵌（看板页）
  22 摘要条四字段齐全（空台账零值）
  23 摘要条计数：active/queued/done_today 与台账一致
  24 今日成本显式传入 -> 摘要条显示 ¥ 值
  25 done_today 只数今天完结（跨天/脏时间/无时间均不计）
  26 今日成本现算：ok 行求和、坏行/非 ok/脏数值跳过
  27 网关日志缺失 -> (0, 可读原因)，不抛栈
  28 CLI --now 非法 -> 退出码 2、错误页、无堆栈
  29 错误页转义（消息里的 HTML 不执行）
  30 generate() 摘要字段齐全且落盘一致

用法：python -X utf8 test_board_view.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import board_view as bv  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
NOW = "2026-11-18T12:00:00+08:00"
NOW_DT = None  # main() 里初始化
CFG = {"dispatch": {"max_attempts": 3, "stale_sec": 1800}}


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def raises(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except bv.BoardError:
        return True
    return False


def task(tid: str, state: str, **kw) -> dict:
    base = {
        "id": tid,
        "title": "任务 " + tid,
        "project": "ai-steward",
        "state": state,
        "attempts": 0,
        "created_at": "2026-11-18T08:00:00+08:00",
        "last_check": "2026-11-18T11:58:00+08:00",
    }
    base.update(kw)
    return base


def build(tasks: list[dict], config: dict | None = None, **kw) -> str:
    return bv.build_board({"tasks": tasks}, config if config is not None else CFG,
                          now=bv.parse_now(NOW), **kw)


def section(page: str, key: str) -> str:
    start = page.index('<section class="col %s"' % key)
    end = page.index("</section>", start)
    return page[start:end]


def card(page: str, tid: str) -> str:
    needle = 'data-id="%s"' % tid
    i = page.index(needle)
    start = page.rindex("<article", 0, i)
    end = page.index("</article>", i)
    return page[start:end]


def summary_cell(page: str, key: str) -> str:
    needle = 'data-k="%s"' % key
    i = page.index(needle)
    start = page.rindex("<div", 0, i)
    end = page.index("</div>", i)
    return page[start:end]


def write_gateway_log(folder: str) -> str:
    """写一份今天的网关流水账：ok 行 + 坏行 + 非 ok + 脏数值混合。"""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "gateway-%s.jsonl" % bv.parse_now(NOW).strftime("%Y%m%d"))
    lines = [
        json.dumps({"ts": 1, "event": "ok", "cost_cny": 0.001}),
        json.dumps({"ts": 2, "event": "ok", "cost_cny": 0.002}),
        json.dumps({"ts": 3, "event": "error", "cost_cny": 99.0}),   # 非 ok 不计
        "not json at all",                                            # 坏行跳过
        json.dumps({"ts": 5, "event": "ok", "cost_cny": "oops"}),     # 脏数值跳过
        json.dumps({"ts": 6, "event": "ok"}),                         # 缺字段按 0
        "[1, 2, 3]",                                                  # 非对象行跳过
    ]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return path


def main() -> int:
    global NOW_DT
    NOW_DT = bv.parse_now(NOW)

    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_board_view")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_board_view")

    # 1-3 空台账 / 正常台账 / 计数
    # 空台账页锁定成本 0：断言零值摘要，不依赖机器上当天的真实网关日志
    empty = build([], cost_cny=0.0, cost_note="")
    check("empty-three-columns",
          all(('data-state="%s"' % k) in empty for k in ("working", "blocked", "done"))
          and empty.count("无任务") == 3 and "任务 0" in empty,
          "cols=3 empty=%d" % empty.count("无任务"))

    tasks = [
        task("W-Q", "queued"),
        task("W-D", "dispatched", attempts=1),
        task("W-R", "running", attempts=2, max_attempts=4,
             evidence_path="reports/evidence/W-D.md"),
        task("W-B", "blocked"),
        task("W-F", "failed", attempts=3),
        task("W-OK", "done", evidence_path="__EVIDENCE_OK__"),
        task("W-AB", "abandoned"),
    ]
    tmp = tempfile.mkdtemp(prefix="steward-boardview-")
    try:
        ev = os.path.join(tmp, "W-D.md")
        open(ev, "w", encoding="utf-8").write("【结论】成功\n")
        tasks[2]["evidence_path"] = ev
        tasks[5]["evidence_path"] = ev
        page = build(tasks)

        working, blocked, done = (section(page, k) for k in ("working", "blocked", "done"))
        check("normal-working",
              all(('data-id="%s"' % i) in working for i in ("W-Q", "W-D", "W-R"))
              and 'data-id="W-B"' not in working,
              "working=%d" % working.count("<article"))
        check("normal-blocked",
              'data-id="W-B"' in blocked and 'data-id="W-F"' in blocked
              and 'data-id="W-R"' not in blocked,
              "blocked=%d" % blocked.count("<article"))
        check("normal-done",
              'data-id="W-OK"' in done and 'data-id="W-AB"' in done
              and 'data-id="W-Q"' not in done,
              "done=%d" % done.count("<article"))
        check("counts-consistent",
              'data-count="3"' in working and 'data-count="2"' in blocked
              and 'data-count="2"' in done and "任务 7" in page,
              "working/blocked/done=3/2/2")

        # 4-5 转义
        evil = build([task('W-X"><img src=x onerror=alert(1)>', "queued",
                          title="<script>alert(1)</script>&\"'",
                          project='A&B "quoted"')])
        check("escape-tags",
              "&lt;script&gt;alert(1)&lt;/script&gt;" in evil and "<script>" not in evil
              and "<img src=x" not in evil,
              "script 标签已转义")
        check("escape-attrs",
              "A&amp;B &quot;quoted&quot;" in evil and '"><img' not in evil,
              "属性引号/& 已转义")

        # 6-8 证据
        no_ev = build([task("W-NOEV", "done")])
        check("evidence-empty",
              'data-ev="empty"' in no_ev and "证据缺失（未登记）" in no_ev,
              "empty 标记")
        miss_path = os.path.join(tmp, "not-there.md")
        missing = build([task("W-MISS", "done", evidence_path=miss_path)])
        check("evidence-missing",
              'data-ev="missing"' in missing and "证据缺失（文件未找到）" in missing
              and bv.esc(miss_path) in missing,
              "missing + 保留路径")
        ok_card = card(page, "W-OK")
        check("evidence-ok",
              'data-ev="ok"' in ok_card and "已登记" in ok_card and "缺失" not in ok_card,
              "ok 标记")

        # 9-10 尝试次数
        check("attempts-explicit", "2/4" in card(page, "W-R"), "2/4")
        check("attempts-default", "0/3" in card(page, "W-Q"), "0/3")
        seven = build([task("W-LIM", "running", attempts=1)],
                      {"dispatch": {"max_attempts": 7}})
        check("attempts-config-limit", "1/7" in card(seven, "W-LIM"), "1/7")

        # 11-13 坏输入
        bad = os.path.join(tmp, "bad.json")
        open(bad, "w", encoding="utf-8").write("{not json")
        check("bad-registry-json", raises(bv.load_registry, bad), "BoardError")
        notlist = os.path.join(tmp, "notlist.json")
        open(notlist, "w", encoding="utf-8").write(json.dumps({"tasks": "nope"}))
        check("tasks-not-list", raises(bv.load_registry, notlist), "BoardError")
        cfg, warn = bv.load_config(bad)
        degraded = bv.build_board({"tasks": [task("W-CFG", "queued")]},
                                  {"dispatch": "not-a-dict"}, now=NOW_DT)
        check("bad-config-tolerant",
              cfg == {} and bool(warn) and 'data-id="W-CFG"' in degraded,
              "warn=%s" % warn[:40])

        # 14 未知状态
        unknown = build([task("W-?", "weird")])
        ucard = card(unknown, "W-?")
        check("unknown-state-blocked",
              'data-id="W-?"' in section(unknown, "blocked")
              and "badge unknown" in ucard and ">weird<" in ucard,
              "归入 blocked")

        # 15-16 编码与离线
        check("utf8-chinese",
              '<meta charset="utf-8">' in page and 'lang="zh-CN"' in page
              and "任务台只读看板" in page and "Working 进行中" in page,
              "charset/lang/标题")
        check("no-external",
              "http://" not in page and "https://" not in page and "<script" not in page,
              "无外链/脚本")

        # 17 原子写
        out = os.path.join(tmp, "board.html")
        bv.write_atomic(out, "中文内容\n")
        leftovers = [x for x in os.listdir(tmp) if x.startswith(".board-")]
        check("atomic-write",
              open(out, encoding="utf-8").read() == "中文内容\n" and not leftovers,
              "leftovers=%d" % len(leftovers))

        # 18-19 CLI
        reg_path = os.path.join(tmp, "registry.json")
        cfg_path = os.path.join(tmp, "steward.json")
        open(reg_path, "w", encoding="utf-8").write(json.dumps(
            {"tasks": tasks}, ensure_ascii=False))
        open(cfg_path, "w", encoding="utf-8").write(json.dumps(CFG))
        out2 = os.path.join(tmp, "cli-board.html")
        code = bv.main(["--registry", reg_path, "--config", cfg_path,
                        "--out", out2, "--now", NOW])
        size = os.path.getsize(out2) if os.path.exists(out2) else 0
        check("cli-ok", code == 0 and size > 2048, "exit=%d bytes=%d" % (code, size))

        # 19 坏台账：退出码 2、stderr 可读无堆栈、落完整错误页（W-022 契约）
        bad_out = os.path.join(tmp, "bad-board.html")
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            code_bad = bv.main(["--registry", bad, "--config", cfg_path,
                                "--out", bad_out, "--now", NOW])
        err_text = err_buf.getvalue()
        err_page = open(bad_out, encoding="utf-8").read() if os.path.exists(bad_out) else ""
        check("cli-bad-registry",
              code_bad == 2 and "Traceback" not in err_text and "[ERROR]" in err_text
              and "台账不是合法 JSON" in err_text
              and "任务台看板·生成失败" in err_page and "台账不是合法 JSON" in err_page
              and "<!DOCTYPE html>" in err_page,
              "exit=%d page=%dB" % (code_bad, len(err_page.encode("utf-8"))))

        # 20 静默标记
        old = build([task("W-OLD", "running", last_check="2026-11-18T08:00:00+08:00")])
        fresh = build([task("W-NEW", "running", last_check=NOW)])
        check("stale-badge",
              "静默 &gt; 30 分钟" in card(old, "W-OLD") and "静默" not in card(fresh, "W-NEW"),
              "stale_sec=1800")

        # 21 meta refresh=60（看板页 + 错误页）
        check("meta-refresh",
              'http-equiv="refresh" content="60"' in page
              and 'http-equiv="refresh" content="60"' in err_page
              and 'content="60"' in bv.build_error_page("x", now=NOW_DT),
              "REFRESH_SEC=%d" % bv.REFRESH_SEC)

        # 22 摘要条四字段（空台账零值）
        check("summary-cells",
              all(('data-k="%s"' % k) in empty for k in
                  ("active", "queued", "done_today", "cost"))
              and 'data-active="0"' in empty and 'data-queued="0"' in empty
              and 'data-done-today="0"' in empty and "¥0.0000" in empty,
              "四 cell 齐全")

        # 23 摘要条计数与台账一致（3 进行中 / 1 排队 / 2 今日完结）
        check("summary-counts",
              'data-active="3"' in page and 'data-queued="1"' in page
              and 'data-done-today="2"' in page,
              "active/queued/done_today=3/1/2")

        # 24 今日成本显式传入
        priced = build([task("W-C", "running")], cost_cny=1.2345,
                       cost_note="测试备注")
        check("summary-cost-explicit",
              'data-cost="1.2345"' in priced and "¥1.2345" in priced
              and "测试备注" in priced,
              "¥1.2345 显示")

        # 25 done_today 只数今天完结
        today_tasks = [
            task("W-T1", "done", updated_at="2026-11-18T09:00:00+08:00"),
            task("W-Y1", "done", updated_at="2026-11-17T23:00:00+08:00"),   # 昨天
            task("W-Y2", "abandoned", updated_at="脏时间",
                 evidence_verified_at="2026-11-18T10:00:00+08:00"),          # 退化核验时间
            task("W-N1", "done", updated_at=None, evidence_verified_at=None,
                 created_at=None),                                           # 无时间可依
            task("W-Q2", "queued", updated_at="2026-11-18T09:00:00+08:00"),  # 非完结态
        ]
        dpage = build(today_tasks, cost_cny=0.0)
        check("done-today-filter",
              'data-done-today="2"' in dpage
              and bv.done_today_count({"tasks": today_tasks}, NOW_DT) == 2,
              "跨天/脏时间/无时间均不计")

        # 26 今日成本现算（ok 求和，坏行/非 ok/脏值跳过）
        gw_dir = os.path.join(tmp, "gw")
        write_gateway_log(gw_dir)
        cost, note = bv.today_cost(NOW_DT, logs_dir=gw_dir)
        check("gateway-cost-live",
              abs(cost - 0.003) < 1e-9 and "3 次请求" in note,
              "cost=%.4f note=%s" % (cost, note[:30]))
        gpage = build([task("W-G", "running")], cost_cny=cost, cost_note=note)
        check("gateway-cost-page",
              'data-cost="0.0030"' in gpage and "3 次请求" in gpage,
              "摘要条同步现算值")

        # 27 网关日志缺失 -> (0, 可读原因)
        miss_cost, miss_note = bv.today_cost(NOW_DT, logs_dir=os.path.join(tmp, "no-gw"))
        check("gateway-missing-log",
              miss_cost == 0.0 and "缺失" in miss_note,
              "note=%s" % miss_note[:30])

        # 28 CLI --now 非法 -> 退出码 2 + 错误页 + 无堆栈
        now_out = os.path.join(tmp, "bad-now.html")
        now_buf = io.StringIO()
        with contextlib.redirect_stderr(now_buf):
            code_now = bv.main(["--registry", reg_path, "--config", cfg_path,
                                "--out", now_out, "--now", "not-a-time"])
        now_text = now_buf.getvalue()
        check("cli-bad-now",
              code_now == 2 and "Traceback" not in now_text
              and os.path.exists(now_out) and "生成失败" in open(
                  now_out, encoding="utf-8").read(),
              "exit=%d" % code_now)

        # 29 错误页转义
        epage = bv.build_error_page('<script>alert(1)</script>&"', now=NOW_DT)
        check("error-page-escape",
              "<script>alert(1)</script>" not in epage
              and "&lt;script&gt;alert(1)&lt;/script&gt;" in epage
              and "&amp;" in epage,
              "错误消息同样转义")

        # 30 generate() 摘要字段齐全且落盘一致
        gen_out = os.path.join(tmp, "gen-board.html")
        gen_page, gen_sum = bv.generate(reg_path, cfg_path, gen_out,
                                        now=NOW_DT, logs_dir=gw_dir)
        disk = open(gen_out, encoding="utf-8").read()
        check("generate-summary",
              set(gen_sum) >= {"out", "bytes", "tasks", "counts", "active", "queued",
                               "done_today", "cost_cny", "cost_note"}
              and gen_sum["active"] == 3 and gen_sum["queued"] == 1
              and gen_sum["done_today"] == 2 and abs(gen_sum["cost_cny"] - 0.003) < 1e-9
              and disk == gen_page and gen_sum["bytes"] == len(disk.encode("utf-8"))
              and 'data-cost="0.0030"' in disk,
              "keys=%d active=%d cost=%.4f" % (len(gen_sum), gen_sum["active"],
                                               gen_sum["cost_cny"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-24s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
