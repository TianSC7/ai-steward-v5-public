# -*- coding: utf-8 -*-
"""夜间摘要：台账 + 网关当日日志 + 证据清单 → 一篇交接日报（只读，供无人值守交接）。

为什么需要它：夜班/无人值守时段结束时要交班，接班的人（明早的管家或用户）需要
一页纸回答五件事——今天收口了什么、什么挂了、什么还在办、明天先动哪件、今天花了
多少钱。这些答案散在三处：state/registry.json（任务状态）、
logs/gateway-<数据日>.jsonl（转发流量与成本）、reports/evidence/（证据文件）。
本脚本只读这三处，渲染成 reports/NIGHT_DIGEST_<YYYYMMDD>.md，不写任何状态文件。

口径（与 steward_board.py / ledger_check.py 的状态机对齐）：
  完成 = state=done   且收口时间落在数据日（updated_at→evidence_verified_at→last_check→created_at）
  失败 = state∈{failed,blocked,abandoned} 且收口时间落在数据日（abandoned=放弃，计入未完成）
  在办 = state∈{queued,dispatched,running}（不限日期，队列现状）
  成本 = 网关日志 event=ok 的 cost_cny/tokens 合计（只覆盖管家转发的流量，
         用户窗口直连成本另见 budget_report.py / usage_facts.py）

坏数据不崩：坏 JSONL 行跳过并计数；坏任务项跳过并计数；只有台账整体不可读
（不是 JSON / 缺 tasks 列表）才 exit 2，且 stderr 给可读原因、不落半成品。

日期滚动：--date 指定数据日（默认今天）。"当日"的一切判定——收口归属、网关日志
文件名、证据清单——都以它为准；凌晨生成时传前一天的日期，即可汇总上一个工作日。

用法：
  python -X utf8 scripts/night_digest.py [--date 20261118] [--root <插件根>]
      [--registry <path>] [--out <path>] [--now <ISO8601>] [--top 5] [--json]
退出码：0 生成成功（含网关日志缺失、坏行跳过等降级）；2 参数/台账/写出错误。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DONE_STATES = ("done",)
FAIL_STATES = ("failed", "blocked", "abandoned")
WORKING_STATES = ("queued", "dispatched", "running")
TERMINAL_STATES = DONE_STATES + FAIL_STATES
TIER_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
STALE_MIN = 30  # 在办任务 last_check 超过这么久算"该催了"


def esc(s) -> str:
    """表格单元格转义：竖线会破坏 Markdown 表格，换行会撑破行。"""
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def parse_day(s: str) -> str:
    try:
        return datetime.datetime.strptime(s.strip(), "%Y%m%d").strftime("%Y%m%d")
    except ValueError:
        raise argparse.ArgumentTypeError("日期必须是 YYYYMMDD，收到 %r" % s)


def parse_now(s: str) -> datetime.datetime:
    try:
        v = datetime.datetime.fromisoformat(s.strip())
    except ValueError:
        raise argparse.ArgumentTypeError("--now 必须是 ISO8601，如 2026-11-18T23:50:00+08:00")
    return v.astimezone().replace(tzinfo=None) if v.tzinfo else v


def parse_ts(s) -> datetime.datetime | None:
    """台账时间戳 → 本地 naive datetime；解析不了返回 None（不猜）。"""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        v = datetime.datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    return v.astimezone().replace(tzinfo=None) if v.tzinfo else v


def first_ts(t: dict) -> str:
    """收口/进展时间：updated_at → evidence_verified_at → last_check → created_at。"""
    for k in ("updated_at", "evidence_verified_at", "last_check", "created_at"):
        v = t.get(k)
        if isinstance(v, str) and len(v.strip()) >= 10:
            return v.strip()
    return ""


def day_of(t: dict) -> str:
    return first_ts(t)[:10].replace("-", "")


def fmt_ts(t: dict) -> str:
    v = parse_ts(first_ts(t))
    return v.strftime("%m-%d %H:%M") if v else "?"


def load_registry(path: str) -> tuple[list[dict], list[str]]:
    """返回 (tasks, 警告)。台账整体不可读抛 ValueError；单项畸形跳过并记警告。"""
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError("台账读取失败: %s" % str(e)[:160])
    if not isinstance(reg, dict) or not isinstance(reg.get("tasks"), list):
        raise ValueError("台账结构不对：根必须是对象且 tasks 是列表")
    tasks: list[dict] = []
    warns: list[str] = []
    for i, t in enumerate(reg.get("tasks", [])):
        if not isinstance(t, dict) or not t.get("id"):
            warns.append("跳过第 %d 个任务项（不是对象或缺 id）" % i)
            continue
        tasks.append(t)
    return tasks, warns


def load_gateway_log(root: str, day: str) -> dict:
    """读 logs/gateway-<day>.jsonl：坏行跳过计数，只对 event=ok 累计成本/tokens。"""
    path = os.path.join(root, "logs", "gateway-%s.jsonl" % day)
    gw: dict = {"path": path, "exists": os.path.isfile(path), "error": "", "lines": 0,
                "bad": 0, "ok": 0, "fail": 0, "other": 0, "cost_cny": 0.0,
                "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "unpriced": 0,
                "lanes": {}}
    if not gw["exists"]:
        return gw
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                gw["lines"] += 1
                try:
                    row = json.loads(line)
                except ValueError:
                    gw["bad"] += 1
                    continue
                if not isinstance(row, dict):
                    gw["bad"] += 1
                    continue
                ev = row.get("event")
                if ev == "ok":
                    gw["ok"] += 1
                    c = row.get("cost_cny")
                    if isinstance(c, (int, float)):
                        gw["cost_cny"] += c
                    for k in ("tokens_in", "tokens_out", "tokens_cached"):
                        v = row.get(k)
                        if isinstance(v, (int, float)):
                            gw[k] += v
                    if row.get("priced") is False:
                        gw["unpriced"] += 1
                    lane = str(row.get("lane") or "?")
                    st = gw["lanes"].setdefault(lane, {"cost": 0.0, "n": 0})
                    st["n"] += 1
                    if isinstance(c, (int, float)):
                        st["cost"] += c
                elif ev == "fail":
                    gw["fail"] += 1
                else:
                    gw["other"] += 1
    except OSError as e:
        gw["error"] = str(e)[:120]
    return gw


def scan_evidence(root: str, day: str) -> list[dict]:
    """reports/evidence 里属于数据日的文件：文件名含日期，或 mtime 落在数据日。"""
    d = os.path.join(root, "reports", "evidence")
    out: list[dict] = []
    if not os.path.isdir(d):
        return out
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        by = "name" if day in name else ""
        if not by:
            try:
                mt = datetime.date.fromtimestamp(os.path.getmtime(p)).strftime("%Y%m%d")
            except OSError:
                mt = ""
            by = "mtime" if mt == day else ""
        if by:
            out.append({"name": name, "by": by})
    return out


def evidence_state(t: dict, root: str) -> tuple[str, str]:
    """任务证据核验：✓ 文件在且哈希一致｜△ 文件在未填哈希｜✗ 缺文件/哈希不符｜· 未填路径。"""
    p = str(t.get("evidence_path") or "").strip()
    if not p:
        return "·", "未填"
    full = p if os.path.isabs(p) else os.path.join(root, p)
    if not os.path.isfile(full):
        return "✗", "缺文件"
    want = str(t.get("evidence_sha256") or "").strip()
    if not want:
        return "△", "文件在"
    try:
        with open(full, "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "✗", "读不了"
    return ("✓", want[:8]) if got == want else ("✗", "哈希不符")


def row_terminal(t: dict, root: str) -> dict:
    mark, note = evidence_state(t, root)
    return {"id": str(t.get("id")), "title": esc(t.get("title")), "tier": str(t.get("tier") or "?"),
            "state": str(t.get("state") or "?"), "resolved": fmt_ts(t),
            "evidence": "%s %s" % (mark, note) if note else mark}


def row_working(t: dict) -> dict:
    return {"id": str(t.get("id")), "title": esc(t.get("title")), "tier": str(t.get("tier") or "?"),
            "state": str(t.get("state") or "?"), "created": fmt_ts(t),
            "next": esc(t.get("next_action")) or "—"}


def classify(tasks: list[dict], day: str) -> tuple[list[dict], list[dict], list[dict]]:
    done = [t for t in tasks if t.get("state") in DONE_STATES and day_of(t) == day]
    fail = [t for t in tasks if t.get("state") in FAIL_STATES and day_of(t) == day]
    working = [t for t in tasks if t.get("state") in WORKING_STATES]

    def sort_terminal(ts: list[dict]) -> list[dict]:
        ts = sorted(ts, key=lambda t: str(t.get("id") or ""))
        ts.sort(key=lambda t: parse_ts(first_ts(t)) or datetime.datetime.min, reverse=True)
        return ts

    working.sort(key=lambda t: (TIER_ORDER.get(str(t.get("tier") or "").upper(), 9),
                                str(t.get("created_at") or ""), str(t.get("id") or "")))
    return sort_terminal(done), sort_terminal(fail), working


def build_suggestions(done: list[dict], fail: list[dict], working: list[dict],
                      warns: list[str], gw: dict, root: str, now: datetime.datetime,
                      day: str, top: int) -> list[str]:
    """确定性规则生成明日建议（按优先级截断 top 条），不发明任务。"""
    sug: list[str] = []
    queued = [t for t in working if t.get("state") == "queued"]
    active = [t for t in working if t.get("state") in ("dispatched", "running")]
    if queued:
        f = queued[0]
        sug.append("派首单 %s（%s，tier=%s）；其后还有 %d 项待派，按 tier 高的先走"
                   % (f.get("id"), esc(f.get("title")), f.get("tier") or "?", len(queued) - 1))
    stale = [t for t in active
             if (lc := parse_ts(t.get("last_check"))) and now - lc > datetime.timedelta(minutes=STALE_MIN)]
    if stale:
        sug.append("催办 %d 项超 %d 分钟没动静：%s——先 ReadSession 看会话，再决定中断重派"
                   % (len(stale), STALE_MIN, "、".join(str(t.get("id")) for t in stale[:3])))
    no_plan = [t for t in working if not str(t.get("next_action") or "").strip()]
    if no_plan:
        sug.append("%d 项在办缺 next_action（%s）：补上计划动作，夜班才知道往哪推"
                   % (len(no_plan), "、".join(str(t.get("id")) for t in no_plan[:3])))
    if active and not queued:
        sug.append("在办 %d 项均已派出、无排队：明早先验收回复再决定加派，别重复派单" % len(active))
    if fail:
        sug.append("复盘今日未收口 %d 项：%s（failed 可重试、blocked 看依赖、abandoned 确认是否真要放弃）"
                   % (len(fail), "、".join(str(t.get("id")) for t in fail[:3])))
    due = [t for t in working
           if (rt := parse_ts(t.get("next_retry_at"))) and rt <= now]
    if due:
        sug.append("到点重试：%s（next_retry_at 已过）"
                   % "、".join(str(t.get("id")) for t in due[:3]))
    hot = [t for t in working
           if isinstance(t.get("attempts"), int) and isinstance(t.get("max_attempts"), int)
           and t["attempts"] >= t["max_attempts"]]
    if hot:
        sug.append("尝试次数将尽 %d 项：%s——升 tier 或换模型，别原地重试"
                   % (len(hot), "、".join(str(t.get("id")) for t in hot[:3])))
    bad_ev = [t for t in done if evidence_state(t, root)[0] == "✗"]
    if bad_ev:
        sug.append("补证据：%s（evidence_path 缺文件或 sha256 不符，done 的证据门会卡）"
                   % "、".join(str(t.get("id")) for t in bad_ev[:3]))
    if not gw["exists"]:
        sug.append("网关当日日志缺失（logs/gateway-%s.jsonl）：成本按 ¥0 计，确认网关进程是否在跑" % day)
    elif gw["bad"]:
        sug.append("网关日志有 %d 条坏行已跳过：关注写入端是否被截断/并发写" % gw["bad"])
    if not sug:
        if warns:
            sug.append("台账有 %d 条畸形任务项已跳过：先修台账再谈派单" % len(warns))
        elif not working and not done and not fail:
            sug.append("三项皆空：确认数据日 %s 与台账时间戳同一天（凌晨生成应传前一日）" % day)
        else:
            sug.append("队列与当日皆干净：保持巡检节奏，无需加动作")
    return sug[:max(1, top)]


def load_weekend_glm_summary(root: str, day: str) -> dict:
    """只读 state/v3_budget_ledger.json 获取免费票（weekend-glm）当日与当月累计 tokens。"""
    out = {"daily_tokens": 0, "month_tokens": 0, "daily_requests": 0}
    try:
        p = os.path.join(root, "state", "v3_budget_ledger.json")
        if not os.path.isfile(p):
            return out
        with open(p, encoding="utf-8") as f:
            led = json.load(f)
        if not isinstance(led, dict):
            return out
        day_iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day
        month_iso = day_iso[:7]
        if led.get("date") == day_iso:
            out["daily_tokens"] = int(led.get("daily_weekend_glm_tokens") or 0)
            out["daily_requests"] = int(led.get("daily_weekend_glm_requests") or 0)
        mlt = (led.get("month_lane_tokens") or {}).get(month_iso) or {}
        out["month_tokens"] = int(mlt.get("weekend-glm") or 0)
    except Exception:
        pass
    return out


def cost_lines(gw: dict, day: str, root: str = ROOT) -> list[str]:
    """成本段固定不超过 5 行（任务卡要求"五行内"）。"""
    wk = load_weekend_glm_summary(root, day)
    wk_line = (
        "- 免费票(weekend-glm) vs 网关池用量：免费票 %d tok（本月累计 %d tok）vs 网关池 %d tok"
        % (wk["daily_tokens"], wk["month_tokens"], gw["tokens_in"] + gw["tokens_out"])
    )
    if gw.get("error"):
        return ["- 网关日志读取失败：%s（成本按 ¥0 计）" % gw["error"], wk_line]
    if not gw["exists"]:
        return ["- 无当日网关日志（logs/gateway-%s.jsonl 不存在）：成本按 ¥0 计，确认网关进程是否在跑" % day, wk_line]
    lanes = sorted(gw["lanes"].items(), key=lambda kv: (-kv[1]["cost"], kv[0]))
    lane_s = "；".join("%s ¥%.4f/%d 次" % (k, v["cost"], v["n"]) for k, v in lanes[:3]) or "无"
    return [
        "- 请求 ok=%d fail=%d（其余事件 %d），费用 ¥%.6f；tokens in=%d out=%d cached=%d"
        % (gw["ok"], gw["fail"], gw["other"], gw["cost_cny"],
           gw["tokens_in"], gw["tokens_out"], gw["tokens_cached"]),
        "- lane 明细（前 3）：%s" % lane_s,
        "- 未计价请求 %d（priced=false）；坏行跳过 %d；数据源：logs/gateway-%s.jsonl（%d 行）" % (gw["unpriced"], gw["bad"], day, gw["lines"]),
        wk_line,
        "- 口径：仅管家转发的流量；用户窗口直连成本另见 budget_report.py / usage_facts.py",
    ]


def build(root: str, tasks: list[dict], warns: list[str], day: str,
          now: datetime.datetime, gw: dict, evidence: list[dict], top: int) -> dict:
    done, fail, working = classify(tasks, day)
    wk = load_weekend_glm_summary(root, day)
    gw_desc = ("logs/gateway-%s.jsonl（%d 行，坏行 %d）" % (day, gw["lines"], gw["bad"])
               if gw["exists"] and not gw["error"]
               else ("读取失败：%s" % gw["error"] if gw["error"] else "缺失"))
    return {
        "day": day,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "root": root,
        "registry_rel": (os.path.relpath(os.path.join(root, "state", "registry.json"), root)
                         if os.path.isdir(root) else os.path.join(root, "state", "registry.json")
                         ).replace(os.sep, "/"),
        "task_count": len(tasks),
        "warnings": warns,
        "gateway_desc": gw_desc,
        "evidence": evidence,
        "done": [row_terminal(t, root) for t in done],
        "fail": [dict(row_terminal(t, root), note=esc(t.get("note") or t.get("evidence") or t.get("next_action"))[:50])
                 for t in fail],
        "working": [row_working(t) for t in working],
        "suggestions": build_suggestions(done, fail, working, warns, gw, root, now, day, top),
        "cost": cost_lines(gw, day, root),
        "cost_summary": {"ok": gw["ok"], "fail": gw["fail"], "cost_cny": round(gw["cost_cny"], 6),
                         "tokens_in": gw["tokens_in"], "tokens_out": gw["tokens_out"],
                         "tokens_cached": gw["tokens_cached"], "bad_lines": gw["bad"],
                         "log_exists": gw["exists"],
                         "weekend_glm_tokens": wk["daily_tokens"],
                         "weekend_glm_month_tokens": wk["month_tokens"]},
    }


def render(d: dict) -> str:
    L: list[str] = []
    ap = L.append
    ap("# 夜间摘要 %s（无人值守交接）" % d["day"])
    ap("")
    ap("- 数据日 **%s**｜生成于 %s（本地）｜改判往日：`--date YYYYMMDD`" % (d["day"], d["generated_at"]))
    ap("- 台账 `%s`：tasks=%d%s" % (d["registry_rel"], d["task_count"],
                                  ("；警告：" + "；".join(d["warnings"])) if d["warnings"] else ""))
    ap("- 网关日志：%s" % d["gateway_desc"])
    if d["evidence"]:
        shown = "、".join("`%s`" % e["name"] for e in d["evidence"][:8])
        ap("- 证据清单：`reports/evidence` 当日 %d 个（%s%s）"
           % (len(d["evidence"]), shown, "…" if len(d["evidence"]) > 8 else ""))
    else:
        ap("- 证据清单：`reports/evidence` 当日 0 个")
    ap("- 只读声明：本文件由 `scripts/night_digest.py` 生成，未写 registry / 日志 / 证据；"
       "数据变化后重跑本脚本即可刷新。")
    ap("")
    ap("## 【完成】%d 项（done 且当日收口）" % len(d["done"]))
    ap("")
    if d["done"]:
        ap("| ID | 标题 | tier | 收口 | 证据 |")
        ap("| --- | --- | --- | --- | --- |")
        for r in d["done"]:
            ap("| %s | %s | %s | %s | %s |" % (r["id"], r["title"], r["tier"], r["resolved"], r["evidence"]))
    else:
        ap("（当日无完成项）")
    ap("")
    ap("## 【失败】%d 项（failed/blocked/abandoned 且当日收口）" % len(d["fail"]))
    ap("")
    if d["fail"]:
        ap("| ID | 标题 | state | tier | 收口 | 备注 |")
        ap("| --- | --- | --- | --- | --- | --- |")
        for r in d["fail"]:
            ap("| %s | %s | %s | %s | %s | %s |"
               % (r["id"], r["title"], r["state"], r["tier"], r["resolved"], r["note"] or "—"))
    else:
        ap("（当日无失败/放弃项）")
    ap("")
    ap("## 【在办】%d 项（queued/dispatched/running）" % len(d["working"]))
    ap("")
    if d["working"]:
        ap("| ID | 标题 | state | tier | 更新 | 计划动作 |")
        ap("| --- | --- | --- | --- | --- | --- |")
        for r in d["working"]:
            ap("| %s | %s | %s | %s | %s | %s |"
               % (r["id"], r["title"], r["state"], r["tier"], r["created"], r["next"]))
    else:
        ap("（无在办任务，队列已空）")
    ap("")
    ap("## 【明日建议】")
    ap("")
    for i, s in enumerate(d["suggestions"], 1):
        ap("%d. %s" % (i, s))
    ap("")
    ap("## 【成本】（网关口径）")
    ap("")
    L.extend(d["cost"])
    ap("")
    ap("---")
    ap("交接入口：本文件 → `state/HANDOVER.md`（当前状态）→ `reports/DAILY_REPORT_*.md`（人读日报）。")
    ap("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="夜间摘要：只读台账/网关日志/证据清单，生成交接日报")
    ap.add_argument("--date", type=parse_day, default=datetime.date.today().strftime("%Y%m%d"),
                    help="数据日 YYYYMMDD，默认今天")
    ap.add_argument("--root", default=ROOT, help="插件根（含 state/ logs/ reports/），默认本插件")
    ap.add_argument("--registry", default="", help="台账路径，默认 <root>/state/registry.json")
    ap.add_argument("--out", default="", help="输出 md，默认 <root>/reports/NIGHT_DIGEST_<date>.md")
    ap.add_argument("--now", type=parse_now, default=None, help="覆盖'现在'（ISO8601），测试/复盘用")
    ap.add_argument("--top", type=int, default=5, help="明日建议最多几条，默认 5")
    ap.add_argument("--json", action="store_true", help="stdout 打结构化摘要（人类可读行改 stderr）")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    reg = args.registry or os.path.join(root, "state", "registry.json")
    out = args.out or os.path.join(root, "reports", "NIGHT_DIGEST_%s.md" % args.date)
    now = args.now or datetime.datetime.now()

    try:
        tasks, warns = load_registry(reg)
    except ValueError as e:
        print("NIGHT-DIGEST-ERR %s" % e, file=sys.stderr)
        return 2

    gw = load_gateway_log(root, args.date)
    evidence = scan_evidence(root, args.date)
    d = build(root, tasks, warns, args.date, now, gw, evidence, args.top)
    d["out"] = out
    md = render(d)

    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
    except OSError as e:
        print("NIGHT-DIGEST-ERR 写出失败 %s: %s" % (out, str(e)[:120]), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(d, ensure_ascii=False))
    else:
        print("DIGEST %s done=%d fail=%d working=%d cost_cny=%.6f out=%s"
              % (d["day"], len(d["done"]), len(d["fail"]), len(d["working"]),
                 d["cost_summary"]["cost_cny"], out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
