# AA 路径适配第二轮（2026-11-18 16:10，管家在 AA 实测后发现第一轮未生效）
# 第一轮 _aa_fix_paths.py 的 FILES 列表覆盖了 config/steward.json 等，但实测 26 处 TIAN
# 仍在（疑跑在误解压出的 mate/ai-steward 副本上，该副本已移 _trash）。本轮：
# 1) 每个文件先备份 .bak-20261118aa（不删任何东西）；
# 2) 只替换路径前缀 C:/Users/TIAN -> C:/Users/Administrator（含无尾斜杠形态与反斜杠形态），
#    散文里的 "TIAN"（机名/叙事）不动，保住历史记录；
# 3) 显式排除：_aa_*.py（适配脚本自身，OLD 常量不能被换）、state/registry.json、
#    state/worktrees.json（已完成 worktree 的历史记录）、reports/（证据历史）、
#    knowledge/（记忆镜像历史）、state/HANDOVER*.md（交接史料）。
import os

ROOT = r'C:\Users\Administrator\Documents\mate\plugins\ai-steward'
BAK = '.bak-20261118aa'
PAIRS = [
    ('C:/Users/TIAN', 'C:/Users/Administrator'),
    ('C:\\Users\\TIAN', 'C:\\Users\\Administrator'),
]
EXCLUDE_DIRS = {'__pycache__', 'reports', 'knowledge', '.git'}
EXCLUDE_FILES = {
    '_aa_fix_paths.py', '_aa_paths_round2.py', '_aa_probe.py',
    'registry.json', 'worktrees.json', 'HANDOVER.md', 'HANDOVER_AA_20261118.md',
}
EXCLUDE_SUFFIX = ('.bak-20261118aa', '.bak', '.pyc')

changed, skipped = [], []
for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    for f in files:
        if f in EXCLUDE_FILES or f.endswith(EXCLUDE_SUFFIX):
            continue
        p = os.path.join(dirpath, f)
        if not os.path.isfile(p) or os.path.islink(p):
            continue
        try:
            with open(p, encoding='utf-8') as fh:
                orig = fh.read()
        except (UnicodeDecodeError, OSError):
            skipped.append(os.path.relpath(p, ROOT))
            continue
        s, n = orig, 0
        for old, new in PAIRS:
            n += s.count(old)
            s = s.replace(old, new)
        if n:
            bak = p + BAK
            if not os.path.exists(bak):
                with open(bak, 'w', encoding='utf-8', newline='') as fh:
                    fh.write(orig)
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8', newline='') as fh:
                fh.write(s)
            os.replace(tmp, p)
            changed.append((os.path.relpath(p, ROOT), n))

for rel, n in sorted(changed):
    print('fixed', rel, n)
print('---')
print('changed files:', len(changed), '| skipped(binary/unreadable):', len(skipped))
for rel in skipped:
    print('  skip', rel)
