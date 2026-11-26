import os
import sqlite3

p = r'C:\Users\Administrator\.zcode\cli\db\db.sqlite'
c = sqlite3.connect(f'file:///{p}?mode=ro', uri=True)
tabs = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
print('TABLES:', tabs)
for t in tabs:
    low = t.lower()
    if 'project' in low or 'session' in low or 'workspace' in low:
        cols = [d[1] for d in c.execute(f'PRAGMA table_info({t})')]
        print('--', t, cols)
        try:
            for r in c.execute(f'select * from {t} limit 3'):
                print('   ', str(r)[:260])
        except Exception as e:
            print('   err', e)
mem = r'C:\Users\Administrator\.zcode\cli\memories'
print('MEMDIR:', os.listdir(mem) if os.path.isdir(mem) else 'NONE')
