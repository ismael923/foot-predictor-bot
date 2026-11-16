import sqlite3

con = sqlite3.connect('penalty_history.db')

for gid in [14, 776, 2270, 2866, 2867, 11146]:
    print(f"\n{'='*50}\nGROUP {gid}\n{'='*50}")
    rows = con.execute("""
        SELECT event_type, group_label, label, outcome_label,
               COUNT(DISTINCT game_id) as matchs,
               AVG(prematch_odds) as avg_odds,
               threshold
        FROM regulation_markets_history
        WHERE group_id = ?
        GROUP BY event_type
    """, (gid,)).fetchall()
    for r in rows:
        odds = f"{r[5]:.2f}" if r[5] is not None else "None"
        print(f"  event={r[0]} | label='{r[2]}' | outcome='{r[3]}' | n={r[4]} | odds={odds} | thresh={r[6]}")

# Regardons dans les matchs finis la correspondance de group 2270
print("\n" + "="*50 + "\nGROUP 2270 vs BUTS REELS (Parité ?)\n" + "="*50)
rows = con.execute("""
    SELECT r.event_type, r.label, m.total_goals, (m.total_goals % 2), COUNT(*)
    FROM regulation_markets_history r
    JOIN matches_history m ON m.game_id = r.game_id
    WHERE r.group_id = 2270 AND m.total_goals IS NOT NULL
    GROUP BY r.event_type, (m.total_goals % 2)
""").fetchall()
for r in rows:
    parite = "PAIR (0)" if r[3] == 0 else "IMPAIR (1)"
    print(f"  event={r[0]} ({r[1]}) -> Matchs {parite} : {r[4]} fois")

# Regardons group 14 vs les scores
print("\n" + "="*50 + "\nGROUP 14 vs Les deux marquent ?\n" + "="*50)
rows = con.execute("""
    SELECT r.event_type, r.label, (m.final_score1 > 0 AND m.final_score2 > 0) as btts, COUNT(*)
    FROM regulation_markets_history r
    JOIN matches_history m ON m.game_id = r.game_id
    WHERE r.group_id = 14 AND m.final_score1 IS NOT NULL
    GROUP BY r.event_type, btts
""").fetchall()
for r in rows:
    btts_str = "LES 2 ONT MARQUÉ" if r[2] else "UN A 0 BUT"
    print(f"  event={r[0]} ({r[1]}) -> {btts_str} : {r[3]} fois")

con.close()
