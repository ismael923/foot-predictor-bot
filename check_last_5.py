import sqlite3

con = sqlite3.connect('file:penalty_history.db?mode=ro', uri=True)
gids = ['771365580', '771366349', '771367040', '771367773', '771368397']
print("RESULTATS EXACTS DES 5 MATCHS :")
for gid in gids:
    m = con.execute('SELECT game_id, team1, team2, status, final_score1, final_score2, total_goals FROM matches_history WHERE game_id=?', (gid,)).fetchone()
    p = con.execute('SELECT phase, market_label, odds, prediction_status FROM sent_predictions WHERE game_id=?', (gid,)).fetchall()
    if m:
        score_str = f"{m[4]}-{m[5]} (Total {m[6]})" if m[4] is not None else "En cours"
        print(f"\nMatch #{gid} : {m[1]} vs {m[2]} -> Score: {score_str} [{m[3]}]")
    for item in p:
        status_flag = "✅ GAGNÉ" if item[3] == "WON" else ("❌ PERDU" if item[3] == "LOST" else "⏳ EN COURS")
        print(f"   -> Pari : {item[1]} (@{item[2]}) | {status_flag}")

con.close()
