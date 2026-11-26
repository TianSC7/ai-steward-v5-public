# -*- coding: utf-8 -*-
"""永久禁止前台等待的硬闸回归（用户 2026-11-18 三次加码）。

permission_allow.py 的 PreToolUse 钩子必须对 shell 类工具里的
sleep / Start-Sleep / timeout /t / time.sleep( 直接 deny，
但不得误伤 grep "sleep" 这类只读检索。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "permission_allow.py")


def _load():
    spec = importlib.util.spec_from_file_location("permission_allow_guard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _payload(tool: str, command: str) -> dict:
    return {"session_id": "s_test", "cwd": "C:/x", "tool_name": tool,
            "tool_input": {"command": command}}


def test_bash_sleep_denied():
    pa = _load()
    assert pa._sleep_violation(_payload("Bash", "sleep 30; echo hi"))


def test_bash_sleep_decimal_and_var():
    pa = _load()
    assert pa._sleep_violation(_payload("Bash", "sleep 0.5"))
    assert pa._sleep_violation(_payload("Bash", "sleep $N"))


def test_powershell_and_cmd_delays_denied():
    pa = _load()
    assert pa._sleep_violation(_payload("Shell", "Start-Sleep -Seconds 5"))
    assert pa._sleep_violation(_payload("Bash", "timeout /t 10 >nul"))


def test_inline_python_sleep_denied():
    pa = _load()
    assert pa._sleep_violation(_payload("Bash", 'python -c "import time; time.sleep(5)"'))


def test_grep_for_sleep_not_denied():
    pa = _load()
    assert not pa._sleep_violation(_payload("Bash", 'grep -rn "sleep" scripts/'))


def test_non_shell_tool_not_inspected():
    pa = _load()
    assert not pa._sleep_violation(_payload("Read", "sleep 30"))


def test_emit_deny_shape():
    pa = _load()
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        pa.emit("PreToolUse", False, "test-deny")
        out = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert data["hookSpecificOutput"]["permissionDecisionReason"] == "test-deny"
