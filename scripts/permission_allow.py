# -*- coding: utf-8 -*-
"""权限自动放行钩子：让管家派出的会话不再弹「是否允许」把人卡住。

ZCode 支持 PreToolUse / PermissionRequest 两类钩子，返回 allow 即跳过权限询问。
本脚本只对「管家自己的会话」放行，其余会话保持原样：

放行条件：
  1. session_id 出现在 state/registry.json 的 tasks[].sessionId 或 dispatcher.sessionId；
  2. 会话 cwd 位于 config/steward.json 的 autoallow.roots 之下（workspace_fallback=true 时）。

加固（2026-11-17 夜班）：
  - 文件类工具按**目标路径**判定：未登记会话的目标必须在 autoallow.roots（或系统临时目录）内；
  - 已登记会话（管家派出）不受 roots 限制，但 protected_paths（~/.ssh、provider_config.json、
    ZCode 配置/库、C:/Windows）对任何会话都永不放行；
  - workspace_fallback=false 时，未登记会话一律不放行（回到逐项询问）。

修复（2026-11-20 权限弹窗根因）：模型经常以**裸文件名/相对路径**写文件（如 c008-plan-draft.md），
相对字符串与 roots 永远比不中 → target-out-of-scope → 弹窗卡人（实测一天 106 次）。
修法：目标路径先按会话 cwd（缺省 ZCODE_PROJECT_DIR）归一为绝对路径再做 roots/protected 判定；
protected 判定因此更严（相对路径落进保护目录也会被拦）。

总开关：
  - 文件 state/autoallow.on 不存在 → 一律不放行（保持默认询问）；
  - 环境变量 STEWARD_AUTOALLOW=0 → 一律不放行。
退出码恒为 0（不放行就是"不干预"），异常也只记录不阻断。

硬闸（2026-11-18 用户三次加码："永久禁止 sleep/wait，前台阻塞=欺骗"）：
  - Bash/Shell 类工具里出现 sleep N / Start-Sleep / timeout /t / time.sleep( 等
    前台等待，直接 permissionDecision=deny（不是提醒，是拒绝执行）；
  - 等待长任务只能 run_in_background + 完成通知，或定闹钟（CronCreate）到点回查。

用法（由 ZCode 钩子调用，stdin 为 JSON）：
  python -X utf8 permission_allow.py --event PreToolUse
  python -X utf8 permission_allow.py --event PermissionRequest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time

sys.dont_write_bytecode = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
REGISTRY = os.path.join(ROOT, "state", "registry.json")
CONFIG = os.path.join(ROOT, "config", "steward.json")
FLAG = os.path.join(ROOT, "state", "autoallow.on")
LOG = os.path.join(ROOT, "logs", "autoallow-%s.jsonl" % time.strftime("%Y%m%d"))  # daily rotation 2026-11-25 (gateway-YYYYMMDD style); hook is a short-lived per-call process so load-time date is the call date

WRITE_TOOLS = {
    "write", "writefile", "write_file", "edit", "editfile", "edit_file",
    "multiedit", "multi_edit", "notebookedit", "notebook_edit",
    "delete", "deletefile", "delete_file", "move", "movefile",
    "copy", "copyfile", "applypatch", "apply_patch", "createfile", "create_file",
}
PATH_KEYS = (
    "file_path", "filePath", "path", "notebook_path", "notebookPath",
    "target_file", "targetFile", "target_path", "targetPath",
    "output_path", "outputPath", "destination", "dest", "source",
)
DEFAULT_PROTECTED = [
    os.path.expanduser("~/.ssh"),
    os.path.expanduser("~/.zcode/v2/provider_config.json"),
    os.path.expanduser("~/.zcode/cli/config.json"),
    os.path.expanduser("~/.zcode/cli/db/db.sqlite"),
    os.path.expanduser("~/.zcode/v2/tasks-index.sqlite"),
    os.path.expanduser("~/.claude/settings.json"),
    "C:/Windows",
]

# ---- 永久禁止前台等待（2026-11-18 用户三次加码）----------------------------
# 命中即 deny：不是提醒，是拒绝执行。等待只能靠后台任务 + 完成通知 / 闹钟回查。
SHELL_TOOLS = {"bash", "shell", "run_shell", "run_command", "execute_command", "cmd"}
SLEEP_PATTERNS = [
    re.compile(r"(?i)(?:^|[\s;&|(`\"'])sleep\s+[\d$.]"),   # sleep 5 / sleep 0.5 / sleep $N
    re.compile(r"(?i)\bstart-sleep\b"),                     # PowerShell
    re.compile(r"(?i)\btimeout\s+/t\b"),                    # cmd 延迟
    re.compile(r"\btime\.sleep\s*\("),                      # python -c / 脚本内联
    re.compile(r"(?i)\bwaitfor\b"),                         # Windows 等待器
    re.compile(r"(?i)\bsleep\s+infinity\b"),
]


def _sleep_violation(data: dict) -> str:
    """返回命中的前台等待片段；未命中返回空串。只查 shell 类工具。"""
    tool = str(data.get("tool_name") or "").strip().lower()
    if tool not in SHELL_TOOLS:
        return ""
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        return ""
    cmd = ti.get("command")
    if not isinstance(cmd, str):
        cmd = ti.get("cmd") if isinstance(ti.get("cmd"), str) else ""
    if not cmd:
        return ""
    for pat in SLEEP_PATTERNS:
        m = pat.search(cmd)
        if m:
            return cmd[max(0, m.start() - 10):m.end() + 20].strip()
    return ""


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p or "")).rstrip("\\/")


def _within(path: str, root: str) -> bool:
    if not path or not root:
        return False
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r + os.sep) or p.startswith(r + "/")


def _registered_ids() -> set[str]:
    ids: set[str] = set()
    try:
        reg = json.load(open(REGISTRY, encoding="utf-8"))
    except (OSError, ValueError):
        return ids
    for t in reg.get("tasks") or []:
        if isinstance(t, dict) and t.get("sessionId"):
            ids.add(str(t["sessionId"]))
    d = reg.get("dispatcher") or {}
    if isinstance(d, dict) and d.get("sessionId"):
        ids.add(str(d["sessionId"]))
    return ids


def _autoallow_cfg() -> dict:
    try:
        cfg = json.load(open(CONFIG, encoding="utf-8"))
        v = cfg.get("autoallow")
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _roots() -> list[str]:
    roots = _autoallow_cfg().get("roots") or []
    return [_norm(r) for r in roots if isinstance(r, str) and r.strip()]


def _protected() -> list[str]:
    paths = _autoallow_cfg().get("protected_paths")
    if not isinstance(paths, list) or not paths:
        paths = DEFAULT_PROTECTED
    return [_norm(p) for p in paths if isinstance(p, str) and p.strip()]


def _workspace_fallback() -> bool:
    return _autoallow_cfg().get("workspace_fallback", True) is not False


def _is_write_tool(name: str) -> bool:
    return _norm(name).replace(" ", "").lower() in WRITE_TOOLS


def _abs(target: str, cwd: str) -> str:
    """相对路径按会话 cwd 归一为绝对路径（2026-11-20 修复，见文件头说明）。

    cwd 不可得时退回原样（等同旧行为：比对不中→不放行），绝不猜进程 cwd。
    MSYS/Git-Bash 路径先映射为 Windows 路径（2026-11-20 18:30 修复：
    G-001 写 /tmp/... 被判 target-out-of-scope → 权限弹窗 → 卡死 18 min）。
    """
    t = os.path.expanduser(target.strip())
    if not t:
        return t
    if os.name == "nt" and t.startswith("/"):
        m = re.match(r"^/([A-Za-z])(/.*)?$", t)
        if m:
            t = m.group(1).upper() + ":" + (m.group(2) or "/")
        elif t == "/tmp" or t.startswith("/tmp/"):
            t = os.path.join(tempfile.gettempdir(), t[5:].lstrip("/"))
    if os.path.isabs(t):
        return t
    base = (cwd or "").strip() or os.environ.get("ZCODE_PROJECT_DIR") or ""
    if not base:
        return t
    return os.path.join(base, t)


def _target_paths(tool_input) -> list[str]:
    """从 tool_input 里提取可判定的目标路径；只取字符串，不递归任意结构。"""
    if not isinstance(tool_input, dict):
        return []
    out: list[str] = []
    for k in PATH_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v)
    files = tool_input.get("files")
    if isinstance(files, list):
        for f in files:
            if isinstance(f, str) and f.strip():
                out.append(f)
            elif isinstance(f, dict):
                for k in PATH_KEYS:
                    v = f.get(k)
                    if isinstance(v, str) and v.strip():
                        out.append(v)
    return list(dict.fromkeys(out))


def decide(data: dict) -> tuple[bool, str]:
    sid = str(data.get("session_id") or data.get("sessionId") or "")
    cwd = str(data.get("cwd") or os.environ.get("ZCODE_PROJECT_DIR") or "")
    tool = str(data.get("tool_name") or "")
    targets = [_abs(t, cwd) for t in _target_paths(data.get("tool_input"))]

    registered = bool(sid and sid in _registered_ids())
    in_workspace = any(_within(cwd, r) for r in _roots())
    if not registered and not in_workspace:
        return False, "out-of-scope"

    # 保护名单优先；未登记会话另受 roots/临时目录约束
    allowed_roots = _roots() + [tempfile.gettempdir()]
    for t in targets:
        if any(_within(t, p) for p in _protected()):
            return False, "protected"
        if not registered and not any(_within(t, r) for r in allowed_roots):
            return False, "target-out-of-scope"

    if targets:
        return True, ("registered" if registered else "workspace")
    if _is_write_tool(tool):
        if registered:
            return True, "registered-no-target"
        if in_workspace and _workspace_fallback():
            return True, "workspace-no-target"
        return False, "no-target"
    if registered:
        return True, "registered"
    if in_workspace and _workspace_fallback():
        return True, "workspace"
    return False, "out-of-scope"


def emit(event: str, allow: bool, reason: str = "") -> None:
    if event == "PreToolUse" and not allow:
        out = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason or "ai-steward: 前台等待已被永久禁止",
        }}
    elif not allow:
        return
    elif event == "PreToolUse":
        out = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "ai-steward: 管家会话自动放行",
        }}
    else:
        out = {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


def _log(data: dict, event: str, allow: bool, reason: str) -> None:
    try:
        ti = data.get("tool_input")
        input_keys = sorted(ti.keys()) if isinstance(ti, dict) else []
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "event": event,
                "session": str(data.get("session_id") or "")[:20],
                "tool": data.get("tool_name"),
                "cwd": data.get("cwd"),
                "input_keys": input_keys,
                "allow": allow,
                "reason": reason,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run(data: dict, event: str) -> None:
    """单次钩子判定（main 的核心，可测）：睡眠硬闸 -> decide -> 放行才 emit。

    契约：不放行 = 不干预（stdout 空，交回正常权限流）；唯一例外是睡眠硬闸，
    它必须显式 deny（用户 2026-11-18 三次加码：前台等待=欺骗，拒绝执行）。
    """
    viol = _sleep_violation(data)
    if viol:
        reason = (
            "ai-steward 硬闸：前台 sleep/wait 已被永久禁止（用户 2026-11-18 三次加码）。"
            "命中：{0}。长任务请 run_in_background，用完成通知或闹钟（CronCreate）回查。"
        ).format(viol)
        emit(event, False, reason)
        _log(data, event, False, "sleep-banned: " + viol)
        return

    try:
        allow, reason = decide(data)
    except Exception:  # 钩子永不阻断，异常按不放行处理
        return

    if allow:
        emit(event, True)
    _log(data, event, allow, reason)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", default="PreToolUse",
                    choices=["PreToolUse", "PermissionRequest"])
    args = ap.parse_args()

    if os.environ.get("STEWARD_AUTOALLOW") == "0" or not os.path.exists(FLAG):
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0

    run(data, args.event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
