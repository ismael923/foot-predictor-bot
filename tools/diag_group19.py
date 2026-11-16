"""Diagnostic : le marché groupe 19 (11273/11274) vs les vraies lignes du groupe 17."""
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

con = sqlite3.connect(Path(__file__).resolve().parent.parent / "penalty_history.db")

print("=== Seuils bruts du groupe 19 ===")
for et, th, n, avg in con.execute(
    "SELECT event_type, threshold, COUNT(*), AVG(prematch_odds) FROM regulation_markets_history "
    "WHERE group_id=19 AND event_type IN (11273,11274) GROUP BY event_type, threshold"
):
    print(f"  type={et} seuil={th} n={n} cote_moy={avg}")

print("\n=== Pour chaque cote g19, quelle ligne g17 a la MEME cote dans le même match ? ===")
matches = defaultdict(Counter)
rows = con.execute(
    "SELECT a.event_type, a.threshold, b.event_type, b.threshold "
    "FROM regulation_markets_history a JOIN regulation_markets_history b "
    "ON a.game_id=b.game_id AND b.group_id=17 AND ABS(a.odds-b.odds)<0.0005 "
    "WHERE a.group_id=19 AND a.event_type IN (11273,11274)"
).fetchall()
for et19, th19, et17, th17 in rows:
    matches[(et19, th19)][(et17, th17)] += 1
for key, counter in sorted(matches.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
    print(f"  g19 {key} -> {counter.most_common(3)}")

print("\n=== Tickets envoyés par marché ===")
for key, label, n, won, lost in con.execute(
    "SELECT market_key, market_label, COUNT(*), SUM(prediction_status='WON'), "
    "SUM(prediction_status='LOST') FROM sent_predictions GROUP BY market_key ORDER BY 3 DESC LIMIT 10"
):
    print(f"  {key:30} {label:25} n={n} gagnés={won} perdus={lost}")
con.close()
