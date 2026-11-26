# -*- coding: utf-8 -*-
"""密钥保险库边界用例：明文绝不落盘/绝不回显，DPAPI 可逆，条目不可删。

覆盖：
  1 DPAPI 加解密往返
  2 加错盐解不开（OSError）
  3 add：字段齐全 + 密文不是明文 + 指纹正确
  4 落盘文件与备份里都搜不到明文
  5 掩码：长密钥 前4…后4；短密钥只给长度
  6 public_entry 剥掉密文
  7 重名拒绝（提示用 rotate）
  8 密钥过短拒绝
  9 多个密钥来源拒绝
  10 rotate：换指纹 + 旧钥进掩码历史 + 仍无明文
  11 disable/enable 往返
  12 set-trust 校验非法值
  13 verify 全绿 + 指纹被篡改能查出
  14 没有 delete 子命令（铁律：只能停用/轮换）
  15 原子写留下备份
  16 id 递增

用法：python -X utf8 test_key_vault.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import key_vault as kv  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
KEY = "sk-test-0123456789abcdefWXYZ"


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def raises(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except (SystemExit, ValueError, OSError):
        return True
    return False


def add_args(**kw):
    base = dict(name="relay-a", trust="low", base_url="https://relay.a/v1",
                models="gpt-4o,claude-*", note="", key_file=None, key_env=None,
                stdin=False)
    base.update(kw)
    return SimpleNamespace(**base)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_key_vault")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_key_vault")

    # 1 DPAPI 往返
    blob = kv.dpapi_protect(KEY.encode("utf-8"))
    back = kv.dpapi_unprotect(blob).decode("utf-8")
    check("dpapi-roundtrip", back == KEY and blob != KEY.encode("utf-8"),
          "len(blob)=%d" % len(blob))

    # 2 换盐解不开
    old_entropy = kv.ENTROPY
    kv.ENTROPY = b"someone-else"
    check("wrong-entropy-denied", raises(kv.dpapi_unprotect, blob))
    kv.ENTROPY = old_entropy

    tmp = tempfile.mkdtemp(prefix="key-vault-")
    try:
        vault_path = os.path.join(tmp, "key_vault.json")
        keyfile = os.path.join(tmp, "k.txt")
        open(keyfile, "w", encoding="utf-8").write(KEY)

        v = kv.load(vault_path)
        e1 = kv.cmd_add(v, add_args(key_file=keyfile))
        # 3 字段 + 密文
        import base64 as b64
        cipher = b64.b64decode(e1["key_cipher"])
        check("add-fields", e1["id"] == "KV-001" and e1["trust"] == "low"
              and e1["enabled"] is True and e1["models"] == ["gpt-4o", "claude-*"]
              and kv.fingerprint_of(KEY) == e1["key_fingerprint"]
              and cipher != KEY.encode("utf-8"),
              "id=%s trust=%s" % (e1["id"], e1["trust"]))

        # 4 落盘无明文（正文 + 备份）
        kv.save(vault_path, v)
        raw1 = open(vault_path, "rb").read()
        kv.cmd_add(v, add_args(name="relay-b", trust="official",
                               base_url="https://api.vendor.com/v1",
                               key_file=keyfile))
        kv.save(vault_path, v)
        baks = [x for x in os.listdir(tmp) if x.startswith("key_vault.json.bak-")]
        raw_all = raw1 + open(vault_path, "rb").read()
        for b in baks:
            raw_all += open(os.path.join(tmp, b), "rb").read()
        check("no-plaintext-on-disk", KEY.encode("utf-8") not in raw_all,
              "baks=%d" % len(baks))

        # 15 原子写备份存在
        check("atomic-backup", len(baks) >= 1, "baks=%d" % len(baks))

        # 16 id 递增
        e2 = [x for x in v["entries"] if x["name"] == "relay-b"][0]
        check("id-increment", e2["id"] == "KV-002", e2["id"])

        # 5 掩码
        m = kv.mask(KEY)
        check("mask-format", m == "sk-t…WXYZ" and KEY not in m, m)
        check("mask-short", kv.mask("short") == "…(5 chars)", kv.mask("short"))

        # 6 public_entry 剥密文
        pub = kv.public_entry(e1)
        check("public-no-cipher", "key_cipher" not in pub
              and pub.get("key_fingerprint") == e1["key_fingerprint"])

        # 7 重名拒绝
        check("dup-name-rejected", raises(kv.cmd_add, v, add_args(key_file=keyfile)))
        # 8 过短拒绝
        open(keyfile, "w", encoding="utf-8").write("sk-short")
        check("short-key-rejected", raises(kv.cmd_add, v,
                                           add_args(name="relay-c", key_file=keyfile)))
        open(keyfile, "w", encoding="utf-8").write(KEY)
        # 9 多来源拒绝
        check("multi-source-rejected",
              raises(kv.cmd_add, v, add_args(name="relay-c", key_file=keyfile,
                                             key_env="PATH")))

        # 10 rotate
        newkey = "sk-new-9876543210abcdefQRST"
        open(keyfile, "w", encoding="utf-8").write(newkey)
        er = kv.cmd_rotate(v, SimpleNamespace(id="KV-001", reason="泄露疑虑",
                                              key_file=keyfile, key_env=None,
                                              stdin=False))
        raw2 = json.dumps(v, ensure_ascii=False)
        check("rotate", er["key_fingerprint"] == kv.fingerprint_of(newkey)
              and len(er["rotated_history"]) == 1
              and er["rotated_history"][0]["key_mask"] == "sk-t…WXYZ"
              and newkey not in raw2 and KEY not in raw2,
              er["key_mask"])

        # 11 disable/enable（先拍快照再 enable，两者是同一个对象）
        snap = dict(kv.cmd_disable(v, SimpleNamespace(id="KV-001", reason="停用观察")))
        e = kv.cmd_enable(v, SimpleNamespace(id="KV-001"))
        check("disable-enable", snap["enabled"] is False
              and snap.get("disabled_reason") == "停用观察" and e["enabled"] is True
              and "disabled_reason" not in e)

        # 12 set-trust 非法值
        check("bad-trust-rejected",
              raises(kv.cmd_set_trust, v, SimpleNamespace(id="KV-001", trust="mid")))
        kv.cmd_set_trust(v, SimpleNamespace(id="KV-001", trust="high"))
        check("set-trust", e["trust"] == "high", e["trust"])

        # 13 verify 全绿
        check("verify-ok", kv.cmd_verify(v) == [], str(kv.cmd_verify(v)))
        e["key_fingerprint"] = "0" * 12  # 篡改
        problems = kv.cmd_verify(v)
        check("verify-tamper", any("指纹不符" in p for p in problems),
              "; ".join(problems)[:80])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 14 没有 delete 子命令
    help_text = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.join(HERE, "key_vault.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    sub_line = [l for l in help_text.splitlines() if "{" in l and "}" in l]
    joined = " ".join(sub_line)
    check("no-delete-cmd", "delete" not in joined and "add" in joined, joined[:80])

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-24s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
