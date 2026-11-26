# -*- coding: utf-8 -*-
"""一次性修复：W-20261118-018 id 撞车（并行接管会话 16:28 先占并已派单）。

只移除本会话 16:31:21 误建的那一条 queued 重复项（无 session/无证据/无进展），
保留并行会话已派单的活卡。持 board 同款文件锁，读-改-写与其它写者串行。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import ai_store  # noqa: E402

REG = os.path.join(ROOT, "state", "registry.json")
MY_CREATED = "2026-11-18T16:31:21+08:00"
MY_TITLE_HINT = "AA 巡检闹钟+归档助手用例锁死"

with ai_store.file_lock(ai_store.lock_name_for(REG), timeout=15.0):
    with open(REG, encoding="utf-8") as f:
        reg = json.load(f)
    tasks = reg.get("tasks") or []
    dups = [t for t in tasks if t.get("id") == "W-20261118-018"]
    if len(dups) != 2:
        print("预期正好 2 条重复，实际 %d 条——不动作，交人工" % len(dups))
        sys.exit(1)
    mine = [t for t in dups
            if t.get("created_at") == MY_CREATED and MY_TITLE_HINT in (t.get("title") or "")]
    if len(mine) != 1:
        print("未能唯一锁定本会话误建项（命中 %d）——不动作" % len(mine))
        sys.exit(1)
    victim = mine[0]
    if victim.get("sessionId") or victim.get("state") != "queued":
        print("误建项状态异常（%s）——不动作" % victim.get("state"))
        sys.exit(1)
    reg["tasks"] = [t for t in tasks if t is not victim]
    ai_store.atomic_write_json(REG, reg, indent=2)
    print("已移除误建重复项：%s | %s" % (victim["id"], victim["title"]))
