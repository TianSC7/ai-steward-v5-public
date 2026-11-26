import os

ROOT = r'C:\Users\Administrator\Documents\mate\plugins\ai-steward'
FILES = [
    'config/steward.json',
    'config/routing.json',
    'state/registry.json',
    'state/worktrees.json',
    'scripts/test_cycle.py',
    'scripts/permission_allow.py',
    'scripts/memory_vec.py',
    'scripts/mate_health.py',
    'scripts/validate_plugin.py',
    'scripts/test_board.py',
]
OLD = 'C:/Users/TIAN/'
NEW = 'C:/Users/Administrator/'

for rel in FILES:
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        print('skip', rel)
        continue
    with open(p, encoding='utf-8') as f:
        s = f.read()
    n = s.count(OLD)
    if n:
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            f.write(s.replace(OLD, NEW))
        os.replace(tmp, p)
    print(rel, 'replaced', n)

# 确保 worktree 根与常用目录存在
for d in (r'C:\Users\Administrator\worktrees\ai-steward',):
    os.makedirs(d, exist_ok=True)
    print('ensured', d)
