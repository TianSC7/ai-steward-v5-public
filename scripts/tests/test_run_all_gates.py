# -*- coding: utf-8 -*-
"""总闸门清单守卫：test_gateway_sticky.py 必须作为独立门挂在网关门旁。

背景（W-20261118-030）：网关粘性用例此前只存在于 scripts/test_gateway_sticky.py，
没人跑；本门把它挂进 run_all.py，并在此锁死"门存在、名字固定、位置在网关自检之后"。

断言只用 run_all.py 源码静态解析（不起子进程、不读运行时状态），确定性、零 token：
  1  test_gateway_sticky.py 被 run_all 恰调起一次；
  2  该门的 run 名固定为 gateway_sticky（禁止改名后静默失联）；
  3  该门紧跟在 selftest（网关自检）之后——"网关门附近"；
  4  展示名与门一一对应（results.append 数量 == run() 数量），且该位为 网关粘性；
  5  被调起的脚本真实存在，且自带 EXIT= 自报契约。
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
RUN_ALL = os.path.join(SCRIPTS, "run_all.py")
STICKY = "test_gateway_sticky.py"
GATE_NAME = "gateway_sticky"
DISPLAY = "网关粘性"


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


def test_sticky_script_wired_once():
    src = _src()
    scripts = _gate_scripts(src)
    assert scripts.count(STICKY) == 1, "test_gateway_sticky.py 应恰好挂在总闸一次，实际 %d 次: %r" % (
        scripts.count(STICKY), scripts)


def test_gate_name_fixed():
    src = _src()
    gates = _gate_names(src)
    assert gates.count(GATE_NAME) == 1, "门名应固定为 %s 且唯一，实际: %r" % (GATE_NAME, gates)
    assert len(gates) == len(set(gates)), "门名重复: %r" % gates
    # 该门调起的脚本必须是粘性用例
    pair = re.search(r'run\("%s",\s*\[os\.path\.join\(HERE,\s*"([^"]+)"\)\]' % GATE_NAME, src)
    assert pair and pair.group(1) == STICKY, "门 %s 应调起 %s，实际 %r" % (
        GATE_NAME, STICKY, pair.group(1) if pair else None)


def test_gate_sits_next_to_gateway_gate():
    gates = _gate_names(_src())
    assert "selftest" in gates, "网关门 selftest 缺失，门清单: %r" % gates
    assert GATE_NAME in gates, "门 %s 缺失，门清单: %r" % (GATE_NAME, gates)
    assert gates.index(GATE_NAME) == gates.index("selftest") + 1, (
        "粘性门应紧跟在网关门之后，实际位置 %d（网关门 %d）: %r" % (
            gates.index(GATE_NAME), gates.index("selftest"), gates))


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
    path = os.path.join(SCRIPTS, STICKY)
    assert os.path.isfile(path), "被调起的脚本不存在: %s" % path
    with open(path, encoding="utf-8") as f:
        body = f.read()
    assert "EXIT=" in body, "%s 缺 EXIT= 自报契约（总闸只看退出码，证据靠自报）" % STICKY
