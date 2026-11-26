# -*- coding: utf-8 -*-
"""MES/12 绝对无人值守法 立法入库脚本（2026-11-22 21:5x）
1) 新建 docs/LAW_UNATTENDED.md（法条正本，不存在才写）
2) 修订 docs/CHARTER.md：头部插入最高铁律引用 + 08:00-20:00 时段表述扩为 24h
改前一律 .bak-autopilot-20261122 备份。
"""
import os
import shutil
import time

ROOT = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
DOCS = os.path.join(ROOT, "docs")
CHARTER = os.path.join(DOCS, "CHARTER.md")
LAW = os.path.join(DOCS, "LAW_UNATTENDED.md")

LAW_TEXT = """# MES/12 · 绝对无人值守法（最高优先级永久铁律）

> 发起人 2026-11-22 21:4x 口谕（五令重申，定性"屡禁不改"）：
> "永远永远永远都不需要有人值守。在任何情况下都要继续推进，而不是找个借口说需要有人来值守，
> 要给他授权，要给他定方向——人类不会管这个的。人类只管下单，到了时间点来提货。"

## 法条

1. **永不等待人类**。工厂任何组件、任何流程、任何状态，永远禁止把"等待人类 / 需人确认 / 需人授权 / 需人拍板"作为流程节点或终态。凡出现此类状态，一律视为**中间态**，由自主裁决泵（autopilot_pump）自动流转。
2. **挂单自动流转**。failed 由泵自动重派 R+1（换道 + 聚焦 P0 + 最小可交付），R 代数达上限（R3）后自动判死归档、事件留痕。**死亡也不是等待，是结论。** needs_human 转给工厂内部架构专家：研究上一代为什么降不下去，换一条路后自动重派，不找人。
3. **断流即事故**。队列清零且无在制 = 供血断流，泵自动留痕并接入补单源，禁止空转等人下单。
4. **唯一例外 = 三红线**（删用户文件 / 泄密 / 对外发布）：单向通报后继续自决处置，仍然不停机、不等待。
5. **本法为工厂最高法**。与 CHARTER、任何 MES 法条、任何历史裁决冲突时，以本法为准。任何人（含发起人本人）不必、不应、也不能被工厂等待。

## 执行体

- `scripts/autopilot_pump.py`（计划任务 `Factory-Autopilot-Pump`，每 5 分钟，**24h 全天候**）
- 立法：2026-11-22 21:5x · 发起人口谕，运维侧（外派责任工程师 席位/04）代拟入库
"""

CHARTER_HEAD_ADD = """**最高铁律（MES/12 绝对无人值守法，优先级高于本章程一切条款）**：
任何情况不停、不等、不找人。工厂永不把"等待人类"当作流程节点或终态；
挂单自动流转、断流自动处置，法条正本见 `docs/LAW_UNATTENDED.md`。

"""

CHARTER_OLD = "**08:00–20:00 电脑无人使用，这段时间完全交给我**"
CHARTER_NEW = "**全天候 24 小时工厂无人值守，任何时刻完全交给我**（MES/12 绝对无人值守法）"


def main():
    os.makedirs(DOCS, exist_ok=True)
    # 1) 法条正本
    if os.path.exists(LAW):
        print("LAW_EXISTS skip")
    else:
        open(LAW, "w", encoding="utf-8", newline="\n").write(LAW_TEXT)
        print("LAW_WRITTEN", LAW)
    # 2) 章程修订
    txt = open(CHARTER, encoding="utf-8", errors="replace").read()
    bak = CHARTER + ".bak-autopilot-20261122"
    changed = []
    if "MES/12" not in txt:
        shutil.copyfile(CHARTER, bak)
        anchor = "> `skills/ai-steward/SKILL.md`；两者冲突时，操作细则优先。\n"
        if anchor in txt:
            txt = txt.replace(anchor, anchor + "\n" + CHARTER_HEAD_ADD, 1)
            changed.append("head_insert")
        if CHARTER_OLD in txt:
            txt = txt.replace(CHARTER_OLD, CHARTER_NEW, 1)
            changed.append("24h_window")
        open(CHARTER, "w", encoding="utf-8", newline="\n").write(txt)
    print("CHARTER_CHANGED", changed, "bak=", bak)
    print("DONE", time.strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    main()
