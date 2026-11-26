# -*- coding: utf-8 -*-
"""本地密钥保险库（Windows DPAPI 加密，用户级绑定）。

定位：
  - 中转站/官方 AI 密钥的**唯一落盘点**：只存 DPAPI 密文，明文只在内存中短暂存在。
  - 信任三档：low（默认会泄露，只准跑无敏感数据的活）/ high（一般不泄露）/ official（官方）。
  - 信任策略与派单红线见 config/KEY_POLICY.md；网关/调度台按 trust 选道，不看明文。

纪律（写死在代码里，不是约定）：
  1. 任何输出不出现明文密钥——list/verify/meta 只出掩码（前4…后4）。
  2. 没有 delete 命令：密钥条目只允许 enable/disable/rotate（轮换留掩码历史）。
  3. 明文不进 argv（进程列表可见）、不进日志、不进报告；录入走 --key-file/--key-env/--stdin。
  4. 落盘原子写 + 排他锁 + 写前备份（与任务台同模式）。

用法：
  python -X utf8 key_vault.py add --name relay-a --trust low --base-url https://a/v1 \
      [--models "gpt-4o,claude-*"] [--note ...] --key-file C:/temp/k.txt
  python -X utf8 key_vault.py list [--json]        # 掩码清单
  python -X utf8 key_vault.py verify               # 全量解密自检（仍只出掩码）
  python -X utf8 key_vault.py disable --id KV-001 --reason "疑似泄露"
  python -X utf8 key_vault.py rotate --id KV-001 --key-env NEWKEY
  python -X utf8 key_vault.py set-trust --id KV-001 --trust high

退出码：0 正常；2 参数/数据错误；3 DPAPI/环境错误。
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import datetime
import hashlib
import json
import os
import sys
import time

sys.dont_write_bytecode = True

try:
    import fingerprint
except Exception:  # 直接跑（非测试）也允许，但缺指纹自检
    fingerprint = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ai-steward")
DEFAULT_VAULT = os.path.join(APP_DIR, "key_vault.json")

TRUST_LEVELS = ("low", "high", "official")
ENTROPY = b"ai-steward-key-vault-v1"  # DPAPI 之外的固定加盐；换掉=全库作废
MIN_KEY_LEN = 16

# ---------------------------------------------------------------- DPAPI 封装

if sys.platform != "win32":
    print("ERROR key_vault 仅支持 Windows（DPAPI）", file=sys.stderr)
    raise SystemExit(3)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_protect(data: bytes) -> bytes:
    """CryptProtectData（当前用户 + 本机绑定；CRYPTPROTECT_UI_FORBIDDEN）。"""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("dpapi_protect 需要 bytes")
    inb, keep_in = _blob(bytes(data))
    ent, keep_ent = _blob(ENTROPY)
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError("CryptProtectData 失败 hr=%s" % ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
        del keep_in, keep_ent


def dpapi_unprotect(blob: bytes) -> bytes:
    inb, keep_in = _blob(bytes(blob))
    ent, keep_ent = _blob(ENTROPY)
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError("CryptUnprotectData 失败 hr=%s（换用户/换机器/换盐都会这样）"
                      % ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
        del keep_in, keep_ent


# ---------------------------------------------------------------- 基础工具

def now_iso() -> str:
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def mask(secret: str) -> str:
    """只出前4…后4；短密钥连掩码都不给。"""
    s = str(secret or "")
    if len(s) < 12:
        return "…(%d chars)" % len(s)
    return "%s…%s" % (s[:4], s[-4:])


def fingerprint_of(secret: str) -> str:
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()[:12]


def load(path: str) -> dict:
    if not os.path.isfile(path):
        return {"entries": [], "created_at": now_iso(), "updated_at": now_iso()}
    data = json.load(open(path, encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("保险库根结构不合法: %s" % path)
    return data


def save(path: str, vault: dict) -> None:
    """临时文件 + fsync + os.replace；写前留带时间戳备份。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if os.path.isfile(path):
        bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        with open(path, "rb") as fr, open(bak, "wb") as fw:
            fw.write(fr.read())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(vault, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(path: str, timeout: float = 10.0):
    """与任务台同款排他锁；>60s 的陈旧锁自动清理。"""
    lock = path + ".lock"
    deadline = time.time() + timeout
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 60:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise SystemExit("LOCK-TIMEOUT %s" % lock)
            time.sleep(0.2)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(lock)


# ---------------------------------------------------------------- 命令实现

def read_secret(args) -> str:
    """明文只从这里进来：--key-file / --key-env / --stdin（tty 时密语不回显）。"""
    got = []
    if getattr(args, "key_file", None):
        got.append(("key-file", open(args.key_file, "r", encoding="utf-8").read().strip()))
    if getattr(args, "key_env", None):
        v = os.environ.get(args.key_env, "")
        if not v:
            raise SystemExit("环境变量 %s 不存在或为空" % args.key_env)
        got.append(("key-env", v.strip()))
    if getattr(args, "stdin", False):
        line = sys.stdin.readline().strip()
        got.append(("stdin", line))
    if not got:
        if sys.stdin.isatty():
            import getpass
            got.append(("prompt", getpass.getpass("粘贴密钥（输入不回显）: ").strip()))
        else:
            raise SystemExit("缺少密钥来源：--key-file / --key-env / --stdin / 交互输入")
    source, secret = got[0]
    if len(got) > 1:
        raise SystemExit("给了多个密钥来源（%s），一次只要一个" % ",".join(s for s, _ in got))
    if not secret:
        raise SystemExit("密钥为空（来源 %s）" % source)
    if len(secret) < MIN_KEY_LEN:
        raise SystemExit("密钥太短（%d < %d），拒绝入库" % (len(secret), MIN_KEY_LEN))
    return secret


def next_id(entries: list) -> str:
    n = 0
    for e in entries:
        try:
            n = max(n, int(str(e.get("id", "KV-0")).split("-")[-1]))
        except ValueError:
            pass
    return "KV-%03d" % (n + 1)


def cmd_add(vault: dict, args) -> dict:
    name = (args.name or "").strip()
    trust = (args.trust or "").strip().lower()
    base_url = (args.base_url or "").strip().rstrip("/")
    if not name or not trust or not base_url:
        raise SystemExit("add 需要 --name --trust --base-url")
    if trust not in TRUST_LEVELS:
        raise SystemExit("BAD-TRUST %r（合法：%s）" % (trust, ",".join(TRUST_LEVELS)))
    if any(e.get("name") == name for e in vault["entries"]):
        raise SystemExit("重名 %r：已有条目请用 rotate 轮换，不要重复添加" % name)
    secret = read_secret(args)
    entry = {
        "id": next_id(vault["entries"]),
        "name": name,
        "trust": trust,
        "base_url": base_url,
        "models": [m.strip() for m in (args.models or "").split(",") if m.strip()],
        "enabled": True,
        "note": (args.note or "").strip(),
        "key_cipher": base64.b64encode(dpapi_protect(secret.encode("utf-8"))).decode("ascii"),
        "key_fingerprint": fingerprint_of(secret),
        "key_mask": mask(secret),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "rotated_history": [],
    }
    vault["entries"].append(entry)
    vault["updated_at"] = now_iso()
    return entry


def _find(vault: dict, ref: str) -> dict:
    for e in vault["entries"]:
        if e["id"] == ref or e["name"] == ref:
            return e
    raise SystemExit("找不到条目 %r（用 list 查 id/name）" % ref)


def cmd_rotate(vault: dict, args) -> dict:
    e = _find(vault, args.id)
    secret = read_secret(args)
    e.setdefault("rotated_history", []).append({
        "key_mask": e.get("key_mask", "?"),
        "key_fingerprint": e.get("key_fingerprint", "?"),
        "retired_at": now_iso(),
        "reason": (args.reason or "").strip(),
    })
    e["key_cipher"] = base64.b64encode(dpapi_protect(secret.encode("utf-8"))).decode("ascii")
    e["key_fingerprint"] = fingerprint_of(secret)
    e["key_mask"] = mask(secret)
    e["updated_at"] = now_iso()
    return e


def cmd_disable(vault: dict, args) -> dict:
    e = _find(vault, args.id)
    e["enabled"] = False
    e["disabled_reason"] = (args.reason or "").strip()
    e["updated_at"] = now_iso()
    return e


def cmd_enable(vault: dict, args) -> dict:
    e = _find(vault, args.id)
    e["enabled"] = True
    e.pop("disabled_reason", None)
    e["updated_at"] = now_iso()
    return e


def cmd_set_trust(vault: dict, args) -> dict:
    trust = (args.trust or "").strip().lower()
    if trust not in TRUST_LEVELS:
        raise SystemExit("BAD-TRUST %r（合法：%s）" % (trust, ",".join(TRUST_LEVELS)))
    e = _find(vault, args.id)
    e["trust"] = trust
    e["updated_at"] = now_iso()
    return e


def public_entry(e: dict) -> dict:
    """对外视图：一切字段可见，唯独没有密文与明文。"""
    out = dict(e)
    out.pop("key_cipher", None)
    return out


def decrypt_entry(e: dict) -> str:
    return dpapi_unprotect(base64.b64decode(e["key_cipher"])).decode("utf-8")


def cmd_verify(vault: dict) -> list:
    """全量解密自检：长度/指纹校验；输出只有掩码。"""
    problems = []
    for e in vault["entries"]:
        try:
            secret = decrypt_entry(e)
            if len(secret) < MIN_KEY_LEN:
                problems.append("%s %s 长度 %d 异常" % (e["id"], e.get("key_mask"), len(secret)))
            elif fingerprint_of(secret) != e.get("key_fingerprint"):
                problems.append("%s %s 指纹不符（条目被改过？）" % (e["id"], e.get("key_mask")))
        except OSError as exc:
            problems.append("%s 解密失败：%s" % (e["id"], exc))
    return problems


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="本地 DPAPI 密钥保险库（输出永远只有掩码）")
    ap.add_argument("--vault", default=DEFAULT_VAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--trust", required=True, choices=TRUST_LEVELS)
    p.add_argument("--base-url", required=True)
    p.add_argument("--models", default="")
    p.add_argument("--note", default="")
    for flag in ("--key-file", "--key-env"):
        p.add_argument(flag, default=None)
    p.add_argument("--stdin", action="store_true")

    for name in ("rotate", "disable", "enable", "set-trust", "meta"):
        p = sub.add_parser(name)
        p.add_argument("--id", required=True)
        if name == "rotate":
            for flag in ("--key-file", "--key-env"):
                p.add_argument(flag, default=None)
            p.add_argument("--stdin", action="store_true")
            p.add_argument("--reason", default="")
        if name == "disable":
            p.add_argument("--reason", default="")
        if name == "set-trust":
            p.add_argument("--trust", required=True, choices=TRUST_LEVELS)

    sub.add_parser("list")
    p = sub.add_parser("verify")

    p = sub.add_parser("export-mask", help="掩码清单 JSON（可进报告/日报）")

    args = ap.parse_args()
    if fingerprint is not None:
        fp = fingerprint.announce("key_vault")
        if not fp["ok"]:
            print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
            return 2

    rc = 0
    if args.cmd == "add":
        with locked(args.vault):
            vault = load(args.vault)
            e = cmd_add(vault, args)
            save(args.vault, vault)
        print("ADDED %s %s trust=%s url=%s key=%s（明文未回显）"
              % (e["id"], e["name"], e["trust"], e["base_url"], e["key_mask"]))
    elif args.cmd == "rotate":
        with locked(args.vault):
            vault = load(args.vault)
            e = cmd_rotate(vault, args)
            save(args.vault, vault)
        print("ROTATED %s %s -> %s（旧钥留掩码历史）" % (e["id"], e["name"], e["key_mask"]))
    elif args.cmd == "disable":
        with locked(args.vault):
            vault = load(args.vault)
            e = cmd_disable(vault, args)
            save(args.vault, vault)
        print("DISABLED %s %s reason=%s" % (e["id"], e["name"], e.get("disabled_reason", "")))
    elif args.cmd == "enable":
        with locked(args.vault):
            vault = load(args.vault)
            e = cmd_enable(vault, args)
            save(args.vault, vault)
        print("ENABLED %s %s" % (e["id"], e["name"]))
    elif args.cmd == "set-trust":
        with locked(args.vault):
            vault = load(args.vault)
            e = cmd_set_trust(vault, args)
            save(args.vault, vault)
        print("SET-TRUST %s %s -> %s" % (e["id"], e["name"], e["trust"]))
    elif args.cmd == "list":
        vault = load(args.vault)
        for e in vault["entries"]:
            print("%s %-18s trust=%-8s %-6s %s %s" % (
                e["id"], e["name"], e["trust"], "on" if e.get("enabled", True) else "OFF",
                e.get("base_url", ""), e.get("key_mask", "")))
        print("total=%d vault=%s" % (len(vault["entries"]), args.vault))
    elif args.cmd == "meta":
        vault = load(args.vault)
        print(json.dumps(public_entry(_find(vault, args.id)), ensure_ascii=False, indent=2))
    elif args.cmd == "verify":
        vault = load(args.vault)
        problems = cmd_verify(vault)
        if problems:
            for x in problems:
                print("[BAD] " + x)
            rc = 2
        else:
            print("OK entries=%d（全部可解密、指纹一致；输出只含掩码）" % len(vault["entries"]))
    elif args.cmd == "export-mask":
        vault = load(args.vault)
        print(json.dumps([public_entry(e) for e in vault["entries"]],
                         ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
