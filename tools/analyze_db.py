"""Diagnostic de la base penalty_history.db : où le bot gagne / perd."""

import sqlite3
from pathlib import Path

# Base située à la racine du projet (ce script vit dans tools/).
DB = Path(__file__).resolve().parent.parent / "penalty_history.db"


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print("Tables :", tables)
    for t in tables:
        print(f"  {t}: {cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} lignes")

    print("\n=== Matchs terminés ===")
    row = cur.execute(
        "SELECT COUNT(*), AVG(total_goals), AVG(final_score1), AVG(final_score2), "
        "AVG(went_to_sudden_death) FROM matches_history WHERE total_goals IS NOT NULL"
    ).fetchone()
    print(f"n={row[0]}  buts moy={row[1]:.2f}  dom={row[2]:.2f}  ext={row[3]:.2f}  mort subite={row[4]*100:.1f}%")

    print("\n=== Distribution des scores (top 15) ===")
    for s1, s2, n in cur.execute(
        "SELECT final_score1, final_score2, COUNT(*) c FROM matches_history "
        "WHERE total_goals IS NOT NULL GROUP BY 1,2 ORDER BY c DESC LIMIT 15"
    ):
        print(f"  {s1}-{s2}: {n}")

    print("\n=== Distribution du total de buts ===")
    for t, n in cur.execute(
        "SELECT total_goals, COUNT(*) FROM matches_history WHERE total_goals IS NOT NULL GROUP BY 1 ORDER BY 1"
    ):
        print(f"  {t}: {n}")

    print("\n=== Tickets envoyés (sent_predictions) ===")
    for phase, status, n, avg_odds, avg_conf in cur.execute(
        "SELECT phase, prediction_status, COUNT(*), AVG(odds), AVG(confidence_pct) "
        "FROM sent_predictions WHERE delivery_status='SENT' GROUP BY 1,2"
    ):
        print(f"  {phase:9} {status:8} n={n:4} cote moy={avg_odds:.2f} confiance moy={avg_conf:.1f}%")
    rows = cur.execute(
        "SELECT odds, prediction_status FROM sent_predictions "
        "WHERE delivery_status='SENT' AND prediction_status IN ('WON','LOST')"
    ).fetchall()
    if rows:
        net = sum((o - 1) if s == "WON" else -1 for o, s in rows)
        wins = sum(s == "WON" for _, s in rows)
        print(f"  => {wins}/{len(rows)} gagnés ({wins/len(rows)*100:.1f}%), ROI = {net/len(rows)*100:+.1f}%")

    print("\n=== Tickets par marché ===")
    for label, n, w, roi in cur.execute(
        "SELECT market_key, COUNT(*), SUM(prediction_status='WON'), "
        "AVG(CASE WHEN prediction_status='WON' THEN odds-1 ELSE -1 END)*100 "
        "FROM sent_predictions WHERE delivery_status='SENT' AND prediction_status IN ('WON','LOST') "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
    ):
        print(f"  {label:35} n={n:4} gagnés={w:4} ROI={roi:+.1f}%")

    print("\n=== Signaux LIVE ===")
    for approved, outcome, n in cur.execute(
        "SELECT approved, outcome, COUNT(*) FROM live_signal_snapshots GROUP BY 1,2"
    ):
        print(f"  approuvé={approved} {outcome:8} n={n}")

    print("\n=== Scores exacts affichés ===")
    for phase, n, w, roi in cur.execute(
        "SELECT phase, COUNT(*), SUM(prediction_status='WON'), "
        "AVG(CASE WHEN prediction_status='WON' THEN odds-1 ELSE -1 END)*100 "
        "FROM exact_score_predictions WHERE prediction_status IN ('WON','LOST') GROUP BY 1"
    ):
        print(f"  {phase:9} n={n:5} gagnés={w:4} ROI={roi:+.1f}%")

    print("\n=== Marge bookmaker moyenne (1X2 / totaux prématch) ===")
    for gid, margin, n in cur.execute(
        "SELECT group_id, AVG(s)-1, COUNT(*) FROM ("
        "  SELECT game_id, group_id, threshold, SUM(1.0/prematch_odds) s FROM regulation_markets_history "
        "  WHERE prematch_odds > 1 AND group_id IN (1,17,15,62) AND is_extra_penalties=0 "
        "  GROUP BY game_id, group_id, threshold HAVING COUNT(*) BETWEEN 2 AND 3"
        ") GROUP BY group_id"
    ):
        print(f"  groupe {gid}: marge={margin*100:.1f}%  (n={n})")

    print("\n=== Marchés prématch au ROI le plus élevé (>= 30 paris) ===")
    data = cur.execute(
        "SELECT q.market_key, q.group_id, q.event_type, q.threshold, q.prematch_odds, "
        "m.final_score1, m.final_score2 FROM regulation_markets_history q "
        "JOIN matches_history m ON m.game_id=q.game_id "
        "WHERE m.total_goals IS NOT NULL AND q.prematch_odds > 1 AND q.is_extra_penalties=0"
    ).fetchall()
    stats = {}
    for key, gid, et, th, odds, s1, s2 in data:
        won = _settle(gid, et, th, s1, s2)
        if won is None:
            continue
        st = stats.setdefault(key, [0, 0, 0.0, 0.0])
        st[0] += 1
        st[1] += won
        st[2] += (odds - 1) if won else -1
        st[3] += odds
    ranked = sorted(
        ((k, v) for k, v in stats.items() if v[0] >= 30),
        key=lambda kv: kv[1][2] / kv[1][0],
        reverse=True,
    )
    for key, (n, w, net, so) in ranked[:15]:
        print(f"  {key:28} n={n:4} réussite={w/n*100:5.1f}% cote moy={so/n:5.2f} ROI={net/n*100:+6.1f}%")
    print("  ...")
    for key, (n, w, net, so) in ranked[-5:]:
        print(f"  {key:28} n={n:4} réussite={w/n*100:5.1f}% cote moy={so/n:5.2f} ROI={net/n*100:+6.1f}%")
    con.close()


def _settle(gid, et, th, s1, s2):
    if gid == 1:
        return {1: s1 > s2, 2: s1 == s2, 3: s2 > s1}.get(et)
    if gid == 8:
        return {4: s1 >= s2, 5: s1 != s2, 6: s2 >= s1}.get(et)
    if th is None:
        return None
    if gid == 2 and et in (7, 8):
        d = (s1 - s2 if et == 7 else s2 - s1) + th
        return None if d == 0 else d > 0
    value = {17: s1 + s2, 19: s1 + s2, 15: s1, 62: s2}.get(gid)
    if value is None or value == th:
        return None
    if et in (9, 11, 13, 11273):
        return value > th
    if et in (10, 12, 14, 11274):
        return value < th
    return None


if __name__ == "__main__":
    main()
