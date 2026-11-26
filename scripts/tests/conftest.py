# -*- coding: utf-8 -*-
"""pytest 公共夹具：把 scripts/ 放上 sys.path，并把状态目录隔离到临时目录。

STEWARD_STATE_DIR 由 ai_store.state_dir() 每次调用实时读取，所以 monkeypatch.setenv
就能把锁文件/状态文件全部关进 tmp_path，测试绝不碰插件真实 state/。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """每个用例一份独立状态目录（锁、登记文件都落这里），返回其路径。"""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("STEWARD_STATE_DIR", str(d))
    return d
