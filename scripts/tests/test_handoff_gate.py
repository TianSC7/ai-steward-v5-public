# -*- coding: utf-8 -*-
"""总闸门清单守卫（W-20261118-036）：test_handoff.py 必须作为独立门 handoff_snapshot 挂在任务台旁。

背景：交接快照用例此前只存在于 scripts/test_handoff.py，没人跑；本门把它挂进
run_all.py，并在此锁死"门存在、名字固定、位置在任务台之后、展示名对齐"。

断言只用 run_all.py 源码静态解析（不起子进程、不读运行时状态），确定性、零 token：
  1  test_handoff.py 被 run_all 恰调起一次；
  2  该门的 run 名固定为 handoff_snapshot（禁止改名后静默失联）；
  3  该门紧跟在 board_edges（任务台）之后——交接快照与任务台账同源；
  4  展示名 交接快照 与门一一对应（results.append 数量 == run() 数量）；
  5  被调起的脚本真实存在，且以 main() 退出码为唯一判据（raise SystemExit(main())）；
  6  负向自证：把门定义从源码副本里删掉后，识别函数必须返回 False（守卫真的会失败）。
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
RUN_ALL = os.path.join(SCRIPTS, "run_all.py")
HANDOFF = "test_handoff.py"
GATE_NAME = "handoff_snapshot"
DISPLAY = "交接快照"
NEIGHBOR = "board_edges"


def _src() -> str:
    with open(RUN_ALL, encoding="utf-8") as f:
        return f.read()


def _gate_names(src: str) -> list:
    """按源码出现顺序取所有门 run 名（run("xxx", ...) 的首个实参）。"""
    return re.findall(r'\brun\("([a-z0-9_]+)"', src)


def _gate_scripts(src: str) -> list:
    """按源码出现顺序取所有以 HERE 脚本路径调起的门脚本名。"""
    return re.findall(r'run\("[a-z0-9_]+",\s*\[os\.path\.join\(HERE,\s*"([^"]+)"\)\]', src)


def _displays(src: str) -> list:
    return re.findall(r'results\.append\(\(ok,\s*"([^"]+)",\s*d\)\)', src)


def _pairs(src: str) -> list:
    """(run 名, 紧随其后的展示名) 对；run 调用可跨行。台账门直接 ledger_check，不在此列。"""
    return re.findall(
        r'run\("([a-z0-9_]+)"(?:.|\n)*?results\.append\(\(ok,\s*"([^"]+)",\s*d\)\)', src)


def _has_gate(src: str) -> bool:
    """门定义是否还在：run("handoff_snapshot", [os.path.join(HERE, "test_handoff.py")])。"""
    return re.search(
        r'\brun\("%s",\s*\[os\.path\.join\(HERE,\s*"%s"\)\]' % (GATE_NAME, re.escape(HANDOFF)),
        src) is not None


def test_handoff_script_wired_once():
    src = _src()
    scripts = _gate_scripts(src)
    assert scripts.count(HANDOFF) == 1, "test_handoff.py 应恰好挂在总闸一次，实际 %d 次: %r" % (
        scripts.count(HANDOFF), scripts)


def test_gate_name_fixed():
    src = _src()
    gates = _gate_names(src)
    assert gates.count(GATE_NAME) == 1, "门名应固定为 %s 且唯一，实际: %r" % (GATE_NAME, gates)
    assert len(gates) == len(set(gates)), "门名重复: %r" % gates
    # 该门调起的脚本必须是交接快照用例
    pair = re.search(r'run\("%s",\s*\[os\.path\.join\(HERE,\s*"([^"]+)"\)\]' % GATE_NAME, src)
    assert pair and pair.group(1) == HANDOFF, "门 %s 应调起 %s，实际 %r" % (
        GATE_NAME, HANDOFF, pair.group(1) if pair else None)


def test_gate_sits_next_to_board_gate():
    gates = _gate_names(_src())
    assert NEIGHBOR in gates, "任务台门 %s 缺失，门清单: %r" % (NEIGHBOR, gates)
    assert GATE_NAME in gates, "门 %s 缺失，门清单: %r" % (GATE_NAME, gates)
    assert gates.index(GATE_NAME) == gates.index(NEIGHBOR) + 1, (
        "交接快照门应紧跟在任务台门之后，实际位置 %d（任务台门 %d）: %r" % (
            gates.index(GATE_NAME), gates.index(NEIGHBOR), gates))


def test_display_name_aligned():
    src = _src()
    gates, pairs, displays = _gate_names(src), _pairs(src), _displays(src)
    assert len(pairs) == len(gates), "门定义 %d 个但登记结果的只有 %d 个（有门跑了没登记/漏门）: %r" % (
        len(gates), len(pairs), pairs)
    pairmap = dict(pairs)
    assert pairmap.get(GATE_NAME) == DISPLAY, "门 %s 展示名应为 %s，实际 %r" % (
        GATE_NAME, DISPLAY, pairmap.get(GATE_NAME))
    # 末位台账门不经 run() 孪生登记，但也必须出现在结果里
    assert displays[-1] == "台账", "末位应为台账门，实际 %r" % (displays[-1],)


def test_referenced_script_contract():
    path = os.path.join(SCRIPTS, HANDOFF)
    assert os.path.isfile(path), "被调起的脚本不存在: %s" % path
    with open(path, encoding="utf-8") as f:
        body = f.read()
    # 总闸只看退出码：脚本必须以 main() 的返回值为进程退出码（自报 19 passed / 19 total）
    assert "raise SystemExit(main())" in body, "%s 缺 main() 退出码契约（总闸只看退出码）" % HANDOFF


def test_guard_fails_when_gate_deleted():
    """负向自证：删掉门定义后 guard 必须失败，证明本守卫不是摆设。"""
    src = _src()
    assert _has_gate(src), "真实 run_all.py 里找不到 %s 门" % GATE_NAME
    mutated = re.sub(
        r'\n    ok, d, _, _ = run\("%s".*?\n    results\.append\(\(ok, "%s", d\)\)\n' % (
            GATE_NAME, DISPLAY),
        "\n", src, count=1, flags=re.S)
    assert mutated != src, "未能从源码副本中删除门定义（用例失效）"
    assert not _has_gate(mutated), "删门定义后守卫仍认门存在——守卫无效"
