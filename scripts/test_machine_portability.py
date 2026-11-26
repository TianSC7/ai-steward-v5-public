# -*- coding: utf-8 -*-
"""机别污染自检：把"旧机关机迁移把 TIAN 盘快照覆盖到 AA 插件目录"挡在门禁外。

背景（INC-20261118-006）：TIAN 关机迁移时把 TIAN 盘旧快照覆盖到 AA 插件目录，
permission_allow.py 丢了 run()，且 DEFAULT_PROTECTED 里保护路径写死旧机用户目录
（旧机用户名即下方 OLD_USER，本文件自身也要过同一条扫描，故不写完整字面量）。
此前只能靠人眼发现。本用例机械化这件事：

  1  全仓扫描 *.py/*.json/*.ps1/*.sh：命中旧机用户目录字面量即失败，逐条打印 文件:行号；
  2  迁移期 3 个一次性脚本按显式白名单放行（它们自身的 OLD 常量就是待替换字面量，
     只作审计追溯、不参与运行）；
  3  state/registry.json、state/worktrees.json 是运行时存档而非代码，只报 WARN 不判红
     （registry.json 存历史任务卡原文，含引用旧路径的卡片；判红会让门禁永远红，
     进而失去拦截新污染的能力。_aa_paths_round2.py 第 7-8 行已把它们列为"历史记录，不改写"）；
  4  文档/注释里的字面量按 (文件, 行号) 精确豁免，且必须程序化证明该行确实落在
     docstring/注释内——行内容一变（例如样板变成真代码）豁免立即失效、用例报红；
  5  DEFAULT_PROTECTED 每条都必须经 os.path.expanduser 展开（"~" 开头），
     或用系统常量（白名单仅 C:/Windows）；用户配置目录写死盘符一律判红
     （同样用临时夹具证伪：裸字面量 / expanduser 套绝对路径两种变体都要报红）；
  6  临时夹具证伪：造带字面量的脚本必须被抓到，证明扫描器真的生效（不是永远为绿）。

自指规避：本文件本身就是被扫描对象，所以禁用令牌拆成 "TI" + "AN" 拼接，
文件里不会出现完整字面量。

用法：python -X utf8 test_machine_portability.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys
import tempfile
import tokenize

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import permission_allow as pa  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---- 扫描规则 ---------------------------------------------------------------
EXTS = (".py", ".json", ".ps1", ".sh")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git"}

# 旧机用户名拆开写：本文件自身也要过同一条扫描，不能自带完整字面量。
OLD_USER = "TI" + "AN"
# 分隔符允许正斜杠、单反斜杠，以及源码/JSON 里转义后的双反斜杠（C:\\Users\\...）。
_SEP = r"(?:[/\\]{1,2})"
_HIT_RE = re.compile(
    r"[A-Za-z]:" + _SEP + r"Users" + _SEP + OLD_USER + r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# 任务卡显式白名单：迁移期 3 个一次性脚本。
# 理由：_aa_fix_paths.py / _aa_paths_round2.py 的 OLD 常量、_fix_hooks_py_path.py 的 OLD
# 变量本身就是"被替换掉的旧路径"，是最直接的历史证据；它们不会被任何调度入口调用。
WHITELIST_SCRIPTS = (
    "scripts/_aa_fix_paths.py",
    "scripts/_aa_paths_round2.py",
    "scripts/_fix_hooks_py_path.py",
)

# 运行时存档（不是代码/配置）：只报 WARN。见文件头 3。
WHITELIST_STATE = (
    "state/registry.json",
    "state/worktrees.json",
)

# 文档豁免：按 (文件, 行号) 精确放行，且必须程序化证明该行在 docstring/注释内。
DOC_EXEMPT = {
    "scripts/set_permission.py": {
        65: "project_id_from_path 的 docstring 样例，记录 project_id 规则的历史形态；非可执行代码",
    },
}

# DEFAULT_PROTECTED 允许的"系统常量"：不随用户名变化，与机别无关。
# 注意：C:/Windows 仍是写死盘符，%SystemRoot% 更可移植——改本体不在本卡范围，已写入未决。
SYSTEM_LITERAL_OK = {"C:/Windows"}


# ---- 扫描器（被用例 1 使用，也被用例 2/3/4/6 拿去证伪）----------------------
def scan_text(text: str) -> list[tuple[int, str]]:
    """返回 [(行号, 片段)]；只报第一处/行，够定位即可。"""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if _HIT_RE.search(line):
            hits.append((i, line.strip()[:120]))
    return hits


def scan_file(path: str) -> list[tuple[int, str]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return scan_text(f.read())
    except OSError:
        return []


def iter_scan_files(root: str):
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for fn in sorted(files):
            if fn.lower().endswith(EXTS):
                yield os.path.join(base, fn)


def _rel(path: str, root: str = ROOT) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _line_is_documentation(path: str, lineno: int) -> bool:
    """证明该行命中落在 docstring / 注释内（而非真正的代码字符串）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return False
    # 1) 行注释
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and tok.start[0] == lineno:
                return True
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    # 2) 模块/类/函数 docstring 覆盖该行
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            continue
        end = getattr(first, "end_lineno", None) or first.lineno
        if first.lineno <= lineno <= end:
            return True
    return False


