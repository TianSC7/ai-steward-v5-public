# -*- coding: utf-8 -*-
"""Generic kernel-order enqueue helper (ASCII args; Chinese title from UTF-8 txt).

Usage: python enqueue_kernel.py <order_id> <spec_file_abs> <title_txt_abs> <max_attempts> [targets_csv]

Target files are resolved from the spec's 「改动文件」 section (backticked repo paths).
An explicit targets_csv overrides detection. There is NO default target: a code order
without declared targets is rejected instead of silently pointing at factory_runner.py.
"""
import re
import subprocess
import sys

RUNNER = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\scripts\factory_runner.py"
REPO = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"

PATH_RE = re.compile(r"`((?:scripts|state|dashboard|commands|hooks|config|knowledge|release)/[A-Za-z0-9_./-]+\.(?:py|html|js|css|json|ps1|md))`")
CHANGE_HEAD_RE = re.compile(r"^#{2,3}\s*.*(改动|修改|交付|变更)")


def detect_targets(spec_text: str) -> list[str]:
    lines = spec_text.splitlines()
    sec: list[str] = []
    in_sec = False
    for ln in lines:
        if re.match(r"^#{2,3}\s", ln):
            in_sec = bool(CHANGE_HEAD_RE.match(ln))
            continue
        if in_sec:
            sec.append(ln)
    found: list[str] = []
    for src in ("\n".join(sec), spec_text):
        for m in PATH_RE.finditer(src):
            p = m.group(1)
            if p not in found:
                found.append(p)
        if found:
            break
    return found


def main() -> int:
    if len(sys.argv) < 5:
        print("usage: enqueue_kernel.py <order_id> <spec_file> <title_txt> <max_attempts> [targets_csv]")
        return 2
    order, spec, title_txt, attempts = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    targets = [t.strip() for t in (sys.argv[5].split(",") if len(sys.argv) > 5 else []) if t.strip()]
    title = open(title_txt, encoding="utf-8").read().strip()
    if not targets:
        targets = detect_targets(open(spec, encoding="utf-8", errors="replace").read())
    if not targets:
        print("REJECTED: spec declares no target files (改动文件 section); pass targets_csv explicitly")
        return 2
    cmd = [sys.executable, "-X", "utf8", RUNNER, "enqueue",
           "--order", order, "--kind", "code",
           "--title", title, "--spec-file", spec,
           "--repo", REPO,
           "--targets", ",".join(targets),
           "--max-attempts", str(attempts)]
    print("TARGETS", ",".join(targets))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("RC", r.returncode)
    print((r.stdout or "")[-500:])
    if r.returncode != 0:
        print((r.stderr or "")[-500:])
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
