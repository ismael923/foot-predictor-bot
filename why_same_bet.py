"""Analyse quels group_ids/event_types sont collectés et lesquels sont absents."""
import sqlite3

con = sqlite3.connect('penalty_history.db')

print("=" * 80)
print("TOUS LES GROUP_IDs COLLECTÉS DANS LA DB")
print("=" * 80)
rows = con.execute("""
    SELECT group_id, event_type, group_label, label, COUNT(DISTINCT game_id) as matchs,
           ROUND(AVG(prematch_odds),3) as avg_odds
    FROM regulation_markets_history
    GROUP BY group_id, event_type
    ORDER BY group_id, event_type
""").fetchall()

current_group = None
for r in rows:
    if r[0] != current_group:
        current_group = r[0]
        print(f"\n--- Group {r[0]} : {r[2]} ---")
    print(f"  event_type={r[1]:>6}  matchs={r[4]:>4}  cote_moy={r[5]:.3f}  ex: {r[3]}")

print("\n" + "=" * 80)
print("MARCHÉS JAMAIS COLLECTÉS (présents sur 1xBet mais absents de notre DB)")
print("= Ces marchés existent dans le jeu mais le bot ne les analyse pas !")
print("=" * 80)
print("""
  Marchés identifiés par l'utilisateur mais ABSENTS de la DB :
  - Total pair / impair (even/odd)
  - Premier pénalty manqué (yes/no)
  - Équipe 1 va marquer ses pénalties (6 options)
  - Équipe 2 va marquer ses pénalties (6 options)
  - Équipe 1 gagne sans prendre de but
  - Équipe 2 gagne sans prendre de but
""")

print("=" * 80)
print("POURQUOI LE BOT CHOISIT TOUJOURS 'Pénalités supplémentaires: Non' ?")
print("=" * 80)

# Vérifier la profitabilité de chaque marché collecté
print("\nMarchés pré-match avec ROI > 0 (qualifiés pour envoi) :")
rows2 = con.execute("""
    SELECT market_key, COUNT(*) as n, 
           SUM(CASE WHEN prediction_status='WON' THEN 1 ELSE 0 END) as wins,
           ROUND(AVG(CASE WHEN prediction_status='WON' THEN odds-1 
                         WHEN prediction_status='LOST' THEN -1 ELSE 0 END)*100,1) as roi,
           GROUP_CONCAT(DISTINCT phase) as phases
    FROM sent_predictions
    WHERE prediction_status IN ('WON','LOST')
    GROUP BY market_key
    ORDER BY n DESC
""").fetchall()

print(f"{'Marché':<50} {'N':>4} {'W':>4} {'ROI':>7} {'Phases'}")
print("-" * 80)
for r in rows2:
    flag = "✅" if r[3] > 0 else "❌"
    print(f"{flag} {r[0]:<48} {r[1]:>4} {r[2]:>4} {r[3]:>6}%  {r[4]}")

# Résumé des types de marchés non exploités
print("\n" + "=" * 80)
print("RÉSUMÉ - Diversité des group_ids dans les prédictions envoyées")
print("=" * 80)
rows3 = con.execute("""
    SELECT group_id, COUNT(*) as n, GROUP_CONCAT(DISTINCT market_key) as keys
    FROM sent_predictions
    GROUP BY group_id
    ORDER BY n DESC
""").fetchall()
for r in rows3:
    print(f"  group_id={r[0]:>6}  envois={r[1]:>4}  → {r[2][:70]}")

con.close()
