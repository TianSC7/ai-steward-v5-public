# -*- coding: utf-8 -*-
"""Regression tests for the 2026-11-24 merge-guard fixes.

Covers the three production bugs found on C-20261124-G41a:
  1) finish() deleted the worktree even when the merge made no change;
  2) _commit_and_merge_worktree() deleted untracked deliverables that were not
     the single hardcoded target file;
  3) Gate1 treated "git status is clean" as failure, so a builder that committed
     its own work always failed the gate.

No LLM, no network, no touch to the real ledger: every case uses a temp git repo.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_runner as fr  # noqa: E402
import worker_worktree as ww  # noqa: E402

PASS = []


def run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, "%s failed: %s\n%s" % (cmd, r.stdout, r.stderr)
    return r.stdout


def make_repo(base):
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, "scripts"), exist_ok=True)
    run(["git", "init", "-q", repo], base)
    run(["git", "-C", repo, "config", "user.email", "t@example.com"], base)
    run(["git", "-C", repo, "config", "user.name", "t"], base)
    run(["git", "-C", repo, "config", "core.autocrlf", "false"], base)
    with open(os.path.join(repo, "scripts", "base.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    run(["git", "-C", repo, "add", "-A"], base)
    run(["git", "-C", repo, "commit", "-qm", "base"], base)
    return repo


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    base = tempfile.mkdtemp(prefix="wtguard_")
    cfg = {"root": os.path.join(base, "wt"), "links": [], "branch_prefix": "wt/"}
    ledger = os.path.join(base, "ledger.json")
    try:
        # A. no commits -> finish must keep the worktree
        repo = make_repo(base)
        res = ww.ensure("A", repo, cfg=cfg, ledger_path=ledger)
        assert res.get("ok"), res
        wt = res["path"]
        fin = ww.finish("A", repo=repo, ledger_path=ledger)
        assert fin.get("ok") is False, fin
        assert fin.get("kept") is True, fin
        assert fin["merge"].get("reason") == "no-commits", fin
        assert fin["merge"].get("same_head") is True, fin
        assert os.path.isdir(wt), "worktree was deleted although nothing was merged"
        PASS.append("A no-commits keeps worktree")

        # B. builder commits its own work -> Gate1 must still see the diff
        res = ww.ensure("B", repo, cfg=cfg, ledger_path=ledger)
        wt = res["path"]
        write(os.path.join(wt, "scripts", "deliver.py"), "print('ok')\n")
        run(["git", "-C", wt, "add", "-A"], base)
        run(["git", "-C", wt, "commit", "-qm", "builder self-commit"], base)
        probe = fr.probe_code_delivery(wt, repo, ["scripts/deliver.py"], builder_rc=0)
        assert probe["all_passed"] is True, probe
        assert "scripts/deliver.py" in probe["checks"]["declared_touched"], probe
        PASS.append("B committed diff passes Gate1")

        # C. uncommitted + untracked targets pass Gate1; empty change fails
        res = ww.ensure("C", repo, cfg=cfg, ledger_path=ledger)
        wt = res["path"]
        write(os.path.join(wt, "scripts", "base.py"), "x = 2\n")
        write(os.path.join(wt, "scripts", "brand_new.py"), "y = 1\n")
        probe = fr.probe_code_delivery(wt, repo, ["scripts/base.py", "scripts/brand_new.py"], builder_rc=0)
        assert probe["all_passed"] is True, probe
        assert set(probe["checks"]["declared_touched"]) == {"scripts/base.py", "scripts/brand_new.py"}, probe
        PASS.append("C uncommitted/untracked diff passes Gate1")

        res = ww.ensure("C0", repo, cfg=cfg, ledger_path=ledger)
        probe = fr.probe_code_delivery(res["path"], repo, ["scripts/base.py"], builder_rc=0)
        assert probe["all_passed"] is False, probe
        assert any("声明目标零改动" in x for x in probe["failures"]), probe
        PASS.append("C0 untouched target fails Gate1")

        # D. merge path must not delete undeclared untracked deliverables (G41a bug)
        res = ww.ensure("D", repo, cfg=cfg, ledger_path=ledger)
        wt = res["path"]
        write(os.path.join(wt, "scripts", "slot_ledger.py"), "def f():\n    return 1\n")
        write(os.path.join(wt, "scripts", "test_slot_ledger.py"), "print('tests')\n")
        ok, info = fr._commit_and_merge_worktree(
            "D", repo, wt, ["scripts/slot_ledger.py"],
            {"scripts/slot_ledger.py": ""}, kind="code", ledger_path=ledger)
        assert ok is True, info
        assert os.path.exists(os.path.join(repo, "scripts", "slot_ledger.py")), "declared file missing in main"
        assert os.path.exists(os.path.join(repo, "scripts", "test_slot_ledger.py")), "undeclared deliverable was deleted"
        assert not os.path.isdir(wt), "worktree should be removed after a real merge"
        PASS.append("D merge keeps undeclared new files")

        # E. a real merge updates main and removes the worktree
        res = ww.ensure("E", repo, cfg=cfg, ledger_path=ledger)
        wt = res["path"]
        write(os.path.join(wt, "scripts", "base.py"), "x = 3\n")
        ok, info = fr._commit_and_merge_worktree(
            "E", repo, wt, ["scripts/base.py"],
            {"scripts/base.py": fr.sha256_file(os.path.join(repo, "scripts", "base.py"))},
            kind="code", ledger_path=ledger)
        assert ok is True, info
        assert "x = 3" in open(os.path.join(repo, "scripts", "base.py"), encoding="utf-8").read()
        assert not os.path.isdir(wt), "worktree should be removed after merge"
        PASS.append("E real merge lands in main")

        # F. redline paths fail Gate1
        res = ww.ensure("F", repo, cfg=cfg, ledger_path=ledger)
        wt = res["path"]
        write(os.path.join(wt, "Private1", "x.txt"), "bad\n")
        write(os.path.join(wt, "scripts", "base.py"), "x = 4\n")
        probe = fr.probe_code_delivery(wt, repo, ["scripts/base.py"], builder_rc=0)
        assert probe["all_passed"] is False, probe
        assert any("禁区改动" in x for x in probe["failures"]), probe
        PASS.append("F redline path fails Gate1")

        # G. enqueue keeps the full target list (no silent single-target downgrade)
        qpath = os.path.join(base, "queue.json")
        old_queue, old_dirs = fr.QUEUE_PATH, fr.ensure_dirs
        fr.QUEUE_PATH, fr.ensure_dirs = qpath, (lambda: None)
        try:
            job = fr.enqueue_job("G1", "code", "t", os.path.join(base, "spec.md"), "",
                                 1, repo=repo, targets=["scripts/a.py", "state/b.py"])
        finally:
            fr.QUEUE_PATH, fr.ensure_dirs = old_queue, old_dirs
        assert job["targets"] == ["scripts/a.py", "state/b.py"], job
        assert job["target"] == "scripts/a.py", job
        PASS.append("G enqueue keeps target list")

        # H. spec-driven enqueue detection picks the two declared files (G40c regression)
        sys.path.insert(0, HERE)
        import enqueue_kernel as ek
        spec = ("# X\n\n## 改动文件（仅此两个）\n\n1. `scripts/autopilot_pump.py`：改 A\n"
                "2. `state/monitor5.py`：改 B\n\n## 验收\n\n跑 `scripts/factory_runner.py` 不算目标\n")
        got = ek.detect_targets(spec)
        assert got == ["scripts/autopilot_pump.py", "state/monitor5.py"], got
        PASS.append("H spec-driven targets detected")

        # I. legacy redispatch target inheritance: spec paths win over the stale single target
        #    (G38/G39/G40x were enqueued with the hardcoded scripts/factory_runner.py)
        import autopilot_pump as ap
        spec_path = os.path.join(base, "legacy_spec.md")
        write(spec_path, "## 施工要求\n\n改 `scripts\\autopilot_pump.py` 与 config\\steward.json，"
                         "跑 scripts/factory_runner.py 复测；产物落 research\\lanes.md\n")
        got = ap.spec_targets(spec_path)
        assert "scripts/autopilot_pump.py" in got, got
        assert "config/steward.json" in got, got          # .json must not be truncated to .js
        assert "scripts/factory_runner.py" in got, got
        assert all("\\" not in p for p in got), got       # normalized to repo-relative form
        assert got[0] == "scripts/autopilot_pump.py", got  # 任务主体在前，旧硬编码目标不会独占
        PASS.append("I legacy spec targets beat stale target")

        print("PASS %d/%d" % (len(PASS), len(PASS)))
        for p in PASS:
            print("  [PASS]", p)
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
