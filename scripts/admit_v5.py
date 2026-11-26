# -*- coding: utf-8 -*-
"""V5.0 派发与收口。

从 dsh-graph-autopilot 接到工厂上的规则：
- 路径重叠留在队列，不判失败。
- 前置订单没交付，下游不派发。
- 同家族还在施工就等。
- 收口分成四条去向，原因只留一句话。
- 重派时上代证据过期，旧的通过不算。
- 验收脚本若落在改动范围内，不当作验收：本轮真被改过的验收脚本，质检简报事前单列请质检专门核对
  （factory_runner 装配质检简报时调 scripts_in_scope）；收尾时按声明范围再记一笔 script_trust。
- 同一单多路 PK：未接线。lanes_to_run / pk_enable 只是设计位，内核没有任何地方读它（2026-11-26 审计核实）；
  要做得先解决同单双沙箱的合入、槽位账目和家族代数，别当成现成功能。
"""
from __future__ import annotations

import re


def norm_path(value) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def job_paths(job: dict) -> list[str]:
    raw = job.get("targets")
    if isinstance(raw, str):
        raw = [raw]
    items = list(raw or [])
    one = job.get("target")
    if one:
        items = [one] + items
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        for chunk in str(item).replace("\\", "/").split(","):
            for part in chunk.split("+"):
                rel = norm_path(part)
                if not rel or rel in seen:
                    continue
                seen.add(rel)
                out.append(rel)
    return out


def claim_conflicts(job: dict, holders: list) -> list[str]:
    """其它在制单声明了同一条路径时，返回可读冲突。同一订单不算冲突。"""
    def _key(path) -> str:
        return str(path or "").replace("\\", "/").strip("/").lower()      # Windows 下路径不区分大小写

    mine = str((job or {}).get("order_id") or "")
    targets = {_key(p) for p in job_paths(job or {})}
    if not targets:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for holder in holders or []:
        if not isinstance(holder, dict):
            continue
        other = str(holder.get("order_id") or "")
        if not other or other == mine:
            continue
        hit = [p for p in job_paths(holder) if _key(p) in targets]
        if not hit:
            continue
        text = other + " 占用 " + "、".join(hit)
        if text not in seen:
            seen.add(text)
            found.append(text)
    return found


def _id_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        oid = str(part or "").strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
    return out


def unmet_depends(job: dict, history, running=None) -> list[str]:
    """前置必须已交付。失败、在制、排队都不算交付。"""
    needed = _id_list((job or {}).get("depends_on"))
    if not needed:
        return []
    delivered = set()
    for row in history or []:
        if isinstance(row, dict) and str(row.get("status") or "") == "done":
            oid = str(row.get("order_id") or "")
            if oid:
                delivered.add(oid)
    return [oid for oid in needed if oid not in delivered]


def one_line(text, limit: int = 80) -> str:
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    for sep in ("。", "；"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
            break
    raw = raw.strip(" -")
    if len(raw) > limit:
        return raw[:limit] + "…"
    return raw


def disposition(status, summary: str = "") -> str:
    """评审四去向。完全实现 / 部分实现 / 转驻厂专家 / 未实现打回。"""
    st = str(status or "")
    text = str(summary or "")
    if st == "needs_human":
        return "转驻厂专家"
    if st == "running":
        return "施工中"
    if st == "done":
        if re.search(r"P1\s*[=:：]\s*[1-9]|部分实现|判返工", text):
            return "部分实现"
        return "完全实现"
    if st in ("failed", "infra_error"):
        return "未实现打回"
    return "未通过"


def scripts_in_scope(test_cmd, paths) -> list[str]:
    """验收命令点名的文件里，哪些落在给定范围（声明范围 / 本轮实际改动）内。原样给相对路径，去重，不区分大小写。"""
    cmd = str(test_cmd or "").replace("\\", "/").lower()
    if not cmd:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for path in paths or []:
        rel = norm_path(path)
        if not rel or rel.lower() not in cmd:
            continue
        key = rel.lower()
        if key not in seen:
            seen.add(key)
            out.append(rel)
    return out


def script_in_scope(test_cmd, paths) -> str:
    """收尾标注用的一句话。真正起作用的是质检简报里的事前单列（factory_runner 装配简报时调 scripts_in_scope）。"""
    if scripts_in_scope(test_cmd, paths):
        return "验收脚本落在改动范围内，本轮不把该脚本当验收"
    return ""


def stale_clause(prev: dict) -> str:
    paths = job_paths(prev or {})
    shown = "、".join(paths[:8]) or "上代声明的改动"
    status = str((prev or {}).get("status") or "未知")
    return (
        "\n\n## 过期证据\n\n"
        f"- 上代终态 `{status}` 的通过记录已经过期，不能当作本代验收。\n"
        f"- 必须重新取证：{shown}。\n"
        "- 没点名的文件不要顺手重写。\n"
    )



def live_line(text: str, elapsed_sec: int = 0, role: str = "worker") -> str:
    """施工或质检还在跑时的一句。分钟数跟着走，有新话就接在冒号后面。"""
    who = "质检" if str(role or "") == "verifier" else "施工"
    mins = max(0, int(elapsed_sec or 0) // 60)
    head = f"{who}进行中，已 {mins} 分"
    tail = one_line(text or "")
    if not tail:
        return head
    return one_line(f"{head}：{tail}")


def lanes_to_run(job: dict, free_slots: int = 1) -> int:
    """同一单多路 PK。默认 1。只有 pk_enable 为真才按空槽放开，上限 3。"""
    if not isinstance(job, dict) or not job.get("pk_enable"):
        return 1
    try:
        asked = int(job.get("pk_lanes") or 1)
    except (TypeError, ValueError):
        asked = 1
    room = max(1, int(free_slots or 1))
    return max(1, min(asked, room, 3))


def plan_starts(queued, running, fam_busy, free_slots, family_of, history=None):
    """选出本轮可以开工的单。被挡住的留下原因，不改成失败。

    返回 (primary, extras, holds)。holds 的值写回队列，给面板显示。
    """
    holds: dict[str, str] = {}
    fams = {f for f in (fam_busy or set()) if f}
    virtual = [j for j in (running or []) if isinstance(j, dict)]
    primary = None
    extras: list = []
    slots_left = max(0, int(free_slots or 0))
    for cand in queued or []:
        if not isinstance(cand, dict):
            continue
        oid = str(cand.get("order_id") or "")
        if str(cand.get("expert_hold") or "").strip():
            if oid:
                holds[oid] = "专家停单：" + str(cand.get("expert_hold")).strip()
            continue
        fam = family_of(oid) if family_of else oid
        if fam and fam in fams:
            if oid:
                holds[oid] = "同家族在制"
            continue
        waiting = unmet_depends(cand, history, virtual)
        if waiting:
            if oid:
                holds[oid] = "前置未交付：" + "、".join(waiting)
            continue
        hits = claim_conflicts(cand, virtual)
        if hits:
            if oid:
                holds[oid] = "路径占用：" + "；".join(hits)
            continue
        if slots_left <= 0:
            continue
        if primary is None:
            primary = cand
        else:
            extras.append(cand)
        slots_left -= 1
        if fam:
            fams.add(fam)
        virtual.append(cand)
    return primary, extras, holds
