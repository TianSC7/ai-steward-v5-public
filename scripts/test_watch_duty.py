# -*- coding: utf-8 -*-
"""A2 · 值守制红灯口径修订校验（C-20261120-010 T02）

6 条断言（plan §1 子需求 2 / A2）：
  1 §4 含「1.5× 黄」「2× 红」「三选」
  2 §5.3 无「只写…不解锁」绝对表述（该段「只写」0 命中）
  3 §4 与 §5.3 的「级别→颜色→owner→30 min」映射一致
  4 GLM 相关段字节不变（与 git HEAD 版本逐字节比对）
  5 文末修改记录新增恰好 1 行且标注 C-20261120-010
  6 git diff --numstat = 1 文件 / ≤8 行增删
  7 §2 含「以 §4 为准」（T04 FAIL 整改：§2 不另立红灯处置人）
  8 §2 无「红灯处置人」字样（旧句及任何处置人另立）
  9 §4 同段映射：1.5×+黄+值班厂长席 / 2×+红+厂长（非全文出现）
 10 全文无「值班厂长席三选」（红色不归值班席）
 11 §5.3 同段映射：黄→值班厂长席 / 红→厂长
 12 §4 非工时类与 2× 同段（非工时类 2× 红升级发起人）

用法：python -X utf8 test_watch_duty.py
退出码 0 = 全过；2 = 有偏差。
"""
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "dll", "factory-blueprint"))
DOC = "席位/02-二十四小时值守制.md"
RESULTS = []


def ok(n, d=""):
    RESULTS.append(True); print("[PASS] %-24s %s" % (n, d))


def bad(n, d=""):
    RESULTS.append(False); print("[FAIL] %-24s %s" % (n, d))


def git(*args):
    r = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def section(text, head):
    m = re.search(r"^## .*%s.*$" % re.escape(head), text, re.M)
    if not m:
        return ""
    rest = text[m.start():]
    nxt = re.search(r"^## ", rest[1:], re.M)
    return rest[:nxt.start() + 1] if nxt else rest


