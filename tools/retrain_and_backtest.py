"""Ré-apprend ai_memory.json avec le nouveau moteur puis backteste les seuils."""

import sqlite3
import sys
from pathlib import Path

# Permet d'importer my_ai_brain depuis la racine du projet (script dans tools/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from my_ai_brain import TerminatAI  # noqa: E402

DB = PROJECT_ROOT / "penalty_history.db"


def retrain():
    brain = TerminatAI()
    brain.maybe_train(force=True)
    memory = brain.memory
    print("=== Nouveau ai_memory.json ===")
    print("Matchs appris      :", memory.get("trained_matches"))
    print("Buts moyens (global): %.2f" % (memory["overall"].get("average_goals") or 0))
    print("Victoires dom/ext   : %.2f / %.2f" % (
        memory["overall"].get("average_score_team1") or 0,
        memory["overall"].get("average_score_team2") or 0,
    ))
    verdicts = {}
    for entry in memory.get("market_profitability", {}).values():
        verdicts[entry["verdict"]] = verdicts.get(entry["verdict"], 0) + 1
    print("Marchés classés     :", verdicts)

    proven = [
        (k, v)
        for k, v in memory.get("market_profitability", {}).items()
        if v["verdict"] == "PROVEN"
    ]
    proven.sort(key=lambda kv: kv[1]["roi_pct"], reverse=True)
    print("\n=== Marchés PROVEN (rentabilité statistiquement prouvée) ===")
    for key, entry in proven[:12]:
        print(
            f"  {key:24} n={entry['bets']:4d} réussite={entry['actual_rate']*100:5.1f}% "
            f"seuil={entry['break_even_rate']*100:5.1f}% "
            f"borne IC95%={entry['wilson_lower_rate']*100:5.1f}% ROI={entry['roi_pct']:+6.1f}%"
        )
    avoid = [
        (k, v)
        for k, v in memory.get("market_profitability", {}).items()
        if v["verdict"] == "AVOID"
    ]
    avoid.sort(key=lambda kv: kv[1]["roi_pct"])
    print("\n=== Marchés AVOID (perte structurelle, désormais bloqués) ===")
    for key, entry in avoid[:12]:
        print(f"  {key:24} n={entry['bets']:4d} ROI={entry['roi_pct']:+7.1f}%")
    return memory


def backtest(memory):
    """Simule la stratégie PROVEN-only sur l'historique prématch."""
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT q.game_id, q.market_key, q.prematch_odds, m.final_score1, m.final_score2 "
        "FROM regulation_markets_history q JOIN matches_history m ON m.game_id=q.game_id "
        "WHERE m.total_goals IS NOT NULL AND q.prematch_odds > 1 "
        "AND q.is_extra_penalties=0 AND q.event_type NOT IN (4,5,6)"
    ).fetchall()
    con.close()

    profitability = memory.get("market_profitability", {})
    settled = avoided = won = 0
    net = 0.0
    for _gid, key, odds, s1, s2 in rows:
        entry = profitability.get(key)
        if not entry:
            continue
        if entry["verdict"] == "AVOID":
            avoided += 1
            continue
        if entry["verdict"] != "PROVEN":
            continue
        result = _settle(key, s1, s2)
        if result is None:
            continue
        settled += 1
        won += int(result)
        net += (odds - 1) if result else -1

    print("\n=== Backtest : stratégie PROVEN uniquement ===")
    print(f"Paris bloqués (AVOID) : {avoided}")
    print(f"Paris joués           : {settled}")
    if settled:
        print(
            f"Réussite              : {won}/{settled} ({won/settled*100:.1f}%)"
        )
        print(f"ROI simulé            : {net/settled*100:+.2f}%  ({net:+.1f} unités)")


def _settle(market_key, s1, s2):
    parts = market_key.split(":")
    if not parts:
        return None
    head = parts[0]
    try:
        if head == "winner":
            return {1: s1 > s2, 2: s1 == s2, 3: s2 > s1}.get(int(parts[1]))
        if head == "extra-penalties":
            return None  # dépend de la mort subite, non reconstituable ici
        if head == "handicap":
            event_type, line = int(parts[1]), float(parts[2])
            margin = (s1 - s2 if event_type == 7 else s2 - s1) + line
            return None if margin == 0 else margin > 0
        if head == "total":
            event_type, line = int(parts[1]), float(parts[2])
            value = s1 + s2
            if value == line:
                return None
            return value > line if event_type in (9, 11273) else value < line
        if head == "team-total":
            team, event_type, line = int(parts[1]), int(parts[2]), float(parts[3])
            value = s1 if team == 1 else s2
            if value == line:
                return None
            return value > line if event_type in (11, 13) else value < line
    except (ValueError, IndexError):
        return None
    return None


if __name__ == "__main__":
    memory = retrain()
    backtest(memory)
    print("\nai_memory.json mis à jour.")
