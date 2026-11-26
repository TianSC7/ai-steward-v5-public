# -*- coding: utf-8 -*-
"""restore_memory.py — 跨机恢复管家私人记忆（2026-11-18 自我迭代项①配套）。

流向：dll2 仓里的 `ai-steward/knowledge/private-memory/*.md`（sync_to_dll.py 镜像上来的）
→ 本机 ZCode 记忆目录 `~/.zcode/cli/memories/projects/<本项目>/memory/`。

用法（换机/重装后跑一次）：
  python -X utf8 restore_memory.py            # 默认目标=本项目 memory 目录
  python -X utf8 restore_memory.py --to <dir> # 显式指定目标

规则：只增改不删（铁律）；同名且内容相同跳过；MEMORY.md 索引以仓内版本为准覆盖
（它是唯一被允许覆盖同名文件的——索引必须与卡集一致）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

SRC = Path(__file__).resolve().parent.parent / "knowledge" / "private-memory"
DEFAULT_TO = Path(os.path.expandvars(
    r"%USERPROFILE%\.zcode\cli\memories\projects\mate-d508a223aa2ee880\memory"))


def copy_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".restore_")
    os.close(fd)
    try:
        shutil.copy2(str(src), tmp)
        os.replace(tmp, str(dst))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", type=Path, default=DEFAULT_TO)
    a = ap.parse_args()
    if not SRC.is_dir():
        print("ERROR 仓内镜像不存在: %s（先跑 sync_to_dll.py 或 git pull）" % SRC, file=sys.stderr)
        return 3
    copied = kept = 0
    for card in sorted(SRC.glob("*.md")):
        target = a.to / card.name
        if target.exists() and target.read_bytes() == card.read_bytes():
            kept += 1
            continue
        copy_atomic(card, target)
        copied += 1
    print("restored=%d uptodate=%d -> %s" % (copied, kept, a.to))
    print("注意：恢复后跑 memory_vec.py index 重建向量索引")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
