# -*- coding: utf-8 -*-
"""五经理制席位稽查器（只读 ZCode 库，不改会话/不删文件）。

正本：dll/factory-blueprint/席位/00-五经理制.md
注册表：plugins/ai-steward/state/seat_registry.json

用法：
  python -X utf8 scripts/factory_seat.py summary
  python -X utf8 scripts/factory_seat.py census [--json]
  python -X utf8 scripts/factory_seat.py verify [--strict] [--json]
  python -X utf8 scripts/factory_seat.py check --seat B-001
  python -X utf8 scripts/factory_seat.py bind --seat B-001 --session sess_xxx
  python -X utf8 scripts/factory_seat.py record-worker --seat W-101 --order C-20261120-001 --session sess_xxx
  python -X utf8 scripts/factory_seat.py check-order --order C-20261120-004
  python -X utf8 scripts/factory_seat.py patrol [--stale-hours 2] [--write 台账.md]
  python -X utf8 scripts/factory_seat.py check-sig --order C-20261120-005 [--tol 10]
  python -X utf8 scripts/factory_seat.py badge-issue --badge sess_x --seat W-105 --dept D --issued-by sess_y [--registry PATH]
  python -X utf8 scripts/factory_seat.py badge-archive --badge sess_x --by sess_y [--registry PATH]

退出码：
  0 合规；3 结构违规（多经理/缺正席/重复厂长）；4 参数/注册表错误；
  verify --strict 且 W=0 时也返 3（员工零上工=P0）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys
import time

sys.dont_write_bytecode = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
REG_PATH = os.path.join(ROOT, "state", "seat_registry.json")
DB_PATH = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
AUDIT_PATH = os.path.join(ROOT, "state", "seat_audit.json")
LOG_PATH = os.path.join(ROOT, "logs", "seat_audit.jsonl")

MANAGER_LIMIT = 5
EXEC_LIMIT = 1

# 经理标题：B经理 / C101经理 / D101生产 / E101质检 / F经理 / 策划经理...
MANAGER_PATTERNS = [
    re.compile(r"(?i)(?:^|[^A-Z0-9])([BCDEF])[-－]?\d{2,3}\s*(?:经理|生产|质检|审计|策划|运保|manager|mgr)"),
    re.compile(r"(?i)(?:^|[^A-Z0-9])([BCDEF])[-－]?\s*经理"),
    re.compile(r"(策划|审计|生产|质检|运保)\s*经理"),
    re.compile(r"(?i)(?:策划|审计|生产|质检|运保)[-－]?\s*([BCDEF])[-－]?\d{2,3}(?![0-9])"),
    re.compile(r"(?i)M-(?:PLAN|SAFETY)"),
]
EXEC_PATTERN = re.compile(r"(?i)(?:A-001|\b厂长\b)")
WORKER_PATTERNS = [
    re.compile(r"(?i)\bW[-－]?\d{2,3}\b"),
    re.compile(r"(?i)\bE-W\d+\b"),
    re.compile(r"员工"),
]


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _ts(epoch_ms) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(epoch_ms) / 1000).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "-"


def load_registry() -> dict:
    with open(REG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("seat_registry.json 不是对象")
    return data


def save_registry(data: dict) -> None:
    data["updated_at"] = _now()
    os.makedirs(os.path.dirname(REG_PATH), exist_ok=True)
    bak = REG_PATH + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists(REG_PATH):
        with open(REG_PATH, "rb") as src, open(bak, "wb") as dst:
            dst.write(src.read())
    tmp = REG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, REG_PATH)


# ---------- F10 工牌签发/收牌（--registry 可指沙箱，缺省活体） ----------

def load_registry_at(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("registry 不是对象：%s" % path)
    return data


def save_registry_at(data: dict, path: str) -> None:
    data["updated_at"] = _now()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    bak = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists(path):
        with open(path, "rb") as src, open(bak, "wb") as dst:
            dst.write(src.read())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _badge_dict(reg: dict) -> dict:
    raw = reg.get("badges")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {(b.get("id") or b.get("badge")): b for b in raw if isinstance(b, dict)}
    return {}


def _active_sessions(reg: dict) -> dict:
    """seats 段 + badges 段：session → 行（05 §一4 经理行即工牌行）。"""
    out = {}
    for s in (reg.get("seats") or []):
        if isinstance(s, dict) and (s.get("session") or "").startswith("sess_"):
            out[s["session"]] = s
    for k, b in _badge_dict(reg).items():
        if isinstance(b, dict):
            out[b.get("badge") or k] = b
    return out


def cmd_badge_issue(args) -> int:
    path = args.registry or REG_PATH
    if not args.badge.startswith("sess_"):
        print("[FAIL] 工牌必须是完整会话 ID（sess_ 前缀）：%s" % args.badge)
        return 3
    dept = (args.dept or "").upper()
    if dept not in ("A", "B", "C", "D", "E", "F"):
        print("[FAIL] 部门必须 A–F：%s" % args.dept)
        return 3
    if not args.issued_by.startswith("sess_"):
        print("[FAIL] --issued-by 必须是签发人工牌（sess_ 前缀）")
        return 3
    try:
        reg = load_registry_at(path)
    except (OSError, ValueError) as e:
        print("[FAIL] 注册表读取失败：%s" % e)
        return 4
    if args.issued_by not in _active_sessions(reg):
        print("[FAIL] 签发人 %s 不在册（seats/badges 均无）" % args.issued_by)
        return 3
    badges = _badge_dict(reg)
    now = _now()
    old = badges.get(args.badge)
    row = {
        "badge": args.badge, "seat": args.seat, "dept": dept,
        "employee": args.employee or args.seat, "order": args.order or "",
        "model": args.model or "", "issued_by": args.issued_by,
        "issued_at": (old or {}).get("issued_at") or now,
        "status": "active",
    }
    if args.expires_at:
        row["expires_at"] = args.expires_at
    if old:
        row["reissued_at"] = now
        row["reissue_count"] = int(old.get("reissue_count") or 0) + 1
    reg["badges"] = badges
    badges[args.badge] = row
    save_registry_at(reg, path)
    print("%s badge %s seat=%s dept=%s employee=%s registry=%s"
          % ("REISSUE" if old else "ISSUED", args.badge, args.seat, dept,
             row["employee"], path))
    return 0


def cmd_badge_archive(args) -> int:
    path = args.registry or REG_PATH
    try:
        reg = load_registry_at(path)
    except (OSError, ValueError) as e:
        print("[FAIL] 注册表读取失败：%s" % e)
        return 4
    badges = _badge_dict(reg)
    b = badges.get(args.badge)
    if not b:
        print("[FAIL] 工牌 %s 不在册" % args.badge)
        return 3
    if not args.by.startswith("sess_") or args.by not in _active_sessions(reg):
        print("[FAIL] 收牌人 %s 不在册" % args.by)
        return 3
    if (b.get("status") or "").lower() == "archived":
        print("ALREADY archived %s" % args.badge)
        return 0
    b["status"] = "archived"
    b["archived_at"] = _now()
    b["archived_by"] = args.by
    reg["badges"] = badges
    save_registry_at(reg, path)
    print("ARCHIVED badge %s by=%s registry=%s" % (args.badge, args.by, path))
    return 0


def db_rows() -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    try:
        cur = con.execute(
            "select id,title,time_created,time_updated,time_archived,time_deleted,last_outcome "
            "from session where time_deleted is null order by time_created"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur]
    finally:
        con.close()


def classify(title: str) -> str:
    t = title or ""
    if any(p.search(t) for p in MANAGER_PATTERNS):
        return "manager"
    if EXEC_PATTERN.search(t):
        return "executive"
    if any(p.search(t) for p in WORKER_PATTERNS):
        return "worker"
    return "other"


def seat_of(title: str) -> str:
    t = title or ""
    # 先认“D101生产 / C101经理 / F经理”这类角色位，避免把订单号 C-004 当席位
    m = re.search(r"(?i)(?:^|[^A-Z0-9])([BCDEF])[-－]?(\d{2,3})\s*(?:经理|生产|质检|审计|策划|运保|manager|mgr)", t)
    if m:
        return "%s-%s" % (m.group(1).upper(), m.group(2))
    # 角色在前：“策划-B005 / 生产 D102”（08:32 测试单漏检形态）
    m = re.search(r"(?i)(?:策划|审计|生产|质检|运保)[-－]?\s*([BCDEF])[-－]?(\d{2,3})(?![0-9])", t)
    if m:
        return "%s-%s" % (m.group(1).upper(), m.group(2))
    m = re.search(r"(?i)(?:^|[^A-Z0-9])([BCDEF])[-－]?\s*经理", t)
    if m:
        return "%s-???" % m.group(1).upper()
    m = re.search(r"(?i)(?:席位|工号|SEAT)\s*[=:：]?\s*([BCDEF])[-－]?(\d{2,3})", t)
    if m:
        return "%s-%s" % (m.group(1).upper(), m.group(2))
    m = re.search(r"(?i)\bW[-－]?(\d{2,3})\b", t)
    if m:
        return "W-%s" % m.group(1)
    if EXEC_PATTERN.search(t):
        return "A-001"
    return "-"


def registry_index(reg: dict) -> tuple[dict, dict]:
    active = {}
    for s in reg.get("seats") or []:
        if isinstance(s, dict) and s.get("seat"):
            active[str(s["seat"])] = s
    retired = {}
    for s in reg.get("retired") or []:
        if isinstance(s, dict) and s.get("seat"):
            retired[str(s["seat"])] = s
    return active, retired


def live_sessions() -> list[dict]:
    rows = db_rows()
    out = []
    for r in rows:
        r = dict(r)
        r["kind"] = classify(r.get("title") or "")
        r["seat"] = seat_of(r.get("title") or "")
        r["archived"] = bool(r.get("time_archived"))
        out.append(r)
    return out


def census(as_json: bool = False, show_all: bool = False) -> int:
    reg = load_registry()
    active_reg, retired_reg = registry_index(reg)
    smap = {}
    for s in list(active_reg.values()) + list(retired_reg.values()):
        if s.get("session"):
            smap[str(s["session"])] = str(s.get("seat"))
    rows = live_sessions()
    for r in rows:
        if r["id"] in smap:
            r["seat"] = smap[r["id"]]
    shown = rows if show_all else [r for r in rows if not r["archived"]]
    groups = {"executive": [], "manager": [], "worker": [], "other": []}
    for r in shown:
        groups[r["kind"]].append(r)
    active_managers = groups["manager"]
    active_workers = groups["worker"]
    summary = {
        "time": _now(),
        "sessions_total": len(shown),
        "sessions_all": len(rows),
        "executive": len(groups["executive"]),
        "manager_total": len(active_managers),
        "manager_seats": sorted({r["seat"] for r in active_managers}),
        "worker_total": len(active_workers),
        "worker_seats": sorted({r["seat"] for r in active_workers}),
        "other": len(groups["other"]),
        "rows": [
            {
                "session": r["id"],
                "title": r["title"],
                "kind": r["kind"],
                "seat": r["seat"],
                "archived": r["archived"],
                "created": _ts(r.get("time_created")),
                "outcome": r.get("last_outcome"),
            }
            for r in shown
        ],
    }
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    print("[CENSUS] %s%s" % (summary["time"], "" if show_all else "（仅未归档）"))
    print("[CENSUS] sessions=%d executive=%d manager=%d worker=%d other=%d"
          % (summary["sessions_total"], summary["executive"], summary["manager_total"],
             summary["worker_total"], summary["other"]))
    print("[CENSUS] manager_seats=%s" % (",".join(summary["manager_seats"]) or "(none)"))
    print("[CENSUS] worker_seats=%s" % (",".join(summary["worker_seats"]) or "(none)"))
    for r in shown:
        flag = "ARCH" if r["archived"] else "LIVE"
        print("  %-4s %-8s %-8s %s | %s" % (flag, r["kind"], r["seat"], r["id"][:20], (r["title"] or "")[:48]))
    return 0


def _iso_to_ms(s: str) -> int:
    try:
        return int(_dt.datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def verify(strict: bool = False, as_json: bool = False) -> int:
    reg = load_registry()
    active, retired = registry_index(reg)
    canonical = {k: v for k, v in active.items() if v.get("kind") == "manager"}
    canonical_sessions = {str(v.get("session")) for v in canonical.values() if v.get("session")}
    retired_sessions = {str(v.get("session")) for v in retired.values() if v.get("session")}
    rows = live_sessions()
    live = [r for r in rows if not r["archived"]]
    managers = [r for r in live if r["kind"] == "manager"]
    workers = [r for r in live if r["kind"] == "worker"]
    execs = [r for r in live if r["kind"] == "executive"]
    manager_ids = {r["id"] for r in managers}

    missing = [seat for seat, s in canonical.items()
               if not s.get("session") or str(s["session"]) not in {r["id"] for r in live}]
    unknown = [r for r in managers if r["id"] not in canonical_sessions and r["id"] not in retired_sessions]
    frozen = [r for r in managers if r["id"] in retired_sessions]
    canon_live = [r for r in managers if r["id"] in canonical_sessions]

    # 注册表更新时间之后新建的经理席：视为“新违规”（旧超额席是冻结基线）
    reg_ms = _iso_to_ms(str(reg.get("updated_at") or ""))
    new_after = [r for r in managers if reg_ms and int(r.get("time_created") or 0) > reg_ms
                 and r["id"] not in canonical_sessions and r["id"] not in retired_sessions]

    violations = []
    if len(canonical) != MANAGER_LIMIT:
        violations.append("manager_limit=%d，应为 %d" % (len(canonical), MANAGER_LIMIT))
    if missing:
        violations.append("正席会话缺失: %s" % ",".join(sorted(missing)))
    if unknown:
        violations.append("注册表外经理会话: %s" % ",".join("%s(%s)" % (r["seat"], r["id"][:12]) for r in unknown))
    if new_after:
        violations.append("注册表更新后新建经理: %s" % ",".join(r["id"][:12] for r in new_after))
    if len(execs) > EXEC_LIMIT:
        violations.append("厂长会话=%d，应≤%d" % (len(execs), EXEC_LIMIT))
    if len(canonical_sessions) != len(canonical):
        violations.append("注册表正席 session 重复/为空")
    if len(manager_ids) < len(canonical):
        violations.append("正席经理未全部在册")

    w_zero = len(workers) == 0
    report = {
        "time": _now(),
        "registry_updated_at": reg.get("updated_at"),
        "manager_limit": MANAGER_LIMIT,
        "canonical_managers": len(canonical),
        "canonical_bound_live": len(canon_live),
        "live_manager_sessions": len(managers),
        "frozen_extra_live": len(frozen),
        "unknown_manager_sessions": len(unknown),
        "new_manager_after_registry": len(new_after),
        "executive_sessions": len(execs),
        "worker_sessions": len(workers),
        "worker_zero": w_zero,
        "violations": violations,
        "ok": not violations and (not strict or not w_zero),
        "seats": [
            {"seat": k, "session": v.get("session"), "status": v.get("status"),
             "live": str(v.get("session")) in {r["id"] for r in rows}}
            for k, v in sorted(canonical.items())
        ],
    }
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    with open(AUDIT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": report["time"], "ok": report["ok"],
                                "violations": violations, "w_zero": w_zero,
                                "managers": len(managers), "workers": len(workers)},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("[VERIFY] %s" % report["time"])
        print("%-4s 正席经理 %d/%d 已绑定" % ("PASS" if len(canon_live) == MANAGER_LIMIT else "FAIL",
                                          len(canon_live), MANAGER_LIMIT))
        print("%-4s 注册表外经理 %d" % ("PASS" if not unknown else "FAIL", len(unknown)))
        print("%-4s 注册表更新后新建经理 %d" % ("PASS" if not new_after else "FAIL", len(new_after)))
        print("%-4s 厂长会话 %d/1" % ("PASS" if len(execs) <= 1 else "FAIL", len(execs)))
        print("%-4s 员工席 %d%s" % ("P0" if w_zero else "PASS", len(workers),
                                    "（W=0，员工零上工）" if w_zero else ""))
        print("      冻结超额经理（基线，不算新违规）=%d；在册经理会话总数=%d"
              % (len(frozen), len(managers)))
        for v in violations:
            print("[FAIL] %s" % v)
        if w_zero:
            print("[P0] W_ZERO：本班无员工席，按 I-01 不得写 delivered")
    return 0 if report["ok"] else 3


def cmd_check(args) -> int:
    reg = load_registry()
    active, retired = registry_index(reg)
    seat = args.seat.upper()
    if seat in retired:
        print("[DENY] %s 是冻结超额席，已并入 %s；不得再派活" % (seat, retired[seat].get("merged_into")))
        return 3
    s = active.get(seat)
    if not s:
        print("[DENY] %s 不在五经理制注册表；只允许 %s" % (seat, ",".join(sorted(active))))
        return 3
    if s.get("kind") == "manager" and s.get("status") != "active":
        print("[DENY] %s 状态=%s，不可派活" % (seat, s.get("status")))
        return 3
    print("[PASS] %s %s session=%s；经理席只复用，不新建" % (seat, s.get("role"), s.get("session")))
    return 0


def cmd_bind(args) -> int:
    reg = load_registry()
    active, _ = registry_index(reg)
    seat = args.seat.upper()
    if seat not in active:
        print("[FAIL] %s 不在正席表，拒绝绑定" % seat)
        return 4
    rows = {r["id"]: r for r in db_rows()}
    if args.session not in rows:
        print("[FAIL] 会话不存在: %s" % args.session)
        return 4
    active[seat]["session"] = args.session
    active[seat]["status"] = "active"
    active[seat]["title"] = (rows[args.session].get("title") or "")[:80]
    save_registry(reg)
    print("[PASS] %s 绑定 %s（已备份旧注册表）" % (seat, args.session))
    return 0


def cmd_record_worker(args) -> int:
    reg = load_registry()
    workers = reg.setdefault("workers", [])
    entry = {
        "seat": args.seat.upper(),
        "order": args.order,
        "session": args.session,
        "status": "active",
        "recorded_at": _now(),
    }
    workers.append(entry)
    save_registry(reg)
    print("[PASS] 登记员工席 %s order=%s session=%s" % (entry["seat"], entry["order"], entry["session"]))
    return 0


# ---- 订单签字闸（发起人 2026-11-20 08:26 令） ----
BLUEPRINT = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "dll", "factory-blueprint")
ORDER_SECTION = re.compile(r"(?m)^#{2,3}\s*(?:员工签字|2\.2\s*人力与工时表|7\s*交付单)[^\n]*$")
PROC_SECTION = re.compile(r"(?m)^#{2,3}\s*(?:流程表|2\.1\s*编成与流转签字表)[^\n]*$")
DELAY_SECTION = re.compile(r"(?m)^#{2,3}\s*延期说明[^\n]*$")
NEXT_H2 = re.compile(r"(?m)^##\s")
DELIV_SECTION = re.compile(r"(?m)^#{2,3}\s*提货清单[^\n]*$")
STATUS_RE = re.compile(r"(?m)^\|\s*当前状态\s*\|\s*([^\n|]*)")
PROGRESS_RE = re.compile(r"(?m)^\|\s*进度\s*\|\s*([^\n|]*)")
DEADLINE_RE = re.compile(r"(?m)^\|\s*交付截止\s*\|\s*([^\n|]*)")
DEADLINE_FALLBACK_RE = re.compile(r"(?m)^\|\s*预期交付\s*\|\s*([^\n|]*)")
DT_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})")
SIG_TIME_RE = re.compile(r"-\s*时间[：:]\s*(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
SIG_FIELDS = ["订单", "任务", "工号", "角色", "结论", "时间", "签字"]
TERMINAL_STATES = {"delivered", "returned", "cancelled", "closed", "作废"}


def _section(text: str, head: re.Pattern) -> str:
    m = head.search(text)
    if not m:
        return ""
    nxt = NEXT_H2.search(text, m.end())
    return text[m.end(): nxt.start() if nxt else len(text)]


def _rows(block: str) -> list[list[str]]:
    out = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        out.append(cells)
    return out


def _col(header: list[str], *keys: str) -> int:
    for i, cell in enumerate(header):
        if any(k in cell for k in keys):
            return i
    return -1


def cmd_check_order(args) -> int:
    code = args.order.strip()
    path = args.file or os.path.join(BLUEPRINT, "orders", code, "order.md")
    if not os.path.isfile(path):
        print("[FAIL] 订单卡不存在: %s" % path)
        return 4
    with open(path, encoding="utf-8") as f:
        text = f.read()

    fails, warns, data = [], [], []
    proc = _section(text, PROC_SECTION)
    sign = _section(text, ORDER_SECTION)

    if not proc:
        fails.append("缺「流程表」：须有 ## 流程表 或「2.1 编成与流转签字表」")
    else:
        rows = _rows(proc)
        header = rows[0] if rows else []
        need = ["流到谁", "员工工号", "会话号", "签字"]
        miss = [k for k in need if _col(header, k) < 0]
        if miss:
            fails.append("流程表表头缺列: %s" % ",".join(miss))
        if not any(re.search(r"(?i)\bW[-－]?\d{2,3}\b", " | ".join(r)) for r in rows[1:] if r):
            warns.append("流程表暂无 W 员工行（未开席前允许，收口前必须补）")

    if not sign:
        fails.append("缺「员工签字」：须有 ## 员工签字 或「2.2 人力与工时表」或「7 交付单」")
    else:
        rows = _rows(sign)
        header = rows[0] if rows else []
        seat_col = _col(header, "工号")
        sess_col = _col(header, "会话号", "任务/会话", "任务/会话号")
        sig_col = _col(header, "签字")
        model_col = _col(header, "模型")
        for r in rows[1:]:
            if not r:
                continue
            line = " | ".join(r)
            m = re.search(r"(?i)\bW[-－]?\d{2,3}\b", line)
            if not m:
                continue
            data.append(r)
            seat = m.group(0).upper().replace("－", "-")
            sess = r[sess_col] if 0 <= sess_col < len(r) else ""
            if not re.search(r"sess_[A-Za-z0-9-]+", sess or line):
                fails.append("%s 签字行缺会话 id（sess_…）" % seat)
            sig = r[sig_col] if 0 <= sig_col < len(r) else ""
            if not sig or sig.strip() in ("—", "-", "待签", "待填", "待开"):
                fails.append("%s 签字行缺签字字段" % seat)
            if model_col >= 0:
                mv = r[model_col] if model_col < len(r) else ""
                if not mv or mv.strip() in ("—", "-", "待定", "待填"):
                    fails.append("%s 签字行缺模型" % seat)
            elif re.search(r"(?i)R4", line) and re.search(r"(?i)STEP", text):
                warns.append("%s 行写 R4，但本单正文出现 STEP——合同模型口径不一致，须一席一模型" % seat)
        if data and model_col < 0:
            warns.append("员工签字区无「模型」列（模板 v0.9 起要求逐席绑定模型）")
        if not data:
            fails.append("员工签字区无 W 员工行")

    for w in warns:
        print("[WARN] %s" % w)
    if fails:
        for f in fails:
            print("[FAIL] %s" % f)
        print("[GATE] 签字闸未过：该单不得写 delivered（只能 internal-done）")
        return 3
    print("[PASS] %s 流程表+员工签字齐全（%d 名员工）" % (code, len(data)))
    return 0


def _parse_status(text: str) -> str:
    m = STATUS_RE.search(text)
    return m.group(1).strip() if m else ""


def _state_word(status: str) -> str:
    m = re.search(r"(?i)delivered|returned|queued|producing|qc|awaiting-closure|delivering|failed|作废|cancelled|closed", status)
    return m.group(0).lower() if m else "?"


def _parse_deadline(text: str):
    # 优先「交付截止」（终验），无该行才退回「预期交付」（可能是首个里程碑）
    m = DEADLINE_RE.search(text) or DEADLINE_FALLBACK_RE.search(text)
    if not m:
        return None
    dm = DT_RE.search(m.group(1))
    if not dm:
        return None
    try:
        return _dt.datetime(*[int(x) for x in dm.groups()])
    except ValueError:
        return None


def cmd_patrol(args) -> int:
    orders_dir = os.path.join(BLUEPRINT, "orders")
    if not os.path.isdir(orders_dir):
        print("[FAIL] orders 目录不存在: %s" % orders_dir)
        return 4
    now = _dt.datetime.now()
    lines = []
    overdue = stalled = nodeadline = nodely = 0
    for code in sorted(os.listdir(orders_dir)):
        if "-TEST-" in code:
            continue  # 测试单不计生产 SLA
        path = os.path.join(orders_dir, code, "order.md")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        status = _parse_status(text)
        state = _state_word(status)
        dead = _parse_deadline(text)
        age_h = (now - _dt.datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 3600.0
        terminal = state in TERMINAL_STATES
        verdict = []
        is_overdue = is_stalled = False
        if dead is None:
            nodeadline += 1
            verdict.append("无交付截止")
        elif not terminal and now > dead:
            is_overdue = True
            overdue += 1
            verdict.append("⚠️超时 %s" % dead.strftime("%m-%d %H:%M"))
        if not terminal and age_h > args.stale_hours:
            is_stalled = True
            stalled += 1
            verdict.append("⚠️停滞 %.1fh" % age_h)
        if (is_overdue or is_stalled) and not _section(text, DELAY_SECTION):
            nodely += 1
            verdict.append("⚠️缺延期说明")
        if not terminal and dead is not None and dead >= now:
            verdict.append("剩 %.1fh" % ((dead - now).total_seconds() / 3600.0))
        lines.append("| %s | %s | %s | %.1fh | %s |" % (
            code, state, dead.strftime("%m-%d %H:%M") if dead else "—", age_h, "；".join(verdict) or "正常"))
    head = ("[PATROL] %s · 停滞阈值 %.1fh · 超时 %d · 停滞 %d · 缺截止 %d · 缺延期说明 %d"
            % (_now(), args.stale_hours, overdue, stalled, nodeadline, nodely))
    out = [head, "| 单号 | 状态 | 交付截止 | 卡面静默 | 判定 |", "|---|---|---|---|---|"] + lines
    for ln in out:
        print(ln)
    if args.write:
        with open(args.write, "a", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        print("[WROTE] %s" % args.write)
    return 3 if (overdue or stalled or nodely) else 0


def cmd_check_sig(args) -> int:
    code = args.order.strip()
    sig_dir = os.path.join(BLUEPRINT, "orders", code, "signatures")
    if not os.path.isdir(sig_dir):
        print("[FAIL] 签字目录不存在: %s" % sig_dir)
        return 4
    files = sorted(f for f in os.listdir(sig_dir) if f.endswith(".md"))
    if not files:
        print("[FAIL] 无签字文件: %s" % sig_dir)
        return 3
    fails = 0
    print("[SIG] %s · %d 件 · 容差 %.0fs" % (code, len(files), args.tol))
    for fn in files:
        p = os.path.join(sig_dir, fn)
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        miss = [k for k in SIG_FIELDS if not re.search(r"-\s*%s[：:]" % re.escape(k), txt)]
        m = SIG_TIME_RE.search(txt)
        if not m:
            print("  %-10s [FAIL] 无时间字段" % fn)
            fails += 1
            continue
        content = _dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        st = os.stat(p)
        fd = _dt.datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_mtime))
        gap = (fd - content).total_seconds()
        tag = "OK"
        if gap > args.tol:
            tag = "FAIL 倒签 %.1fs" % gap
            fails += 1
        elif gap < -args.tol:
            tag = "FAIL 未来时间 %.1fs" % (-gap)
            fails += 1
        if miss:
            tag += " 缺字段:%s" % ",".join(miss)
            fails += 1
        print("  %-10s 文内 %s · 落盘 %s · 差 %+.1fs  %s" % (
            fn, content.strftime("%H:%M:%S"), fd.strftime("%H:%M:%S"), gap, tag))
    print("[%s] %s 签字时间核验：%d 件，失败 %d" % ("PASS" if not fails else "FAIL", code, len(files), fails))
    return 0 if not fails else 3


def cmd_summary(args) -> int:
    reg = load_registry()
    active, retired = registry_index(reg)
    print("[SUMMARY] 五经理制 · 厂长1 + 经理%d" % len([s for s in active.values() if s.get("kind") == "manager"]))
    for seat, s in sorted(active.items()):
        print("  %-6s %-8s %-8s %s" % (seat, s.get("dept"), s.get("role"), s.get("session")))
    print("  frozen: %s" % (",".join(sorted(retired)) or "(none)"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="五经理制席位稽查器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("summary")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("census")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="包含已归档会话")
    p.set_defaults(func=lambda a: census(a.json, a.all))

    p = sub.add_parser("verify")
    p.add_argument("--strict", action="store_true", help="W=0 也返 3（P0）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: verify(a.strict, a.json))

    p = sub.add_parser("check")
    p.add_argument("--seat", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("bind")
    p.add_argument("--seat", required=True)
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("record-worker")
    p.add_argument("--seat", required=True)
    p.add_argument("--order", required=True)
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_record_worker)

    p = sub.add_parser("check-order")
    p.add_argument("--order", required=True)
    p.add_argument("--file", default="", help="直接指定 order.md 路径（测试用）")
    p.set_defaults(func=cmd_check_order)

    p = sub.add_parser("patrol")
    p.add_argument("--stale-hours", type=float, default=2.0, help="非终态卡面静默超过该小时数判停滞")
    p.add_argument("--write", default="", help="追加写入指定台账文件")
    p.set_defaults(func=cmd_patrol)

    p = sub.add_parser("check-sig")
    p.add_argument("--order", required=True)
    p.add_argument("--tol", type=float, default=10.0, help="文内时间与文件落盘时间允许差（秒）")
    p.set_defaults(func=cmd_check_sig)

    p = sub.add_parser("badge-issue", help="F10 发牌：登记 badges 段（--registry 可指沙箱）")
    p.add_argument("--badge", required=True, help="完整会话 ID（sess_…）")
    p.add_argument("--seat", required=True)
    p.add_argument("--dept", required=True, help="A–F")
    p.add_argument("--employee", default="")
    p.add_argument("--order", default="")
    p.add_argument("--model", default="")
    p.add_argument("--issued-by", required=True)
    p.add_argument("--expires-at", default="")
    p.add_argument("--registry", default="", help="缺省活体 state/seat_registry.json")
    p.set_defaults(func=cmd_badge_issue)

    p = sub.add_parser("badge-archive", help="F10 收牌：status→archived（--registry 可指沙箱）")
    p.add_argument("--badge", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--registry", default="")
    p.set_defaults(func=cmd_badge_archive)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
