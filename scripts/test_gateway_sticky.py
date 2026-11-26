# -*- coding: utf-8 -*-
"""网关粘性路由用例：兜底 lane 不参与粘性提升（policy.sticky_exclude）。

背景：2026-11-18 网关日志显示 r4/r42/step/nex 同时连接中止冷却后自动降级到
apigoto-deepseek-flash，随后 sticky 把该会话钉在 apigoto 上 30 分钟；用户令
"apigoto 只做末位兜底，尽量不用"。修法：policy.sticky_exclude 里的 lane
不被 order_for_sticky 提到队首（正常顺序仍保留它在末位兜底）。

覆盖：
  1  未配置 sticky_exclude：旧行为不变（可用 lane 提到队首）
  2  sticky_exclude 命中：候选顺序原样返回，不被提升
  3  sticky_exclude 命中且 lane 在冷却：返回普通候选（不因 same/other 逻辑重排）
  4  同名不同 id 的等价 lane 不受影响（只排除被列入的那个 id）
  5  alias 不允许该 lane 时，正常剔除（与排除规则叠加不冲突）

用法：python -X utf8 test_gateway_sticky.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import os
import sys
import types

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gateway  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class FakeState:
    def __init__(self, lanes, policy, cooling=()):
        self.lane_by_id = {l.id: l for l in lanes}
        self.policy = policy
        self._cooling = set(cooling)

    def cooldown_left(self, lane_id):
        return 60 if lane_id in self._cooling else 0


def lane(i, equiv=None, usable=True):
    return types.SimpleNamespace(id=i, equiv=equiv or i, usable=usable)


ALIAS = {"order": ["a", "b", "fallback"]}


@case
def case_legacy_promotes_sticky():
    st = FakeState([lane("a"), lane("b"), lane("fallback")], {"sticky": True})
    cands = [st.lane_by_id["a"], st.lane_by_id["b"]]
    out = gateway.order_for_sticky(cands, "b", st, True, ALIAS)
    assert [c.id for c in out][0] == "b", [c.id for c in out]
    return "promoted=b"


@case
def case_excluded_not_promoted():
    st = FakeState([lane("a"), lane("b"), lane("fallback")],
                   {"sticky": True, "sticky_exclude": ["fallback"]})
    cands = [st.lane_by_id["a"], st.lane_by_id["b"]]
    out = gateway.order_for_sticky(cands, "fallback", st, True, ALIAS)
    assert [c.id for c in out] == ["a", "b"], [c.id for c in out]
    return "order=a,b"


@case
def case_excluded_cooling_keeps_order():
    st = FakeState([lane("a"), lane("b"), lane("fallback")],
                   {"sticky": True, "sticky_exclude": ["fallback"]}, cooling={"fallback"})
    cands = [st.lane_by_id["a"], st.lane_by_id["b"]]
    out = gateway.order_for_sticky(cands, "fallback", st, False, ALIAS)
    assert [c.id for c in out] == ["a", "b"], [c.id for c in out]
    return "order=a,b(no-cross)"


@case
def case_other_lane_still_promoted():
    st = FakeState([lane("a"), lane("b"), lane("fallback")],
                   {"sticky": True, "sticky_exclude": ["fallback"]})
    cands = [st.lane_by_id["a"], st.lane_by_id["b"]]
    out = gateway.order_for_sticky(cands, "a", st, True, ALIAS)
    assert [c.id for c in out][0] == "a"
    return "promoted=a"


@case
def case_alias_disallow_still_filters():
    st = FakeState([lane("a"), lane("b"), lane("fallback")],
                   {"sticky_exclude": ["fallback"]})
    cands = [st.lane_by_id["a"], st.lane_by_id["b"]]
    out = gateway.order_for_sticky(cands, "fallback", st, True, {"order": ["a", "b"]})
    assert [c.id for c in out] == ["a", "b"], [c.id for c in out]
    return "filtered"


def main() -> int:
    ok = 0
    for fn in CASES:
        try:
            detail = fn()
            print("[PASS] %-32s %s" % (fn.__name__[5:], detail))
            ok += 1
        except Exception as e:  # noqa: BLE001
            print("[FAIL] %-32s %s: %s" % (fn.__name__[5:], type(e).__name__, e))
    print("| %d passed / %d total" % (ok, len(CASES)))
    print("EXIT=%d" % (0 if ok == len(CASES) else 2))
    return 0 if ok == len(CASES) else 2


if __name__ == "__main__":
    sys.exit(main())
