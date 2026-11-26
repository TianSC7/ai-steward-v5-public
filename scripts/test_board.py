# -*- coding: utf-8 -*-
"""任务台边界用例：状态机/证据门/重试/并发写不能形同虚设。

覆盖：
  1 add 默认值 + 模型分层
  2 SIMPLE 档模型
  3 claim 领取（attempts+1、session/command 落卡）
  4 非 queued 不能 claim
  5 done 缺证据 -> 拒绝
  6 done 证据不存在 -> 拒绝
  7 done 成功（sha256 自动登记）
  8 非法流转 queued -> done -> 拒绝
  9 fail --retry 回队列（带退避，attempts 保留）
  10 fail 不重试 -> failed
  11 到达 max_attempts 不能再 claim
  12 steer 留痕 + 非法 interrupt 拒绝
  13 pull 计数
  14 retry 退避期内不能 claim
  15 原子写留下备份
  16 当日 id 递增
  17 坏台账读取失败
  18 progress 刷新进展时间
  19 未给 workspace -> 按 workspace_map 补全
  20 未映射项目保持空 workspace
  21 claim 边界：--worktree 必须带分支/仓，且不能等于主 workspace（W-017）

用法：python -X utf8 test_board.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import steward_board as sb  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
NOW = "2026-11-18T08:00:00+08:00"


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def raises(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except (SystemExit, ValueError):
        return True
    return False


def reg() -> dict:
    return {"tasks": [], "decisions": []}


def add_args(**kw):
    base = dict(id=None, title="修桥接超时", goal="把桥接超时修到 pytest 全绿",
                acceptance="pytest tests/test_bridge.py 全绿", project="mate-lib-ai",
                workspace="C:/Users/Administrator/Documents/mate/lib/ai", tier="MEDIUM", model=None,
                priority=1, depends_on="", max_attempts=3, boundary="", source="", note="")
    base.update(kw)
    return SimpleNamespace(**base)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_board")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_board")

    now = sb.now_dt(NOW)
    r = reg()

    t = sb.cmd_add(r, add_args(), now)
    check("add-defaults", t["state"] == "queued" and t["attempts"] == 0
          and t["model"] == "new-provider-7/auto" and t["id"] == "W-20261118-001",
          "id=%s model=%s" % (t["id"], t["model"]))
    t2 = sb.cmd_add(r, add_args(tier="SIMPLE", priority=0), now)
    check("tier-simple", t2["model"] == "new-provider-7/auto-cheap" and t2["priority"] == 0,
          "model=%s" % t2["model"])

    # 2b 未给 workspace -> 按 config workspace_map 补全；未映射保持空
    cfgdir = tempfile.mkdtemp(prefix="steward-board-cfg-")
    try:
        cfgp = os.path.join(cfgdir, "steward.json")
        open(cfgp, "w", encoding="utf-8").write(json.dumps(
            {"dispatch": {"workspace_map": {"mate-lib-ai": "C:/x/ai"}}}, ensure_ascii=False))
        old_cfg = sb.DEFAULT_CONFIG
        sb.DEFAULT_CONFIG = cfgp
        try:
            t3 = sb.cmd_add(r, add_args(workspace="", id="W-20261118-097"), now)
            t4 = sb.cmd_add(r, add_args(workspace="", project="other",
                                        id="W-20261118-098"), now)
        finally:
            sb.DEFAULT_CONFIG = old_cfg
        check("workspace-map-fill", t3["workspace"] == "C:/x/ai", t3["workspace"] or "-")
        check("workspace-map-miss", t4["workspace"] == "", t4["workspace"] or "-")
    finally:
        shutil.rmtree(cfgdir, ignore_errors=True)

    claimed = sb.cmd_claim(r, SimpleNamespace(id=t["id"], session="sess_a",
                                             command="app-control-1", note=""), now)
    check("claim", claimed["state"] == "dispatched" and claimed["attempts"] == 1
          and claimed["sessionId"] == "sess_a" and claimed["last_command_id"] == "app-control-1"
          and claimed.get("last_progress_at"),
          "state=%s attempts=%s" % (claimed["state"], claimed["attempts"]))
    check("claim-twice-rejected", raises(sb.cmd_claim, r, SimpleNamespace(
        id=t["id"], session="sess_b", command="", note=""), now))

    # 20b 派单时登记隔离工作区（W-20261118-011）
    tw = sb.cmd_add(r, add_args(id="W-20261118-096", title="隔离工作区任务"), now)
    cw = sb.cmd_claim(r, SimpleNamespace(id=tw["id"], session="sess_w", command="",
                                         worktree="C:\\wt\\W-97", worktree_branch="wt/W-97",
                                         worktree_repo="C:\\repo", note=""), now)
    wt = cw.get("worktree") or {}
    check("claim-worktree-recorded", wt.get("path") == "C:/wt/W-97"
          and wt.get("branch") == "wt/W-97" and wt.get("repo") == "C:/repo"
          and wt.get("source_workspace") == tw["workspace"], str(wt))

    # 20c claim 边界（W-20261118-017）：三件套齐全，且不能拿主 workspace 冒充隔离区
    t_noiso = sb.cmd_add(r, add_args(id="W-20261118-095", title="claim 边界"), now)
    check("claim-worktree-needs-branch-repo", raises(sb.cmd_claim, r, SimpleNamespace(
        id=t_noiso["id"], session="sess_x", command="",
        worktree="C:\\wt\\W-95", note=""), now))
    r4 = reg()
    t_alias = sb.cmd_add(r4, add_args(id="W-20261118-094", title="alias 冒充"), now)
    check("claim-worktree-not-workspace", raises(sb.cmd_claim, r4, SimpleNamespace(
        id=t_alias["id"], session="sess_y", command="", worktree=t_alias["workspace"],
        worktree_branch="wt/W-94", worktree_repo="C:\\repo", note=""), now))

    tmp = tempfile.mkdtemp(prefix="steward-board-")
    old_frozen = sb.FROZEN_DIR
    sb.FROZEN_DIR = os.path.join(tmp, "frozen")
    try:
        ev = os.path.join(tmp, "W.md")
        open(ev, "w", encoding="utf-8").write("【结论】成功\n【证据】pytest 12 passed\n")

        # done 证据门
        r2 = reg()
        td = sb.cmd_add(r2, add_args(id="W-T-1"), now)
        check("done-no-evidence", raises(sb.cmd_done, r2, SimpleNamespace(
            id=td["id"], evidence="", summary="", note="", force=False), now))
        check("done-missing-file", raises(sb.cmd_done, r2, SimpleNamespace(
            id=td["id"], evidence=os.path.join(tmp, "nope.md"), summary="", note="",
            force=False), now))

        r3 = reg()
        tb = sb.cmd_add(r3, add_args(id="W-T-2"), now)
        sb.cmd_claim(r3, SimpleNamespace(id=tb["id"], session="s1", command="", note=""), now)
        sb.cmd_state(r3, SimpleNamespace(id=tb["id"], state="running", note="",
                                         next_action="", force=False), now)
        done = sb.cmd_done(r3, SimpleNamespace(id=tb["id"], evidence=ev, summary="本地校验",
                                               note="", force=False), now)
        check("done-ok", done["state"] == "done" and done["evidence_sha256"] == sb.sha256(ev)
              and done["evidence_verified_at"] == NOW,
              "sha=%s" % (done["evidence_sha256"] or "")[:12])
        check("done-frozen", bool(done.get("evidence_frozen_path"))
              and os.path.isfile(done["evidence_frozen_path"])
              and done.get("evidence_frozen_sha256") == done["evidence_sha256"],
              done.get("evidence_frozen_path") or "-")

        # 冻结幂等 + 漂移拦截 + reattest 留痕
        skip = sb.cmd_freeze(r3, SimpleNamespace(id=tb["id"], all_done=False, force=False), now)
        check("freeze-idempotent", tb["id"] in skip["skipped"] and not skip["frozen"],
              str(skip))
        with open(ev, "a", encoding="utf-8") as f:
            f.write("验收后追加\n")
        drift = sb.cmd_freeze(r3, SimpleNamespace(id=tb["id"], all_done=False, force=True), now)
        check("freeze-drift-skip", any("reattest" in x for x in drift["skipped"]), str(drift))
        old_sha = done["evidence_sha256"]
        rt = sb.cmd_reattest(r3, SimpleNamespace(id=tb["id"], evidence=ev, reason="补充审计附录",
                                                actor="auditor", sha_mode=None, note=""), now)
        check("reattest", rt["evidence_sha256"] == sb.sha256(ev)
              and rt["evidence_sha256"] != old_sha
              and len(rt.get("reattest_history") or []) == 1
              and rt["reattest_history"][0]["old_sha256"] == old_sha,
              "history=%d" % len(rt.get("reattest_history") or []))
        check("reattest-needs-reason", raises(sb.cmd_reattest, r3, SimpleNamespace(
            id=tb["id"], evidence=ev, reason="", actor="x", sha_mode=None, note=""), now))
        check("reattest-done-only", raises(sb.cmd_reattest, r3, SimpleNamespace(
            id=td["id"], evidence=ev, reason="x", actor="x", sha_mode=None, note=""), now))

        r4 = reg()
        tc = sb.cmd_add(r4, add_args(id="W-T-3"), now)
        check("bad-transition", raises(sb.cmd_state, r4, SimpleNamespace(
            id=tc["id"], state="done", note="", next_action="", force=False), now))

        # 重试链
        r5 = reg()
        tr = sb.cmd_add(r5, add_args(id="W-T-4", max_attempts=2), now)
        sb.cmd_claim(r5, SimpleNamespace(id=tr["id"], session="s1", command="", note=""), now)
        failed = sb.cmd_fail(r5, SimpleNamespace(id=tr["id"], reason="测试失败",
                                                 retry=True, force=False), now)
        check("fail-retry", failed["state"] == "queued" and failed["attempts"] == 1
              and failed["next_retry_at"] and failed["next_action"] == "retry",
              "state=%s retry_at=%s" % (failed["state"], failed["next_retry_at"]))
        check("retry-backoff", raises(sb.cmd_claim, r5, SimpleNamespace(
            id=tr["id"], session="s2", command="", note=""), now))
        later = sb.now_dt("2026-11-18T08:10:00+08:00")
        sb.cmd_claim(r5, SimpleNamespace(id=tr["id"], session="s2", command="", note=""), later)
        sb.cmd_fail(r5, SimpleNamespace(id=tr["id"], reason="又失败", retry=True,
                                        force=False), later)
        check("max-attempts", raises(sb.cmd_claim, r5, SimpleNamespace(
            id=tr["id"], session="s3", command="", note=""),
            sb.now_dt("2026-11-18T09:00:00+08:00")))

        r6 = reg()
        tf = sb.cmd_add(r6, add_args(id="W-T-5"), now)
        sb.cmd_claim(r6, SimpleNamespace(id=tf["id"], session="s1", command="", note=""), now)
        hard = sb.cmd_fail(r6, SimpleNamespace(id=tf["id"], reason="不可恢复",
                                               retry=False, force=False), now)
        check("fail-final", hard["state"] == "failed" and hard["next_action"] == "report")

        # steer / pull / progress
        r7 = reg()
        ts = sb.cmd_add(r7, add_args(id="W-T-6"), now)
        sb.cmd_claim(r7, SimpleNamespace(id=ts["id"], session="s1", command="", note=""), now)
        evt = sb.cmd_steer(r7, SimpleNamespace(id=ts["id"], reason="跑偏", interrupt="applied",
                                               ack="OK", command="app-x", note="",
                                               next_action=""), now)
        check("steer", len(evt["steer_events"]) == 1
              and evt["steer_events"][0]["ack"] == "OK"
              and evt["steer_events"][0]["commandId"] == "app-x")
        check("steer-bad", raises(sb.cmd_steer, r7, SimpleNamespace(
            id=ts["id"], reason="x", interrupt="maybe", ack="", command="", note="",
            next_action=""), now))
        pulled = sb.cmd_pull(r7, SimpleNamespace(id=ts["id"], note="拉起一次"), now)
        check("pull", pulled["pulls"] == 1)
        prog = sb.cmd_progress(r7, SimpleNamespace(id=ts["id"], note="有新证据"), now)
        check("progress", prog["last_progress_at"] == NOW)

        # 原子写 + 备份 + id 递增
        reg_path = os.path.join(tmp, "registry.json")
        r8 = reg()
        sb.cmd_add(r8, add_args(id=None), now)
        sb.save(reg_path, r8)
        sb.cmd_add(r8, add_args(id=None), now)
        sb.save(reg_path, r8)
        loaded = sb.load(reg_path)
        baks = [x for x in os.listdir(tmp) if x.startswith("registry.json.bak-")]
        check("atomic-backup", len(loaded["tasks"]) == 2 and len(baks) >= 1,
              "tasks=%d baks=%d" % (len(loaded["tasks"]), len(baks)))
        check("id-sequence", loaded["tasks"][1]["id"] == "W-20261118-002",
              loaded["tasks"][1]["id"])

        bad = os.path.join(tmp, "bad.json")
        open(bad, "w", encoding="utf-8").write("{not json")
        check("bad-registry", raises(sb.load, bad))
    finally:
        sb.FROZEN_DIR = old_frozen
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-20s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
