# -*- coding: utf-8 -*-
"""隔离工作区边界用例：worktree 必须真的隔离、真的能合并、清理不伤主仓。

覆盖（对标 W-20261118-011 验收）：
  1  ensure 建工作区：目录/分支/.git/git worktree 登记齐全
  2  ensure 幂等：重复调用复用同一路径，不重复登记
  3  skip-not-a-git-repo / skip-no-repo：非 git 仓不报错，派单可继续
  4  两 worker 同仓并行：各自只看见自己的文件，主仓 index 零污染
  5  status：ahead/dirty 如实上报
  6  merge 拒绝脏工作区（未提交改动不会被静默吞掉）
  7  merge 无提交 -> no-commits（不是失败）
  8  合并冲突 -> abort 回滚，主仓不留 MERGE_HEAD，分支保留
  9  remove --delete-branch：未合并分支保留（安全），已合并才删
  10 finish：合并 + 清理 + 删分支一条龙，产物落到主仓
  11 半成品目录：默认拒绝，--force 清理后重建成功
  12  node_modules 联接：工作区共享、remove 只解联接不删主仓实体
  13 detached HEAD 也能建（基准取 HEAD 提交）
  14 分支已存在：复用分支而不是 -b 失败
  15 主仓不在基准分支：merge 明确拒绝，不擅自切分支
  16 list/prune：目录被删后记录转 stale 并 git prune
  17 台账 JSON 合法、无 .tmp 残留
  18 任务 id 里带 ../ 不会逃出工作区根
  19 同一任务卡换仓库 -> repo-conflict（不静默覆盖）
  20 squash 合并会补 commit，不留半提交状态

用法：python -X utf8 test_worktree.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import worker_worktree as ww  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def out(r: subprocess.CompletedProcess) -> str:
    return (r.stdout or "").strip()


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def mk_repo(path: str, branch: str = "main") -> str:
    os.makedirs(path, exist_ok=True)
    git(["init", "-q", "-b", branch], path)
    git(["config", "user.email", "t@example.invalid"], path)
    git(["config", "user.name", "t"], path)
    git(["config", "core.autocrlf", "false"], path)
    write(os.path.join(path, ".gitignore"), "node_modules/\nfrontend/node_modules/\n")
    write(os.path.join(path, "README.md"), "base\n")
    git(["add", "-A"], path)
    git(["commit", "-qm", "init"], path)
    return ww.to_slash(path)


def commit_all(wt: str, msg: str) -> None:
    git(["add", "-A"], wt)
    git(["-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", msg], wt)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_worktree")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(240, "test_worktree")

    tmp = tempfile.mkdtemp(prefix="steward-wt-")
    repo = mk_repo(os.path.join(tmp, "repo"))
    ledger = os.path.join(tmp, "worktrees.json")
    cfg = {"root": os.path.join(tmp, "wts"), "branch_prefix": "wt/",
           "links": ["frontend/node_modules"]}

    try:
        # 1 创建
        a = ww.ensure("W-A", repo, cfg=cfg, ledger_path=ledger)
        check("ensure-created", a.get("ok") and a.get("created") and os.path.isdir(a["path"]),
              str(a.get("reason") or a.get("detail") or ""))
        check("branch-name", a.get("branch") == "wt/W-A", str(a.get("branch")))
        check("base-branch", a.get("base_branch") == "main", str(a.get("base_branch")))
        check("git-registered", "wt/W-A" in out(git(["worktree", "list", "--porcelain"], repo)))

        # 2 幂等
        a2 = ww.ensure("W-A", repo, cfg=cfg, ledger_path=ledger)
        check("ensure-idempotent", a2.get("ok") and a2.get("created") is False
              and a2.get("reused") and a2.get("path") == a.get("path"), str(a2.get("reason")))
        listed = out(git(["worktree", "list", "--porcelain"], repo)).count("worktree ")
        check("no-duplicate-worktree", listed == 2, "worktrees=%d（主仓+W-A，W-B 尚未建）" % listed)

        # 3 跳过
        plain = os.path.join(tmp, "plain")
        os.makedirs(plain, exist_ok=True)
        r = ww.ensure("X-1", plain, cfg=cfg, ledger_path=ledger)
        check("skip-not-a-git-repo", r.get("skipped") and r.get("reason") == "not-a-git-repo",
              str(r.get("reason")))
        r = ww.ensure("X-2", "", cfg=cfg, ledger_path=ledger)
        check("skip-no-repo", r.get("skipped") and r.get("reason") == "no-repo",
              str(r.get("reason")))

        # 4 两 worker 同仓并行（验收②：零共树冲突）
        b = ww.ensure("W-B", repo, cfg=cfg, ledger_path=ledger)
        write(os.path.join(a["path"], "file-a.txt"), "A\n")
        write(os.path.join(b["path"], "file-b.txt"), "B\n")
        check("isolation-main-clean", out(git(["status", "--porcelain"], repo)) == "",
              out(git(["status", "--porcelain"], repo))[:80])
        check("isolation-a-sees-own",
              os.path.exists(os.path.join(a["path"], "file-a.txt"))
              and not os.path.exists(os.path.join(a["path"], "file-b.txt")))
        check("isolation-b-sees-own",
              os.path.exists(os.path.join(b["path"], "file-b.txt"))
              and not os.path.exists(os.path.join(b["path"], "file-a.txt")))

        # 5 status
        commit_all(a["path"], "A work")
        st = ww.status("W-A", ledger_path=ledger)
        check("status-ahead", st.get("ok") and st.get("ahead") == 1 and st.get("dirty") == [],
              "ahead=%s dirty=%s" % (st.get("ahead"), st.get("dirty")))

        # 6 脏工作区拒绝合并
        write(os.path.join(b["path"], "dirty.txt"), "x\n")
        r = ww.merge("W-B", ledger_path=ledger)
        check("merge-dirty-refused", not r.get("ok") and r.get("reason") == "worktree-dirty"
              and any("dirty.txt" in f for f in (r.get("files") or [])), str(r.get("reason")))
        os.remove(os.path.join(b["path"], "dirty.txt"))

        # 7 无提交
        c = ww.ensure("W-C", repo, cfg=cfg, ledger_path=ledger)
        r = ww.merge("W-C", ledger_path=ledger)
        check("merge-no-commits", r.get("ok") and r.get("merged") is False
              and r.get("reason") == "no-commits", str(r.get("reason")))

        # 4b 并行合并（两条分支先后合并，互不冲突）
        commit_all(b["path"], "B work")
        m1 = ww.merge("W-A", ledger_path=ledger)
        m2 = ww.merge("W-B", ledger_path=ledger)
        check("parallel-merge-both-ok", m1.get("ok") and m1.get("merged")
              and m2.get("ok") and m2.get("merged"), "%s / %s" % (m1.get("reason"), m2.get("reason")))
        check("parallel-both-files-landed",
              os.path.exists(os.path.join(repo, "file-a.txt"))
              and os.path.exists(os.path.join(repo, "file-b.txt")))
        check("parallel-main-clean", out(git(["status", "--porcelain"], repo)) == "",
              out(git(["status", "--porcelain"], repo))[:80])

        # 8 冲突 -> abort 回滚
        d = ww.ensure("W-D", repo, cfg=cfg, ledger_path=ledger)
        e = ww.ensure("W-E", repo, cfg=cfg, ledger_path=ledger)
        write(os.path.join(d["path"], "same.txt"), "from D\n")
        write(os.path.join(e["path"], "same.txt"), "from E\n")
        commit_all(d["path"], "D same")
        commit_all(e["path"], "E same")
        md = ww.merge("W-D", ledger_path=ledger)
        me = ww.merge("W-E", ledger_path=ledger)
        check("conflict-detected", md.get("ok") and not me.get("ok")
              and me.get("reason") == "merge-conflict", str(me.get("reason")))
        check("conflict-aborted", me.get("aborted") is True
              and not os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD")),
              "aborted=%s" % me.get("aborted"))
        check("conflict-branch-kept", ww.branch_exists(repo, "wt/W-E")
              and os.path.exists(os.path.join(e["path"], "same.txt")))

        # 9 未合并分支安全保留
        rm_e = ww.remove("W-E", repo, delete_branch=True, ledger_path=ledger)
        check("remove-keeps-unmerged-branch", rm_e.get("ok") and rm_e.get("branch_kept") == "wt/W-E"
              and ww.branch_exists(repo, "wt/W-E"), str(rm_e.get("branch_kept")))

        # 10 finish 一条龙
        f = ww.ensure("W-F", repo, cfg=cfg, ledger_path=ledger)
        write(os.path.join(f["path"], "file-f.txt"), "F\n")
        commit_all(f["path"], "F work")
        fin = ww.finish("W-F", repo, ledger_path=ledger)
        check("finish-ok", fin.get("ok") and fin["merge"].get("merged"), str(fin.get("stage")))
        check("finish-worktree-gone", not os.path.exists(f["path"]))
        check("finish-branch-gone", not ww.branch_exists(repo, "wt/W-F"))
        check("finish-file-in-main", os.path.exists(os.path.join(repo, "file-f.txt")))

        # 11 半成品目录
        broken = os.path.join(cfg["root"], "W-H")
        write(os.path.join(broken, "junk.txt"), "x\n")
        r = ww.ensure("W-H", repo, cfg=cfg, ledger_path=ledger)
        check("halfbaked-refused", not r.get("ok") and r.get("reason") == "path-exists-not-worktree",
              str(r.get("reason")))
        r = ww.ensure("W-H", repo, cfg=cfg, ledger_path=ledger, force=True)
        check("halfbaked-force-rebuilt", r.get("ok") and r.get("created")
              and os.path.exists(os.path.join(r.get("path") or "", ".git")), str(r.get("reason")))

        # 12 node_modules 联接：共享但安全解除
        nm = os.path.join(repo, "frontend", "node_modules")
        write(os.path.join(nm, "marker.txt"), "keep\n")
        l = ww.ensure("W-L", repo, cfg=cfg, ledger_path=ledger)
        wt_nm = os.path.join(l["path"], "frontend", "node_modules")
        check("link-shared", os.path.isfile(os.path.join(wt_nm, "marker.txt")))
        ww.remove("W-L", repo, ledger_path=ledger)
        check("link-unlinked-source-intact",
              not os.path.exists(wt_nm)
              and os.path.isfile(os.path.join(nm, "marker.txt")))

        # 13 detached HEAD
        repo2 = mk_repo(os.path.join(tmp, "repo2"))
        git(["checkout", "-q", "--detach"], repo2)
        det = ww.ensure("W-DET", repo2, cfg=cfg, ledger_path=ledger)
        check("detached-head-ok", det.get("ok") and det.get("created")
              and not det.get("base_branch"), str(det.get("reason")))

        # 14 分支已存在 -> 复用
        git(["branch", "wt/W-P"], repo)
        p = ww.ensure("W-P", repo, cfg=cfg, ledger_path=ledger)
        check("existing-branch-reused", p.get("ok") and p.get("branch") == "wt/W-P",
              str(p.get("reason")))

        # 15 主仓不在基准分支
        rq = ww.ensure("W-R", repo, cfg=cfg, ledger_path=ledger)
        write(os.path.join(rq["path"], "file-r.txt"), "R\n")
        commit_all(rq["path"], "R work")
        git(["checkout", "-q", "-b", "other"], repo)
        r = ww.merge("W-R", ledger_path=ledger)
        check("merge-wrong-branch-refused", not r.get("ok")
              and r.get("reason") == "main-repo-not-on-target", str(r.get("reason")))
        git(["checkout", "-q", "main"], repo)
        r = ww.merge("W-R", ledger_path=ledger)
        check("merge-right-branch-ok", r.get("ok") and r.get("merged"), str(r.get("reason")))

        # 16 prune：目录被删 -> 记录转 stale
        shutil.rmtree(c["path"], ignore_errors=True)
        pr = ww.prune(repo, ledger_path=ledger)
        check("prune-stale", "W-C" in (pr.get("pruned") or []), str(pr.get("pruned")))
        rec = json.load(open(ledger, encoding="utf-8"))["worktrees"]["W-C"]
        check("prune-marks-removed", bool(rec.get("removed_at")) and rec.get("stale") is True)

        # 17 台账合法
        data = json.load(open(ledger, encoding="utf-8"))
        check("ledger-shape", isinstance(data.get("worktrees"), dict) and data.get("updated_at"))
        check("no-tmp-residue", not os.path.exists(ledger + ".tmp"))

        # 18 路径逃逸
        evil = ww.ensure("../../evil", repo, cfg=cfg, ledger_path=ledger)
        check("safe-name-inside-root", evil.get("ok")
              and ww.under(evil["path"], cfg["root"])
              and ww.norm(os.path.dirname(evil["path"])) == ww.norm(cfg["root"]),
              str(evil.get("path")))

        # 19 同任务换仓库
        r = ww.ensure("W-A", repo2, cfg=cfg, ledger_path=ledger)
        check("repo-conflict", not r.get("ok") and not r.get("skipped")
              and r.get("reason") == "repo-conflict", str(r.get("reason")))

        # 20 squash
        s = ww.ensure("W-S", repo, cfg=cfg, ledger_path=ledger)
        write(os.path.join(s["path"], "file-s.txt"), "S\n")
        commit_all(s["path"], "S work")
        ms = ww.merge("W-S", ledger_path=ledger, squash=True)
        log = out(git(["log", "-1", "--pretty=%s"], repo))
        check("squash-committed", ms.get("ok") and ms.get("merged")
              and log.startswith("merge(W-S)"), "%s | %s" % (ms.get("reason"), log))
        check("squash-clean-tree", out(git(["status", "--porcelain"], repo)) == "",
              out(git(["status", "--porcelain"], repo))[:80])

    finally:
        # 清理：先把工作区从 git 注销，再删临时目录（不碰用户目录）
        for repo_path in (repo, ww.to_slash(os.path.join(tmp, "repo2"))):
            if os.path.isdir(repo_path):
                git(["worktree", "prune"], repo_path)
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-30s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
