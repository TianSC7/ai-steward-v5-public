# -*- coding: utf-8 -*-
"""A1 · returned 法定出路推演（C-20261120-010 T01）

覆盖：
  chain    returned 原单 --repair-open--> 新单 created→queued→planning→producing→qc，事件恰好 5 行
  字段     ts/actor/action/order/from/to/ref_order/d_no/reason 全非空
  原单     卡 sha256 前后同值、原单状态仍 returned
  check    mes.py check 重放 diff=0
  负例 5  原单非 returned / 缺 D 号 / 缺 reason / 工牌部门错 / 同原单二次 repair-open → exit 3
  幂等     同 idem-key 同 actor → REPLAY 且事件不增；不同 actor → exit 3

全部写 tmp 沙箱（MES_STATE_DIR 语义），不写活体 state；不碰订单卡。
用法：python -X utf8 test_mes_returned.py
退出码 0 = 全部符合预期；2 = 有偏差。
"""
import hashlib
import io
import json
import os
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mes  # noqa: E402

RESULTS = []


def ok(name, detail=""):
    RESULTS.append((True, name, detail))
    print("[PASS] %-22s %s" % (name, detail))


def bad(name, detail=""):
    RESULTS.append((False, name, detail))
    print("[FAIL] %-22s %s" % (name, detail))


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run(argv):
    """in-process 调用，捕获 stdout/stderr 与退出码。"""
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = mes.main(argv)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
    finally:
        sys.stdout, sys.stderr = so, se
    return rc, out.getvalue(), err.getvalue()


def write_card(root, code, state):
    d = os.path.join(root, code)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "order.md")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("# %s ｜ fixture\n\n" % code)
        f.write("| 订单编号 | %s |\n|---|---|\n" % code)
        f.write("| 优先级 | P1 普通 |\n")
        f.write("| 当前状态 | `%s` 夹具 |\n" % state)
        f.write("| 进度 | 阶段 10/10 · 子需求 1/1 |\n")
    return p


def write_registry(path):
    reg = {"badges": [
        {"id": "A-001", "dept": "A", "status": "active", "name": "厂长"},
        {"id": "B-001", "dept": "B", "status": "active", "name": "策划"},
        {"id": "W-001", "dept": "D", "status": "active", "name": "生产"},
        {"id": "E-001", "dept": "E", "status": "active", "name": "质检"},
        {"id": "D-9XX", "dept": "D", "status": "active", "name": "生产(错部门用)"},
        {"id": "W-9XX", "dept": "D", "status": "archived", "name": "归档牌"},
    ]}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(reg, f, ensure_ascii=False)


