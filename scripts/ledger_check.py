# -*- coding: utf-8 -*-
"""台账校验（纯函数，供 run_all 与 test_ledger 复用）。

规则：
  1. 根必须是对象，tasks 必须是列表，id 必须唯一
  2. state 只能取 dispatched/running/blocked/done/failed/abandoned
  3. state=done 必须有 evidence_path（或有冻结副本 evidence_frozen_path）
  4. 冻结副本优先核对 evidence_frozen_sha256；无冻结副本时工作树 raw/lf 双口径
     核对 evidence_sha256（防证据被改后不更新）
  5. steer_events 若存在：必须是列表，每条含 ts，interrupt ∈ applied/queued/failed
"""
from __future__ import annotations

import hashlib
import json
import os

LEGAL_STATES = {"queued", "dispatched", "running", "blocked", "done", "failed", "abandoned"}
LEGAL_INTERRUPTS = {"applied", "queued", "failed"}


def file_sha256(path: str, normalize_lf: bool = False) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        data = f.read()
    if normalize_lf:
        data = data.replace(b"\r\n", b"\n")
    h.update(data)
    return h.hexdigest()


def _evidence_ok(t: dict, root: str) -> tuple[bool, str]:
    """done 证据复核：冻结副本优先；无冻结副本时工作树 raw/lf 双口径。"""
    sha = t.get("evidence_sha256")
    fp = t.get("evidence_frozen_path")
    if fp:
        full = fp if os.path.isabs(fp) else os.path.join(root, fp)
        if not os.path.exists(full):
            return False, "冻结证据不存在:%s" % fp
        fsha = t.get("evidence_frozen_sha256") or sha
        if not fsha:
            return True, ""
        try:
            if file_sha256(full) == fsha or file_sha256(full, True) == fsha:
                return True, ""
        except OSError as e:
            return False, "冻结证据读取失败:%s" % e
        return False, "冻结证据哈希不匹配"
    ep = t.get("evidence_path")
    if not ep:
        return False, "done缺evidence_path"
    full = ep if os.path.isabs(ep) else os.path.join(root, ep)
    if not os.path.exists(full):
        return False, "证据文件不存在:%s" % ep
    if sha:
        try:
            if file_sha256(full) == sha or file_sha256(full, True) == sha:
                return True, ""
        except OSError as e:
            return False, "证据读取失败:%s" % e
        return False, "证据哈希不匹配"
    return True, ""


def check(reg_path: str, root: str) -> tuple[bool, str]:
    """返回 (ok, 摘要)。任何结构/证据/哈希问题都判失败，不静默放行。"""
    try:
        reg = json.load(open(reg_path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, "台账读取失败: %s" % str(e)[:120]
    if not isinstance(reg, dict):
        return False, "台账根不是对象"
    tasks = reg.get("tasks")
    if not isinstance(tasks, list):
        return False, "tasks 不是列表"

    problems: list[str] = []
    ids: list[str] = []
    for t in tasks:
        if not isinstance(t, dict):
            problems.append("任务项不是对象")
            continue
        tid = t.get("id") or "?"
        ids.append(str(tid))
        if t.get("state") not in LEGAL_STATES:
            problems.append("%s 非法state=%r" % (tid, t.get("state")))
        if t.get("state") == "done":
            ok, why = _evidence_ok(t, root)
            if not ok:
                problems.append("%s %s" % (tid, why))
        evs = t.get("steer_events")
        if evs is not None:
            if not isinstance(evs, list):
                problems.append("%s steer_events 不是列表" % tid)
            else:
                for i, ev in enumerate(evs):
                    if not isinstance(ev, dict) or not ev.get("ts"):
                        problems.append("%s steer_events[%d] 缺ts" % (tid, i))
                    elif ev.get("interrupt") not in LEGAL_INTERRUPTS:
                        problems.append("%s steer_events[%d] 非法interrupt=%r"
                                        % (tid, i, ev.get("interrupt")))
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        problems.append("重复id:" + ",".join(dup))

    ok = not problems
    detail = "tasks=%d" % len(tasks)
    if problems:
        detail += " " + " | ".join(problems[:6])
    return ok, detail
