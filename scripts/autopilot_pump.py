# -*- coding: utf-8 -*-
"""Autopilot Pump v2 — MES/12 绝对无人值守法的执行体（2026-11-22 立法当晚上线）

发起人永久口谕：任何情况不停、不等、不找人。人类只管下单和到点提货。

v2 修正（dry-run 复盘）：
  - 家族最新单按 job_id 时间戳排序（字符串序会选错代）
  - 区分两种 R 后缀：紧贴式 R1..R9 = 真重派代数；连字符式 -R60/-R8617 = 24h 打回免罚返工 Rev 号（并入家族、不计代数）
  - 每轮全家族判定进日志（可溯、防静默缺席）

职责（每轮）：
  1. 挂单自动流转：家族最新单终态 needs_human/failed 且队列无该家族活单
     -> 自动重派 R+1（spec 注入上代死因 + 换道/聚焦P0/最小可交付）
     -> 真代数 >= MAX_GEN 后自动判死（事件留痕；死亡是结论，不是等待）
  2. 断流自检：队列空且无在制 -> 事件留痕（1 小时限频），补单源接入位预留
安全：factory_queue.json 只读；入队走 factory_runner.py enqueue 官方口；
  runner lock 活着则避让；当日成本超闸只判死不重派；幂等（新 order_id 已存在则跳过）。
用法：python -X utf8 autopilot_pump.py [--dry-run]
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
STATE = os.path.join(ROOT, "state")
QF = os.path.join(STATE, "factory_queue.json")
EV = os.path.join(STATE, "events.jsonl")
LOCK = os.path.join(STATE, "factory_runner.lock")
LOG = os.path.join(STATE, "autopilot_pump.log")
SPEC_DIR = os.path.join(STATE, "spec_autopilot")
RUNNER = os.path.join(ROOT, "scripts", "factory_runner.py")
STARVE_MARK = os.path.join(STATE, "autopilot_last_starve.ts")

MAX_GEN = 3          # 真代数上限：原单=R0，可重派至 R3。保持 3：放宽会把历史上所有停在 R3 以前的家族一次性重派
                     # （含 G18——发起人明示重启须其点头）。代死者由工程师按需带种手动复活（--seed-from）。
PER_ROUND = 5        # 单轮最多重派张数（防风暴）
BUDGET_CAP_CNY = 3000.0   # 2026-11-25 发起人口谕：临期 Token 烧完为止，累计烧满 100 亿再议（原 150）
LOCK_FRESH_SEC = 300
MANUAL_FRESH_SEC = 48 * 3600   # 「人下的那一代」只在这么久之内算数：老家族不因为历史上有人重下过就被泵拉起来

DRY = "--dry-run" in sys.argv


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))


def event(order, rnd, phase, summary):
    e = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ts_ms": int(time.time() * 1000),
         "order": order, "round": rnd, "phase": phase, "actor": "自主裁决泵(autopilot)",
         "summary": summary, "files_changed": [], "cost": 0.0}
    with open(EV, "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def runner_busy():
    """G40c：v4.slots 并行道按槽位账本余量判忙（账本缺失/损坏→True 宁可不派；
    切 N=2 后首个 tick 由运维/派发器 kick 一次完成账本 bootstrap）；单道（slots 关）沿用旧锁新鲜度判定（两态等价）。"""
    try:
        import slot_ledger
        parallel = slot_ledger.slots_max() > 1
    except Exception:
        parallel = False                 # 账本/配置读不了：回落单道旧口径，单道行为零变更
    if parallel:
        try:
            d = json.loads(open(os.path.join(slot_ledger.state_dir(), "runner_slots.json"), encoding="utf-8").read())
            return int(d.get("max") or 0) - len(d.get("slots") or {}) <= 0
        except Exception:
            return True                  # 并行道账本缺/坏：宁可不派（G40c 规格）
    try:
        return (time.time() - os.path.getmtime(LOCK)) < LOCK_FRESH_SEC
    except OSError:
        return False


# 2026-11-24 修复：历史上所有 code 单被硬编码 target=scripts/factory_runner.py 掩盖了真实目标。
# 重派继承时若 job 没有 targets 列表（老记录），必须以 spec 里「改动/交付」小节的反引号路径为准，
# 否则明天重派 G38/G39/G40x 仍会带着错误目标再空跑一整代。
_TARGET_RE = re.compile(r"((?:scripts|state|dashboard|commands|hooks|config|knowledge|release|outputs|research|reports|plugins)"
                        r"[\\/][A-Za-z0-9_./\\-]+\.(?:json|html|ps1|css|txt|py|js|md)(?![A-Za-z0-9_]))")


def spec_targets(path):
    """从 spec 全文提取候选目标文件（Gate1 只需命中其一，多声明无害）。"""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    out = []
    for mm in _TARGET_RE.finditer(text):
        rel = mm.group(1).replace("\\", "/")
        if rel not in out:
            out.append(rel)
    return out


def today_cost():
    try:
        m = json.load(open(os.path.join(STATE, "monitor_5min.json"), encoding="utf-8"))
        return float(m.get("daily_cost_cny") or 0)
    except Exception:
        return 0.0


REV_RE = re.compile(r"-R\d{2,}$")            # 24h 打回免罚返工 Rev 号（-R60/-R8617）
GEN_RE = re.compile(r"(?<!-)R(\d+)$")        # 紧贴式真代数（G18R2 -> 2）
TS_RE = re.compile(r"_(\d{8})_(\d{6})")      # job_id 时间戳


def base_of(oid):
    oid = REV_RE.sub("", oid or "")
    return GEN_RE.sub("", oid)


def gen_of(oid):
    m = GEN_RE.search(oid or "")
    return int(m.group(1)) if m else 0


def sort_key(it):
    m = TS_RE.search(str(it.get("job_id", "")))
    if m:
        return int(m.group(1) + m.group(2))
    return 0


def next_free(base, start, hist, limit=None):
    top = MAX_GEN if limit is None else limit
    n = start
    while n <= top and any(it.get("order_id") == "%sR%d" % (base, n) for it in hist):
        n += 1
    return n  # > top 表示无空位


def entry_ts(it):
    """这一代下单的时刻（优先用单号里的时间戳）。"""
    m = TS_RE.search(str((it or {}).get("job_id", "")))
    if m:
        try:
            return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
        except (ValueError, OverflowError):
            pass
    try:
        return float((it or {}).get("enqueued_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def pump_made(it) -> bool:
    """这一代的考卷是不是泵自己写的（泵写的都在 spec_autopilot 下）。"""
    spec = str((it or {}).get("spec_file") or "")
    if not spec:
        return False
    return os.path.normcase(os.path.abspath(spec)).startswith(os.path.normcase(os.path.abspath(SPEC_DIR)).rstrip("\\/") + os.sep)


def family_span(items, hist, base, now=None):
    """(anchor, g, free)：最近一次人下的那一代 / 泵派到的最新一代 / 下一个空位。
    泵的重派代数从 anchor 往后数 MAX_GEN 代。工程师带种手动复活（R4）的家族由此重新有额度；
    没被复活的家族（人下单已太早，anchor=0）行为同旧：R3 之后判死。"""
    now = time.time() if now is None else now
    manual = [gen_of(it.get("order_id", "")) for it in items
              if not pump_made(it) and now - entry_ts(it) <= MANUAL_FRESH_SEC]
    anchor = max(manual) if manual else 0
    g = max([gen_of(it.get("order_id", "")) for it in items if pump_made(it)] or [anchor])
    return anchor, g, next_free(base, g + 1, hist, anchor + MAX_GEN)


def last_verdict_material(last):
    """上一代的材料：(末轮质检判决, 最终结论一句话, 质检开的修改清单, 质检确认的缺陷)。读不到就都是空。"""
    summary, rework, verdict, defects = "", "", "", []
    report = str((last or {}).get("report_json") or "")
    if report and os.path.isfile(report):
        try:
            data = json.load(open(report, encoding="utf-8"))
            summary = str(data.get("final_summary") or "")[:500]
            attempts = data.get("attempts") or []
            if attempts:
                v = attempts[-1].get("verifier_verdict") or {}
                verdict = str(v.get("verdict") or "")
                rework = str(v.get("rework_prompt") or v.get("summary") or "")[:2000]
                defects = [str(d) for d in (v.get("defects") or [])][:10]
        except Exception:
            summary, rework, verdict, defects = "", "", "", []
    return verdict, summary, rework, defects


def architect_note(last, free):
    """规格里点名驻厂外聘专家的那一段。专家在本单沙箱的声明目标里动手（活仓只读、验收不归专家改），施工接着做。
    旧写法让专家「修订验收」「只诊断不改文件」——前者越权，后者等于把问题原样退回。"""
    _verdict, summary, rework, defects = last_verdict_material(last)
    material = "\n".join("- " + d[:300] for d in defects) or "（报告里没有缺陷清单，按上代结论自己判断）"
    if rework:
        material += "\n\n质检开过的修改清单（厂内照着做过、没解决，别原样再抄）：\n" + rework
    return (
        "\n\n---\n## 驻厂外聘专家（R%d）\n\n" % free
        + "本单点名驻厂外聘专家：内核会在开工前请专家先出手——在本单沙箱的声明目标里直接动手解决，活工厂对专家只读。\n"
        + "上代结论：" + summary + "\n\n"
        + "质检确认的缺陷：\n" + material + "\n\n"
        + "- 上代为什么降不下去，由专家读代码复现后判断；施工照专家的「交给施工」做。\n"
        + "- 验收条件不归专家改：认为验收本身做不到或不合理，写进「验收异议」交厂里裁决。\n"
        + "- 只做质检能看完的最小改动。不要交出一份长到被截断的 diff。\n"
        + "- 禁止删除已落地的 _alert_family、line_details、admit_v5。\n"
        + "- 禁止改无关文件。做完即停。\n"
    )


def main():
    os.makedirs(SPEC_DIR, exist_ok=True)
    q = json.load(open(QF, encoding="utf-8"))
    jobs = q.get("jobs", [])
    hist = q.get("history", [])

    fam = {}
    for it in hist:
        fam.setdefault(base_of(it.get("order_id", "")), []).append(it)
    # P4b：jobs 里残留的终态单（典型=cancelled）不再算「活单」，否则家族被永久 live-skip、永远不重派。
    # needs_human 仍算活单（可能等人工签字后 promote），语义不动。runner 侧 reap_terminal_jobs 负责把终态单移入 history。
    live = {base_of(j.get("order_id", "")) for j in jobs
            if str(j.get("status") or "") not in ("cancelled", "done", "failed", "infra_error", "error", "skipped")}
    try:
        mon = json.load(open(os.path.join(STATE, "monitor_5min.json"), encoding="utf-8"))
        if mon.get("running"):
            live.add(base_of(mon["running"]))
    except Exception:
        pass

    busy = runner_busy()
    cost = today_cost()
    plan = []
    for b in sorted(fam):
        items = fam[b]
        if b in live:
            log("FAMILY %s live-skip" % b)
            continue
        last = max(items, key=sort_key)
        st = last.get("status")
        if st not in ("needs_human", "failed"):
            log("FAMILY %s closed (last=%s %s)" % (b, last.get("order_id"), st))
            continue
        anchor, g, free = family_span(items, hist, b)
        act = "dead" if free > anchor + MAX_GEN else ("architect" if st == "needs_human" else "redispatch")
        if anchor and not DRY:
            dead_mark = os.path.join(SPEC_DIR, b + ".dead")      # 人重新下过这一家族：上一次判死的标记作废
            if os.path.exists(dead_mark):
                os.remove(dead_mark)
        log("FAMILY %s (last=%s %s anchor=%d gennow=%d next_free=R%d -> %s)"
            % (b, last.get("order_id"), st, anchor, g, free, act))
        plan.append((act, last, b, free))

    plan.sort(key=lambda item: 0 if item[0] == "architect" else 1)
    dispatched = 0
    for act, last, b, free in plan:
        oid = last.get("order_id", "")
        if act == "dead":
            dead_mark = os.path.join(SPEC_DIR, b + ".dead")
            if os.path.exists(dead_mark):
                log("DEAD %s verdict already recorded, skip re-event" % oid)
                continue
            if not DRY:
                open(dead_mark, "w", encoding="utf-8").write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            msg = "重派代数已达上限 R%d，按 MES/12 自动判死归档（终态结论，非等待）" % MAX_GEN
            log("DEAD %s %s" % (oid, msg))
            if not DRY:
                event(oid, MAX_GEN, "autopilot_dead", msg + "；家族 %s 全代失败留痕可溯" % b)
            continue
        new_oid = "%sR%d" % (b, free)
        if dispatched >= PER_ROUND:
            log("PLAN %s -> %s (本轮配额已满，下轮续派)" % (oid, new_oid))
            continue
        if busy:
            log("SKIP %s -> %s (runner 在跑，本轮避让)" % (oid, new_oid))
            continue
        if cost >= BUDGET_CAP_CNY and act != "architect":
            log("SKIP %s -> %s (当日成本 %.2f 已超闸 %.2f，只判死不重派)" % (oid, new_oid, cost, BUDGET_CAP_CNY))
            continue
        if cost >= BUDGET_CAP_CNY and act == "architect":
            log("ARCHITECT %s -> %s 重派闸 %.2f 已过，架构升级仍放行，施工仍受内核日闸约束" % (oid, new_oid, BUDGET_CAP_CNY))
        if act == "architect":
            title = str(last.get("title") or oid) + "·架构升级R%d" % free
        else:
            title = str(last.get("title") or oid) + "·自动重派R%d" % free
        spec_path = os.path.join(SPEC_DIR, new_oid + ".md")
        body = []
        src_spec = last.get("spec_file") or ""
        if src_spec and os.path.exists(src_spec):
            body.append(open(src_spec, encoding="utf-8", errors="replace").read().strip())
        else:
            body.append("# %s\n\n（原 spec 缺失，按标题目标施工）" % (last.get("title") or oid))
        body.append("\n\n---\n## 自动重派增量（Autopilot R%d · MES/12 绝对无人值守法）\n\n" % free
                    + "- 上代终态：`%s`；本代策略：**换道施工**（若上代通道异常，直接走网关池稳定道）。\n" % last.get("status")
                    + "- 聚焦消灭 P0：P0=0 为唯一硬指标；P1 力争。\n"
                    + "- 最小可交付原则：墙钟内完不成全量，就交付缩小范围的最小可用版本，**禁止空手、禁止等待任何人**。\n"
                    + "- 本单由自主裁决泵生成，无需任何人类参与。\n")
        try:
            import admit_v5
            body.append(admit_v5.stale_clause(last))
        except Exception:
            pass
        if act == "architect":
            body.append(architect_note(last, free))
        else:
            _verdict, _summary, _rework, _defects = last_verdict_material(last)
            if _defects or _rework:
                body.append("\n\n---\n## 上代质检结论（必须逐条销账）\n\n" + "上代终态结论：" + (_summary or "（无）") + "\n\n"
                            + "".join("- [ ] " + d[:300] + "\n" for d in _defects)
                            + ("\n质检开过的修改清单：\n" + _rework + "\n" if _rework else "")
                            + "- 回执里逐条写明每一条是怎么关掉的（命令 + 原始输出）；关不掉的写明原因。\n")
        if not DRY:
            open(spec_path, "w", encoding="utf-8").write("\n".join(body))
        cmd = [sys.executable, "-X", "utf8", RUNNER, "enqueue",
               "--order", new_oid, "--kind", str(last.get("kind") or "code"),
               "--title", title, "--spec-file", spec_path,
               "--repo", str(last.get("repo") or ""),
               "--max-attempts", "2"]
        if oid and oid != new_oid:
            # 带种重下：新一代订单的沙箱先把上一代已提交的成果并进来，工人不必从零重做
            cmd += ["--seed-from", oid]
            if last_verdict_material(last)[0] == "PASS":
                cmd += ["--seed-gates"]          # 上一代双门已过、只差合入：新一代只补双门 + 合入，不再施工
        _tg = last.get("targets") or []
        if not _tg and str(last.get("kind") or "") == "code":
            for _p in (src_spec, spec_path):
                if _p and os.path.exists(_p):
                    _tg = spec_targets(_p)
                    if _tg:
                        log("TARGETS %s derived from spec %s: %s" % (new_oid, os.path.basename(_p), ",".join(_tg)))
                        break
        if not _tg and last.get("target"):
            _tg = [last.get("target")]
        _tg = [str(t).strip() for t in _tg if str(t).strip()]
        if _tg:
            cmd += ["--targets", ",".join(_tg)]
        for flag, key in (("--build-target", "build_target"), ("--test-cmd", "test_cmd")):
            v = str(last.get(key) or "")
            if v:
                cmd += [flag, v]
        if DRY:
            log("DRY would dispatch %s -> %s (spec=%s, targets=%s)"
                % (oid, new_oid, spec_path, ",".join(_tg) or "-"))
            dispatched += 1
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            out = (r.stdout or b"").decode("utf-8", "replace").strip()
            if r.returncode == 0:
                dispatched += 1
                event(new_oid, free, "autopilot_redispatch",
                      "MES/12 自动重派：上代 %s 终态 %s，本代换道+聚焦P0+最小可交付" % (oid, last.get("status")))
                log("DISPATCH %s -> %s OK" % (oid, new_oid))
            else:
                err = (r.stderr or b"").decode("gbk", "replace").strip()[-300:]
                log("DISPATCH %s FAIL rc=%d %s %s" % (new_oid, r.returncode, out[-200:], err))
        except Exception as e:
            log("DISPATCH %s EXC %r" % (new_oid, e))

    if not jobs and not plan and not live:
        now = time.time()
        try:
            last_s = float(open(STARVE_MARK).read().strip() or 0)
        except Exception:
            last_s = 0.0
        if now - last_s > 3600 and not DRY:
            open(STARVE_MARK, "w").write(str(now))
            event("FACTORY", 0, "backlog_starve",
                  "队列空且无在制：供血断流按 MES/12 留痕（永不空等：补单源接入为下一步）")
            log("STARVE recorded")
    log("ROUND done dry=%s busy=%s cost=%.2f plan=%d dispatched=%d" % (DRY, busy, cost, len(plan), dispatched))


if __name__ == "__main__":
    main()
