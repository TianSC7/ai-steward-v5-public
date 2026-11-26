# -*- coding: utf-8 -*-
"""worker worktree：给每个写码任务一个物理隔离的工作区（git worktree）。

为什么需要：
  调度器默认「同项目只跑 1 个写手」（max_running_per_project=1），因为两个 worker
  共享同一工作区时会在 .git/index、同一条分支、同一批文件上互相踩踏（W-009 类共树冲突）。
  拆 worktree 后，同一仓可以真正并行：各自独立目录 + 独立分支，验收后合并再清理。

对标与来源：
  - 业界编排器（Claude Code / Conductor / Crystal 等）的 worker sandbox 标配；
  - newcad/scripts/worktree_manager.py（443 行，含 2026-09-14 T301 修复）在本仓的移植适配：
      嵌套分支名 wt/<x> 写入受限时自动降级为顶层级分支 <x>；
      创建后校验 .git 文件，杜绝「cd 进去不是 git 仓库」的静默坏工作区；
      remove 遇半成品目录先 prune 再 --force 清理，且失败必须如实上报；
      node_modules 用 Windows Junction 秒级共享，避免每个工作区重装依赖。

台账：state/worktrees.json（原子写 + 跨进程锁，与 registry.json 分开：
  任务卡只经 steward_board.py 写；worktree 生命周期只经本脚本写）。

用法：
  python -X utf8 worker_worktree.py ensure --id W-... [--project dll] [--repo PATH] [--json]
  python -X utf8 worker_worktree.py list [--repo PATH] [--json]
  python -X utf8 worker_worktree.py status --id W-... [--repo PATH] [--json]
  python -X utf8 worker_worktree.py merge  --id W-... [--into BRANCH] [--squash] [--json]
  python -X utf8 worker_worktree.py remove --id W-... [--delete-branch] [--force] [--json]
  python -X utf8 worker_worktree.py finish --id W-... [--keep] [--json]   # 合并 + 清理一条龙
  python -X utf8 worker_worktree.py prune  [--repo PATH] [--json]
  python -X utf8 worker_worktree.py path   --id W-... [--repo PATH]

退出码：0 成功/已复用；1 失败；3 跳过（不是 git 仓库，派单可继续但无隔离）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CONFIG = os.path.join(ROOT, "config", "steward.json")
DEFAULT_LEDGER = os.path.join(ROOT, "state", "worktrees.json")

sys.path.insert(0, HERE)
try:  # 复用任务台的跨进程锁；它只依赖标准库
    from steward_board import locked as _file_lock
except Exception:  # pragma: no cover - 极端情况下退化为无锁（仍原子写）
    _file_lock = None

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), "worktrees", "ai-steward")
DEFAULT_LINKS = ["frontend/node_modules", "node_modules", ".ai-sessions"]
BRANCH_PREFIX = "wt/"


def now_iso() -> str:
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _git(args: list[str], cwd: str, timeout: float = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git"] + args, 127, "", str(exc))


def _out(r: subprocess.CompletedProcess) -> str:
    return (r.stdout or "").strip()


def _err(r: subprocess.CompletedProcess) -> str:
    text = (r.stderr or r.stdout or "").strip()
    return text.splitlines()[-1] if text else ""


def norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path or "")))


def to_slash(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/")


def under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    p, r = norm(path), norm(root)
    return p == r or p.startswith(r + os.sep)


def safe_name(name: str) -> str:
    """目录名 + 分支名双重安全：git 拒绝 refs 里的 `..`、结尾 `.`、`.lock`。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "").strip())
    cleaned = re.sub(r"\.{2,}", "_", cleaned).strip("._-")
    if cleaned.lower().endswith(".lock"):
        cleaned = cleaned[:-5].strip("._-")
    return cleaned[:64] or "task"


def load_config(path: str = DEFAULT_CONFIG) -> dict:
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    wt = ((cfg.get("dispatch") or {}).get("worktree") or {})
    return wt if isinstance(wt, dict) else {}


def git_repo_root(path: str) -> str | None:
    if not path or not os.path.isdir(path):
        return None
    r = _git(["rev-parse", "--show-toplevel"], path, timeout=30)
    if r.returncode != 0:
        return None
    root = _out(r)
    return to_slash(root) if root else None


def head_branch(repo: str) -> str:
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo, timeout=30)
    b = _out(r)
    return "" if not b or b == "HEAD" else b


def head_commit(repo: str, ref: str = "HEAD") -> str:
    return _out(_git(["rev-parse", ref], repo, timeout=30))


def branch_exists(repo: str, branch: str) -> bool:
    return _git(["show-ref", "--verify", "--quiet", "refs/heads/" + branch], repo,
                timeout=30).returncode == 0


def worktree_branch_of(repo: str, path: str) -> str:
    """从 git worktree list --porcelain 读该路径实际挂载的分支（取不到返回空串）。"""
    r = _git(["worktree", "list", "--porcelain"], repo, timeout=30)
    if r.returncode != 0:
        return ""
    cur_path, cur_branch = "", ""
    for line in list((r.stdout or "").splitlines()) + [""]:
        if line.startswith("worktree "):
            cur_path, cur_branch = line.split(" ", 1)[1].strip(), ""
        elif line.startswith("branch "):
            cur_branch = line.split(" ", 1)[1].strip().replace("refs/heads/", "")
        elif not line.strip() and cur_path:
            if norm(cur_path) == norm(path):
                return cur_branch
            cur_path, cur_branch = "", ""
    return ""


