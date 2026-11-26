# -*- coding: utf-8 -*-
"""A3b · mes_orders.STATES 与 00 §二 状态字典逐行一致（C-20261120-010 T01）

断言（plan §1 子需求 3 / A3b）：
  1 key 数 = 16
  2 顺序与 00 表格行序一致（accepted/returned 同表行展开为两个 key）
  3 line 字段 = 00 文档物理行号
  4 accepted 与 returned 同 line:47
  5 cn/owner 与 00 表逐行语义一致（去 ** 与括号注，owner 按部门字母/角色归一）
  6 RETURN_ACTIONS / allowed_actions('returned') 只有一个动作且是 repair-open

用法：python -X utf8 test_states_dict.py
退出码 0 = 全部符合预期；2 = 有偏差。
"""
import os
import re
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mes_orders as MO  # noqa: E402

DOC = os.path.normpath(os.path.join(HERE, "..", "..", "..", "dll", "factory-blueprint",
                                    "MES系统", "00-MES系统设计.md"))
RESULTS = []


def ok(name, detail=""):
    RESULTS.append(True)
    print("[PASS] %-20s %s" % (name, detail))


def bad(name, detail=""):
    RESULTS.append(False)
    print("[FAIL] %-20s %s" % (name, detail))


def norm(s):
    s = re.sub(r"\*\*", "", s or "")
    s = re.sub(r"（[^）]*）", "", s)
    return s.strip()


# 00 同一表行「accepted / returned = 你验收完毕」无法区分两个 key，
# code 按看板口径分写 验收接受/验收退回（mes_orders.STATES 头部注释 R-9）；
# 其余差异只允许标点（、 vs ·），出现语义新漂移即判 FAIL。
CN_ALLOW = {"accepted": "你验收完毕", "returned": "你验收完毕"}


def cn_norm(s):
    return norm(s).replace("·", "，").replace(" ", "")


def dept_tokens(s):
    s = norm(s)
    toks = set(re.findall(r"\b([A-F])\b", s))
    if "厂长" in s:
        toks.add("厂长")
    if "发起人" in s or s.strip() == "你":
        toks.add("发起人")
    if s.strip() == "—":
        toks.add("—")
    return toks


def parse_doc():
    rows = []
    with open(DOC, encoding="utf-8") as f:
        lines = f.read().splitlines()
    start = None
    for i, ln in enumerate(lines, 1):
        if ln.startswith("### 状态表"):
            start = i
            break
    if not start:
        raise SystemExit("00 文档未见「### 状态表」")
    for i in range(start, len(lines) + 1):
        ln = lines[i - 1]
        if ln.startswith("| `"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) < 5:
                continue
            key_cell = cells[0]
            keys = re.findall(r"`([a-z\-]+)`", key_cell)
            rows.append({"line": i, "keys": keys, "cn": cells[1],
                         "cond": cells[2], "owner": cells[3], "tone": cells[4]})
        elif rows and not ln.startswith("|") and ln.strip():
            break
    return rows


def main():
    doc = parse_doc()
    # 1 key 数
    if len(MO.STATES) == 16:
        ok("keys.count", "16")
    else:
        bad("keys.count", "%d" % len(MO.STATES))

    # 2 顺序
    doc_keys = [k for r in doc for k in r["keys"]]
    state_keys = [s["k"] for s in MO.STATES]
    if doc_keys == state_keys:
        ok("keys.order", "15 行展开 16 key，与 00 逐行同序")
    else:
        bad("keys.order", "\n  doc=%s\n  code=%s" % (doc_keys, state_keys))

    # 3 line 字段
    want = {k: r["line"] for r in doc for k in r["keys"]}
    wrong = {s["k"]: (s["line"], want.get(s["k"])) for s in MO.STATES if s["line"] != want.get(s["k"])}
    if not wrong:
        ok("line.match", "16 个 key 的 line 均为 00 物理行号 36–50")
    else:
        bad("line.match", str(wrong))

    # 4 accepted/returned 同 line
    m = MO.STATE_MAP
    if m["accepted"]["line"] == m["returned"]["line"] == 47:
        ok("line.same-row", "accepted/returned 同 line:47")
    else:
        bad("line.same-row", "%s/%s" % (m["accepted"]["line"], m["returned"]["line"]))

    # 5 cn / owner
    bad_cn, bad_own = [], []
    for r in doc:
        for k in r["keys"]:
            s = m[k]
            if k in CN_ALLOW:
                if norm(r["cn"]) != CN_ALLOW[k]:
                    bad_cn.append((k, s["cn"], r["cn"]))
            elif cn_norm(s["cn"]) != cn_norm(r["cn"]):
                bad_cn.append((k, s["cn"], r["cn"]))
            a, b = dept_tokens(s["owner"]), dept_tokens(r["owner"])
            if not a <= b:
                bad_own.append((k, s["owner"], r["owner"]))
    if not bad_cn:
        ok("cn.match", "16/16 一致（去 **/括号注/标点归一；accepted+returned 按 00 同行「你验收完毕」豁免分写）")
    else:
        bad("cn.match", str(bad_cn))
    if not bad_own:
        ok("owner.match", "16/16 主责部门一致（字母/角色归一）")
    else:
        bad("owner.match", str(bad_own))

    # 6 returned 动作表
    if MO.RETURN_ACTIONS == ("repair-open",) and MO.allowed_actions("returned") == ("repair-open",):
        ok("return.actions", "returned→('repair-open',)；producing→%s" % (MO.allowed_actions("producing"),))
    else:
        bad("return.actions", "%s / %s" % (MO.RETURN_ACTIONS, MO.allowed_actions("returned")))

    passed = sum(1 for x in RESULTS if x)
    print("\n%d passed / %d total" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 2


if __name__ == "__main__":
    sys.exit(main())
