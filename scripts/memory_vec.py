# -*- coding: utf-8 -*-
"""管家私人记忆的向量索引入口（2026-11-18 用户令："你自己的也要用那个向量引擎来管"）。

复用 dll/library 的 Qwen 向量引擎（memory.py --root 任意库根），索引对象是 ZCode
私人记忆卡。卡片真身绝不移动；每次 index 前先把 memory/*.md 镜像到 veckb/projects/steward/
（引擎 reindex 只扫根下 tasks/projects/shared 三目录，且 resolve() 会穿 junction，
故用"先镜像再索引"，镜像与索引同轮完成不会漂）。

用法：
  python -X utf8 memory_vec.py index              # 镜像+建/增量索引（写完记忆卡跑一次）
  python -X utf8 memory_vec.py search "用户红线"   # 语义检索私人卡
  python -X utf8 memory_vec.py status             # 引擎侧索引状态
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

ENGINE = Path(r"C:/Users/Administrator/Documents/mate/dll/library/memory.py")
MEMDIR = Path.home() / ".zcode/cli/memories/projects/mate-d508a223aa2ee880/memory"
VECROOT = MEMDIR.parent / "veckb"
MIRROR = VECROOT / "projects/steward"

CONFIG = {
    "endpoint": "https://api.siliconflow.cn/v1/embeddings",
    "model": "Qwen/Qwen3-VL-Embedding-8B",
    "credential_file": "${USERPROFILE}/.codex/secrets/siliconflow.dpapi",
    "timeout_seconds": 25,
    "chunk_characters": 1800,
    "chunk_overlap": 160,
    "projects": {"steward": [str(MEMDIR)]},
}


def engine(*args: str) -> int:
    r = subprocess.run([sys.executable, "-X", "utf8", str(ENGINE), "--root", str(VECROOT), *args])
    return r.returncode


def mirror() -> int:
    MIRROR.mkdir(parents=True, exist_ok=True)
    want = {p.name for p in MEMDIR.glob("*.md")}
    have = {p.name for p in MIRROR.glob("*.md")}
    for stale in have - want:
        (MIRROR / stale).unlink()
    copied = 0
    for name in sorted(want):
        src, dst = MEMDIR / name, MIRROR / name
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            shutil.copyfile(src, dst)
            copied += 1
    return copied


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["index", "search", "status"])
    ap.add_argument("query", nargs="*", help="search 词（空格分隔拼接）")
    ap.add_argument("--limit", type=int, default=5)
    a = ap.parse_args()

    if not (VECROOT / "config.json").exists():
        VECROOT.mkdir(parents=True, exist_ok=True)
        tmp = VECROOT / "config.json.tmp"
        tmp.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(VECROOT / "config.json")

    if a.cmd == "index":
        n = mirror()
        print("mirror: %d 张卡更新" % n)
        return engine("index")
    if a.cmd == "status":
        return engine("status")
    return engine("search", " ".join(a.query), "--all", "--limit", str(a.limit))


if __name__ == "__main__":
    raise SystemExit(main())