def collect(root: str = ROOT):
    """扫全树。返回 (offenders, warns)，元素为 (相对路径, 行号, 片段)。"""
    offenders, warns = [], []
    for path in iter_scan_files(root):
        r = _rel(path, root)
        for lineno, snippet in scan_file(path):
            if r in WHITELIST_SCRIPTS or r in WHITELIST_STATE:
                warns.append((r, lineno, snippet))
                continue
            if r in DOC_EXEMPT and lineno in DOC_EXEMPT[r] \
                    and _line_is_documentation(path, lineno):
                warns.append((r, lineno, snippet))
                continue
            offenders.append((r, lineno, snippet))
    return offenders, warns


# ---- DEFAULT_PROTECTED 检查 -------------------------------------------------
def _default_protected_node(path: str):
    with open(path, encoding="utf-8", errors="replace") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "DEFAULT_PROTECTED":
                return node.value
    return None


def _is_expanduser_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    return name == "expanduser"


def _audit_default_protected(path: str):
    """返回 (node, violations, system_literals)。violations 非空 = 不合规。"""
    node = _default_protected_node(path)
    assert node is not None, "%s 里找不到 DEFAULT_PROTECTED" % os.path.basename(path)
    assert isinstance(node, ast.List) and node.elts, "DEFAULT_PROTECTED 必须是字面量列表"
    bad, literals = [], []
    for el in node.elts:
        if _is_expanduser_call(el):
            arg = el.args[0] if el.args else None
            txt = getattr(arg, "value", None) if isinstance(arg, ast.Constant) else None
            if not (isinstance(txt, str) and txt.startswith("~")):
                bad.append("expanduser 参数不是 ~ 开头: %r" % (txt,))
            elif re.search(r"[A-Za-z]:[/\\]+", txt):
                bad.append("用户路径写死盘符: %r" % (txt,))
        elif isinstance(el, ast.Constant) and isinstance(el.value, str):
            if el.value in SYSTEM_LITERAL_OK:
                literals.append(el.value)
            else:
                bad.append("写死路径未经 expanduser: %r" % (el.value,))
        else:
            bad.append("无法静态判定的条目: %s" % ast.dump(el)[:80])
    return node, bad, literals


