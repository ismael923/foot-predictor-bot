"""Analyse la structure réelle du jeu FC24 Penalty depuis la DB."""
import sqlite3

con = sqlite3.connect('penalty_history.db')

print("=" * 70)
print("DISTRIBUTION DES SCORES FINAUX (hors mort subite)")
print("= Combien de buts max par équipe en temps réglementaire ?")
print("=" * 70)

rows = con.execute("""
    SELECT final_score1, final_score2, went_to_sudden_death, COUNT(*) as n
    FROM matches_history
    WHERE final_score1 IS NOT NULL AND final_score2 IS NOT NULL
      AND went_to_sudden_death = 0
    GROUP BY final_score1, final_score2
    ORDER BY final_score1 DESC, final_score2 DESC
    LIMIT 30
""").fetchall()

print(f"{'Score':<12} {'Mort subite':>12} {'N':>5}")
print("-" * 32)
for r in rows:
    print(f"  {r[0]}-{r[1]:<10} {'Non':>12} {r[3]:>5}")

print("\n" + "=" * 70)
print("SCORE MAX PAR ÉQUIPE (tous matchs terminés)")
print("=" * 70)
row = con.execute("""
    SELECT MAX(final_score1), MAX(final_score2), MAX(total_goals)
    FROM matches_history
    WHERE final_score1 IS NOT NULL
""").fetchone()
print(f"  Max score équipe 1 : {row[0]}")
print(f"  Max score équipe 2 : {row[1]}")
print(f"  Max total buts     : {row[2]}")

print("\n" + "=" * 70)
print("DISTRIBUTION DU TOTAL DE BUTS (tous matchs)")
print("=" * 70)
rows2 = con.execute("""
    SELECT total_goals, COUNT(*) as n
    FROM matches_history
    WHERE total_goals IS NOT NULL
    GROUP BY total_goals
    ORDER BY total_goals ASC
""").fetchall()
total_all = sum(r[1] for r in rows2)
for r in rows2:
    bar = "█" * int(r[1]/total_all*50)
    print(f"  {r[0]:>3} buts : {r[1]:>4} matchs ({r[1]/total_all*100:>5.1f}%) {bar}")

print("\n" + "=" * 70)
print("MATCHS AVEC MORT SUBITE : score réglementaire ?")
print("= (ce qu'on voit AVANT la mort subite)")
print("=" * 70)
rows3 = con.execute("""
    SELECT final_score1, final_score2, total_goals, COUNT(*) as n
    FROM matches_history
    WHERE went_to_sudden_death = 1
      AND final_score1 IS NOT NULL
    GROUP BY final_score1, final_score2, total_goals
    ORDER BY n DESC
    LIMIT 15
""").fetchall()
for r in rows3:
    print(f"  Score final: {r[0]}-{r[1]}  total={r[2]}  → {r[3]} fois")

con.close()
