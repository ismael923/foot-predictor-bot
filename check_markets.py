"""Même ligne 7.5 : pourquoi deux ROI opposés ?"""

import sqlite3
from pathlib import Path

con = sqlite3.connect(Path("penalty_history.db"))

for key in ("total:9:7.5", "total:11273:4"):
    print(f"=== {key} ===")
    for status, n, avg, mn, mx in con.execute(
        "SELECT status, COUNT(1), AVG(prematch_odds), MIN(prematch_odds), "
        "MAX(prematch_odds) FROM regulation_markets_history "
        "WHERE market_key=? AND prematch_odds>1 GROUP BY status",
        (key,),
    ):
        print(f"  status={status:10} n={n:3d} moy={avg:.3f} [{mn:.2f} ; {mx:.2f}]")

    # Taux de réussite réel sur les matchs réglés (score réglementaire)
    rows = con.execute(
        "SELECT m.final_score1, m.final_score2, m.went_to_sudden_death "
        "FROM regulation_markets_history q JOIN matches_history m "
        "ON m.game_id=q.game_id WHERE q.market_key=? AND m.total_goals IS NOT NULL",
        (key,),
    ).fetchall()
    over = 0
    total_settled = 0
    for s1, s2, sudden in rows:
        if s1 is None or s2 is None:
            continue
        goal_total = 10 if sudden else s1 + s2
        total_settled += 1
        if goal_total > 7.5:
            over += 1
    if total_settled:
        print(f"  matchs reglés={total_settled}  'Plus de 7.5' gagne={over} "
              f"({over/total_settled*100:.1f}%)")
    print()

print("=== COMBIEN DE MATCHS ONT LES DEUX CLES ? ===")
n = con.execute(
    "SELECT COUNT(DISTINCT a.game_id) FROM regulation_markets_history a "
    "JOIN regulation_markets_history b ON b.game_id=a.game_id "
    "WHERE a.market_key='total:9:7.5' AND b.market_key='total:11273:4'"
).fetchone()[0]
print(f"  {n} matchs ont les deux")

print("\n=== SUR CES MATCHS, LES COTES SONT-ELLES EGALES ? ===")
for gid, o1, o2 in con.execute(
    "SELECT a.game_id, a.prematch_odds, b.prematch_odds "
    "FROM regulation_markets_history a "
    "JOIN regulation_markets_history b ON b.game_id=a.game_id "
    "WHERE a.market_key='total:9:7.5' AND b.market_key='total:11273:4' "
    "AND a.prematch_odds>1 AND b.prematch_odds>1 LIMIT 10"
):
    same = "IDENTIQUE" if abs(o1 - o2) < 0.005 else f"DIFFERENT ({o1:.2f} vs {o2:.2f})"
    print(f"  {gid}: {same}")
con.close()