def worktree_valid(path: str) -> tuple[bool, str]:
    """工作区可用性：.git 文件在，且 git 能解析 git-dir。"""
    if not os.path.exists(path):
        return False, "目录不存在"
    if not os.path.exists(os.path.join(path, ".git")):
        return False, ".git 文件未生成"
    r = _git(["-C", path, "rev-parse", "--git-dir"], path, timeout=30)
    if r.returncode != 0:
        return False, "git rev-parse 失败: %s" % _err(r)
    return True, _out(r)


def is_junction(path: str) -> bool:
    if os.path.islink(path):
        return True
    try:
        attrs = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def create_link(src: str, dst: str) -> bool:
    if os.path.exists(dst) or os.path.islink(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if sys.platform == "win32":
        # cmd 的 mklink 不认正斜杠路径（会当成开关：无效参数 - "Users"），必须用本地分隔符
        r = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.normpath(dst),
                            os.path.normpath(src)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError((r.stdout or r.stderr or "").strip()[:120])
    else:
        os.symlink(src, dst, target_is_directory=True)
    return True


def remove_link(dst: str) -> bool:
    """只解联接，绝不递归删除源目录（node_modules 是主仓实体）。"""
    if not os.path.exists(dst) and not os.path.islink(dst):
        return False
    if not is_junction(dst):
        return False  # 真实目录不碰
    try:
        os.rmdir(dst)
    except OSError:
        subprocess.run(["cmd", "/c", "rmdir", os.path.normpath(dst)]
                       if sys.platform == "win32" else ["rmdir", dst],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return True


def _branch_fallback(branch: str) -> str:
    """wt/<id> 在 refs 命名空间写入受限时降级为顶层级 <id>。"""
    return safe_name(branch.split("/")[-1]) if "/" in branch else branch


def ledger_load(path: str) -> dict:
    if not os.path.exists(path):
        return {"version": 1, "updated_at": None, "worktrees": {}}
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("LEDGER-BAD %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise SystemExit("LEDGER-BAD %s: 根不是对象" % path)
    if not isinstance(data.get("worktrees"), dict):
        data["worktrees"] = {}
    return data


def ledger_save(path: str, data: dict) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    data["updated_at"] = now_iso()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ledger_lock(path: str):
    return _file_lock(path) if _file_lock else _NullLock()


def repo_for(project: str, repo: str = "", cfg: dict | None = None) -> str:
    """把 project 名映射到真实仓库根：显式 --repo > worktree.repos > workspace_map。"""
    if repo:
        return to_slash(repo) if os.path.isdir(repo) else repo
    cfg = cfg if cfg is not None else load_config()
    if not project:
        return ""
    repos = cfg.get("repos") or {}
    cand = repos.get(project)
    if not cand:
        try:
            full = json.load(open(DEFAULT_CONFIG, encoding="utf-8"))
            cand = ((full.get("dispatch") or {}).get("workspace_map") or {}).get(project)
        except (OSError, ValueError):
            cand = ""
    return str(cand or "")


def worktree_root(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    return str(cfg.get("root") or DEFAULT_ROOT)


def plan_paths(task_id: str, repo: str, cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else load_config()
    prefix = str(cfg.get("branch_prefix") or BRANCH_PREFIX)
    return {
        "id": task_id,
        "repo": repo,
        "path": to_slash(os.path.join(worktree_root(cfg), safe_name(task_id))),
        "branch": prefix + safe_name(task_id),
        "links": list(cfg.get("links") or DEFAULT_LINKS),
    }


def ensure(task_id: str, repo: str, cfg: dict | None = None, ledger_path: str = DEFAULT_LEDGER,
           base: str = "", force: bool = False) -> dict:
    """幂等创建/复用工作区。返回结果字典；调用方按 ok/skipped 决定是否继续派单。"""
    cfg = cfg if cfg is not None else load_config()
    if not str(repo or "").strip():
        return {"ok": False, "skipped": True, "reason": "no-repo", "repo": "",
                "id": task_id, "hint": "没有可用的仓库路径（--repo 或 worktree.repos）"}
    repo_root = git_repo_root(repo)
    if not repo_root:
        return {"ok": False, "skipped": True, "reason": "not-a-git-repo", "repo": repo,
                "id": task_id,
                "hint": "工作区不存在或不是 git 仓库；派单可继续，但该任务没有物理隔离"}

    plan = plan_paths(task_id, repo_root, cfg)
    wt_path, branch = plan["path"], plan["branch"]

    with ledger_lock(ledger_path):
        led = ledger_load(ledger_path)
        rec = led["worktrees"].get(task_id)
        if rec and rec.get("repo") and norm(rec["repo"]) != norm(repo_root) and \
                not rec.get("removed_at") and os.path.exists(rec.get("path") or ""):
            return {"ok": False, "skipped": False, "reason": "repo-conflict",
                    "id": task_id, "repo": repo_root, "existing_repo": rec["repo"],
                    "hint": "同一任务卡已有其他仓库的活跃工作区；先 remove 或换 id"}

        if rec and not rec.get("removed_at"):
            ok, detail = worktree_valid(rec.get("path") or "")
            if ok and not worktree_belongs(repo_root, rec["path"])[0]:
                ok, detail = False, "工作区属于别的仓库：" + worktree_belongs(repo_root, rec["path"])[1]
            if ok:
                rec["updated_at"] = now_iso()
                rec["branch"] = worktree_branch_of(repo_root, rec["path"]) or rec.get("branch") or branch
                led["worktrees"][task_id] = rec
                ledger_save(ledger_path, led)
                return {"ok": True, "created": False, "reused": True, "id": task_id,
                        "repo": repo_root, "path": rec["path"], "branch": rec["branch"],
                        "base_branch": rec.get("base_branch"), "links": rec.get("links") or [],
                        "detail": detail}
            rec["stale_reason"] = detail

        # 目录已存在但台账没有（或已失效）：能认就认，认不出就清（只清本工具 root 下）
        if os.path.exists(wt_path):
            ok, detail = worktree_valid(wt_path)
            if ok and not worktree_belongs(repo_root, wt_path)[0]:
                # 目录是别的仓的工作区：不能认。认了就会把别家内容往本仓合。
                return {"ok": False, "skipped": False, "reason": "worktree-foreign-repo",
                        "id": task_id, "repo": repo_root, "path": wt_path,
                        "detail": worktree_belongs(repo_root, wt_path)[1],
                        "hint": "该目录是别的仓库的工作区，拒绝收养；确认无用后手工移走，或换任务号"}
            if ok:
                actual = worktree_branch_of(repo_root, wt_path) or branch
                rec = {"id": task_id, "repo": repo_root, "path": wt_path, "branch": actual,
                       "base_branch": head_branch(repo_root) or "",
                       "base_commit": head_commit(repo_root), "created_at": now_iso(),
                       "updated_at": now_iso(), "adopted": True,
                       "links": list(cfg.get("links") or DEFAULT_LINKS)}
                led["worktrees"][task_id] = rec
                ledger_save(ledger_path, led)
                return {"ok": True, "created": False, "reused": True, "adopted": True,
                        "id": task_id, "repo": repo_root, "path": wt_path,
                        "branch": actual, "base_branch": rec["base_branch"],
                        "links": rec["links"], "detail": detail}
            if not force:
                return {"ok": False, "skipped": False, "reason": "path-exists-not-worktree",
                        "id": task_id, "repo": repo_root, "path": wt_path, "detail": detail,
                        "hint": "目标目录存在但不是可用工作区；确认无残留后加 --force 清理"}
            if not under(wt_path, worktree_root(cfg)):
                return {"ok": False, "skipped": False, "reason": "refuse-delete-outside-root",
                        "path": wt_path, "hint": "拒绝删除工作区根目录之外的路径"}
            shutil.rmtree(wt_path, ignore_errors=True)
            if os.path.exists(wt_path):
                return {"ok": False, "skipped": False, "reason": "stale-dir-not-removed",
                        "path": wt_path}

        base_branch = head_branch(repo_root)
        base_ref = base or base_branch or head_commit(repo_root)
        attempts = [branch]
        fb = _branch_fallback(branch)
        if fb != branch:
            attempts.append(fb)

        used_branch, last_err = "", ""
        for attempt in attempts:
            cmd = ["worktree", "add"]
            if not branch_exists(repo_root, attempt):
                cmd += ["-b", attempt]
            cmd.append(wt_path)
            if not branch_exists(repo_root, attempt) and base_ref:
                cmd.append(base_ref)
            r = _git(cmd, repo_root)
            if r.returncode == 0:
                used_branch = attempt
                break
            last_err = _err(r)
            # 失败尝试可能留下半成品目录：只清本次刚产生的，且必须在 root 下
            if os.path.exists(wt_path) and under(wt_path, worktree_root(cfg)):
                ok2, _ = worktree_valid(wt_path)
                if not ok2:
                    shutil.rmtree(wt_path, ignore_errors=True)

        if not used_branch:
            return {"ok": False, "skipped": False, "reason": "worktree-add-failed",
                    "id": task_id, "repo": repo_root, "path": wt_path,
                    "branch": branch, "error": last_err,
                    "hint": "git worktree add 失败；嵌套 ref 写入受限时脚本会自动降级顶层级分支名"}

        ok, detail = worktree_valid(wt_path)
        if not ok:
            return {"ok": False, "skipped": False, "reason": "worktree-invalid",
                    "id": task_id, "repo": repo_root, "path": wt_path,
                    "detail": detail,
                    "hint": "创建后校验失败（.git 未落盘）；remove --force 后换非沙箱终端重试"}

        linked = []
        for rel in (cfg.get("links") or DEFAULT_LINKS):
            src = os.path.join(repo_root, rel)
            dst = os.path.join(wt_path, rel)
            if os.path.isdir(src) and not is_junction(src):
                try:
                    if create_link(src, dst):
                        linked.append(rel)
                except Exception:
                    pass

        rec = {
            "id": task_id, "repo": repo_root, "path": to_slash(wt_path),
            "branch": used_branch, "base_branch": base_branch,
            "base_commit": head_commit(repo_root, base_ref or "HEAD"),
            "created_at": now_iso(), "updated_at": now_iso(),
            "links": linked, "merged_at": None, "merged_commit": None, "removed_at": None,
        }
        led["worktrees"][task_id] = rec
        ledger_save(ledger_path, led)
        out = {"ok": True, "created": True, "reused": False, "id": task_id,
               "repo": repo_root, "path": rec["path"], "branch": used_branch,
               "base_branch": base_branch, "base_commit": rec["base_commit"],
               "links": linked, "git_dir": detail}
        if used_branch != branch:
            out["branch_fallback"] = True
            out["note"] = "嵌套分支名写入受限，已降级为 %s" % used_branch
        return out


def find_record(led, task_id: str, repo: str = "") -> dict | None:
    rec = (led.get("worktrees") or {}).get(task_id)
    if rec and repo:
        if rec.get("repo") and norm(rec["repo"]) != norm(git_repo_root(repo) or repo):
            return None
    return rec


def status(task_id: str, repo: str = "", ledger_path: str = DEFAULT_LEDGER) -> dict:
    led = ledger_load(ledger_path)
    rec = find_record(led, task_id, repo)
    if not rec:
        return {"ok": False, "reason": "not-found", "id": task_id}
    path = rec.get("path") or ""
    repo_root = git_repo_root(rec.get("repo") or repo or "")
    ok, detail = worktree_valid(path)
    out = {"ok": True, "id": task_id, "repo": rec.get("repo"), "path": path,
           "branch": rec.get("branch"), "base_branch": rec.get("base_branch"),
           "exists": os.path.exists(path), "valid": ok, "detail": detail,
           "merged_at": rec.get("merged_at"), "merged_commit": rec.get("merged_commit"),
           "removed_at": rec.get("removed_at")}
    if ok and repo_root:
        dirty = _out(_git(["-C", path, "status", "--porcelain"], path, timeout=30))
        out["dirty"] = [ln for ln in dirty.splitlines() if ln.strip()]
        base = rec.get("base_branch") or head_branch(repo_root)
        if base:
            ahead = _out(_git(["-C", path, "rev-list", "--count", "%s..%s" % (base, "HEAD")],
                              path, timeout=30))
            behind = _out(_git(["-C", path, "rev-list", "--count", "%s..%s" % ("HEAD", base)],
                               path, timeout=30))
            out["ahead"] = int(ahead) if ahead.isdigit() else None
            out["behind"] = int(behind) if behind.isdigit() else None
        out["head"] = head_commit(path)
    return out


def _np(path: str) -> str:
    """仓库相对路径归一键：统一斜杠、去引号，Windows 下大小写不敏感。"""
    text = str(path or "").strip().strip('"').replace("\\", "/")
    return text.strip("/").lower()


def git_dir_file(repo_root: str, name: str) -> str:
    """主仓 / 链接工作区通用的 .git 内文件路径（链接工作区的 .git 是文件不是目录）。"""
    p = _out(_git(["rev-parse", "--git-path", name], repo_root, timeout=30))
    return os.path.normpath(os.path.join(repo_root, p)) if p else os.path.join(repo_root, ".git", name)


def worktree_belongs(repo_root: str, wt_path: str) -> tuple[bool, str]:
    """这个工作区目录是不是 repo_root 的（而不是别的仓的）。只看 .git 指针指向哪个仓，不看目录名。
    2026-11-25 G48B / G52：别的仓留下的同名目录被当成本仓沙箱「收养」，后续合入要么合错仓、要么拿旧分支当成果。"""
    try:
        text = open(os.path.join(wt_path, ".git"), encoding="utf-8", errors="replace").read().strip()
    except OSError:
        return False, "没有 .git 指针文件"
    if not text.lower().startswith("gitdir:"):
        return False, ".git 不是工作区指针文件"
    pointer = text.split(":", 1)[1].strip()
    gitdir = os.path.normcase(os.path.abspath(os.path.join(wt_path, pointer)))
    r = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo_root, timeout=30)
    common = os.path.normcase(os.path.abspath((_out(r) or "").strip() or os.path.join(repo_root, ".git")))
    if gitdir.startswith(common.rstrip("\\/") + os.sep):
        return True, ""
    return False, "工作区指向 %s，不是本仓 %s 的工作区" % (gitdir, common)


def wait_index_lock(repo_root: str, timeout_sec: float = 60.0, poll_sec: float = 0.5) -> bool:
    """等别的 git 进程放开 index.lock。并行道下几单同时收口很常见，撞锁不该记到工人头上。"""
    lock = git_dir_file(repo_root, "index.lock")
    deadline = time.time() + max(0.0, float(timeout_sec))
    while os.path.exists(lock):
        if time.time() >= deadline:
            return False
        time.sleep(poll_sec)
    return True


def branch_changed_files(repo_root: str, target: str, branch: str) -> list[str]:
    """分支相对「与目标的分叉点」提交过的文件——合并会动到主仓的正是这些路径。"""
    base = _out(_git(["merge-base", target, branch], repo_root, timeout=30))
    if not base:
        return []
    out = _out(_git(["diff", "--name-only", "-z", base, branch], repo_root, timeout=60))
    return [p for p in out.split("\0") if p]


def main_dirty_paths(repo_root: str) -> tuple[dict, list]:
    """主仓工作区的未提交手改：({归一键: (状态码, 原路径)}, [(归一键, 原路径)])。"""
    r = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], repo_root, timeout=60)
    tracked: dict = {}
    untracked: list = []
    parts = [p for p in (r.stdout or "").split("\0") if p]
    i = 0
    while i < len(parts):
        xy, path = parts[i][:2], parts[i][3:]
        i += 1
        if "R" in xy or "C" in xy:      # 改名 / 拷贝：下一段是原路径
            i += 1
        key = _np(path)
        if not key:
            continue
        if xy == "??":
            untracked.append((key, path))
        elif xy != "!!":
            tracked[key] = (xy, path)
    return tracked, untracked


def protect_live_edits(repo_root: str, paths: list[str], task_id: str = "") -> dict:
    """合入前把主仓里会被这次合并覆盖的手改落成一笔保护性提交。

    只提交点名的路径（--only，不动暂存区里别的货）；提交的就是看板此刻在用的那几份字节，
    运行态零变化。目的只是让 git 肯合并——合不合得拢仍由 merge 自己判，真冲突照常回冲突。
    """
    paths = [p for p in dict.fromkeys(paths or []) if p]
    if not paths:
        return {"ok": True, "committed": False, "files": []}
    if not wait_index_lock(repo_root, 30):
        return {"ok": False, "error": "index.lock 被占用", "files": paths}
    untracked_hit = {k for k, _ in main_dirty_paths(repo_root)[1]}
    fresh = [p for p in paths if _np(p) in untracked_hit]
    if fresh:
        a = _git(["add", "--"] + fresh, repo_root, timeout=60)
        if a.returncode != 0:
            return {"ok": False, "error": _err(a)[:300], "files": paths}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    msg = ("chore(maint) %s protective commit (auto, pre-merge %s): live-tree edits overlapping the delivery\n\n"
           "Commits the exact working bytes the board already serves, so git will let the verified delivery\n"
           "merge. No behaviour change.\n\nFiles:\n%s" % (stamp, task_id, "\n".join("- " + p for p in paths)))
    c = _git(["commit", "--only", "-q", "-m", msg, "--"] + paths, repo_root, timeout=120)
    if c.returncode != 0:
        text = ((c.stdout or "") + (c.stderr or "")).strip()
        if "nothing to commit" in text.lower() or "no changes" in text.lower():
            return {"ok": True, "committed": False, "files": paths}
        return {"ok": False, "error": (text.splitlines() or [""])[-1][:300], "files": paths}
    return {"ok": True, "committed": True, "commit": head_commit(repo_root), "files": paths}


def _overwritten_paths(err_text: str) -> list[str]:
    """从 git 的「would be overwritten by merge」报错里抠出被挡的路径。"""
    out: list[str] = []
    take = False
    for ln in (err_text or "").splitlines():
        low = ln.lower()
        if "would be overwritten by merge" in low:
            take = True
            continue
        if take:
            if ln.startswith("\t"):
                out.append(ln.strip())
            elif out or ln.strip():
                take = False
    return out


def merge(task_id: str, repo: str = "", into: str = "", squash: bool = False,
          allow_dirty: bool = False, ledger_path: str = DEFAULT_LEDGER) -> dict:
    led = ledger_load(ledger_path)
    rec = find_record(led, task_id, repo)
    if not rec:
        return {"ok": False, "reason": "not-found", "id": task_id}
    repo_root = git_repo_root(rec.get("repo") or repo or "")
    if not repo_root:
        return {"ok": False, "reason": "not-a-git-repo", "id": task_id}
    wt_path = rec.get("path") or ""
    ok, detail = worktree_valid(wt_path)
    if not ok:
        return {"ok": False, "reason": "worktree-invalid", "id": task_id,
                "path": wt_path, "detail": detail}
    if not worktree_belongs(repo_root, wt_path)[0]:
        # 认错仓比合不进去严重得多：沙箱不是本仓的，就什么都不合
        return {"ok": False, "reason": "worktree-foreign-repo", "id": task_id, "path": wt_path,
                "detail": worktree_belongs(repo_root, wt_path)[1],
                "hint": "沙箱目录是别的仓库的工作区，拒绝往本仓合"}

    branch = worktree_branch_of(repo_root, wt_path) or rec.get("branch") or ""
    target = into or rec.get("base_branch") or head_branch(repo_root)
    if not target:
        return {"ok": False, "reason": "no-target-branch", "id": task_id}
    if not branch:
        return {"ok": False, "reason": "no-branch", "id": task_id, "path": wt_path}
    if not branch_exists(repo_root, branch):
        return {"ok": False, "reason": "branch-missing", "id": task_id, "branch": branch}
    current = head_branch(repo_root)
    if current != target:
        return {"ok": False, "reason": "main-repo-not-on-target", "id": task_id,
                "target": target, "main_head": current or "(detached)",
                "hint": "主仓当前不在 %s；先 git -C %s checkout %s 再合并" % (target, repo_root, target)}

    dirty = [ln for ln in _out(_git(["-C", wt_path, "status", "--porcelain"], wt_path, timeout=30)
                               ).splitlines() if ln.strip()]
    if dirty and not allow_dirty:
        return {"ok": False, "reason": "worktree-dirty", "id": task_id,
                "files": dirty[:20], "hint": "工作区有未提交改动；先 commit，或显式 --allow-dirty 只合并已提交部分"}

    ahead_raw = _out(_git(["-C", repo_root, "rev-list", "--count", "%s..%s" % (target, branch)],
                          repo_root, timeout=60))
    branch_sha = head_commit(repo_root, branch)
    target_sha = head_commit(repo_root, target)
    if ahead_raw.isdigit() and int(ahead_raw) == 0:
        same_head = bool(branch_sha) and branch_sha == target_sha
        return {"ok": True, "merged": False, "reason": "no-commits", "id": task_id,
                "branch": branch, "target": target,
                "ahead": 0, "branch_sha": branch_sha, "target_sha": target_sha,
                "same_head": same_head,
                "hint": ("分支与 %s 同头，无需合并" % target) if same_head else
                        ("分支没有领先 %s 的提交；确认 Builder 的改动是否已提交到本分支" % target)}

    # V5.0：合并前先看主仓。旧实现不看，被 git 一拒就记成 merge-conflict，还把整张脏清单当「冲突」——
    # 2026-11-25 下午 G44R3 / G47R2 双门全绿却合不进，就是这么死的。
    if not wait_index_lock(repo_root, 60):
        return {"ok": False, "reason": "repo-busy", "id": task_id, "branch": branch, "target": target,
                "error": "index.lock 一直被占用", "conflicts": [],
                "hint": "仓库正被别的 git 操作占用，稍后重试即可；不是施工的问题"}
    protective: dict = {}
    touched = branch_changed_files(repo_root, target, branch)
    if touched:
        tracked, untracked = main_dirty_paths(repo_root)
        tset = {_np(p) for p in touched}
        overlap = [p for k, (_xy, p) in tracked.items() if k in tset] + [p for k, p in untracked if k in tset]
        if overlap:
            protective = protect_live_edits(repo_root, overlap, task_id)
            if not protective.get("ok"):
                # 保护性提交没落下：此时去合并只会被 git 以「会被覆盖」拒掉，直接如实回报
                return {"ok": False, "reason": "target-dirty-overlap", "id": task_id, "branch": branch,
                        "target": target, "error": str(protective.get("error") or "protective commit failed")[:600],
                        "conflicts": overlap[:40], "aborted": False, "protective_commit": protective,
                        "hint": "主干工作区同一路径有未提交手改，自动保护性提交没能落下；属工厂侧问题而非施工缺陷，排除后可直接补合入"}

    msg = "merge(%s): %s -> %s" % (task_id, branch, target)
    cmd = ["merge", "--no-ff" if not squash else "--squash", "-m", msg, branch]
    def _busy_text(text: str) -> bool:
        low = (text or "").lower()
        return ("not concluded your merge" in low
                or ("index.lock" in low and ("file exists" in low or "unable to create" in low or "cannot lock" in low)))

    # 多道并行收口时仍可能恰好撞锁：这类失败重试即可，不记到工人头上
    for _try in range(3):
        r = _git(cmd, repo_root, timeout=180)
        if r.returncode == 0 or _try == 2:
            break
        if not _busy_text((r.stderr or "") + "\n" + (r.stdout or "")):
            break
        time.sleep(1.0)
        if not wait_index_lock(repo_root, 60):
            break
    if r.returncode != 0:
        err_text = ((r.stderr or "") + "\n" + (r.stdout or ""))
        unmerged = [ln.strip() for ln in _out(_git(["diff", "--name-only", "--diff-filter=U"],
                                                   repo_root, timeout=30)).splitlines() if ln.strip()]
        blocked = _overwritten_paths(err_text)
        aborted = False
        if os.path.exists(git_dir_file(repo_root, "MERGE_HEAD")):
            aborted = _git(["merge", "--abort"], repo_root, timeout=60).returncode == 0
        low = err_text.lower()
        if blocked or "would be overwritten by merge" in low:
            reason = "target-dirty-overlap"
        elif _busy_text(err_text):
            reason = "repo-busy"
        elif unmerged or "conflict" in low or "automatic merge failed" in low:
            reason = "merge-conflict"
        elif "unrelated histories" in low:
            reason = "unrelated-histories"
        else:
            reason = "merge-failed"
        hints = {
            "merge-conflict": "沙箱分支与主干在同一处都有改动：在沙箱内 git merge main，逐个解决冲突并提交后再合入",
            "target-dirty-overlap": "主干工作区同一路径有未提交手改，自动保护性提交没能落下；属工厂侧问题而非施工缺陷，排除后可直接补合入",
            "repo-busy": "仓库正被别的 git 操作占用（index.lock / 未结束的合并），稍后重试即可；不是施工的问题",
            "unrelated-histories": "分支与主干没有共同祖先，无法合并",
            "merge-failed": "git merge 非零退出且未归类，见 error 原文",
        }
        return {"ok": False, "reason": reason, "id": task_id, "branch": branch,
                "target": target, "error": (r.stderr or r.stdout or "").strip()[-1500:],
                "conflicts": (unmerged or blocked)[:40], "unmerged": unmerged[:40], "blocked": blocked[:40],
                "aborted": aborted, "protective_commit": protective,
                "hint": hints[reason]}

    if squash:
        # --squash 只暂存不提交；不补这一步会留下未提交改动，等于没收尾
        staged = _git(["diff", "--cached", "--quiet"], repo_root, timeout=30).returncode != 0
        if staged:
            c = _git(["commit", "-m", msg], repo_root, timeout=120)
            if c.returncode != 0:
                return {"ok": False, "reason": "squash-commit-failed", "id": task_id,
                        "error": _err(c), "staged": True,
                        "hint": "改动已在主仓暂存区；配置 git user.name/user.email 后手动 commit"}

    commit = head_commit(repo_root)
    rec["merged_at"] = now_iso()
    rec["merged_commit"] = commit
    rec["merged_into"] = target
    rec["updated_at"] = now_iso()
    with ledger_lock(ledger_path):
        fresh = ledger_load(ledger_path)
        if isinstance(fresh.get("worktrees"), dict):
            fresh["worktrees"][task_id] = rec
            ledger_save(ledger_path, fresh)
    return {"ok": True, "merged": True, "id": task_id, "branch": branch, "target": target,
            "commit": commit, "branch_sha": branch_sha, "target_sha": target_sha,
            "ahead": int(ahead_raw) if ahead_raw.isdigit() else None,
            "squash": squash, "protective_commit": protective}


def remove(task_id: str, repo: str = "", delete_branch: bool = False,
           force: bool = False, ledger_path: str = DEFAULT_LEDGER) -> dict:
    led = ledger_load(ledger_path)
    rec = find_record(led, task_id, repo)
    if not rec:
        return {"ok": False, "reason": "not-found", "id": task_id}
    repo_root = git_repo_root(rec.get("repo") or repo or "")
    if not repo_root:
        return {"ok": False, "reason": "not-a-git-repo", "id": task_id}
    wt_path = rec.get("path") or ""
    branch = worktree_branch_of(repo_root, wt_path) or rec.get("branch") or ""

    unlinked = []
    for rel in rec.get("links") or []:
        if remove_link(os.path.join(wt_path, rel)):
            unlinked.append(rel)

    removed = False
    rm_cmd = ["worktree", "remove", wt_path] + (["--force"] if force else [])
    r = _git(rm_cmd, repo_root, timeout=120)
    # git 返回 0 不代表目录真的没了（Windows 上常见 rc=0 但残留），必须以盘上事实为准
    if r.returncode == 0 and not os.path.exists(wt_path):
        removed = True
    else:
        _git(["worktree", "prune"], repo_root, timeout=60)
        if os.path.exists(wt_path):
            if not force:
                return {"ok": False, "reason": "worktree-remove-failed", "id": task_id,
                        "path": wt_path, "error": _err(r) or "rc=0 但目录仍存在",
                        "hint": "加 --force 清理残留目录（只限本工具 root 下）"}
            if not under(wt_path, worktree_root(load_config())):
                return {"ok": False, "reason": "refuse-delete-outside-root",
                        "path": wt_path}
            shutil.rmtree(wt_path, ignore_errors=True)
            if os.path.exists(wt_path):
                return {"ok": False, "reason": "residue-not-removed", "id": task_id,
                        "path": wt_path,
                        "hint": "残留目录被占用或删除被拦截；git 侧已 prune，可手动回收"}
            removed = True
        else:
            removed = True

    _git(["worktree", "prune"], repo_root, timeout=60)

    deleted_branch, branch_kept = "", ""
    if delete_branch and branch and branch_exists(repo_root, branch):
        d = _git(["branch", "-d", branch], repo_root, timeout=60)
        if d.returncode == 0 and not branch_exists(repo_root, branch):
            deleted_branch = branch
        else:
            branch_kept = branch
    elif delete_branch and branch:
        deleted_branch = branch  # 分支本就不存在，记账上等同已清理

    rec["removed_at"] = now_iso()
    rec["updated_at"] = now_iso()
    if deleted_branch:
        rec["branch_deleted"] = True
    with ledger_lock(ledger_path):
        fresh = ledger_load(ledger_path)
        fresh["worktrees"][task_id] = rec
        ledger_save(ledger_path, fresh)
    return {"ok": removed, "id": task_id, "path": wt_path, "removed": removed,
            "unlinked": unlinked, "branch_deleted": deleted_branch,
            "branch_kept": branch_kept,
            "note": ("分支 %s 未完全合并，已保留（安全）" % branch_kept) if branch_kept else ""}


def finish(task_id: str, repo: str = "", keep: bool = False, squash: bool = False,
           allow_dirty: bool = False, ledger_path: str = DEFAULT_LEDGER) -> dict:
    """验收通过后的收尾：合并 -> 清理工作区 -> 删分支。合并失败即停，绝不丢改动。"""
    m = merge(task_id, repo=repo, squash=squash, allow_dirty=allow_dirty,
              ledger_path=ledger_path)
    if not m.get("ok"):
        return {"ok": False, "stage": "merge", "merge": m, "kept": True}
    if m.get("merged") is not True:
        # 没有真正发生合并（no-commits / 同头 / 空合并）：保留现场，绝不删工作区
        return {"ok": False, "stage": "merge", "merge": m, "kept": True,
                "reason": "merge-made-no-change"}
    if keep:
        return {"ok": True, "stage": "merge", "merge": m, "kept": True}
    rm = remove(task_id, repo=repo, delete_branch=True, force=False, ledger_path=ledger_path)
    return {"ok": bool(rm.get("ok")), "stage": "remove", "merge": m, "remove": rm}


def prune(repo: str = "", ledger_path: str = DEFAULT_LEDGER) -> dict:
    """清掉台账里目录已不存在的记录（并执行 git worktree prune）。"""
    repo_root = git_repo_root(repo) if repo else ""
    led = ledger_load(ledger_path)
    dropped = []
    for tid, rec in list((led.get("worktrees") or {}).items()):
        if repo_root and rec.get("repo") and norm(rec["repo"]) != norm(repo_root):
            continue
        if rec.get("removed_at"):
            continue
        if not os.path.exists(rec.get("path") or ""):
            rec["removed_at"] = now_iso()
            rec["stale"] = True
            dropped.append(tid)
    if repo_root:
        _git(["worktree", "prune"], repo_root, timeout=60)
    if dropped:
        with ledger_lock(ledger_path):
            fresh = ledger_load(ledger_path)
            for tid in dropped:
                if tid in fresh["worktrees"]:
                    fresh["worktrees"][tid] = led["worktrees"][tid]
            ledger_save(ledger_path, fresh)
    return {"ok": True, "pruned": dropped, "repo": repo_root or None}


def list_records(repo: str = "", ledger_path: str = DEFAULT_LEDGER, live: bool = False) -> list[dict]:
    led = ledger_load(ledger_path)
    repo_root = git_repo_root(repo) if repo else ""
    out = []
    for tid, rec in sorted(led["worktrees"].items()):
        if repo_root and rec.get("repo") and norm(rec["repo"]) != norm(repo_root):
            continue
        item = {"id": tid, "repo": rec.get("repo"), "path": rec.get("path"),
                "branch": rec.get("branch"), "base_branch": rec.get("base_branch"),
                "created_at": rec.get("created_at"), "merged_at": rec.get("merged_at"),
                "removed_at": rec.get("removed_at"),
                "exists": os.path.exists(rec.get("path") or "")}
        if live:
            item.update({k: v for k, v in status(tid, repo="", ledger_path=ledger_path).items()
                         if k in ("valid", "dirty", "ahead", "behind", "head")})
        out.append(item)
    return out


def render(res: dict) -> None:
    if not isinstance(res, dict):
        print(json.dumps(res, ensure_ascii=False))
        return
    if "worktrees" in res:
        for w in res["worktrees"]:
            print("%-22s %-28s %s %s" % (w["id"], w.get("branch") or "-",
                                         "removed" if w.get("removed_at") else
                                         ("merged" if w.get("merged_at") else "active"),
                                         w.get("path") or ""))
        print("%d 个记录" % len(res["worktrees"]))
        return
    keys = ("ok", "created", "reused", "merged", "removed", "reason", "id", "path",
            "branch", "base_branch", "target", "commit", "skipped", "hint", "note",
            "branch_kept", "branch_deleted")
    print(" ".join("%s=%s" % (k, res[k]) for k in keys if k in res))


def main() -> int:
    ap = argparse.ArgumentParser(description="worker worktree 隔离工作区管理器")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--json", action="store_true")
    # 顶层参数写在子命令后面也要认（调度台习惯 --id ... --json）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ledger", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("ensure", "create"):
        p = sub.add_parser(name, parents=[common], help="幂等创建/复用任务工作区")
        p.add_argument("--id", required=True)
        p.add_argument("--project", default="")
        p.add_argument("--repo", default="")
        p.add_argument("--base", default="")
        p.add_argument("--root", default="")
        p.add_argument("--force", action="store_true")
        p.set_defaults(fn="ensure")

    p = sub.add_parser("status", parents=[common], help="查看单个工作区状态")
    p.add_argument("--id", required=True)
    p.add_argument("--repo", default="")
    p.set_defaults(fn="status")

    p = sub.add_parser("merge", parents=[common], help="把工作区分支合并回基准分支")
    p.add_argument("--id", required=True)
    p.add_argument("--repo", default="")
    p.add_argument("--into", default="")
    p.add_argument("--squash", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    p.set_defaults(fn="merge")

    p = sub.add_parser("remove", parents=[common], help="清理工作区")
    p.add_argument("--id", required=True)
    p.add_argument("--repo", default="")
    p.add_argument("--delete-branch", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn="remove")

    p = sub.add_parser("finish", parents=[common], help="合并 + 清理（验收通过后的标准收尾）")
    p.add_argument("--id", required=True)
    p.add_argument("--repo", default="")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--squash", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    p.set_defaults(fn="finish")

    p = sub.add_parser("prune", parents=[common], help="清理失效记录")
    p.add_argument("--repo", default="")
    p.set_defaults(fn="prune")

    p = sub.add_parser("list", parents=[common], help="列出工作区")
    p.add_argument("--repo", default="")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn="list")

    p = sub.add_parser("path", parents=[common], help="只打印工作区路径（给脚本用）")
    p.add_argument("--id", required=True)
    p.add_argument("--repo", default="")
    p.set_defaults(fn="path")

    args = ap.parse_args()

    if args.cmd in ("ensure", "create"):
        cfg = load_config()
        if args.root:
            cfg = dict(cfg)
            cfg["root"] = args.root
        repo = repo_for(args.project, args.repo, cfg)
        if not repo:
            res = {"ok": False, "skipped": True, "reason": "no-repo",
                   "id": args.id, "hint": "给 --repo 或在 config 里配 worktree.repos"}
        else:
            res = ensure(args.id, repo, cfg=cfg, ledger_path=args.ledger,
                         base=args.base, force=args.force)
        code = 0 if res.get("ok") else (3 if res.get("skipped") else 1)
    elif args.cmd == "status":
        res = status(args.id, args.repo, args.ledger)
        code = 0 if res.get("ok") else 1
    elif args.cmd == "merge":
        res = merge(args.id, args.repo, args.into, args.squash, args.allow_dirty, args.ledger)
        code = 0 if res.get("ok") else 1
    elif args.cmd == "remove":
        res = remove(args.id, args.repo, args.delete_branch, args.force, args.ledger)
        code = 0 if res.get("ok") else 1
    elif args.cmd == "finish":
        res = finish(args.id, args.repo, args.keep, args.squash, args.allow_dirty, args.ledger)
        code = 0 if res.get("ok") else 1
    elif args.cmd == "prune":
        res = prune(args.repo, args.ledger)
        code = 0
    elif args.cmd == "list":
        res = {"worktrees": list_records(args.repo, args.ledger, args.live)}
        code = 0
    else:  # path
        st = status(args.id, args.repo, args.ledger)
        if st.get("ok"):
            print(st.get("path") or "")
            return 0
        print(json.dumps(st, ensure_ascii=False))
        return 1

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        render(res)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