def main():
    tmp = tempfile.mkdtemp(prefix="c010_t01_")
    state = os.path.join(tmp, "state")
    orders = os.path.join(tmp, "orders")
    reg = os.path.join(tmp, "registry.json")
    write_registry(reg)
    ORIG, NEW = "C-20261120-010-F0", "C-20261120-010-F1"
    card = write_card(orders, ORIG, "returned")
    sha_before = sha256(card)

    def A(*argv):
        return run(list(argv) + ["--state-dir", state, "--registry", reg,
                                 "--orders-dir", orders])

    # --- chain ---
    rc, out, err = A("repair-open", "--order", ORIG, "--to-order", NEW,
                     "--d", "D-20261120-099", "--reason", "首屏 1347>800 质检不过",
                     "--actor", "A-001", "--idem-key", "ro-f0")
    if rc == 0 and "OK repair-open" in out:
        ok("chain.repair-open", out.strip())
    else:
        bad("chain.repair-open", "rc=%s %s %s" % (rc, out, err))

    steps = [("B-001", "queued"), ("B-001", "planning"), ("W-001", "producing"), ("E-001", "qc")]
    chain_ok = True
    for actor, to in steps:
        rc, out, err = A("transition", "--order", NEW, "--to", to, "--actor", actor)
        if rc != 0:
            chain_ok = False
            bad("chain.%s->%s" % (NEW[-2:], to), "rc=%s %s %s" % (rc, out, err))
            break
    if chain_ok:
        ok("chain.transitions", "created→queued→planning→producing→qc")

    ev_path = os.path.join(state, "mes", "events.jsonl")
    evs = []
    if os.path.exists(ev_path):
        evs = [json.loads(l) for l in open(ev_path, encoding="utf-8") if l.strip()]
    if len(evs) == 5:
        ok("events.count", "5 行")
    else:
        bad("events.count", "实际 %d 行" % len(evs))

    need = ("ts", "actor", "action", "order", "from", "to")
    miss = [(i, k) for i, e in enumerate(evs) for k in need if not str(e.get(k) or "").strip()]
    # repair-open 行 additionally requires ref_order/d_no/reason（plan §2.2）
    miss += [(i, k) for i, e in enumerate(evs) if e.get("action") == "repair-open"
             for k in ("ref_order", "d_no", "reason") if not str(e.get(k) or "").strip()]
    if evs and not miss:
        ok("events.fields", "公共 6 字段全非空；repair-open 行 ref_order/d_no/reason 必填")
    else:
        bad("events.fields", "缺 %s" % miss)

    order_of = [e.get("order") for e in evs]
    if order_of == [ORIG] + [NEW] * 4:
        ok("events.sequence", "第 1 行原单，后 4 行新单")
    else:
        bad("events.sequence", str(order_of))

    rc, out, err = A("check")
    if rc == 0 and "diff=0" in out:
        ok("check.diff", out.strip().replace("\n", " | "))
    else:
        bad("check.diff", "rc=%s %s %s" % (rc, out, err))

    if sha256(card) == sha_before:
        ok("original.card", "sha 前后同值 %s" % sha_before[:12])
    else:
        bad("original.card", "sha 变化")

    st = mes.card_state(orders, ORIG)
    if st == "returned":
        ok("original.state", "仍 returned（原单终态不变）")
    else:
        bad("original.state", st)

    # --- 负例 5 ---
    nums = 0
    # 1 原单非 returned
    ORIG2 = "C-20261120-010-F2"
    write_card(orders, ORIG2, "producing")
    ORIG3 = "C-20261120-010-F8"
    write_card(orders, ORIG3, "returned")
    rc, out, err = A("repair-open", "--order", ORIG2, "--to-order", "C-20261120-010-F3",
                     "--d", "D-1", "--reason", "x", "--actor", "A-001")
    if rc == 3 and "非 returned" in err:
        ok("neg.not-returned", err.strip()); nums += 1
    else:
        bad("neg.not-returned", "rc=%s %s" % (rc, err))
    # 2 缺 D 号
    rc, out, err = A("repair-open", "--order", ORIG, "--to-order", "C-20261120-010-F4",
                     "--d", "", "--reason", "x", "--actor", "A-001")
    if rc == 3 and "D 号" in err:
        ok("neg.no-dno", err.strip()); nums += 1
    else:
        bad("neg.no-dno", "rc=%s %s" % (rc, err))
    # 3 缺 reason
    rc, out, err = A("repair-open", "--order", ORIG, "--to-order", "C-20261120-010-F5",
                     "--d", "D-2", "--reason", " ", "--actor", "A-001")
    if rc == 3 and "原因" in err:
        ok("neg.no-reason", err.strip()); nums += 1
    else:
        bad("neg.no-reason", "rc=%s %s" % (rc, err))
    # 4 工牌部门错（生产 D 牌开返修单，目标态 created 需 A）
    rc, out, err = A("repair-open", "--order", ORIG3, "--to-order", "C-20261120-010-F6",
                     "--d", "D-3", "--reason", "x", "--actor", "D-9XX")
    if rc == 3 and "dept" in err:
        ok("neg.badge-dept", err.strip()); nums += 1
    else:
        bad("neg.badge-dept", "rc=%s %s" % (rc, err))
    # 5 同原单二次 repair-open
    rc, out, err = A("repair-open", "--order", ORIG, "--to-order", "C-20261120-010-F7",
                     "--d", "D-4", "--reason", "y", "--actor", "A-001")
    if rc == 3 and "二次 repair-open" in err:
        ok("neg二次repair", err.strip()); nums += 1
    else:
        bad("neg二次repair", "rc=%s %s" % (rc, err))

    # --- 负例 5b（T05-FIX 回归）：卡头 returned 不得被 transition 推进 -----------
    # 独立空台账夹具：卡头 returned、台账 0 行。
    # 原缺陷：cmd_transition 的 cur 缺 card_state 兜底 → 折成 created → 省略 --from 时
    # created->queued 合法 → rc=0 并落账（returned 唯一入口被绕过），check 仍 diff=0。
    state_bp = os.path.join(tmp, "state_bypass")
    ORIG_BP = "C-20261120-010-F9"
    write_card(orders, ORIG_BP, "returned")
    ev_bp = os.path.join(state_bp, "mes", "events.jsonl")

    def B(*argv):
        return run(list(argv) + ["--state-dir", state_bp, "--registry", reg,
                                 "--orders-dir", orders])

    def bp_rows():
        if not os.path.exists(ev_bp):
            return 0
        return len([l for l in open(ev_bp, encoding="utf-8") if l.strip()])

    # ① 省略 --from：必须 rc=3，且理由是 returned 出边为空（不是「from 与台账不符」）
    rc, out, err = B("transition", "--order", ORIG_BP, "--to", "queued", "--actor", "B-001")
    if (rc == 3 and "edge" in err and "returned" in err
            and "from 与台账不符" not in err and bp_rows() == 0):
        ok("neg.returned-no-from", "rc=3 拦对理由；台账 0 行；%s" % err.strip())
    else:
        bad("neg.returned-no-from", "rc=%s rows=%d err=%s" % (rc, bp_rows(), err.strip()))

    # ② 显式 --from returned：必须 rc=3，走 returned 出边为空拦截（不再撞 from 不符）
    rc, out, err = B("transition", "--order", ORIG_BP, "--from", "returned",
                     "--to", "planning", "--actor", "B-001")
    if rc == 3 and "returned" in err and ("edge" in err or "合法出边" in err) and bp_rows() == 0:
        ok("neg.returned-explicit-from", "rc=3 returned 出边为空；台账 0 行；%s" % err.strip())
    else:
        bad("neg.returned-explicit-from", "rc=%s rows=%d err=%s" % (rc, bp_rows(), err.strip()))

    # --- 幂等 ---
    before = len(evs)
    rc, out, err = A("transition", "--order", NEW, "--to", "awaiting-closure",
                     "--actor", "A-001", "--idem-key", "k1")
    evs1 = [json.loads(l) for l in open(ev_path, encoding="utf-8") if l.strip()]
    rc2, out2, err2 = A("transition", "--order", NEW, "--to", "awaiting-closure",
                        "--actor", "A-001", "--idem-key", "k1")
    evs2 = [json.loads(l) for l in open(ev_path, encoding="utf-8") if l.strip()]
    if rc == 0 and rc2 == 0 and "REPLAY" in out2 and len(evs1) == len(evs2) == before + 1:
        ok("idem.replay", "同 key 同 actor → REPLAY，事件 %d→%d" % (len(evs1), len(evs2)))
    else:
        bad("idem.replay", "rc=%s/%s evs %d→%d out=%s" % (rc, rc2, len(evs1), len(evs2), out2))
    rc, out, err = A("transition", "--order", NEW, "--to", "awaiting-closure",
                     "--actor", "E-001", "--idem-key", "k1")
    if rc == 3 and "跨席顶替" in err:
        ok("idem.cross-actor", err.strip())
    else:
        bad("idem.cross-actor", "rc=%s %s" % (rc, err))

    # --- 订单卡字节未动 ---
    rc, out, err = A("check")
    passed = sum(1 for r in RESULTS if r[0])
    print("\n%d passed / %d total" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 2


if __name__ == "__main__":
    sys.exit(main())
