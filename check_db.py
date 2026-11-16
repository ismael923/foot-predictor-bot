import sqlite3

con = sqlite3.connect('penalty_history.db')

# Vérifier les matchs avec prédictions VOID
print("=== MATCHS avec prédictions VOID ===")
rows = con.execute("""
    SELECT sp.game_id, sp.market_key, sp.event_type, sp.prediction_status,
           sp.final_score1, sp.final_score2,
           mh.final_score1 as mh_score1, mh.final_score2 as mh_score2,
           mh.went_to_sudden_death, mh.total_goals
    FROM sent_predictions sp
    LEFT JOIN matches_history mh ON sp.game_id = mh.game_id
    WHERE sp.prediction_status = 'VOID'
    ORDER BY sp.id DESC
    LIMIT 15
""").fetchall()
for r in rows:
    print(r)

print()
print("=== MATCHS avec prédictions WON/LOST ===")
rows2 = con.execute("""
    SELECT sp.game_id, sp.market_key, sp.event_type, sp.prediction_status,
           sp.final_score1, sp.final_score2,
           mh.final_score1, mh.final_score2, mh.went_to_sudden_death
    FROM sent_predictions sp
    LEFT JOIN matches_history mh ON sp.game_id = mh.game_id
    WHERE sp.prediction_status IN ('WON', 'LOST')
    ORDER BY sp.id DESC
    LIMIT 10
""").fetchall()
for r in rows2:
    print(r)

con.close()
