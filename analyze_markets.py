"""Analyse complète de tous les marchés envoyés (sent_predictions)."""
import sqlite3

con = sqlite3.connect('penalty_history.db')

print("=" * 90)
print("TOUS LES MARCHÉS ENVOYÉS (sent_predictions - résultats réglés)")
print("=" * 90)
print(f"{'Marché':<55} {'N':>4} {'Wins':>5} {'WR%':>6} {'ROI%':>7}")
print("-" * 90)

rows = con.execute("""
    SELECT market_key,
           COUNT(*) as total,
           SUM(CASE WHEN prediction_status='WON' THEN 1 ELSE 0 END) as wins,
           ROUND(100.0*SUM(CASE WHEN prediction_status='WON' THEN 1 ELSE 0 END)/COUNT(*), 1) as wr,
           ROUND(AVG(CASE WHEN prediction_status='WON' THEN odds-1 WHEN prediction_status='LOST' THEN -1 ELSE 0 END)*100, 1) as roi
    FROM sent_predictions
    WHERE prediction_status IN ('WON','LOST')
    GROUP BY market_key
    HAVING total >= 3
    ORDER BY roi DESC
""").fetchall()

for r in rows:
    flag = "✅" if r[4] > 0 else "❌"
    print(f"{flag} {r[0]:<53} {r[1]:>4} {r[2]:>5} {r[3]:>6}% {r[4]:>7}%")

print(f"\nTotal marchés: {len(rows)}")

# Stats live signals
print("\n" + "=" * 90)
print("LIVE SIGNALS - Résultats par marché (live_signal_snapshots approved=1)")
print("=" * 90)
rows2 = con.execute("""
    SELECT market_key,
           COUNT(*) as total,
           SUM(CASE WHEN outcome='WON' THEN 1 ELSE 0 END) as wins,
           ROUND(100.0*SUM(CASE WHEN outcome='WON' THEN 1 ELSE 0 END)/COUNT(*), 1) as wr,
           ROUND(AVG(CASE WHEN outcome='WON' THEN odds_at_trigger-1 WHEN outcome='LOST' THEN -1 ELSE 0 END)*100, 1) as roi
    FROM live_signal_snapshots
    WHERE outcome IN ('WON','LOST')
    GROUP BY market_key
    HAVING total >= 3
    ORDER BY roi DESC
""").fetchall()

print(f"{'Marché':<55} {'N':>4} {'Wins':>5} {'WR%':>6} {'ROI%':>7}")
print("-" * 90)
for r in rows2:
    flag = "✅" if r[4] > 0 else "❌"
    print(f"{flag} {r[0]:<53} {r[1]:>4} {r[2]:>5} {r[3]:>6}% {r[4]:>7}%")

# Quels marchés disponibles per match (regulation_markets_history)
print("\n" + "=" * 90)
print("MARCHÉS DISPONIBLES par match (regulation_markets_history - top 30)")
print("=" * 90)
rows3 = con.execute("""
    SELECT market_key, COUNT(DISTINCT game_id) as n_matchs
    FROM regulation_markets_history
    GROUP BY market_key
    ORDER BY n_matchs DESC
    LIMIT 30
""").fetchall()
for r in rows3:
    print(f"  {r[0]:<55} {r[1]:>4} matchs")

con.close()
