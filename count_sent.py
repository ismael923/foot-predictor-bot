import sqlite3

con = sqlite3.connect('penalty_history.db')
total = con.execute("SELECT COUNT(*) FROM sent_predictions WHERE delivery_status='SENT'").fetchone()[0]
rows = con.execute("""
    SELECT prediction_status, COUNT(*) 
    FROM sent_predictions 
    WHERE delivery_status='SENT' 
    GROUP BY prediction_status
""").fetchall()

print("TOTAL_SENT =", total)
for r in rows:
    print(" ", r[0], ":", r[1])

print("\n--- 10 DERNIÈRES PRÉDICTIONS ENVOYÉES ---")
recent = con.execute("""
    SELECT id, game_id, phase, market_label, odds, confidence_pct, prediction_status, sent_at 
    FROM sent_predictions 
    WHERE delivery_status='SENT' 
    ORDER BY id DESC LIMIT 10
""").fetchall()
for r in recent:
    print(f"#{r[0]} | Match {r[1]} | {r[2]} | {r[3]} | @{r[4]} | Conf:{r[5]}% | Statut:{r[6]} | Date:{r[7]}")

con.close()
