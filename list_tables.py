import sqlite3
con = sqlite3.connect('penalty_history.db')
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables:", tables)
for t in tables:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t}: {n} rows, cols={cols}")
con.close()
