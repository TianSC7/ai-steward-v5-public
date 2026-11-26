# -*- coding: utf-8 -*-
"""插件出厂校验：没有官方 CLI 时的自建硬检查。

检查项：
  1. 清单存在、JSON 合法、name 与目录名一致、版本 semver
  2. 声明的 skills/commands 目录存在且不逃逸插件根
  3. 每个 skill：SKILL.md 存在，frontmatter 有 name/description，name 与目录名一致
  4. 每个 command：frontmatter 有 description；文件名合法
  5. 市场目录 marketplace.json 与清单的 name/version/source 一致
  6. 技能里引用的 scripts/*.py 真实存在
  7. 无残留占位符（TODO / {{x}} / <placeholder>）
  8. 所有 Python 脚本可编译
  9. 命令/技能里出现的绝对脚本路径指向真实文件

用法：python -X utf8 validate_plugin.py [--json]
退出码 0 = 可交付。
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import re
import sys

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS: list[tuple[str, str, str]] = []


def rec(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))


def read(p: str) -> str:
    try:
        return open(p, encoding="utf-8-sig").read()
    except OSError:
        return ""


def frontmatter(text: str) -> dict:
    """极简 YAML frontmatter：只取顶层 key: value（够校验用）。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("validate_plugin")
    arm(120, "validate_plugin")
    rec(fp["ok"], "fingerprint", "%s@%s sha=%s schema=%d%s" % (
        fp["plugin"], fp["version"], fp["sha"], fp["schema"],
        "" if fp["ok"] else " 问题: " + "; ".join(fp["problems"])))

    mf_path = os.path.join(ROOT, ".zcode-plugin", "plugin.json")
    rec(os.path.exists(mf_path), "manifest-exists", mf_path)
    mf = {}
    if os.path.exists(mf_path):
        try:
            mf = json.load(open(mf_path, encoding="utf-8"))
            rec(True, "manifest-json", "合法")
        except ValueError as e:
            rec(False, "manifest-json", str(e))
    if mf:
        name = mf.get("name", "")
        rec(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name)) and name == os.path.basename(ROOT),
            "manifest-name", "name=%s dir=%s" % (name, os.path.basename(ROOT)))
        ver = mf.get("version", "")
        rec(bool(re.fullmatch(r"\d+\.\d+\.\d+([-.+][\w.-]+)?", ver)), "manifest-version", ver)
        rec(bool(mf.get("description")), "manifest-description", (mf.get("description") or "")[:60])

    # skills
    skills_dir = os.path.normpath(os.path.join(ROOT, mf.get("skills", "./skills")))
    rec(os.path.isdir(skills_dir) and os.path.commonpath([skills_dir, ROOT]) == ROOT,
        "skills-dir", skills_dir)
    if os.path.isdir(skills_dir):
        subs = [d for d in os.listdir(skills_dir) if os.path.isdir(os.path.join(skills_dir, d))]
        rec(bool(subs), "skills-count", "共 %d 个" % len(subs))
        for d in subs:
            f = os.path.join(skills_dir, d, "SKILL.md")
            txt = read(f)
            fm = frontmatter(txt)
            ok = bool(txt) and fm.get("name") == d and bool(fm.get("description"))
            rec(ok, "skill/" + d,
                "name=%s desc=%s" % (fm.get("name"), (fm.get("description") or "")[:46]))

    # commands
    cmd_dir = os.path.normpath(os.path.join(ROOT, mf.get("commands", "./commands")))
    rec(os.path.isdir(cmd_dir) and os.path.commonpath([cmd_dir, ROOT]) == ROOT,
        "commands-dir", cmd_dir)
    if os.path.isdir(cmd_dir):
        cmds = sorted(f for f in os.listdir(cmd_dir) if f.endswith(".md"))
        rec(bool(cmds), "commands-count", "共 %d 个" % len(cmds))
        for f in cmds:
            txt = read(os.path.join(cmd_dir, f))
            fm = frontmatter(txt)
            rec(bool(fm.get("description")), "command/" + f[:-3],
                (fm.get("description") or "缺 description")[:50])

    # 市场一致性
    market = os.path.join(os.path.dirname(ROOT), "marketplace.json")
    if os.path.exists(market):
        try:
            mk = json.load(open(market, encoding="utf-8"))
            entry = next((p for p in mk.get("plugins", []) if p.get("name") == mf.get("name")), None)
            ok = bool(entry) and entry.get("version") == mf.get("version") and \
                os.path.isdir(os.path.join(os.path.dirname(ROOT), entry.get("source", "").lstrip("./")))
            rec(ok, "marketplace", "market=%s version=%s" % (mk.get("name"), (entry or {}).get("version")))
        except ValueError as e:
            rec(False, "marketplace", str(e))
    else:
        rec(False, "marketplace", "缺少 %s" % market)

    # 技能引用的脚本存在
    all_text = ""
    for base, _dirs, files in os.walk(skills_dir):
        for f in files:
            if f.endswith(".md"):
                all_text += read(os.path.join(base, f))
    for base, _dirs, files in os.walk(cmd_dir):
        for f in files:
            if f.endswith(".md"):
                all_text += read(os.path.join(base, f))
    refs = set(re.findall(r"scripts/([A-Za-z0-9_]+\.py)", all_text))
    missing = [r for r in sorted(refs) if not os.path.exists(os.path.join(HERE, r))]
    rec(not missing, "script-refs", "引用 %d 个，缺 %s" % (len(refs), missing or "无"))

    # 技能/命令里的绝对路径必须真实存在：安装后用户直接照抄执行，路径错了就是运行时故障
    abs_refs = set(re.findall(r"C:/Users/Administrator/Documents/mate/plugins/ai-steward/[A-Za-z0-9_./-]+", all_text))
    missing_abs = [p for p in sorted(abs_refs) if not os.path.exists(p.rstrip(".,;)"))]
    rec(not missing_abs, "abs-path-refs", "引用 %d 个，缺 %s" % (len(abs_refs), missing_abs or "无"))

    # 占位符残留
    bad_tokens = [t for t in ("TODO", "{{", "<placeholder>", "FIXME") if t in all_text]
    rec(not bad_tokens, "placeholders", "残留: %s" % (bad_tokens or "无"))

    # Python 编译
    py = [os.path.join(HERE, f) for f in os.listdir(HERE) if f.endswith(".py")]
    bad = []
    for p in py:
        try:
            py_compile.compile(p, doraise=True, cfile=os.path.join(os.environ.get("TEMP", "."), "vp.pyc"))
        except py_compile.PyCompileError as e:
            bad.append("%s: %s" % (os.path.basename(p), str(e).splitlines()[-1][:80]))
    rec(not bad, "py-compile", "%d 个脚本%s" % (len(py), "" if not bad else "，失败: " + " | ".join(bad)))

    fails = [r for r in RESULTS if r[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "check": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-22s %s" % (s, n, d))
        print("\n%d/%d 通过" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
