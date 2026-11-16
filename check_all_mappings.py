"""Vérification exacte du mapping et calcul de chaque groupe de marchés."""
import sqlite3

con = sqlite3.connect('penalty_history.db')

# 1. Group 14: Deux équipes vont marquer
# event_type 182 vs 183. Regardons les cotes: 182 avg 4.66, 183 avg 1.14
# Donc 182 = NON (@4.66), 183 = OUI (@1.14)
print("--- Group 14 ---")
for r in con.execute("SELECT event_type, AVG(prematch_odds) FROM regulation_markets_history WHERE group_id=14 GROUP BY event_type"):
    print(r)

# 2. Group 2270: Total pair / impair
# event_type 2511 vs 2512. 2511 avg 2.69, 2512 avg 1.39
# 387 matchs impairs (82%) vs 85 pairs (18%)
# Probabilité implicite: 1/1.39 = 72% -> 2512 = TOTAL IMPAIR. 1/2.69 = 37% -> 2511 = TOTAL PAIR.
print("--- Group 2270 ---")
for r in con.execute("SELECT event_type, AVG(prematch_odds) FROM regulation_markets_history WHERE group_id=2270 GROUP BY event_type"):
    print(r)

# 3. Group 2866 & 2867: Équipe gagne sans prendre de but
# 2866: event 200 avg 30.0, event 205 avg 1.00
# 2867: event 195 avg 29.86, event 210 avg 1.00
print("--- Group 2866 & 2867 ---")
for r in con.execute("SELECT group_id, event_type, AVG(prematch_odds) FROM regulation_markets_history WHERE group_id IN (2866, 2867) GROUP BY group_id, event_type"):
    print(r)

# 4. Group 11146: Premier pénalty manqué
# event 15627 avg 1.34, event 15628 avg 2.91
print("--- Group 11146 ---")
for r in con.execute("SELECT event_type, AVG(prematch_odds) FROM regulation_markets_history WHERE group_id=11146 GROUP BY event_type"):
    print(r)

# 5. Group 776: Équipe marque ses penalties
print("--- Group 776 ---")
for r in con.execute("SELECT event_type, threshold, AVG(prematch_odds) FROM regulation_markets_history WHERE group_id=776 GROUP BY event_type"):
    print(r)

con.close()
