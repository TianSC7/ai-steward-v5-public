#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台账/审计口径漂移巡检器（只读）· W-20261119-003 / E-056（NEX 道）

① 提交引用漂移：扫描 state/status/*.md 与 state/registry.json 中的短 sha 引用，
   按已知漂移表 DRIFT_MAP 判定：
     7cc8f956（W-055 worktree 旧头）→ 应指向合并后 c0ee2344
     （registry.json:2710 "merge_commit": "c0ee23449067d12e9039b669d692babe10ef40bc"）。
   命中即报 file:line；审计/派单原文类提及走白名单（L1.md:158-159 勾稽记录、
   registry.json 派单 goal 原文），其余卡零误报。
② 归档 idle 口径：旧口径以 session.time_updated 判 idle（batch_archive.py:90-91
   "time_updated<?"），但归档动作会刷新 time_updated（archive_session.py:97
   "UPDATE session SET time_archived=?, time_updated=?"），造成"归档刷新掩盖真实
   闲置"的失真。新口径 = max(message.time_created, model_usage.started_at)，
   两者皆无记录时回退 session.time_created。内置合成 sqlite fixture 复现失真：
   同一会话，旧口径 <30m（漏判）、新口径 >=30m（正确判 idle），断言成立。

边界（红线）：真实库仅以 mode=ro 打开；不写 registry/board、不改状态文件、
不归档、不杀会话、不碰 dll/newCAD。
"""

import json
import os
import re
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_DIR = os.path.join(BASE, "state", "status")
REGISTRY_PATH = os.path.join(BASE, "state", "registry.json")

# 已知漂移表：旧短 sha（8 位前缀）→ 合并后应指向的短 sha
DRIFT_MAP = {"7cc8f956": "c0ee2344"}
# 白名单：文件级。L1.md:158-159 是 L1 的审计勾稽记录（本身在描述该漂移并要求回填），
# registry.json 的派单 goal 原文引用属工单文本，均非台账口径漂移。
WHITELIST_FILES = {"L1.md", "registry.json"}

SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
IDLE_THRESHOLD_MIN = 30
MINUTE_MS = 60 * 1000

DB_PATH = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")


# ---------------------------------------------------------------- ① sha 漂移

def extract_sha_tokens(text):
    """提取文本中的短 sha 形 token（7-40 位十六进制），返回小写集合。"""
    return {m.group(0).lower() for m in SHA_RE.finditer(text)}


def scan_status_drift(status_dir=STATUS_DIR, registry_path=REGISTRY_PATH,
                      drift_map=None, whitelist=None):
    """扫描状态文件与 registry 中的提交引用，返回漂移发现列表。

    每项：{file, line, token, expect, context}；白名单文件跳过（审计/派单原文）。
    """
    drift_map = drift_map or DRIFT_MAP
    whitelist = whitelist if whitelist is not None else WHITELIST_FILES
    drift_prefixes = {k[:8].lower() for k in drift_map}
    findings = []

    targets = []
    if os.path.isdir(status_dir):
        for name in sorted(os.listdir(status_dir)):
            if name.endswith(".md"):
                targets.append((name, os.path.join(status_dir, name)))
    if os.path.exists(registry_path):
        targets.append(("registry.json", registry_path))

    for fname, path in targets:
        if fname in whitelist:
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                seen_prefixes = set()  # 同一行同一漂移前缀只记一处（长短 sha 同现去重）
                for tok in extract_sha_tokens(line):
                    prefix = tok[:8]
                    if prefix in drift_prefixes and prefix not in seen_prefixes:
                        seen_prefixes.add(prefix)
                        findings.append({
                            "file": fname,
                            "line": lineno,
                            "token": tok,
                            "expect": drift_map.get(prefix) or drift_map.get(tok),
                            "context": line.strip()[:120],
                        })
    return findings


def registry_merge_commit(registry_path=REGISTRY_PATH, card="W-20261118-055"):
    """从 registry.json 读指定卡的 merge_commit（合并后权威指向）。文本级只读。"""
    with open(registry_path, "r", encoding="utf-8", errors="replace") as f:
        reg = json.load(f)
    for t in reg.get("tasks", []):
        if t.get("id") == card and t.get("merge_commit"):
            return t["merge_commit"]
    return None


# ---------------------------------------------------------------- ② idle 口径

FIXTURE_SCHEMA = """
CREATE TABLE session(
  id TEXT PRIMARY KEY, title TEXT, time_created INTEGER, time_updated INTEGER,
  time_archived INTEGER, time_deleted INTEGER);
CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER);
CREATE TABLE model_usage(id TEXT, session_id TEXT, started_at INTEGER);
"""


def open_ro(path=DB_PATH):
    """只读打开 sqlite（mode=ro），失败时返回 None。"""
    if not os.path.exists(path):
        return None
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                           uri=True, timeout=5)


def activity_map(con):
    """各会话最后真实活动时间 = max(message.time_created, model_usage.started_at)。

    两者皆无记录时由调用方回退 session.time_created。返回 {session_id: ms}。
    """
    act = {}
    for sid, t in con.execute("SELECT session_id, MAX(time_created) FROM message GROUP BY session_id"):
        act[sid] = max(act.get(sid, 0), t or 0)
    for sid, t in con.execute("SELECT session_id, MAX(started_at) FROM model_usage GROUP BY session_id"):
        act[sid] = max(act.get(sid, 0), t or 0)
    return act


def classify_sessions(con, now_ms=None, threshold_min=IDLE_THRESHOLD_MIN):
    """按新旧双口径给未归档会话分类。返回 dict 列表（含两类 idle 分钟数与判定）。"""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    threshold = threshold_min * MINUTE_MS
    act = activity_map(con)
    out = []
    q = ("SELECT id, title, time_updated, time_created FROM session "
         "WHERE (time_archived IS NULL OR time_archived=0) "
         "AND (time_deleted IS NULL OR time_deleted=0)")
    for sid, title, t_updated, t_created in con.execute(q):
        last_real = act.get(sid) or t_created or 0
        old_idle_min = (now_ms - (t_updated or 0)) / MINUTE_MS
        new_idle_min = (now_ms - last_real) / MINUTE_MS
        out.append({
            "id": sid, "title": title,
            "old_idle_min": round(old_idle_min, 1),
            "new_idle_min": round(new_idle_min, 1),
            "old_idle": old_idle_min * MINUTE_MS >= threshold,
            "new_idle": new_idle_min * MINUTE_MS >= threshold,
        })
    return out


def build_fixture(now_ms=None):
    """合成 sqlite fixture（内存库）：复现"归档刷新 time_updated 掩盖真实闲置"。

    sess_distorted：真实活动停在 45m 前，但归档刷新把 time_updated 顶到 10m 前；
    sess_ctrl_idle / sess_ctrl_active：无刷新的对照组（两口径应判定一致）。
    """
    con = sqlite3.connect(":memory:")
    con.executescript(FIXTURE_SCHEMA)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    rows = [
        # id, title, created, updated(归档刷新后), activity(消息/用量)
        ("sess_distorted",   "失真样例：归档刷新顶新 time_updated", now - 120 * MINUTE_MS, now - 10 * MINUTE_MS, now - 45 * MINUTE_MS),
        ("sess_ctrl_idle",   "对照：真闲置（无刷新）",             now - 120 * MINUTE_MS, now - 45 * MINUTE_MS, now - 45 * MINUTE_MS),
        ("sess_ctrl_active", "对照：真活跃",                       now - 120 * MINUTE_MS, now - 5 * MINUTE_MS,  now - 5 * MINUTE_MS),
    ]
    for sid, title, created, updated, act in rows:
        con.execute("INSERT INTO session VALUES (?,?,?,?,?,?)", (sid, title, created, updated, None, None))
        con.execute("INSERT INTO message VALUES (?,?,?)", ("m_" + sid, sid, act))
        con.execute("INSERT INTO model_usage VALUES (?,?,?)", ("u_" + sid, sid, act))
    con.commit()
    return con


def repro_distortion(now_ms=None, threshold_min=IDLE_THRESHOLD_MIN):
    """在 fixture 上复现失真并断言：旧口径 <30m（漏判）、新口径 >=30m（正确判 idle）。

    返回 sess_distorted 的 (old_idle_min, new_idle_min)。
    """
    con = build_fixture(now_ms)
    try:
        res = {r["id"]: r for r in classify_sessions(con, now_ms=now_ms, threshold_min=threshold_min)}
    finally:
        con.close()
    d = res["sess_distorted"]
    assert d["old_idle_min"] < threshold_min, "旧口径应因 time_updated 被刷新而漏判（<30m）"
    assert d["new_idle_min"] >= threshold_min, "新口径应按真实活动判 idle（>=30m）"
    assert res["sess_ctrl_idle"]["old_idle"] and res["sess_ctrl_idle"]["new_idle"]
    assert not res["sess_ctrl_active"]["old_idle"] and not res["sess_ctrl_active"]["new_idle"]
    return d["old_idle_min"], d["new_idle_min"]


# ---------------------------------------------------------------- 入口

def main(argv=None):
    print("== ① 台账提交引用漂移（state/status/*.md + state/registry.json）==")
    findings = scan_status_drift()
    for f in findings:
        print("DRIFT %s:%d 引用 %s → 应指向 %s" % (f["file"], f["line"], f["token"][:8], f["expect"]))
    print("命中 %d 处（预期 2：W-055.md 两处旧 7cc8f956）" % len(findings))
    print("白名单（合法提及，不记漂移）：L1.md 审计勾稽记录、registry.json 派单 goal 原文")
    merged = registry_merge_commit()
    print("registry 权威指向（W-055 merge_commit）：%s" % (merged or "未登记"))

    print()
    print("== ② 归档 idle 口径复核（真实库 mode=ro）==")
    con = open_ro()
    if con is None:
        print("真实库不存在，跳过实库扫描（fixture 复现见下）")
    else:
        try:
            res = classify_sessions(con)
            old = sum(1 for r in res if r["old_idle"])
            new = sum(1 for r in res if r["new_idle"])
            dist = [r for r in res if not r["old_idle"] and r["new_idle"]]
            print("未归档会话 %d：旧口径 idle %d / 新口径 idle %d / 失真(旧漏判) %d" % (len(res), old, new, len(dist)))
            for r in dist:
                print("  失真 %s %s old=%.1fmin new=%.1fmin" % (r["id"][:24], r["title"][:24], r["old_idle_min"], r["new_idle_min"]))
        finally:
            con.close()

    print()
    print("== ③ 合成 fixture 复现（内存 sqlite）==")
    old_m, new_m = repro_distortion()
    print("sess_distorted：旧口径 idle=%.1fmin（<30 漏判）｜新口径 idle=%.1fmin（>=30 正确）→ 断言成立" % (old_m, new_m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