# ---- 用例 -------------------------------------------------------------------
@case
def case_repo_scan_clean():
    offenders, warns = collect()
    by_file: dict[str, list[int]] = {}
    for r, lineno, _ in warns:
        by_file.setdefault(r, []).append(lineno)
    for r in sorted(by_file):
        print("      [WARN] 豁免命中 %s:%s（白名单/存档/docstring）" % (
            r, ",".join(str(x) for x in by_file[r])))
    assert not offenders, "机别污染 %d 处（文件:行号）：\n  %s" % (
        len(offenders),
        "\n  ".join("%s:%d: %s" % (r, ln, sn) for r, ln, sn in offenders),
    )
    return "代码/配置 0 命中；豁免 %d 处/%d 文件（已逐条打印）" % (len(warns), len(by_file))


@case
def case_fixture_forward_slash_red():
    token = "C:/Users/" + OLD_USER + "/Documents/mate"
    with tempfile.TemporaryDirectory(prefix="w021f_") as d:
        p = os.path.join(d, "polluted.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# 旧机快照\nROOT = %r\n" % token)
        hits = scan_file(p)
    assert hits and hits[0][0] == 2, hits
    return "正斜杠形态被抓：行 %d" % hits[0][0]


@case
def case_fixture_backslash_forms_red():
    single = "C:" + "\\" + "Users" + "\\" + OLD_USER + "\\Documents"  # 文件里就是单反斜杠
    escaped = single.replace("\\", "\\\\")  # 文件里是双反斜杠（py/json 转义形态）
    for label, token in (("单反斜杠", single), ("双反斜杠转义", escaped)):
        with tempfile.TemporaryDirectory(prefix="w021b_") as d:
            p = os.path.join(d, "polluted.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"root": "%s"}\n' % token)
            hits = scan_file(p)
        assert hits, "%s 形态漏抓: %r" % (label, token)
    return "单/双反斜杠形态均被抓"


@case
def case_fixture_all_extensions():
    token = "C:/Users/" + OLD_USER + "/x"
    with tempfile.TemporaryDirectory(prefix="w021e_") as d:
        for ext in EXTS:
            p = os.path.join(d, "f" + ext)
            with open(p, "w", encoding="utf-8") as f:
                f.write("root=%s\n" % token)
        p = os.path.join(d, "clean.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("root = 'C:/Users/Administrator/x'\n")
        got = sorted(_rel(x, d) for x in iter_scan_files(d) if scan_file(x))
    assert got == sorted("f" + e for e in EXTS), got
    return "覆盖 %s；干净文件不误报" % "/".join(EXTS)


@case
def case_allowlist_explicit():
    for r in WHITELIST_SCRIPTS + WHITELIST_STATE:
        assert "*" not in r and "?" not in r and "[" not in r, "白名单禁止通配: " + r
        assert os.path.isfile(os.path.join(ROOT, r)), "白名单文件不存在: " + r
    for r in WHITELIST_STATE:
        assert r.startswith("state/") and r.endswith(".json"), "存档豁免越界: " + r
    assert tuple(WHITELIST_SCRIPTS) == (
        "scripts/_aa_fix_paths.py",
        "scripts/_aa_paths_round2.py",
        "scripts/_fix_hooks_py_path.py",
    ), "迁移脚本白名单被改动，需与任务卡同步: %r" % (WHITELIST_SCRIPTS,)
    return "3 迁移脚本 + 2 运行时存档，均为逐文件、无通配"


@case
def case_lineno_accuracy():
    token = "C:/Users/" + OLD_USER + "/y"
    with tempfile.TemporaryDirectory(prefix="w021l_") as d:
        p = os.path.join(d, "a.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("a = 1\nb = 2\n# 注释\nc = 3\nd = %r\ne = 5\n" % token)
        offenders, _ = collect(d)
    assert len(offenders) == 1, offenders
    assert offenders[0][0] == "a.py" and offenders[0][1] == 5, offenders
    return "行号定位准确：a.py:5"


@case
def case_default_protected_ast():
    _, bad, literals = _audit_default_protected(os.path.join(HERE, "permission_allow.py"))
    assert not bad, "DEFAULT_PROTECTED 不合规：\n  " + "\n  ".join(bad)
    assert literals == ["C:/Windows"], "系统常量白名单被扩大: %r" % (literals,)
    node = _default_protected_node(os.path.join(HERE, "permission_allow.py"))
    return "%d 条全部 expanduser；系统常量仅 %s" % (len(node.elts) - len(literals),
                                                    literals[0])


@case
def case_default_protected_rejects_literal():
    """证伪：写死旧机用户目录 / expanduser 套绝对路径，两种变体都必须判红。"""
    variants = (
        ("裸字面量", '"%s"' % ("C:/Users/" + OLD_USER + "/secrets")),
        ("expanduser 套绝对路径", 'os.path.expanduser("%s")'
         % ("C:/Users/" + OLD_USER + "/.ssh")),
    )
    for label, entry in variants:
        src = 'import os\nDEFAULT_PROTECTED = [\n    os.path.expanduser("~/.ssh"),\n    %s,\n]\n' % entry
        with tempfile.TemporaryDirectory(prefix="w021p_") as d:
            p = os.path.join(d, "polluted_protected.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            _, bad, _ = _audit_default_protected(p)
        assert bad, "%s 变体未被判红" % label
    return "裸字面量 / expanduser 套盘符 均判红"


@case
def case_default_protected_runtime():
    vals = list(pa.DEFAULT_PROTECTED)
    home = os.path.expanduser("~")
    assert len(vals) >= 5, vals
    for v in vals:
        assert "~" not in v, "未展开（残留 ~）: %r" % (v,)
        assert os.path.isabs(v), "非绝对路径: %r" % (v,)
        assert OLD_USER.lower() not in v.lower(), "旧机用户名残留: %r" % (v,)
    under_home = [v for v in vals if os.path.normcase(v).startswith(os.path.normcase(home))]
    assert len(under_home) >= 4, "指向当前用户 home 的条目过少: %r" % (under_home,)
    assert os.path.normcase(os.path.expanduser("~/.ssh")) in [os.path.normcase(v) for v in vals]
    return "%d 条已展开、均绝对、%d 条落在当前 home" % (len(vals), len(under_home))


@case
def case_doc_exempt_verified():
    for r, lines in DOC_EXEMPT.items():
        p = os.path.join(ROOT, r)
        assert os.path.isfile(p), "文档豁免文件不存在: " + r
        hits = dict(scan_file(p))
        for lineno, why in lines.items():
            assert lineno in hits, "豁免行 %s:%d 已不存在该字面量，请删掉这条豁免" % (r, lineno)
            assert _line_is_documentation(p, lineno), \
                "%s:%d 不是 docstring/注释（真代码不能豁免）" % (r, lineno)
            assert why.strip(), "豁免必须写理由"
    return "%d 个文件 %d 行，均为 docstring 且理由齐备" % (
        len(DOC_EXEMPT), sum(len(v) for v in DOC_EXEMPT.values()))


@case
def case_hook_entrypoints_exist():
    # INC-20261118-006 的另一半：旧快照把 run() 冲掉了，钩子静默失效。
    assert callable(getattr(pa, "run", None)), "permission_allow.run 丢失"
    assert callable(getattr(pa, "main", None)), "permission_allow.main 丢失"
    out = pa.decide({"session_id": "x", "cwd": "/nowhere", "tool_name": "read",
                     "tool_input": {}})
    assert isinstance(out, tuple) and len(out) == 2, out
    return "run/main/decide 均在且可调用"


def main() -> int:
    ok = 0
    for fn in CASES:
        try:
            detail = fn()
            print("[PASS] %-34s %s" % (fn.__name__[5:], detail))
            ok += 1
        except Exception as e:  # noqa: BLE001
            print("[FAIL] %-34s %s: %s" % (fn.__name__[5:], type(e).__name__, e))
    print("| %d passed / %d total" % (ok, len(CASES)))
    print("EXIT=%d" % (0 if ok == len(CASES) else 2))
    return 0 if ok == len(CASES) else 2


if __name__ == "__main__":
    sys.exit(main())
