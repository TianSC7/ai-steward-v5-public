# -*- coding: utf-8 -*-
"""调度引擎边界用例：并发/依赖/超时升级/证据/预算不能形同虚设。

覆盖：
  1 空台账 -> idle
  2 queued 可派 + worker prompt 六段齐全（含“不停”）
  3 并发上限：active 满 -> 不派
  4 优先级：P0 先派
  5 依赖未完成 -> 不派；依赖完成后 -> 派
  6 依赖失败 -> replan
  7 在办超时 pulls=0 -> check/nudge
  8 pulls=1 -> steer
  9 pulls=2 -> replace
  10 pulls=3 -> split
  11 blocked -> unblock + 诊断清单
  12 failed 未到上限 -> replace
  13 failed 到上限 -> split
  14 done 缺证据 -> reject-done
  15 done 证据有效 -> 不产生动作
  16 预算到顶 -> budget，不派新单
  17 重试退避未到 -> 不派
  18 长时间零进展 -> progress-alert
  19 digest 计数
  20 依赖不存在 -> replan
  21-28 写码任务隔离工作区（建/复用/跳过/失败回退/异常安全/验收清理）
  29-35 worker 纪律三条机械强制（prompt 三闸 / 主仓他人活跃 / 主仓脏 / 隔离跳过 /
        离场标记 verify 打回 / prompt 自检）

用法：python -X utf8 test_cycle.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import steward_cycle as sc  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
NOW = sc.now_dt("2026-11-18T12:00:00+08:00")


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def cfg(**kw) -> dict:
    base = {"max_running": 3, "max_running_per_project": 1, "stale_sec": 1800,
            "max_attempts": 3, "max_pulls": 3, "daily_budget_cny": 0,
            "idle_alert_sec": 7200, "work_start": "08:00", "work_end": "20:00",
            "project_map": {"mate-lib-ai": "newcad", "cdn": "cdn"}}
    base.update(kw)
    return {"dispatch": base}


def task(tid: str, state: str = "queued", **kw) -> dict:
    base = {
        "id": tid, "title": "任务 %s" % tid, "goal": "把 %s 做完" % tid,
        "acceptance": "pytest 全绿", "project": "mate-lib-ai", "priority": 1,
        "state": state, "attempts": 0, "max_attempts": 3, "pulls": 0,
        "created_at": "2026-11-18T08:00:00+08:00",
        "last_progress_at": "2026-11-18T11:50:00+08:00",
        "sessionId": "sess_%s" % tid, "depends_on": [], "steer_events": [],
        "evidence_path": None, "evidence_sha256": None,
    }
    base.update(kw)
    return base


def types(result: dict) -> list[str]:
    return [a["type"] for a in result["actions"]]


def actions(result: dict, kind: str) -> list[dict]:
    return [a for a in result["actions"] if a["type"] == kind]


class FakeGit:
    """可控 git 探针：记录调用，返回配置的仓内/改动状态。"""

    def __init__(self, inside=True, status_lines=()):
        self.calls = []
        self.inside = inside
        self.status_lines = list(status_lines)

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        if args[:1] == ["rev-parse"]:
            return (0, "true") if self.inside else (128, "")
        if args[:1] == ["status"]:
            return 0, "\n".join(self.status_lines)
        return 0, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_cycle")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_cycle")

    # 1 empty：工作时段 -> queue-empty；非工作时段 -> idle
    r = sc.build({"tasks": []}, cfg(), NOW, None, use_kb=False)
    check("queue-empty-workhours", types(r) == ["queue-empty"], str(types(r)))
    r = sc.build({"tasks": []}, cfg(queue_empty_policy="stop"), NOW, None, use_kb=False)
    check("queue-empty-stop", types(r) == ["idle"], str(types(r)))
    NIGHT = sc.now_dt("2026-11-18T22:30:00+08:00")
    r = sc.build({"tasks": []}, cfg(), NIGHT, None, use_kb=False)
    check("idle-offhours", types(r) == ["idle"], str(types(r)))

    # 2 dispatch + prompt
    r = sc.build({"tasks": [task("W-1")]}, cfg(), NOW, None, use_kb=False)
    d = actions(r, "dispatch")
    prompt = d[0]["prompt"] if d else ""
    check("dispatch", len(d) == 1 and d[0]["model"] == "new-provider-7/auto"
          and d[0]["zcode_project"] == "newcad",
          str(types(r)))
    check("prompt-sections", all(x in prompt for x in ("【任务】", "【边界】", "【验收】", "【不停】", "【交付】")),
          prompt[:60].replace("\n", " | "))

    # 3 concurrency cap
    r = sc.build({"tasks": [task("A", "running"), task("B", "dispatched"), task("C")]},
                 cfg(max_running=2), NOW, None, use_kb=False)
    check("concurrency-cap", "dispatch" not in types(r) and r["digest"]["slots"] == 0,
          "slots=%s" % r["digest"]["slots"])

    # 4 priority
    r = sc.build({"tasks": [task("P1", priority=1), task("P0", priority=0)]},
                 cfg(max_running=1), NOW, None, use_kb=False)
    d = actions(r, "dispatch")
    check("priority", len(d) == 1 and d[0]["task_id"] == "P0",
          str([x["task_id"] for x in d]))

    # 4b 同项目并发上限（默认 1，防两个写手踩同一工作区）
    r = sc.build({"tasks": [task("A1"), task("A2")]}, cfg(), NOW, None, use_kb=False)
    check("per-project-cap", len(actions(r, "dispatch")) == 1,
          str([x["task_id"] for x in actions(r, "dispatch")]))
    r = sc.build({"tasks": [task("A1", project="p1"), task("A2", project="p2")]},
                 cfg(), NOW, None, use_kb=False)
    check("cross-project-parallel", len(actions(r, "dispatch")) == 2,
          str([x["task_id"] for x in actions(r, "dispatch")]))

    # 5 dependency
    r = sc.build({"tasks": [task("A", depends_on=["B"]), task("B")]},
                 cfg(), NOW, None, use_kb=False)
    check("dep-blocked", not any(x["task_id"] == "A" for x in actions(r, "dispatch")),
          str([x["task_id"] for x in actions(r, "dispatch")]))
    r = sc.build({"tasks": [task("A", depends_on=["B"]), task("B", "done")]},
                 cfg(), NOW, None, use_kb=False)
    check("dep-done", any(x["task_id"] == "A" for x in actions(r, "dispatch")))

    # 6 bad dep
    r = sc.build({"tasks": [task("A", depends_on=["B"]), task("B", "failed")]},
                 cfg(), NOW, None, use_kb=False)
    check("dep-failed-replan", any(x["task_id"] == "A" for x in actions(r, "replan")))
    r = sc.build({"tasks": [task("A", depends_on=["NOPE"])]}, cfg(), NOW, None, use_kb=False)
    check("dep-missing-replan", any(x["task_id"] == "A" for x in actions(r, "replan")))

    # 7-10 escalation ladder
    for pulls, level in ((0, "nudge"), (1, "steer"), (2, "replace"), (3, "split")):
        old = "2026-11-18T10:00:00+08:00"
        r = sc.build({"tasks": [task("S", "running", pulls=pulls, last_progress_at=old)]},
                     cfg(), NOW, None, use_kb=False)
        c = actions(r, "check")
        check("ladder-%d" % pulls, len(c) == 1 and c[0]["level"] == level,
              c[0]["level"] if c else "none")

    # 10b 有证据 -> verify（优先于超时）
    tmp_ev = tempfile.mkdtemp(prefix="steward-cycle-ev-")
    try:
        ev = os.path.join(tmp_ev, "ev.md")
        open(ev, "w", encoding="utf-8").write("【结论】成功\nEXIT=PASS\n")
        old = "2026-11-18T10:00:00+08:00"
        r = sc.build({"tasks": [task("V", "running", evidence_path=ev,
                                     last_progress_at=old)]},
                     cfg(), NOW, None, use_kb=False)
        check("evidence-verify", bool(actions(r, "verify")) and not actions(r, "check"),
              str(types(r)))
    finally:
        shutil.rmtree(tmp_ev, ignore_errors=True)

    # 11 blocked unblock
    r = sc.build({"tasks": [task("B", "blocked", last_error="权限被拒")]},
                 cfg(), NOW, None, use_kb=False)
    u = actions(r, "unblock")
    check("unblock", len(u) == 1 and len(u[0]["checklist"]) >= 4
          and u[0]["level"] in ("nudge", "steer", "replace"),
          u[0]["level"] if u else "none")

    # 12-13 failed
    r = sc.build({"tasks": [task("F", "failed", attempts=1)]}, cfg(), NOW, None, use_kb=False)
    check("failed-replace", bool(actions(r, "replace")))
    r = sc.build({"tasks": [task("F", "failed", attempts=3)]}, cfg(), NOW, None, use_kb=False)
    check("failed-split", bool(actions(r, "split")))

    # 14-15 evidence
    r = sc.build({"tasks": [task("D", "done")]}, cfg(), NOW, None, use_kb=False)
    check("done-missing-evidence", bool(actions(r, "reject-done")))

    tmp = tempfile.mkdtemp(prefix="steward-cycle-")
    try:
        ev = os.path.join(tmp, "ev.md")
        open(ev, "w", encoding="utf-8").write("【结论】成功\n【证据】12 passed\n")
        sha = hashlib.sha256(open(ev, "rb").read()).hexdigest()
        good = task("D2", "done", evidence_path=ev, evidence_sha256=sha)
        r = sc.build({"tasks": [good]}, cfg(), NOW, None, use_kb=False)
        check("done-evidence-ok", not actions(r, "reject-done"))
        bad = task("D3", "done", evidence_path=ev, evidence_sha256="0" * 64)
        r = sc.build({"tasks": [bad]}, cfg(), NOW, None, use_kb=False)
        check("done-hash-mismatch", bool(actions(r, "reject-done")))
        # 15b: registry 用 --sha-mode lf 登记、工作树是 CRLF -> 不得误判为改过
        crlf = os.path.join(tmp, "ev_crlf.md")
        with open(crlf, "wb") as f:
            f.write("【结论】成功\r\n[PASS]\r\n".encode("utf-8"))
        lf_sha = hashlib.sha256(open(crlf, "rb").read().replace(b"\r\n", b"\n")).hexdigest()
        lf_card = task("D2LF", "done", evidence_path=crlf, evidence_sha256=lf_sha)
        r = sc.build({"tasks": [lf_card]}, cfg(), NOW, None, use_kb=False)
        check("done-lf-anchor-ok", not actions(r, "reject-done"))

        # 15c: 冻结副本优先——工作树后来被追加，验收不受影响，只报 drift
        frozen = os.path.join(tmp, "D2FROZEN.md")
        with open(frozen, "wb") as f:
            f.write("【结论】成功\n【证据】12 passed\n".encode("utf-8"))
        frozen_sha = hashlib.sha256(open(frozen, "rb").read()).hexdigest()
        with open(ev, "a", encoding="utf-8") as f:
            f.write("审计追加：验收后补充说明\n")
        fr_card = task("D2F", "done", evidence_path=ev, evidence_sha256=frozen_sha,
                       evidence_frozen_path=frozen, evidence_frozen_sha256=frozen_sha)
        r = sc.build({"tasks": [fr_card]}, cfg(), NOW, None, use_kb=False)
        check("done-frozen-first", not actions(r, "reject-done"))
        check("done-frozen-drift-advisory",
              any(x.get("id") == "D2F" for x in r["digest"].get("evidence_drift") or []),
              str(r["digest"].get("evidence_drift")))

        # 15d: 冻结副本本身被改 -> 真漂移，必须打回
        with open(frozen, "ab") as f:
            f.write(b"tamper")
        r = sc.build({"tasks": [fr_card]}, cfg(), NOW, None, use_kb=False)
        check("done-frozen-tamper", bool(actions(r, "reject-done")))

        # 15e: 冻结副本丢失 -> 打回（不回退工作树，避免降级验收）
        gone = task("D2G", "done", evidence_path=ev, evidence_sha256=sha,
                    evidence_frozen_path=os.path.join(tmp, "nope.md"),
                    evidence_frozen_sha256=sha)
        r = sc.build({"tasks": [gone]}, cfg(), NOW, None, use_kb=False)
        check("done-frozen-missing", bool(actions(r, "reject-done")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 15f: DAG 瓶颈/关键路径（A 挡 B 挡 C -> 瓶颈 A，关键链 A->B->C）
    dag_tasks = [task("A"), task("B", depends_on=["A"]), task("C", depends_on=["B"])]
    r = sc.build({"tasks": dag_tasks}, cfg(), NOW, None, use_kb=False)
    bn = r["digest"].get("bottleneck") or []
    cp = r["digest"].get("critical_path") or {}
    check("dag-bottleneck", bool(bn) and bn[0]["id"] == "A" and bn[0]["blocks"] == 2,
          str(bn[:1]))
    check("dag-critical-path", cp.get("length") == 3 and cp.get("chain") == ["A", "B", "C"],
          str(cp))
    done_dep = [task("A", "done"), task("B", "done"), task("C")]
    r = sc.build({"tasks": done_dep}, cfg(), NOW, None, use_kb=False)
    check("dag-ignores-done", not (r["digest"].get("bottleneck")), str(r["digest"]["bottleneck"]))

    # 16 budget
    r = sc.build({"tasks": [task("Q")]}, cfg(daily_budget_cny=5), NOW, 10.0, use_kb=False)
    check("budget", bool(actions(r, "budget")) and "dispatch" not in types(r),
          str(types(r)))

    # 17 retry backoff
    r = sc.build({"tasks": [task("R", next_retry_at="2026-11-18T12:10:00+08:00")]},
                 cfg(), NOW, None, use_kb=False)
    check("retry-backoff", "dispatch" not in types(r))

    # 18 progress alert（stale 阈值拉大，只触发零进展告警）
    old = "2026-11-18T08:00:00+08:00"
    r = sc.build({"tasks": [task("A", "running", last_progress_at=old)]},
                 cfg(stale_sec=999999, idle_alert_sec=3600), NOW, None, use_kb=False)
    check("progress-alert", bool(actions(r, "progress-alert")), str(types(r)))

    # 19 digest
    r = sc.build({"tasks": [task("A", "running"), task("B", "done", evidence_path=None)]},
                 cfg(), NOW, None, use_kb=False)
    check("digest", r["digest"]["active"] == 1 and r["digest"]["counts"].get("done") == 1,
          str(r["digest"]["counts"]))

    # 20 bad config falls back to defaults
    lim = sc.limits({"dispatch": {"max_running": "bad"}})
    check("bad-config-default", lim["max_running"] == 3, str(lim["max_running"]))

    # 21 未回填 evidence_path 时认约定路径 reports/evidence/<id>.md
    want = os.path.join(sc.ROOT, "reports", "evidence", "X-1.md")
    check("evidence-default-path", sc.evidence_path({"id": "X-1"}) == want,
          str(sc.evidence_path({"id": "X-1"})))

    # 22-28 写码任务隔离工作区（W-20261118-011）
    r = sc.build({"tasks": [task("W")]}, cfg(), NOW, None, use_kb=False)
    d = actions(r, "dispatch")
    check("worktree-off-by-default", bool(d) and "worktree" not in d[0]
          and "【隔离工作区】" not in d[0]["prompt"], str(d[0].get("worktree")))

    wt_cfg = {"enabled": True, "root": "C:/tmp/wts", "branch_prefix": "wt/",
              "repos": {"dll": "C:/Users/Administrator/Documents/mate/dll"}}
    calls = []

    def fake_create(task_id, repo, conf):
        calls.append((task_id, repo, conf.get("root")))
        return {"ok": True, "created": True, "path": "%s/%s" % (conf["root"], task_id),
                "branch": "wt/" + task_id, "base_branch": "main", "repo": repo,
                "base_commit": "abc"}

    r = sc.build({"tasks": [task("W", project="dll")]}, cfg(worktree=wt_cfg), NOW, None,
                 use_kb=False, worktree_factory=fake_create)
    d = actions(r, "dispatch")
    wt = (d[0].get("worktree") or {}) if d else {}
    check("worktree-materialized", bool(d) and wt.get("path") == "C:/tmp/wts/W"
          and d[0].get("workspace") == "C:/tmp/wts/W", str(wt))
    check("worktree-prompt", "【隔离工作区】" in (d[0]["prompt"] if d else "")
          and "wt/W" in (d[0]["prompt"] if d else "")
          and "commit" in (d[0]["prompt"] if d else ""), "")
    check("worktree-repo-resolved", calls and calls[0][1] == "C:/Users/Administrator/Documents/mate/dll",
          str(calls[:1]))

    r = sc.build({"tasks": [task("W", project="dll")]}, cfg(worktree=wt_cfg), NOW, None,
                 use_kb=False, materialize=False, worktree_factory=fake_create,
                 git_runner=FakeGit(inside=True))
    d = actions(r, "dispatch")
    wt = (d[0].get("worktree") or {}) if d else {}
    check("worktree-dry-run", wt.get("planned") is True and "path" not in wt, str(wt))

    def fake_skip(task_id, repo, conf):
        return {"ok": False, "skipped": True, "reason": "not-a-git-repo",
                "hint": "不是 git 仓"}

    r = sc.build({"tasks": [task("W", project="dll")]}, cfg(worktree=wt_cfg), NOW, None,
                 use_kb=False, worktree_factory=fake_skip, git_runner=FakeGit(inside=True))
    d = actions(r, "dispatch")
    wt = (d[0].get("worktree") or {}) if d else {}
    check("worktree-skip", bool(d) and wt.get("skipped") == "not-a-git-repo"
          and "【隔离工作区】" not in d[0]["prompt"], str(wt))

    def fake_fail(task_id, repo, conf):
        return {"ok": False, "reason": "worktree-add-failed", "error": "boom"}

    r = sc.build({"tasks": [task("W", project="dll")]}, cfg(worktree=wt_cfg), NOW, None,
                 use_kb=False, worktree_factory=fake_fail, git_runner=FakeGit(inside=True))
    d = actions(r, "dispatch")
    wt = (d[0].get("worktree") or {}) if d else {}
    check("worktree-fail-fallback", bool(d) and wt.get("error") == "worktree-add-failed"
          and "创建失败" in d[0]["prompt"], str(wt))

    def fake_boom(task_id, repo, conf):
        raise RuntimeError("git 挂了")

    r = sc.build({"tasks": [task("W", project="dll")]}, cfg(worktree=wt_cfg), NOW, None,
                 use_kb=False, worktree_factory=fake_boom, git_runner=FakeGit(inside=True))
    d = actions(r, "dispatch")
    wt = (d[0].get("worktree") or {}) if d else {}
    check("worktree-exception-safe", bool(d) and wt.get("error") == "exception",
          str(wt))

    # 27 已完成任务的遗留工作区 -> worktree-cleanup
    tmp_wt = tempfile.mkdtemp(prefix="steward-cycle-wt-")
    try:
        led = os.path.join(tmp_wt, "worktrees.json")
        ev = os.path.join(tmp_wt, "ev.md")
        open(ev, "w", encoding="utf-8").write("【结论】成功\n")
        sha = hashlib.sha256(open(ev, "rb").read()).hexdigest()
        json.dump({"worktrees": {
            "D": {"path": "C:/tmp/wts/D", "branch": "wt/D", "repo": "C:/repo",
                  "created_at": "2026-11-18T08:00:00+08:00"},
            "D2": {"path": "C:/tmp/wts/D2", "branch": "wt/D2", "repo": "C:/repo",
                   "created_at": "2026-11-18T08:00:00+08:00", "removed_at": "2026-11-18T09:00:00+08:00"},
        }}, open(led, "w", encoding="utf-8"))
        wt2 = dict(wt_cfg)
        wt2["ledger"] = led
        good = task("D", "done", evidence_path=ev, evidence_sha256=sha)
        cleaned = task("D2", "done", evidence_path=ev, evidence_sha256=sha)
        r = sc.build({"tasks": [good, cleaned]}, cfg(worktree=wt2), NOW, None, use_kb=False)
        cl = actions(r, "worktree-cleanup")
        check("worktree-cleanup-action", len(cl) == 1 and cl[0]["task_id"] == "D"
              and cl[0].get("branch") == "wt/D", str([a["task_id"] for a in cl]))
        r = sc.build({"tasks": [cleaned]}, cfg(worktree=wt2), NOW, None, use_kb=False)
        check("worktree-cleanup-removed-skip", not actions(r, "worktree-cleanup"),
              str(types(r)))
    finally:
        shutil.rmtree(tmp_wt, ignore_errors=True)

    # 29-35 W-20261118-017 worker 纪律三条机械强制
    # 29 prompt 必含三条硬纪律、离场标记要求、身份前缀提交命令
    prompt = sc.worker_prompt(task("W-1"), use_kb=False)
    check("prompt-hard-disciplines",
          all(m in prompt for m in ("【硬纪律1", "【硬纪律2", "【硬纪律3"))
          and "EXIT=" in prompt and "[PASS]" in prompt and "W-1: " in prompt,
          "len=%d" % len(prompt))

    # 30 同一 git 主仓已有未隔离在办任务 -> 拒派（按 workspace 判，不按项目名）
    git = FakeGit(inside=True)
    r = sc.build({"tasks": [task("G1", project="p1", workspace="C:/repo/a"),
                            task("G2", project="p2", workspace="C:/repo/a")]},
                 cfg(), NOW, None, use_kb=False, git_runner=git)
    blocked = actions(r, "dispatch-blocked")
    check("guard-other-active-main",
          len(actions(r, "dispatch")) == 1 and len(blocked) == 1
          and blocked[0]["task_id"] == "G2"
          and "主仓他人活跃" in blocked[0]["reason"]
          and blocked[0]["guard"]["others"] == ["G1"], str(types(r)))
    check("guard-stays-queued", blocked and blocked[0]["state"] == "queued", "")

    # 31 主仓有已跟踪未提交改动 -> 拒派；只认非 ?? 行
    git = FakeGit(status_lines=[" M scripts/x.py", "?? reports/out.md"])
    r = sc.build({"tasks": [task("G3", workspace="C:/repo/a")]},
                 cfg(), NOW, None, use_kb=False, git_runner=git)
    b = actions(r, "dispatch-blocked")
    check("guard-main-dirty",
          len(b) == 1 and "未提交的已跟踪改动" in b[0]["reason"]
          and b[0]["guard"]["git"]["dirty_tracked"] == 1
          and b[0]["guard"]["git"]["untracked"] == 1, str(types(r)))
    git = FakeGit(status_lines=["?? reports/out.md"])
    r = sc.build({"tasks": [task("G4", workspace="C:/repo/a")]},
                 cfg(), NOW, None, use_kb=False, git_runner=git)
    check("guard-untracked-ok", bool(actions(r, "dispatch")) and not actions(r, "dispatch-blocked"),
          str(types(r)))

    # 32 主仓干净 -> 正常派单；非 git 工作区跳过探针
    git = FakeGit(inside=True)
    r = sc.build({"tasks": [task("G5", workspace="C:/repo/a")]},
                 cfg(), NOW, None, use_kb=False, git_runner=git)
    check("guard-clean-dispatch", len(actions(r, "dispatch")) == 1 and git.calls,
          str(git.calls[:2]))
    git = FakeGit(inside=False)
    r = sc.build({"tasks": [task("G6", workspace="C:/repo/nogit")]},
                 cfg(), NOW, None, use_kb=False, git_runner=git)
    check("guard-nongit-ok", len(actions(r, "dispatch")) == 1, str(types(r)))

    # 33 隔离工作区不查主仓（探针一次都不该被调用）
    def boom_git(args, cwd):
        raise AssertionError("隔离工作区不应探测主仓: %s" % (args,))

    def mk(task_id, repo, conf):
        return {"ok": True, "created": True, "path": "C:/tmp/wts/" + task_id,
                "branch": "wt/" + task_id, "base_branch": "main", "repo": repo}

    r = sc.build({"tasks": [task("G7", project="dll", workspace="C:/repo/a")]},
                 cfg(worktree={"enabled": True, "repos": {"dll": "C:/repo/a"}}),
                 NOW, None, use_kb=False, worktree_factory=mk, git_runner=boom_git)
    check("guard-isolated-skip", len(actions(r, "dispatch")) == 1, str(types(r)))

    # 34 证据缺 EXIT= / [PASS] -> verify 阶段 reject-done 打回
    tmp = tempfile.mkdtemp(prefix="steward-cycle-marker-")
    try:
        ev = os.path.join(tmp, "ev.md")
        open(ev, "w", encoding="utf-8").write("【结论】成功\n【证据】12 passed\n")
        r = sc.build({"tasks": [task("M", "running", evidence_path=ev,
                                     last_progress_at="2026-11-18T10:00:00+08:00")]},
                     cfg(), NOW, None, use_kb=False)
        m = actions(r, "reject-done")
        check("verify-marker-missing", len(m) == 1 and m[0].get("marker_missing") is True
              and "EXIT=" in m[0]["reason"] and not actions(r, "verify"), str(types(r)))
        open(ev, "w", encoding="utf-8").write("【结论】成功\n[PASS]\n")
        r = sc.build({"tasks": [task("M", "running", evidence_path=ev,
                                     last_progress_at="2026-11-18T10:00:00+08:00")]},
                     cfg(), NOW, None, use_kb=False)
        check("verify-marker-pass", bool(actions(r, "verify")) and not actions(r, "reject-done"),
              str(types(r)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 35 dispatch 自检直接判 prompt 缺纪律
    bad = sc.dispatch_guard(task("G8", workspace="C:/repo/a"), sc.limits(cfg()),
                            None, [], "只有任务没有纪律", git_runner=FakeGit())
    check("guard-prompt-selfcheck", bad["ok"] is False
          and any("硬纪律" in p for p in bad["problems"]), str(bad["problems"]))

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-22s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
