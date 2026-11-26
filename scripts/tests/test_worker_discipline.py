# -*- coding: utf-8 -*-
"""W-20261118-017 worker 纪律三条机械强制（pytest 版）。

覆盖：
  1. worker prompt 必含【硬纪律1/2/3】三段（可 grep）与离场标记要求；
  2. dispatch 前主仓他人活跃自检：同 workspace 未隔离在办 -> 拒派；
  3. dispatch 前主仓脏自检：已跟踪未提交改动 -> 拒派（?? 不误伤）；
  4. 隔离工作区跳过主仓探针；
  5. prompt 缺纪律 -> dispatch_guard 拒派（引擎自检）；
  6. verify 阶段证据缺 EXIT= / [PASS] -> reject-done 打回；
  7. claim 边界：--worktree 缺分支/仓、或等于主 workspace -> 拒绝认领。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import steward_board as sb
import steward_cycle as sc

NOW = sc.now_dt("2026-11-18T12:00:00+08:00")


def cfg(**kw):
    base = {"max_running": 3, "max_running_per_project": 1, "stale_sec": 1800,
            "max_attempts": 3, "max_pulls": 3, "daily_budget_cny": 0,
            "idle_alert_sec": 7200, "work_start": "08:00", "work_end": "20:00"}
    base.update(kw)
    return {"dispatch": base}


def task(tid="W-1", state="queued", **kw):
    base = {
        "id": tid, "title": "任务 %s" % tid, "goal": "把 %s 做完" % tid,
        "acceptance": "pytest 全绿", "project": "ai-steward", "priority": 1,
        "state": state, "attempts": 0, "max_attempts": 3, "pulls": 0,
        "created_at": "2026-11-18T08:00:00+08:00",
        "last_progress_at": "2026-11-18T11:50:00+08:00",
        "sessionId": "sess_%s" % tid, "depends_on": [], "steer_events": [],
        "evidence_path": None, "evidence_sha256": None,
    }
    base.update(kw)
    return base


def actions(result, kind):
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


def test_prompt_contains_three_hard_disciplines():
    prompt = sc.worker_prompt(task(), use_kb=False)
    for marker in ("【硬纪律1", "【硬纪律2", "【硬纪律3"):
        assert marker in prompt
    assert "checkin 拒主仓" in prompt
    assert "claim 边界" in prompt
    assert "离场三闸门" in prompt


def test_prompt_requires_exit_marker_and_identity_prefix():
    prompt = sc.worker_prompt(task("W-20261118-777"), use_kb=False)
    assert "EXIT=" in prompt and "[PASS]" in prompt
    assert "W-20261118-777: " in prompt          # 提交身份前缀
    assert "reports/evidence/W-20261118-777.md" in prompt


def test_dispatch_guard_blocks_other_active_on_main_repo():
    reg = {"tasks": [task("A1", project="p1", workspace="C:/repo/a"),
                     task("A2", project="p2", workspace="C:/repo/a")]}
    r = sc.build(reg, cfg(), NOW, None, use_kb=False, git_runner=FakeGit())
    assert len(actions(r, "dispatch")) == 1
    blocked = actions(r, "dispatch-blocked")
    assert len(blocked) == 1
    assert blocked[0]["task_id"] == "A2"
    assert "主仓他人活跃" in blocked[0]["reason"]
    assert blocked[0]["guard"]["others"] == ["A1"]
    assert blocked[0]["state"] == "queued"


def test_dispatch_guard_blocks_dirty_tracked_main_repo():
    git = FakeGit(status_lines=[" M scripts/x.py", "?? reports/out.md"])
    r = sc.build({"tasks": [task("D1", workspace="C:/repo/a")]},
                 cfg(), NOW, None, use_kb=False, git_runner=git)
    blocked = actions(r, "dispatch-blocked")
    assert len(blocked) == 1
    assert "未提交的已跟踪改动" in blocked[0]["reason"]
    assert blocked[0]["guard"]["git"]["dirty_tracked"] == 1
    assert blocked[0]["guard"]["git"]["untracked"] == 1
    assert not actions(r, "dispatch")


@pytest.mark.parametrize("git,ok", [(FakeGit(status_lines=["?? reports/out.md"]), True),
                                    (FakeGit(inside=False), True)])
def test_dispatch_guard_allows_clean_or_non_git(git, ok):
    r = sc.build({"tasks": [task("C1", workspace="C:/repo/a")]},
                 cfg(), NOW, None, use_kb=False, git_runner=git)
    assert bool(actions(r, "dispatch")) is ok
    assert not actions(r, "dispatch-blocked")


def test_dispatch_guard_skips_git_probe_for_isolated_plan():
    def boom(args, cwd):
        raise AssertionError("隔离工作区不应探测主仓")

    guard = sc.dispatch_guard(task("I1", workspace="C:/repo/a"), sc.limits(cfg()),
                              {"path": "C:/wts/I1", "branch": "wt/I1",
                               "repo": "C:/repo/a"}, [],
                              sc.worker_prompt(task("I1"), use_kb=False),
                              git_runner=boom)
    assert guard["ok"] is True
    assert guard["detail"]["isolated"] is True


def test_dispatch_guard_rejects_prompt_without_disciplines():
    guard = sc.dispatch_guard(task("P1", workspace="C:/repo/a"), sc.limits(cfg()),
                              None, [], "只有任务没有纪律", git_runner=FakeGit())
    assert guard["ok"] is False
    assert any("硬纪律" in p for p in guard["problems"])
    assert any("EXIT=" in p for p in guard["problems"])


def test_verify_stage_rejects_evidence_without_marker(tmp_path):
    ev = tmp_path / "ev.md"
    ev.write_text("【结论】成功\n【证据】12 passed\n", encoding="utf-8")
    r = sc.build({"tasks": [task("V1", "running", evidence_path=str(ev))]},
                 cfg(), NOW, None, use_kb=False)
    rejected = actions(r, "reject-done")
    assert len(rejected) == 1
    assert rejected[0]["marker_missing"] is True
    assert "EXIT=" in rejected[0]["reason"]
    assert not actions(r, "verify")


@pytest.mark.parametrize("text", ["【结论】成功\nEXIT=PASS\n",
                                  "【结论】成功\n[PASS] pytest 12 passed\n"])
def test_verify_stage_accepts_marker(tmp_path, text):
    ev = tmp_path / "ev.md"
    ev.write_text(text, encoding="utf-8")
    r = sc.build({"tasks": [task("V2", "running", evidence_path=str(ev))]},
                 cfg(), NOW, None, use_kb=False)
    assert len(actions(r, "verify")) == 1
    assert not actions(r, "reject-done")


def _add_args(**kw):
    base = dict(id=None, title="claim 边界", goal="g", acceptance="a",
                project="ai-steward", workspace="C:/repo/a", tier="MEDIUM",
                model=None, priority=1, depends_on="", max_attempts=3,
                boundary="", source="", note="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_claim_rejects_worktree_without_branch_repo():
    reg = {"tasks": [], "decisions": []}
    t = sb.cmd_add(reg, _add_args(id="W-20261118-801"), NOW)
    with pytest.raises(SystemExit):
        sb.cmd_claim(reg, SimpleNamespace(id=t["id"], session="s1", command="",
                                          worktree="C:/wts/W-801", note=""), NOW)


def test_claim_rejects_worktree_equal_to_workspace():
    reg = {"tasks": [], "decisions": []}
    t = sb.cmd_add(reg, _add_args(id="W-20261118-802"), NOW)
    with pytest.raises(SystemExit):
        sb.cmd_claim(reg, SimpleNamespace(id=t["id"], session="s2", command="",
                                          worktree=t["workspace"],
                                          worktree_branch="wt/W-802",
                                          worktree_repo="C:/repo", note=""), NOW)