def main():
    path = os.path.join(ROOT, DOC)
    text = open(path, encoding="utf-8").read()
    s4, s53 = section(text, "接单即开工"), section(text, "无单待机")

    # 1
    need = ["1.5×", "黄", "2×", "红", "三选"]
    if all(k in s4 for k in need):
        ok("S4 三档齐全", "1.5×黄 / 2×红 / 三选")
    else:
        bad("S4 三档齐全", "缺 %s" % [k for k in need if k not in s4])

    # 2
    m = re.search(r"3\.\s*patrol.*?$", s53, re.M)
    line3 = m.group(0) if m else ""
    old_abs = "只写 `state/PATROL_ALERT.txt`，**不解锁 LLM**"
    if line3 and old_abs not in line3 and "不解锁生产席 LLM" in line3 and "责任席" in line3:
        ok("S5.3 无绝对表述", "旧句已消；改为带责任席/时限 + 不解锁生产席 LLM")
    else:
        bad("S5.3 无绝对表述", line3[:80])

    # 3
    pair = [("值班厂长席", "黄"), ("厂长", "红")]
    if all(a in s4 and a in s53 for a, _ in pair) and "30 min" in s4 and "30 min" in s53:
        ok("S4/S5.3 映射一致", "黄→值班厂长席 / 红→厂长，均 30 min")
    else:
        bad("S4/S5.3 映射一致", "映射或缺时限不一致")

    # 4 GLM 段字节不变（对 git HEAD）
    rc, head_txt, err = git("show", "HEAD:" + DOC)
    if rc != 0:
        bad("GLM 段字节不变", "git show 失败 %s" % err[:60])
    else:
        g_now = [l for l in text.splitlines() if "GLM" in l]
        g_head = [l for l in head_txt.splitlines() if "GLM" in l]
        if g_now == g_head and g_now:
            ok("GLM 段字节不变", "%d 行逐字节一致" % len(g_now))
        else:
            bad("GLM 段字节不变", "now=%d head=%d" % (len(g_now), len(g_head)))

    # 5
    tail_new = [l for l in text.splitlines() if "C-20261120-010" in l]
    tail_head = [l for l in head_txt.splitlines() if "C-20261120-010" in l]
    if len(tail_new) == 1 and not tail_head:
        ok("修改记录 1 行", tail_new[0][:52])
    else:
        bad("修改记录 1 行", "now=%d head=%d" % (len(tail_new), len(tail_head)))

    # 6
    rc, out, err = git("diff", "--numstat", "HEAD", "--", DOC)
    nums = out.strip().split("\t") if out.strip() else []
    n_files = len([l for l in out.strip().splitlines() if l.strip()])
    if n_files == 1 and nums and nums[0].isdigit() and nums[1].isdigit() and int(nums[0]) + int(nums[1]) <= 8:
        ok("diff 边界", "%s 增 / %s 删 / 1 文件" % (nums[0], nums[1]))
    else:
        bad("diff 边界", out.strip() or err[:60])

    # 7 §2 服从 §4（T04 FAIL 整改：§2 不得另立红灯处置人）
    s2 = section(text, "值守层")
    if s2 and "以 §4 为准" in s2:
        ok("S2 服从 S4", "§2 含「以 §4 为准」")
    else:
        bad("S2 服从 S4", "§2 缺「以 §4 为准」")

    # 8 §2 不再另立红灯处置人（旧句及任何「红灯处置人」字样）
    if s2 and "红灯处置人" not in s2:
        ok("S2 旧句已消", "§2 无「红灯处置人」字样")
    else:
        bad("S2 旧句已消", "§2 仍含「红灯处置人」字样")

    # 9 真实映射（同段共现，不是全文出现）
    seg4 = [x for x in re.split(r"[；;，]", s4) if x.strip()]

    def same_seg(segs, keys):
        return any(all(k in s for k in keys) for s in segs)

    miss = []
    if not same_seg(seg4, ["1.5×", "黄", "值班厂长席"]):
        miss.append("S4 缺 1.5×+黄+值班厂长席 同段")
    if not same_seg(seg4, ["2×", "红", "厂长"]):
        miss.append("S4 缺 2×+红+厂长 同段")
    if miss:
        bad("S4 同段映射", "；".join(miss))
    else:
        ok("S4 同段映射", "1.5×黄→值班厂长席 / 2×红→厂长 各自同段")

    # 10 红色不得由「值班厂长席」三选（T04 FAIL 的实质）
    bad_owner = "值班厂长席三选"
    if bad_owner in text:
        bad("S4 红不归值班席", "全文含「%s」" % bad_owner)
    else:
        ok("S4 红不归值班席", "全文无「%s」" % bad_owner)

    # 11 §5.3 同段映射（不带倍率，只验 owner 归属）
    seg53 = [x for x in re.split(r"[；;，]", s53) if x.strip()]
    miss2 = []
    if not same_seg(seg53, ["黄", "值班厂长席"]):
        miss2.append("S5.3 缺 黄+值班厂长席 同段")
    if not same_seg(seg53, ["红", "厂长"]):
        miss2.append("S5.3 缺 红+厂长 同段")
    if miss2:
        bad("S5.3 同段映射", "；".join(miss2))
    else:
        ok("S5.3 同段映射", "黄→值班厂长席 / 红→厂长 各自同段")

    # 12 非工时类 2× 红升级发起人（T04 FAIL 第二处）
    if any("非工时类" in x and "2×" in x for x in seg4):
        ok("非工时类升级", "S4 非工时类与 2× 同段")
    else:
        bad("非工时类升级", "S4 缺「非工时类 2× 红升级发起人」")

    # 13 §2 的「升级发起人」须与「非工时类」同段（T04b FAIL：§2 括注过宽）
    #    键按 04:36 令文口径：令文里 2× 与非工时类按设计不同段，故不再要求 2× 同段
    seg2 = [x for x in re.split(r"[；;，、（）()]", s2) if x.strip()]
    if s2 and any("非工时类" in x and "升级发起人" in x for x in seg2):
        ok("S2 限定升级", "§2 非工时类与升级发起人同段")
    else:
        bad("S2 限定升级", "§2 缺「非工时类…升级发起人」同段限定")

    # 14 §2 须写明工时类豁免（与 §4/§5.3 一致；令文用「豁免」，旧稿用「不升级」，两者均认）
    if s2 and any("工时类" in x and ("豁免" in x or "不升级" in x) for x in seg2):
        ok("S2 工时类豁免", "§2 含「工时类…豁免/不升级」")
    else:
        bad("S2 工时类豁免", "§2 未写工时类豁免或不升级")

    # 15 §2 不得出现无条件「三选并升级发起人」过宽表述
    over = "三选并升级发起人"
    if s2 and over not in s2:
        ok("S2 无过宽句", "§2 无「%s」" % over)
    else:
        bad("S2 无过宽句", "§2 仍含「%s」" % over)

    # 16 §2 须同时含「非工时类」与「MES/08 §二2」（T04c-FIX 令 2-①）
    if s2 and '非工时类' in s2 and 'MES/08 §二2' in s2:
        ok("S2 非工时类+MES08引用", "§2 含「非工时类」与「MES/08 §二2」")
    else:
        bad("S2 非工时类+MES08引用", "§2 缺「非工时类」或「MES/08 §二2」")

    # 17 §2 中「升级发起人」必须与「非工时类」同括号（T04c-FIX 令 2-②）
    parens = re.findall(r'（[^）]*）', s2)
    esc = [p for p in parens if '升级发起人' in p]
    if esc and all('非工时类' in p for p in esc):
        ok("S2 同括号限定", "§2 含「升级发起人」的 %d 个括号均带「非工时类」" % len(esc))
    elif not esc:
        bad("S2 同括号限定", "§2 无含「升级发起人」的括号")
    else:
        bad("S2 同括号限定", "§2 有括号写「升级发起人」但无「非工时类」：%s" % esc[0][:80])

    # 18 §2 不得再出现无条件「并升级发起人」旧串（T04c-FIX 令 2-③）
    if s2 and '并升级发起人' not in s2:
        ok("S2 无并升级旧串", "§2 无「并升级发起人」")
    else:
        bad("S2 无并升级旧串", "§2 仍含「并升级发起人」")

    passed = sum(1 for x in RESULTS if x)
    print("\n%d passed / %d total" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 2


if __name__ == "__main__":
    sys.exit(main())
