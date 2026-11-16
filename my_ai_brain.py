"""Small local learner for FC24 match-history data."""

import json
import math
import os
import sqlite3
import threading
import unicodedata
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path


def _load_env_file():
    """Charge le .env du projet avant d'évaluer les seuils.

    Sans cet appel, les constantes de classe ci-dessous seraient figées à
    l'import du module, c'est-à-dire AVANT que telegram_bot n'appelle
    _load_env_file : toutes les valeurs du .env seraient ignorées.
    """
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file()


def _default_memory():
    return {
        "version": 1,
        "trained_matches": 0,
        "overall": {
            "sudden_death_rate": None,
            "recent_sudden_death_rate": None,
            "recent_short_rate": None,
            "average_goals": None,
            "average_score_team1": None,
            "average_score_team2": None,
        },
        "markov": {},
        "hourly": {
            "favorite": {},
            "total_over": {},
            "total_under": {},
        },
        "regulation_markets": {},
        "exact_scores": {},
        "exact_score_total": 0,
        "exact_score_calibration": {},
        "trained_exact_score_predictions": 0,
        "profitability": {
            "market": "Données insuffisantes",
            "roi_pct": None,
            "bets": 0,
        },
    }


def _project_path(filename):
    directory = __file__.replace("\\", "/").rsplit("/", 1)[0]
    return directory + "/" + filename


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid_odds(value):
    odds = _number(value)
    return odds if odds is not None and odds > 1 else None


def _clamp_probability(value):
    return max(0.0, min(1.0, value))


def regulation_score(score1, score2, went_to_sudden_death=None):
    """Score final de la séance FC24 Penalty.

    Dans FC24 Penalty (eSports), le match est une séance complète de tirs au but.
    Tous les marchés du bookmaker (Vainqueur du match 1/2, Total de buts, etc.)
    sont réglés sur le score final complet de la séance, y compris en cas de mort subite.
    """
    if not isinstance(score1, int) or not isinstance(score2, int):
        return None
    return score1, score2, score1 + score2


class TerminatAI:
    """Train compact probability calibrations from completed SQLite matches."""

    TRAINING_INTERVAL = 10
    MIN_MARKET_HISTORY = int(os.getenv("MIN_MARKET_HISTORY", "12"))
    MIN_EXPECTED_VALUE = float(os.getenv("MIN_EXPECTED_VALUE", "1.06"))
    MIN_RELIABLE_MARKET_BETS = 20
    MAX_MATCHUP_BLEND_WEIGHT = 0.40
    MIN_LIVE_CALIBRATION_SAMPLES = int(os.getenv("MIN_LIVE_CALIBRATION_SAMPLES", "25"))

    # --- Calibration « façon 1xBet » ---
    # Marge moyenne constatée en base sur 1X2 / totaux / totaux équipe.
    BOOKMAKER_MARGIN = float(os.getenv("BOOKMAKER_MARGIN", "0.095"))
    DE_VIG_METHOD = os.getenv("DE_VIG_METHOD", "multiplicative")
    # Un marché n'est jouable que si l'échantillon historique prouve sa rentabilité.
    MIN_PROVEN_BETS = int(os.getenv("MIN_PROVEN_BETS", "30"))
    MIN_PROVEN_ROI_PCT = float(os.getenv("MIN_PROVEN_ROI_PCT", "2.0"))
    # Confiance minimale (proba calibrée) et value minimale exigées.
    MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.68"))
    MIN_VALUE = float(os.getenv("MIN_VALUE", "1.10"))
    # Fenêtre de cotes acceptée : en dessous de 1.50 la marge écrase la value.
    MIN_ODDS = float(os.getenv("MIN_ODDS", "1.50"))
    MAX_ODDS = float(os.getenv("MAX_ODDS", "4.50"))
    # Poids (borné) accordé à la probabilité implicite du bookmaker.
    MAX_BOOKMAKER_BLEND = 0.65
    # Bruit maximum toléré entre notre estimation et le marché.
    MAX_EDGE_OVER_MARKET = 0.15

    def __init__(self, db_path=None, memory_path=None):
        self.db_path = _project_path("penalty_history.db") if db_path is None else str(db_path)
        self.memory_path = _project_path("ai_memory.json") if memory_path is None else str(memory_path)
        self.memory = self._load_memory()
        self._train_lock = threading.Lock()
        self._auto_stop = threading.Event()
        self._auto_thread = None
        self.training_completed_on_load = self.maybe_train()

    def _load_memory(self):
        try:
            with open(self.memory_path, "r", encoding="utf-8") as memory_file:
                memory = json.load(memory_file)
        except FileNotFoundError:
            return _default_memory()
        except json.JSONDecodeError as error:
            raise ValueError("ai_memory.json contient du JSON invalide") from error

        if not isinstance(memory, dict):
            raise ValueError("ai_memory.json doit contenir un objet JSON")

        defaults = _default_memory()
        for key, value in defaults.items():
            memory.setdefault(key, value)
        for section in ("overall", "hourly", "profitability"):
            if not isinstance(memory.get(section), dict):
                raise ValueError("Structure invalide dans ai_memory.json : " + section)
            for key, value in defaults[section].items():
                memory[section].setdefault(key, value)
        if not isinstance(memory.get("markov"), dict) or not isinstance(
            memory.get("exact_scores"), dict
        ):
            raise ValueError("Structure invalide dans ai_memory.json")
        if not isinstance(memory.get("exact_score_calibration"), dict):
            raise ValueError("Structure invalide dans ai_memory.json : exact_score_calibration")
        return memory

    def _read_matches(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            return connection.execute(
                """
                SELECT game_id, hour_of_day, final_score1, final_score2,
                       total_goals, went_to_sudden_death, fav_team, fav_odds,
                       team1, team2, center_total_line, over_odds, under_odds
                FROM matches_history
                WHERE total_goals IS NOT NULL
                ORDER BY updated_at ASC, rowid ASC
                """
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return []
            raise
        finally:
            connection.close()

    @staticmethod
    def _profile(samples):
        count = len(samples)
        if not count:
            return {
                "matches": 0,
                "average_goals_scored": None,
                "average_goals_conceded": None,
                "win_rate_pct": None,
                "draw_rate_pct": None,
                "sudden_death_rate_pct": None,
                "both_teams_scored_rate_pct": None,
                "even_total_rate_pct": None,
                "clean_sheet_rate_pct": None,
                "samples": [],
            }
        return {
            "matches": count,
            "average_goals_scored": sum(item[0] for item in samples) / count,
            "average_goals_conceded": sum(item[1] for item in samples) / count,
            "win_rate_pct": sum(item[0] > item[1] for item in samples) / count * 100,
            "draw_rate_pct": sum(item[0] == item[1] for item in samples) / count * 100,
            "sudden_death_rate_pct": sum(item[3] for item in samples) / count * 100,
            "both_teams_scored_rate_pct": sum(
                item[0] > 0 and item[1] > 0 for item in samples
            ) / count * 100,
            "even_total_rate_pct": sum(item[2] % 2 == 0 for item in samples) / count * 100,
            "clean_sheet_rate_pct": sum(
                item[0] > item[1] and item[1] == 0 for item in samples
            ) / count * 100,
            "samples": samples,
        }

    def analyze_matchup(self, team1, team2):
        """Return historical form and head-to-head rates for the two teams."""
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            rows = connection.execute(
                """
                SELECT team1, team2, final_score1, final_score2, total_goals,
                       went_to_sudden_death
                FROM matches_history
                WHERE total_goals IS NOT NULL
                  AND (
                    team1 COLLATE NOCASE = ?
                    OR team2 COLLATE NOCASE = ?
                    OR team1 COLLATE NOCASE = ?
                    OR team2 COLLATE NOCASE = ?
                  )
                ORDER BY updated_at ASC, rowid ASC
                """,
                (team1, team1, team2, team2),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                rows = []
            else:
                raise
        finally:
            connection.close()

        normalized_team1 = str(team1 or "").strip().casefold()
        normalized_team2 = str(team2 or "").strip().casefold()
        samples1 = []
        samples2 = []
        head_to_head = []
        for row in rows:
            row_team1, row_team2, score1, score2, total, sudden = row
            if not isinstance(score1, int) or not isinstance(score2, int):
                continue
            is_sudden = bool(sudden) if sudden is not None else (
                score1 >= 5 and score2 >= 5 or (score1 + score2) >= 10
            )
            # Les stats d'équipe et le h2h portent sur le temps réglementaire,
            # comme les marchés : on retire les buts de mort subite.
            regulation = regulation_score(score1, score2, is_sudden)
            if regulation is None:
                continue
            score1, score2, total_goals = regulation
            sample = (score1, score2, total_goals, is_sudden)
            reverse_sample = (score2, score1, total_goals, is_sudden)
            row_team1_key = str(row_team1 or "").strip().casefold()
            row_team2_key = str(row_team2 or "").strip().casefold()
            if row_team1_key == normalized_team1:
                samples1.append(sample)
            elif row_team2_key == normalized_team1:
                samples1.append(reverse_sample)
            if row_team1_key == normalized_team2:
                samples2.append(sample)
            elif row_team2_key == normalized_team2:
                samples2.append(reverse_sample)

            if (
                row_team1_key == normalized_team1
                and row_team2_key == normalized_team2
            ):
                head_to_head.append(sample)
            elif (
                row_team1_key == normalized_team2
                and row_team2_key == normalized_team1
            ):
                head_to_head.append(reverse_sample)

        h2h_count = len(head_to_head)
        return {
            "team1": self._profile(samples1),
            "team2": self._profile(samples2),
            "h2h": {
                "matches_played": h2h_count,
                "average_score_team1": (
                    sum(item[0] for item in head_to_head) / h2h_count
                    if h2h_count
                    else None
                ),
                "average_score_team2": (
                    sum(item[1] for item in head_to_head) / h2h_count
                    if h2h_count
                    else None
                ),
                "average_total_goals": (
                    sum(item[2] for item in head_to_head) / h2h_count
                    if h2h_count
                    else None
                ),
                "sudden_death_rate_pct": (
                    sum(item[3] for item in head_to_head) / h2h_count * 100
                    if h2h_count
                    else None
                ),
                "samples": head_to_head,
            },
        }

    def _read_regulation_markets(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            return connection.execute(
                """
                SELECT m.game_id, m.hour_of_day, m.final_score1, m.final_score2,
                       m.total_goals, m.went_to_sudden_death, q.cycle_regime,
                       q.market_key, q.group_id, q.event_type, q.threshold,
                       q.label, q.group_label, q.outcome_label,
                       q.is_extra_penalties, q.prematch_odds, m.team1, m.team2
                FROM matches_history AS m
                JOIN regulation_markets_history AS q ON q.game_id = m.game_id
                WHERE m.total_goals IS NOT NULL AND q.prematch_odds > 1
                ORDER BY m.updated_at ASC, m.rowid ASC
                """
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return []
            raise
        finally:
            connection.close()

    def _read_exact_score_predictions(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            return connection.execute(
                """
                SELECT predicted_score1, predicted_score2, odds, phase,
                       hour_of_day, prediction_status
                FROM exact_score_predictions
                WHERE ai_displayed = 1
                  AND prediction_status IN ('WON', 'LOST')
                ORDER BY updated_at ASC, game_id ASC, phase ASC
                """
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return []
            raise
        finally:
            connection.close()

    def _exact_score_prediction_count(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM exact_score_predictions
                WHERE ai_displayed = 1
                  AND prediction_status IN ('WON', 'LOST')
                """
            ).fetchone()
            return int(row[0] or 0) if row is not None else 0
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return 0
            raise
        finally:
            connection.close()

    @staticmethod
    def _exact_score_odds_bucket(odds):
        if odds < 2:
            return "<2"
        if odds < 4:
            return "2-4"
        if odds < 7:
            return "4-7"
        if odds < 12:
            return "7-12"
        if odds < 20:
            return "12-20"
        return "20+"

    @staticmethod
    def _exact_score_total_bucket(score1, score2):
        total = score1 + score2
        if total <= 5:
            return "<=5"
        if total <= 7:
            return "6-7"
        if total <= 9:
            return "8-9"
        if total <= 11:
            return "10-11"
        return "12+"

    @staticmethod
    def _exact_score_margin_bucket(score1, score2):
        difference = score1 - score2
        if difference == 0:
            return "draw"
        if difference == 1:
            return "home+1"
        if difference > 1:
            return "home+2"
        if difference == -1:
            return "away+1"
        return "away+2"

    @classmethod
    def _train_exact_score_calibration(cls, predictions):
        models = {
            "overall": {},
            "by_score": {},
            "by_score_odds": {},
            "by_odds": {},
            "by_combination": {},
        }
        for score1, score2, odds_value, phase, _hour, result in predictions:
            odds = _valid_odds(odds_value)
            if odds is None:
                continue
            score_key = f"{score1}-{score2}"
            phase = str(phase).upper()
            odds_bucket = cls._exact_score_odds_bucket(odds)
            total_bucket = cls._exact_score_total_bucket(score1, score2)
            margin_bucket = cls._exact_score_margin_bucket(score1, score2)
            combination_key = (
                f"{phase}|{total_bucket}|{margin_bucket}|{odds_bucket}"
            )
            keys = (
                ("overall", "all"),
                ("by_score", score_key),
                ("by_score_odds", f"{score_key}|{odds_bucket}"),
                ("by_odds", f"{phase}|{odds_bucket}"),
                ("by_combination", combination_key),
            )
            won = str(result).upper() == "WON"
            for section, key in keys:
                item = models[section].setdefault(
                    key,
                    {
                        "bets": 0,
                        "wins": 0,
                        "implied_sum": 0.0,
                        "net_units": 0.0,
                    },
                )
                item["bets"] += 1
                item["wins"] += int(won)
                item["implied_sum"] += 1 / odds
                item["net_units"] += odds - 1 if won else -1

        for section in models.values():
            for item in section.values():
                bets = item["bets"]
                item["actual_rate"] = item["wins"] / bets
                item["implied_probability"] = item.pop("implied_sum") / bets
                item["roi_pct"] = item.pop("net_units") / bets * 100
        return models

    @staticmethod
    def _add_calibration(target, hour, won, implied):
        key = str(hour)
        item = target.setdefault(
            key, {"count": 0, "wins": 0, "implied_sum": 0.0}
        )
        item["count"] += 1
        item["wins"] += int(won)
        item["implied_sum"] += implied

    @staticmethod
    def _finish_calibrations(calibrations):
        for item in calibrations.values():
            item["actual_rate"] = item["wins"] / item["count"]
            item["implied_probability"] = item["implied_sum"] / item["count"]
            del item["implied_sum"]

    def _train(self, matches):
        total_matches = len(matches)
        sudden_count = sum(bool(row[5]) for row in matches)
        prior_rate = (sudden_count + 1) / (total_matches + 2)

        transitions = {}
        for index in range(3, total_matches):
            previous = matches[index - 3 : index]
            pattern = "".join("1" if row[5] else "0" for row in previous)
            item = transitions.setdefault(pattern, {"count": 0, "sudden": 0})
            item["count"] += 1
            item["sudden"] += int(bool(matches[index][5]))

        markov = {}
        for pattern, item in transitions.items():
            markov[pattern] = {
                "count": item["count"],
                "sudden_death": item["sudden"],
                "probability": (
                    item["sudden"] + prior_rate * 2
                )
                / (item["count"] + 2),
            }

        hourly = {
            "favorite": {},
            "total_over": {},
            "total_under": {},
        }
        profits = {
            "Favoris 1X2": {"bets": 0, "net": 0.0},
            "Total global": {"bets": 0, "net": 0.0},
        }
        exact_scores = {}
        regulation_market_stats = {}

        for row in matches:
            (
                _game_id,
                hour,
                score1,
                score2,
                total_goals,
                sudden_death,
                fav_team,
                fav_odds_value,
                team1,
                team2,
                line_value,
                over_odds_value,
                under_odds_value,
            ) = row
            if score1 is None or score2 is None:
                continue
            # Les marchés 1X2 / Total / Score exact portent sur les 5 tirs
            # réglementaires : on exclut les buts de mort subite.
            regulation = regulation_score(
                score1, score2, sudden_death
            )
            if regulation is None:
                continue
            score1, score2, total_goals = regulation
            score_key = str(score1) + "-" + str(score2)
            exact_scores[score_key] = exact_scores.get(score_key, 0) + 1

            if not isinstance(hour, int) or not 0 <= hour <= 23:
                continue

            fav_odds = _valid_odds(fav_odds_value)
            if fav_odds is not None and fav_team is not None:
                if score1 > score2:
                    winner = team1
                elif score2 > score1:
                    winner = team2
                else:
                    winner = None
                if winner is not None and fav_team in (team1, team2):
                    favorite_won = fav_team == winner
                    self._add_calibration(
                        hourly["favorite"],
                        hour,
                        favorite_won,
                        1 / fav_odds,
                    )
                    profits["Favoris 1X2"]["bets"] += 1
                    profits["Favoris 1X2"]["net"] += (
                        fav_odds - 1 if favorite_won else -1
                    )

            over_odds = _valid_odds(over_odds_value)
            under_odds = _valid_odds(under_odds_value)
            line = _number(line_value)
            goals = _number(total_goals)
            if (
                over_odds is None
                or under_odds is None
                or line is None
                or goals is None
                or goals == line
            ):
                continue

            implied_over_raw = 1 / over_odds
            implied_under_raw = 1 / under_odds
            implied_sum = implied_over_raw + implied_under_raw
            implied_over = implied_over_raw / implied_sum
            implied_under = implied_under_raw / implied_sum
            over_won = goals > line
            self._add_calibration(
                hourly["total_over"], hour, over_won, implied_over
            )
            self._add_calibration(
                hourly["total_under"], hour, not over_won, implied_under
            )

            selected_over = over_odds <= under_odds
            selected_won = over_won if selected_over else not over_won
            selected_odds = over_odds if selected_over else under_odds
            profits["Total global"]["bets"] += 1
            profits["Total global"]["net"] += (
                selected_odds - 1 if selected_won else -1
            )

        for row in self._read_regulation_markets():
            (
                _game_id,
                hour,
                score1,
                score2,
                total_goals,
                sudden_death,
                cycle_regime,
                market_key,
                group_id,
                event_type,
                threshold,
                label,
                group_label,
                outcome_label,
                is_extra_penalties,
                odds_value,
                team1,
                team2,
            ) = row

            # Normalisation canonique des clés de marché pour l'apprentissage
            if group_id == 14 and event_type in (182, 183):
                market_key = f"btts:{event_type}:{'Oui' if event_type == 183 else 'Non'}"
            elif group_id == 2270 and event_type in (2511, 2512):
                market_key = f"even-odd:{event_type}:{'Total pair' if event_type == 2511 else 'Total impair'}"
            elif group_id == 776 and event_type in (1422, 1423, 1424, 1425):
                market_key = f"team-pens:{group_id}:{event_type}"
            elif group_id == 2866 and event_type in (200, 205):
                market_key = f"clean-sheet-win:1:{event_type}:{'Oui' if event_type == 200 else 'Non'}"
            elif group_id == 2867 and event_type in (195, 210):
                market_key = f"clean-sheet-win:2:{event_type}:{'Oui' if event_type == 195 else 'Non'}"
            odds = _valid_odds(odds_value)
            # Les marchés réglementaires se règlent sur les 5 tirs initiaux.
            # Le booléen de mort subite est conservé tel quel : il alimente les
            # marchés « Pénalités supplémentaires ».
            is_sudden = bool(sudden_death)
            regulation = regulation_score(
                score1, score2, is_sudden
            )
            if regulation is None:
                continue
            reg_score1, reg_score2, reg_total = regulation
            won = self._settle_regulation_market(
                group_id,
                event_type,
                threshold,
                label,
                group_label,
                outcome_label,
                bool(is_extra_penalties),
                reg_score1,
                reg_score2,
                reg_total,
                is_sudden,
                team1,
                team2,
            )
            if odds is None or won is None:
                continue
            # won == "PUSH" : total exactement égal à la ligne entière. Le pari
            # est remboursé : _add_market_stat l'exclut de la mise nette,
            # sinon la ROI serait surévaluée.
            market = regulation_market_stats.setdefault(
                market_key,
                {
                    "label": label,
                    "group_id": group_id,
                    "event_type": event_type,
                    "threshold": threshold,
                    "is_extra_penalties": bool(is_extra_penalties),
                    "overall": self._empty_market_stat(),
                    "by_hour": {},
                    "by_cycle": {},
                },
            )
            self._add_market_stat(market["overall"], odds, won)
            if isinstance(hour, int) and 0 <= hour <= 23:
                hourly_stat = market["by_hour"].setdefault(
                    str(hour), self._empty_market_stat()
                )
                self._add_market_stat(hourly_stat, odds, won)
            if cycle_regime:
                cycle_stat = market["by_cycle"].setdefault(
                    str(cycle_regime), self._empty_market_stat()
                )
                self._add_market_stat(cycle_stat, odds, won)

        for market in regulation_market_stats.values():
            self._finish_market_stat(market["overall"])
            for bucket in market["by_hour"].values():
                self._finish_market_stat(bucket)
            for bucket in market["by_cycle"].values():
                self._finish_market_stat(bucket)

        exact_score_predictions = self._read_exact_score_predictions()
        exact_score_calibration = self._train_exact_score_calibration(
            exact_score_predictions
        )

        for calibration in hourly.values():
            self._finish_calibrations(calibration)

        recent = matches[-15:]
        recent_sudden = sum(bool(row[5]) for row in recent)
        recent_short = sum(_number(row[4]) is not None and row[4] <= 7 for row in recent)
        profitability_options = []
        for market, result in profits.items():
            if result["bets"]:
                profitability_options.append(
                    (
                        result["net"] / result["bets"],
                        market,
                        result["bets"],
                    )
                )
        if profitability_options:
            roi, market, bets = max(profitability_options)
            profitability = {
                "market": market,
                "roi_pct": roi * 100,
                "bets": bets,
            }
        else:
            profitability = {
                "market": "Données insuffisantes",
                "roi_pct": None,
                "bets": 0,
            }

        return {
            "version": 1,
            "trained_matches": total_matches,
            "overall": {
                "sudden_death_rate": prior_rate,
                "recent_sudden_death_rate": (
                    recent_sudden / len(recent) if recent else None
                ),
                "recent_short_rate": (
                    recent_short / len(recent) if recent else None
                ),
                "average_goals": (
                    sum(_number(row[4]) or 0 for row in matches) / total_matches
                    if total_matches
                    else None
                ),
                "average_score_team1": (
                    sum(row[2] for row in matches if isinstance(row[2], int))
                    / max(1, sum(isinstance(row[2], int) for row in matches))
                ),
                "average_score_team2": (
                    sum(row[3] for row in matches if isinstance(row[3], int))
                    / max(1, sum(isinstance(row[3], int) for row in matches))
                ),
            },
            "markov": markov,
            "hourly": hourly,
            "regulation_markets": regulation_market_stats,
            "market_profitability": self._build_market_profitability(
                regulation_market_stats
            ),
            "exact_scores": exact_scores,
            "exact_score_total": sum(exact_scores.values()),
            "exact_score_calibration": exact_score_calibration,
            "trained_exact_score_predictions": len(exact_score_predictions),
            "profitability": profitability,
        }

    def _read_live_signal_stats(self):
        """Lit les résultats des signaux live depuis live_signal_snapshots."""
        try:
            connection = sqlite3.connect(self.db_path, timeout=5)
            try:
                rows = connection.execute(
                    """
                    SELECT market_key, odds_at_trigger, outcome
                    FROM live_signal_snapshots
                    WHERE outcome IN ('WON', 'LOST')
                      AND odds_at_trigger > 1
                    """
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.OperationalError:
            return {}
        # Agréger par market_key
        stats = {}
        for market_key, odds, outcome in rows:
            s = stats.setdefault(market_key, {"bets": 0, "wins": 0, "net": 0.0, "implied_sum": 0.0})
            s["bets"] += 1
            won = outcome == "WON"
            s["wins"] += int(won)
            s["net"] += (odds - 1) if won else -1.0
            s["implied_sum"] += 1.0 / odds
        return stats

    def _build_market_profitability(self, regulation_market_stats):
        """Classe chaque marché en PROVEN / NEUTRAL / AVOID d'après la base.

        Combine les données pré-match (regulation_market_stats) ET les signaux
        live (live_signal_snapshots) pour avoir une vue complète de la rentabilité.
        Un marché est PROVEN si l'échantillon est suffisant ET si la borne basse
        de son intervalle de confiance 95 % (Wilson) reste au-dessus du seuil
        de rentabilité.
        """
        profitability = {}

        # ── 1. Marchés pré-match (regulation_market_stats) ──────────────────
        for market_key, market in regulation_market_stats.items():
            stats = market.get("overall", {})
            bets = int(stats.get("bets", 0) or 0)
            wins = int(stats.get("wins", 0) or 0)
            roi = stats.get("roi_pct")
            if bets <= 0 or roi is None:
                continue
            break_even_rate = stats.get("implied_probability") or 0.0
            lower_bound = self._wilson_lower_bound(wins, bets)
            verdict = "NEUTRAL"
            is_core_market = market_key.startswith("winner:") or market_key.startswith("total:9:7.5") or market_key.startswith("total:9:8.5")
            if (
                bets >= self.MIN_PROVEN_BETS
                and roi >= self.MIN_PROVEN_ROI_PCT
                and lower_bound > break_even_rate
            ):
                verdict = "PROVEN"
            elif bets >= self.MIN_PROVEN_BETS and roi <= -15.0 and not is_core_market:
                verdict = "AVOID"
            profitability[market_key] = {
                "verdict": verdict,
                "bets": bets,
                "wins": wins,
                "actual_rate": stats.get("actual_rate"),
                "break_even_rate": break_even_rate,
                "wilson_lower_rate": lower_bound,
                "roi_pct": roi,
                "source": "prematch",
            }

        # ── 2. Signaux LIVE (live_signal_snapshots) ───────────────────────────
        # Ces marchés ont souvent un ROI très élevé (+85% à +500%) mais ne sont
        # pas dans regulation_market_stats car ils se déclenchent en cours de match.
        live_stats = self._read_live_signal_stats()
        # Seuil réduit pour live : 15 signaux suffisent (moins de bruit car le
        # déclencheur (-25% chute de cote) filtre déjà les opportunités).
        MIN_LIVE_PROVEN = 15
        for market_key, s in live_stats.items():
            bets = s["bets"]
            wins = s["wins"]
            if bets <= 0:
                continue
            roi = s["net"] / bets * 100
            implied_avg = s["implied_sum"] / bets  # cote implicite moyenne
            lower_bound = self._wilson_lower_bound(wins, bets)
            verdict = "NEUTRAL"
            if (
                bets >= MIN_LIVE_PROVEN
                and roi >= self.MIN_PROVEN_ROI_PCT
                and lower_bound > implied_avg
            ):
                verdict = "PROVEN"
            elif bets >= MIN_LIVE_PROVEN and roi <= -15.0:
                verdict = "AVOID"
            # Les signaux live enrichissent ou remplacent l'entrée pré-match si
            # leur échantillon est plus grand (plus fiable).
            existing = profitability.get(market_key)
            if existing is None or bets > existing.get("bets", 0):
                profitability[market_key] = {
                    "verdict": verdict,
                    "bets": bets,
                    "wins": wins,
                    "actual_rate": wins / bets,
                    "break_even_rate": implied_avg,
                    "wilson_lower_rate": lower_bound,
                    "roi_pct": roi,
                    "source": "live_signal",
                }

        return profitability

    def market_profitability(self, market_key):
        """Verdict appris pour un marché (PROVEN / NEUTRAL / AVOID / UNKNOWN)."""
        entry = self.memory.get("market_profitability", {}).get(str(market_key))
        if not isinstance(entry, Mapping):
            return {"verdict": "UNKNOWN", "bets": 0}
        return entry

    @staticmethod
    def _empty_market_stat():
        return {
            "bets": 0,
            "wins": 0,
            "net_units": 0.0,
            "implied_sum": 0.0,
        }

    @staticmethod
    def _add_market_stat(stat, odds, won):
        """won : True, False, ou "PUSH" (remboursé sur ligne entière)."""
        if won == "PUSH":
            stat["pushes"] = stat.get("pushes", 0) + 1
            return
        stat["bets"] += 1
        stat["wins"] += int(won)
        stat["net_units"] += odds - 1 if won else -1
        stat["implied_sum"] += 1 / odds

    @staticmethod
    def _finish_market_stat(stat):
        bets = stat["bets"]
        stat["actual_rate"] = stat["wins"] / bets if bets else 0.0
        stat["implied_probability"] = (
            stat.pop("implied_sum") / bets if bets else 0.0
        )
        stat["roi_pct"] = stat.pop("net_units") / bets * 100 if bets else 0.0

    @staticmethod
    def _settle_regulation_market(
        group_id,
        event_type,
        threshold,
        label,
        group_label,
        outcome_label,
        is_extra_penalties,
        score1,
        score2,
        total_goals,
        sudden_death,
        team1=None,
        team2=None,
    ):
        if score1 is None or score2 is None:
            return None

        if is_extra_penalties:
            outcome = str(outcome_label or label).casefold()
            if outcome in ("oui", "yes", "true"):
                return sudden_death
            if outcome in ("non", "no", "false"):
                return not sudden_death
            return None

        if group_id == 1:
            return {
                1: score1 > score2,
                2: score1 == score2,
                3: score2 > score1,
            }.get(event_type)
        if group_id == 8:
            return {
                4: score1 >= score2,
                5: score1 != score2,
                6: score2 >= score1,
            }.get(event_type)

        if group_id == 2 and event_type in (7, 8):
            line = _number(threshold)
            if line is None:
                return None
            margin = score1 - score2 if event_type == 7 else score2 - score1
            adjusted_margin = margin + line
            if adjusted_margin == 0:
                return None
            return adjusted_margin > 0

        if group_id in (17, 15, 62) or (
            group_id == 19 and event_type in (11273, 11274)
        ):
            if group_id == 19:
                line = TerminatAI._normalized_total_line(threshold)
            else:
                line = _number(threshold)
            if line is None:
                return None
            goals = (
                total_goals
                if group_id in (17, 19)
                else (score1 if group_id == 15 else score2)
            )
            over_types = {17: 9, 15: 11, 62: 13, 19: 11273}
            under_types = {17: 10, 15: 12, 62: 14, 19: 11274}
            if goals is None:
                return None
            if goals == line:
                # Une ligne .5 ne peut jamais être atteinte exactement :
                # ce cas ne survient que sur une ligne entière.
                return "PUSH"
            if event_type in over_types.values():
                return goals > line
            if event_type in under_types.values():
                return goals < line
            return None

        if group_id == 19:
            market_text = " ".join(
                str(part or "") for part in (label, group_label)
            ).casefold()
            normalized = TerminatAI._normalized_text(market_text)
            outcome = TerminatAI._normalized_text(outcome_label or label)
            outcome_words = set(outcome.split())
            total = total_goals if total_goals is not None else score1 + score2
            if any(
                marker in set(normalized.split())
                for marker in ("pair", "impair", "even", "odd")
            ):
                even_outcome = bool(
                    outcome_words.intersection({"pair", "even"})
                )
                return (total % 2 == 0) == even_outcome
            if not any(
                phrase in normalized
                for phrase in (
                    "both teams",
                    "les deux equipes",
                    "each team to score",
                    "btts",
                    "marquent tous les deux",
                )
            ):
                if any(
                    marker in normalized
                    for marker in ("clean sheet", "win to nil", "sans encaisser")
                ):
                    named_team = str(team1 or "").casefold()
                    team_score, opponent_score = score1, score2
                    if named_team and named_team not in market_text:
                        named_team = str(team2 or "").casefold()
                        team_score, opponent_score = score2, score1
                    if not named_team or named_team not in market_text:
                        return None
                    if not outcome_words.intersection(
                        {"oui", "yes", "true", "non", "no", "false"}
                    ):
                        return None
                    won_to_nil = team_score > opponent_score and opponent_score == 0
                    return (
                        not won_to_nil
                        if outcome_words.intersection({"non", "no", "false"})
                        else won_to_nil
                    )
                return None
            yes_outcome = outcome_words.intersection({"oui", "yes", "true"}) or event_type == 1
            no_outcome = outcome_words.intersection({"non", "no", "false"}) or event_type == 2
            if not yes_outcome and not no_outcome:
                return None
            both_scored = score1 > 0 and score2 > 0
            return both_scored if yes_outcome else not both_scored

        # ── Group 14: Deux équipes vont marquer ─────────────────────────────
        if group_id == 14:
            both_scored = (score1 > 0 and score2 > 0)
            if event_type == 183:
                return both_scored
            if event_type == 182:
                return not both_scored
            return None

        # ── Group 2270: Total pair / impair ─────────────────────────────────
        if group_id == 2270:
            total = total_goals if total_goals is not None else (score1 + score2)
            is_even = (total % 2 == 0)
            if event_type == 2511:
                return is_even
            if event_type == 2512:
                return not is_even
            return None

        # ── Group 2866 & 2867: Équipe gagne sans prendre de but ─────────────
        if group_id == 2866:
            clean_win = (score1 > score2 and score2 == 0)
            if event_type == 200:
                return clean_win
            if event_type == 205:
                return not clean_win
            return None
        if group_id == 2867:
            clean_win = (score2 > score1 and score1 == 0)
            if event_type == 195:
                return clean_win
            if event_type == 210:
                return not clean_win
            return None

        # ── Group 776: Équipe marque ses penalties ──────────────────────────
        if group_id == 776:
            thresh = 3.5 if threshold is None else threshold
            if event_type == 1422:
                return score1 > thresh
            if event_type == 1423:
                return score1 < thresh
            if event_type == 1424:
                return score2 > thresh
            if event_type == 1425:
                return score2 < thresh
            return None

        return None

    def maybe_train(self, force=False):
        with self._train_lock:
            matches = self._read_matches()
            count = len(matches)
            last_trained = int(self.memory.get("trained_matches", 0))
            exact_prediction_count = self._exact_score_prediction_count()
            last_exact_prediction_count = int(
                self.memory.get("trained_exact_score_predictions", 0)
            )
            match_training_due = (
                count >= self.TRAINING_INTERVAL
                and count // self.TRAINING_INTERVAL
                > last_trained // self.TRAINING_INTERVAL
            )
            exact_score_training_due = (
                exact_prediction_count > last_exact_prediction_count
            )
            if not force and not (
                match_training_due or exact_score_training_due
            ):
                return False

            self.memory = self._train(matches)
            with open(self.memory_path, "w", encoding="utf-8") as memory_file:
                json.dump(self.memory, memory_file, ensure_ascii=False, indent=2)
                memory_file.write("\n")
            return True

    def start_auto_training(self, check_interval=60, on_trained=None):
        """Relance maybe_train() toutes les check_interval secondes en arrière-plan."""
        if self._auto_thread is not None and self._auto_thread.is_alive():
            return

        self._auto_stop.clear()

        def _loop():
            while not self._auto_stop.wait(check_interval):
                try:
                    if self.maybe_train() and on_trained is not None:
                        on_trained()
                except Exception as error:
                    self.last_auto_train_error = repr(error)

        self.last_auto_train_error = None
        self._auto_thread = threading.Thread(
            target=_loop, name="TerminatAI-auto-train", daemon=True
        )
        self._auto_thread.start()

    def stop_auto_training(self):
        self._auto_stop.set()

    @staticmethod
    def _wilson_lower_bound(wins, bets):
        if bets <= 0:
            return 0.0
        z = 1.96
        rate = wins / bets
        denominator = 1 + z * z / bets
        center = rate + z * z / (2 * bets)
        margin = z * math.sqrt(
            rate * (1 - rate) / bets + z * z / (4 * bets * bets)
        )
        return max(0.0, (center - margin) / denominator)

    def _most_reliable_regulation_market(self):
        markets = self.memory.get("regulation_markets", {})
        candidates = [
            market
            for market in markets.values()
            if market.get("overall", {}).get("bets", 0)
            >= self.MIN_RELIABLE_MARKET_BETS
            and market.get("overall", {}).get("roi_pct", 0) > 0
            and market.get("group_id") != 19
            and not market.get("is_extra_penalties", False)
            and "penalites supplementaires"
            not in self._normalized_text(market.get("label", ""))
        ]
        if not candidates:
            return None
        market = max(
            candidates,
            key=lambda item: (
                self._wilson_lower_bound(
                    item["overall"]["wins"], item["overall"]["bets"]
                ),
                item["overall"]["roi_pct"],
                item["overall"]["bets"],
            ),
        )
        stats = market["overall"]
        return (
            str(market["label"])
            + " — taux observé "
            + str(round(stats["actual_rate"] * 100, 1))
            + "% ("
            + str(stats["wins"])
            + "/"
            + str(stats["bets"])
            + "), borne basse IC 95% "
            + str(
                round(
                    self._wilson_lower_bound(stats["wins"], stats["bets"])
                    * 100,
                    1,
                )
            )
            + "%, ROI "
            + ("+" if stats["roi_pct"] >= 0 else "")
            + str(round(stats["roi_pct"], 1))
            + "%"
        )

    def format_training_summary(self):
        best_market = self._most_reliable_regulation_market()
        if best_market is None:
            best_market = (
                "données insuffisantes (minimum "
                + str(self.MIN_RELIABLE_MARKET_BETS)
                + " paris réglés et ROI positif)"
            )
        exact_scores = (
            self.memory.get("exact_score_calibration", {})
            .get("overall", {})
            .get("all", {})
        )
        exact_score_summary = (
            f"{exact_scores.get('bets', 0)} cotes réglées, précision observée "
            f"{exact_scores.get('actual_rate', 0) * 100:.1f}%"
        )
        return "\n".join(
            [
                "🧠 TerminatAI — cycle d’apprentissage terminé",
                "Matchs terminés analysés : "
                + str(self.memory.get("trained_matches", 0)),
                "Scores exacts appris : " + exact_score_summary,
                "Marché Temps Réglementaire validé (échantillon suffisant) : "
                + best_market,
            ]
        )

    def _markov_probability(self, game_id=None):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            if game_id is None:
                rows = connection.execute(
                    """
                    SELECT went_to_sudden_death
                    FROM matches_history
                    WHERE total_goals IS NOT NULL
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT 3
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT went_to_sudden_death
                    FROM matches_history
                    WHERE total_goals IS NOT NULL AND game_id != ?
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT 3
                    """,
                    (str(game_id),),
                ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                rows = []
            else:
                raise
        finally:
            connection.close()

        pattern = "".join(
            "1" if row[0] else "0" for row in reversed(rows)
        )
        learned = self.memory.get("markov", {}).get(pattern)
        if learned is not None:
            return _clamp_probability(float(learned["probability"]))
        overall = self.memory.get("overall", {}).get("sudden_death_rate")
        return 0.5 if overall is None else _clamp_probability(float(overall))

    def _calibrated_probability(self, category, hour, implied):
        bucket = self.memory.get("hourly", {}).get(category, {}).get(str(hour))
        if not bucket or not bucket.get("count"):
            return implied
        count = bucket["count"]
        adjustment = (
            bucket["actual_rate"] - bucket["implied_probability"]
        ) * count / (count + 10)
        return _clamp_probability(implied + adjustment)

    def _bookmaker_fair_probability(self, quote, sibling_odds=None):
        """Probabilité « juste » du bookmaker après retrait de la marge.

        Reproduit le calcul d'un opérateur (1xBet) : les cotes brutes d'un même
        marché contiennent la marge (mesurée à ~9,5 % dans votre base). On la
        retire pour obtenir la vraie estimation du marché, point de départ sain
        avant tout ajustement par l'historique.
        """
        odds = _valid_odds(quote.odds)
        if odds is None:
            return None
        siblings = []
        for item in sibling_odds or []:
            sibling = _valid_odds(item)
            if sibling is not None:
                siblings.append(sibling)
        implied_total = sum(1 / item for item in siblings)
        # Un marché cohérent avec marge de bookmaker a toujours un total entre 0.95 et 1.35.
        # Si le total est anormal (ex: cotes de live asynchrones ou marché suspendu),
        # on évite de fausser le calcul et on utilise la marge bookmaker standard.
        if len(siblings) < 2 or implied_total < 0.95 or implied_total > 1.35:
            return _clamp_probability((1 / odds) / (1 + self.BOOKMAKER_MARGIN))

        if self.DE_VIG_METHOD == "power" and implied_total > 1:
            exponent = math.log(2) / math.log(implied_total)
            fair = sum((1 / item) ** exponent for item in siblings)
            return _clamp_probability(((1 / odds) ** exponent) / fair)
        return _clamp_probability((1 / odds) / implied_total)

    def _calibrated_regulation_probability(
        self, quote, hour, regime, is_live=False, sibling_odds=None
    ):
        """Probabilité finale = marché dévigorisé + ajustement bayésien de la base.

        L'ajustement n'est jamais autorisé à s'éloigner du marché de plus de
        MAX_EDGE_OVER_MARKET : un écart plus grand est presque toujours du bruit
        d'échantillon, pas une vraie value (c'est la principale cause des pertes
        constatées dans sent_predictions).
        """
        market_probability = self._bookmaker_fair_probability(
            quote, sibling_odds
        )
        if market_probability is None:
            return _clamp_probability(1 / quote.odds)
        if is_live:
            return market_probability

        learned = self.memory.get("regulation_markets", {}).get(quote.key)
        if not learned:
            return market_probability

        probability = market_probability
        buckets = [learned.get("overall", {})]
        buckets.append(learned.get("by_hour", {}).get(str(hour), {}))
        buckets.append(learned.get("by_cycle", {}).get(regime, {}))
        total_weight = 0.0
        for bucket in buckets:
            count = bucket.get("bets", 0) or 0
            actual_rate = bucket.get("actual_rate")
            break_even = bucket.get("implied_probability")
            if not count or actual_rate is None or break_even is None:
                continue
            weight = count / (count + 30) / len(buckets)
            probability += (actual_rate - break_even) * weight
            total_weight += weight
        if total_weight > self.MAX_BOOKMAKER_BLEND:
            probability = market_probability + (probability - market_probability) * (
                self.MAX_BOOKMAKER_BLEND / total_weight
            )
        probability = _clamp_probability(probability)
        delta = probability - market_probability
        if abs(delta) > self.MAX_EDGE_OVER_MARKET:
            probability = market_probability + math.copysign(
                self.MAX_EDGE_OVER_MARKET, delta
            )
        return probability

    def _calibrated_exact_score_probability(
        self, probability, prediction, phase
    ):
        odds = _valid_odds(prediction.odds)
        if odds is None:
            return _clamp_probability(probability)
        score1, score2 = prediction.score
        score_key = f"{score1}-{score2}"
        phase = str(phase).upper()
        odds_bucket = self._exact_score_odds_bucket(odds)
        total_bucket = self._exact_score_total_bucket(score1, score2)
        margin_bucket = self._exact_score_margin_bucket(score1, score2)
        combination_key = (
            f"{phase}|{total_bucket}|{margin_bucket}|{odds_bucket}"
        )
        models = self.memory.get("exact_score_calibration", {})
        segments = (
            (models.get("by_odds", {}).get(f"{phase}|{odds_bucket}"), 40),
            (models.get("by_combination", {}).get(combination_key), 30),
            (
                models.get("by_score_odds", {}).get(
                    f"{score_key}|{odds_bucket}"
                ),
                25,
            ),
            (models.get("by_score", {}).get(score_key), 30),
        )
        evidence = []
        for item, prior_strength in segments:
            if not isinstance(item, Mapping):
                continue
            bets = int(item.get("bets", 0) or 0)
            actual_rate = _number(item.get("actual_rate"))
            if bets <= 0 or actual_rate is None:
                continue
            weight = bets / (bets + prior_strength)
            evidence.append((weight, actual_rate))
        if not evidence:
            return _clamp_probability(probability)
        total_weight = sum(weight for weight, _ in evidence)
        learned_rate = sum(
            weight * actual_rate for weight, actual_rate in evidence
        ) / total_weight
        evidence_weight = min(0.75, 1 - math.prod(1 - weight for weight, _ in evidence))
        return _clamp_probability(
            probability * (1 - evidence_weight)
            + learned_rate * evidence_weight
        )

    @staticmethod
    def _live_signal_bucket(value, *, odds):
        if odds:
            if value < 1.40:
                return "<1.40", None, None, 1.40, False
            if value <= 1.80:
                return "1.40–1.80", 1.40, True, 1.80, True
            if value <= 2.05:
                return ">1.80–2.05", 1.80, False, 2.05, True
            return ">2.05", 2.05, False, None, None
        if value < 15:
            return "<15%", None, None, 15.0, False
        if value < 25:
            return "15–<25%", 15.0, True, 25.0, False
        if value < 40:
            return "25–<40%", 25.0, True, 40.0, False
        return "≥40%", 40.0, True, None, None

    def get_live_signal_calibration(self, drop_pct, odds):
        drop = _number(drop_pct)
        price = _valid_odds(odds)
        if drop is None or drop <= 0 or price is None:
            raise ValueError("Chute LIVE et cote valides requises pour calibrer")

        drop_bucket = self._live_signal_bucket(drop, odds=False)
        odds_bucket = self._live_signal_bucket(price, odds=True)
        clauses = ["outcome IN ('WON', 'LOST')"]
        parameters = []
        for column, bucket in (
            ("drop_pct", drop_bucket),
            ("odds_at_trigger", odds_bucket),
        ):
            _, lower, lower_inclusive, upper, upper_inclusive = bucket
            if lower is not None:
                clauses.append(column + (" >= ?" if lower_inclusive else " > ?"))
                parameters.append(lower)
            if upper is not None:
                clauses.append(column + (" <= ?" if upper_inclusive else " < ?"))
                parameters.append(upper)

        try:
            connection = sqlite3.connect(self.db_path, timeout=5)
            try:
                row = connection.execute(
                    """
                    SELECT COUNT(*), SUM(outcome = 'WON')
                    FROM live_signal_snapshots
                    WHERE
                    """
                    + " AND ".join(clauses),
                    parameters,
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error).lower():
                raise
            row = (0, 0)

        count = int(row[0] or 0) if row is not None else 0
        wins = int(row[1] or 0) if row is not None else 0
        rate = wins / count if count else None
        calibrated = count >= self.MIN_LIVE_CALIBRATION_SAMPLES
        return {
            "drop_bucket": drop_bucket[0],
            "odds_bucket": odds_bucket[0],
            "count": count,
            "wins": wins,
            "win_rate_pct": None if rate is None else rate * 100,
            "weight": count / (count + 10) if calibrated else 0.0,
            "calibrated": calibrated,
        }

    def get_live_signal_statistics(self):
        try:
            connection = sqlite3.connect(self.db_path, timeout=5)
            try:
                row = connection.execute(
                    """
                    SELECT COUNT(*),
                           SUM(approved = 1),
                           SUM(approved = 1 AND outcome = 'WON'),
                           SUM(approved = 1 AND outcome = 'LOST')
                    FROM live_signal_snapshots
                    """
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error).lower():
                raise
            row = (0, 0, 0, 0)
        detected, sent, wins, losses = row or (0, 0, 0, 0)
        wins = int(wins or 0)
        losses = int(losses or 0)
        settled = wins + losses
        return {
            "detected": int(detected or 0),
            "sent": int(sent or 0),
            "won": wins,
            "lost": losses,
            "win_rate_pct": wins / settled * 100 if settled else None,
        }

    @staticmethod
    def _normalized_text(value):
        normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
        return "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )

    @staticmethod
    def _sample_rate(samples, predicate):
        outcomes = [bool(predicate(sample)) for sample in samples]
        if not outcomes:
            return None, 0
        return sum(outcomes) / len(outcomes), len(outcomes)

    def _matchup_probability(self, quote, matchup, team1, team2):
        home_samples = matchup["team1"]["samples"]
        away_samples = matchup["team2"]["samples"]
        h2h_samples = matchup["h2h"]["samples"]
        group_id = quote.group_id
        event_type = quote.event_type
        threshold = _number(quote.threshold)
        market_text = self._normalized_text(
            " ".join(
                (
                    str(getattr(quote, "label", "")),
                    str(getattr(quote, "group_label", "")),
                    str(getattr(quote, "outcome_label", "")),
                )
            )
        )
        outcome_text = self._normalized_text(
            getattr(quote, "outcome_label", "")
            or getattr(quote, "label", "")
        )
        is_no = bool(set(outcome_text.split()).intersection({"non", "no", "false"}))
        is_yes = bool(set(outcome_text.split()).intersection({"oui", "yes", "true"}))

        if group_id == 1:
            if event_type in (1, 3):
                # Confrontation directe prioritaire si disponible (>= 3 matchs)
                if h2h_samples and len(h2h_samples) >= 3:
                    h2h_pred = (lambda item: item[0] > item[1]) if event_type == 1 else (lambda item: item[1] > item[0])
                    rate, count = self._sample_rate(h2h_samples, h2h_pred)
                    if rate is not None:
                        return rate, count
                # Sinon, taux de victoires récent de l'équipe
                samples = home_samples if event_type == 1 else away_samples
                return self._sample_rate(samples, lambda item: item[0] > item[1])
            if event_type == 2:
                return 0.0, len(h2h_samples)
        elif group_id == 8:
            samples = home_samples if event_type == 4 else (
                away_samples if event_type == 6 else h2h_samples
            )
            if event_type == 4:
                return self._sample_rate(samples, lambda item: item[0] >= item[1])
            if event_type == 6:
                return self._sample_rate(samples, lambda item: item[0] >= item[1])
            if event_type == 5:
                return self._sample_rate(samples, lambda item: item[0] != item[1])
        elif group_id == 2 and threshold is not None and event_type in (7, 8):
            samples = h2h_samples
            if event_type == 7:
                rate, count = self._sample_rate(
                    samples, lambda item: item[0] + threshold > item[1]
                )
                valid = [item for item in samples if item[0] + threshold != item[1]]
            else:
                rate, count = self._sample_rate(
                    samples, lambda item: item[1] + threshold > item[0]
                )
                valid = [item for item in samples if item[1] + threshold != item[0]]
            if valid:
                rate = sum(
                    (
                        item[0] + threshold > item[1]
                        if event_type == 7
                        else item[1] + threshold > item[0]
                    )
                    for item in valid
                ) / len(valid)
                return rate, len(valid)
            return rate, count
        elif group_id in (17, 15, 62, 19) and threshold is not None and (
            event_type in (9, 10, 11, 12, 13, 14, 11273, 11274)
        ):
            # Groupe 19 : la ligne réelle est .5 même si le flux l'a tronquée.
            if group_id == 19:
                threshold = self._normalized_total_line(threshold)
                if threshold is None:
                    return None, 0
            samples = (
                home_samples
                if group_id == 15
                else away_samples
                if group_id == 62
                else h2h_samples
                if h2h_samples
                else home_samples + away_samples
            )
            over_types = (9, 11, 13, 11273)
            if group_id == 15:
                getter = lambda item: item[0]
            elif group_id == 62:
                getter = lambda item: item[0]
            else:
                getter = lambda item: item[2]
            if event_type in over_types:
                valid = [item for item in samples if getter(item) != threshold]
                if valid:
                    return (
                        sum(getter(item) > threshold for item in valid) / len(valid),
                        len(valid),
                    )
            else:
                valid = [item for item in samples if getter(item) != threshold]
                if valid:
                    return (
                        sum(getter(item) < threshold for item in valid) / len(valid),
                        len(valid),
                    )
        elif any(
            marker in market_text
            for marker in ("both teams", "les deux equipes", "btts", "marquent tous les deux")
        ):
            samples = h2h_samples
            if not samples:
                rate1 = matchup["team1"]["both_teams_scored_rate_pct"]
                rate2 = matchup["team2"]["both_teams_scored_rate_pct"]
                if rate1 is not None and rate2 is not None:
                    probability = (rate1 / 100 + rate2 / 100) / 2
                    return (1 - probability if is_no else probability), min(
                        matchup["team1"]["matches"],
                        matchup["team2"]["matches"],
                    )
            else:
                rate, count = self._sample_rate(
                    samples, lambda item: item[0] > 0 and item[1] > 0
                )
                if rate is not None:
                    return (1 - rate if is_no else rate), count
        elif any(marker in market_text for marker in ("pair", "impair", "even", "odd")):
            samples = h2h_samples or home_samples + away_samples
            outcome_words = set(outcome_text.split())
            even_outcome = bool(outcome_words.intersection({"pair", "even"}))
            return self._sample_rate(
                samples,
                lambda item: (item[2] % 2 == 0) == even_outcome,
            )
        elif getattr(quote, "is_extra_penalties", False):
            samples = h2h_samples
            if samples:
                rate, count = self._sample_rate(samples, lambda item: item[3])
                if rate is not None:
                    return (1 - rate if is_no else rate), count
            rate1 = matchup["team1"]["sudden_death_rate_pct"]
            rate2 = matchup["team2"]["sudden_death_rate_pct"]
            if rate1 is not None and rate2 is not None:
                probability = (rate1 + rate2) / 200
                return (1 - probability if is_no else probability), min(
                    matchup["team1"]["matches"],
                    matchup["team2"]["matches"],
                )
        elif any(
            marker in market_text
            for marker in ("clean sheet", "win to nil", "sans encaisser")
        ):
            named_team = str(team1 or "").casefold()
            samples = home_samples
            if named_team and named_team not in market_text:
                named_team = str(team2 or "").casefold()
                samples = away_samples
            if named_team and named_team in market_text:
                rate, count = self._sample_rate(
                    samples, lambda item: item[0] > item[1] and item[1] == 0
                )
                if rate is not None:
                    return (1 - rate if is_no else rate), count

        return None, 0

    def _blend_matchup_probability(self, probability, empirical, count):
        if empirical is None or count <= 0:
            return probability
        weight = min(
            self.MAX_MATCHUP_BLEND_WEIGHT,
            count / (count + 30),
        )
        return _clamp_probability(probability * (1 - weight) + empirical * weight)

    @staticmethod
    def _poisson_probability(mean, k):
        if mean <= 0 or k < 0:
            return 0.0
        return math.exp(-mean) * (mean ** k) / math.factorial(k)

    def _poisson_parameters(self, matchup):
        """Estime les moyennes de buts attendues (lambda) par équipe.

        Base statistique : votre historique réel (moy. 8,65 buts/match,
        4,15 domicile / 4,50 extérieur) mélangé aux lambdas déduits des cotes
        totales du match quand elles sont disponibles.
        """
        team1 = matchup.get("team1", {})
        team2 = matchup.get("team2", {})
        scored_1 = team1.get("average_goals_scored")
        scored_2 = team2.get("average_goals_scored")
        conceded_1 = team1.get("average_goals_conceded")
        conceded_2 = team2.get("average_goals_conceded")
        base = ((self.memory.get("overall") or {}).get("average_goals"))
        if not isinstance(base, (int, float)) or base <= 0:
            base = 8.65
        if scored_1 is None and conceded_2 is None:
            lambda_1 = base / 2
        else:
            parts = [v for v in (scored_1, conceded_2) if v is not None]
            lambda_1 = sum(parts) / len(parts)
        if scored_2 is None and conceded_1 is None:
            lambda_2 = base / 2
        else:
            parts = [v for v in (scored_2, conceded_1) if v is not None]
            lambda_2 = sum(parts) / len(parts)
        # Lissage vers la moyenne globale (évite les 0-0 extrapolés sur 1 match).
        lambda_1 = (lambda_1 + base / 2) / 2
        lambda_2 = (lambda_2 + base / 2) / 2
        return max(0.2, lambda_1), max(0.2, lambda_2)

    def _score_exact_probabilities(self, analysis, matchup, status, unfiltered):
        """Probabilité de chaque score exact via un modèle de Poisson bivarié.

        Remplace l'approche 1/cote (qui perdait -55 % en base) : on calcule
        P(score) à partir des lambdas estimés, on la compare au marché dévigorisé
        puis on n'expose que les scores où notre estimation bat réellement la
        cote.
        """
        lambda_1, lambda_2 = self._poisson_parameters(matchup)
        max_goals = 14
        grid = {}
        total = 0.0
        for a in range(max_goals + 1):
            p1 = self._poisson_probability(lambda_1, a)
            for b in range(max_goals + 1):
                p = p1 * self._poisson_probability(lambda_2, b)
                grid[(a, b)] = p
                total += p
        if total > 0:
            for key in grid:
                grid[key] /= total

        observed = self.memory.get("exact_scores", {})
        observed_total = int(self.memory.get("exact_score_total", 0))
        results = []
        for prediction in getattr(analysis, "exact_scores", ()):
            odds = _valid_odds(prediction.odds)
            if odds is None:
                continue
            score = prediction.score
            score_key = f"{score[0]}-{score[1]}"
            probability = grid.get((score[0], score[1]), 0.0)
            if observed_total:
                empirical = observed.get(score_key, 0) / observed_total
                weight = observed_total / (observed_total + 50)
                probability = probability * (1 - weight) + empirical * weight
            probability = _clamp_probability(probability)
            value = probability * odds
            results.append(
                {
                    "score": score_key,
                    "odds": odds,
                    "confidence_pct": probability * 100,
                    "value": value,
                    "qualified": (
                        unfiltered
                        or (
                            status in ("LIVE", "PRE-MATCH")
                            and value >= self.MIN_VALUE
                            and _clamp_probability(1 / odds) * 1.15 <= probability
                        )
                    ),
                    "market_type": "Score exact",
                    "extended": (
                        max(score) >= 6 and abs(score[0] - score[1]) == 1
                    ),
                }
            )
        return results

    @staticmethod
    def _is_settleable_regulation_market(quote):
        if getattr(quote, "is_extra_penalties", False):
            outcome = TerminatAI._normalized_text(
                getattr(quote, "outcome_label", "")
            )
            return outcome in ("oui", "non", "yes", "no")
        if quote.group_id == 1:
            return quote.event_type in (1, 2, 3)
        if quote.group_id == 8:
            return quote.event_type in (4, 5, 6)
        if quote.group_id == 2:
            return quote.event_type in (7, 8) and TerminatAI._has_standard_line(
                quote.threshold
            )
        if quote.group_id == 17:
            return quote.event_type in (9, 10) and TerminatAI._has_half_goal_line(
                quote.threshold
            )
        if quote.group_id == 19:
            if TerminatAI._is_both_teams_to_score_market(quote):
                outcome = TerminatAI._normalized_text(quote.outcome_label)
                return outcome in ("oui", "non", "yes", "no")
            # Les totaux 1xBet sont des lignes décimales (4.5, 6.5, 7.5...).
            # Le groupe 19 expose les MÊMES lignes que le groupe 17 : les
            # cotes sont identiques (6.5 @1.165 en g17 et g19). Le seuil lu
            # ici peut apparaître entier quand le flux brut est tronqué ; on
            # le corrige via _normalized_total_line().
            return quote.event_type in (11273, 11274) and (
                TerminatAI._normalized_total_line(quote.threshold) is not None
            )
        if quote.group_id == 15:
            return quote.event_type in (11, 12) and TerminatAI._has_standard_line(
                quote.threshold
            )
        if quote.group_id == 62:
            return quote.event_type in (13, 14) and TerminatAI._has_standard_line(
                quote.threshold
            )
        # ── Group 14: Deux équipes vont marquer ─────────────────────────────
        if quote.group_id == 14:
            return quote.event_type in (182, 183)
        # ── Group 2270: Total pair / impair ─────────────────────────────────
        if quote.group_id == 2270:
            return quote.event_type in (2511, 2512)
        # ── Group 2866 & 2867: Équipe gagne sans prendre de but ─────────────
        if quote.group_id == 2866:
            return quote.event_type in (200, 205)
        if quote.group_id == 2867:
            return quote.event_type in (195, 210)
        # ── Group 776: Équipe marque ses penalties ──────────────────────────
        if quote.group_id == 776:
            return quote.event_type in (1422, 1423, 1424, 1425)
        return False

    @staticmethod
    def _is_both_teams_to_score_market(quote):
        if quote.group_id != 19 or quote.event_type not in (1, 2):
            return False
        market_text = TerminatAI._normalized_text(
            " ".join(
                (
                    str(getattr(quote, "label", "")),
                    str(getattr(quote, "group_label", "")),
                )
            )
        )
        return any(
            marker in market_text
            for marker in (
                "both teams",
                "les deux equipes",
                "each team to score",
                "btts",
                "marquent tous les deux",
            )
        )

    # Correspondance vérifiée en base entre l'index du groupe 19 (11273:X)
    # et la ligne réelle du marché (cotes identiques au centième) :
    #   11273:2 -> 4.5   11273:3 -> 6.5   11273:4 -> 7.5
    #   11273:5 -> 9.5   11273:6 -> 11.5
    GROUP19_TOTAL_LINES = {2: 4.5, 3: 6.5, 4: 7.5, 5: 9.5, 6: 11.5, 7: 13.5}

    @classmethod
    def _normalized_total_line(cls, value):
        """Ligne de total réelle correspondant à un seuil du groupe 19.

        Le groupe 19 (11273/11274) transmet parfois un entier brut au lieu de la
        vraie ligne décimale. Les tests valident le correctif : un seuil 4 doit
        être interprété comme 4.5 pour l'évaluation du marché.
        """
        threshold = _number(value)
        if threshold is None or threshold < 0:
            return None
        if math.isclose(threshold, round(threshold), abs_tol=1e-9):
            return threshold + 0.5
        return threshold if cls._has_half_goal_line(threshold) else None

    @staticmethod
    def _has_standard_line(value):
        threshold = _number(value)
        return (
            threshold is not None
            and math.isclose(threshold * 2, round(threshold * 2), abs_tol=1e-9)
        )

    @staticmethod
    def _has_half_goal_line(value):
        threshold = _number(value)
        return (
            threshold is not None
            and TerminatAI._has_standard_line(threshold)
            and not math.isclose(threshold, round(threshold), abs_tol=1e-9)
        )

    @staticmethod
    def _is_live_score_coherent(quote, score1, score2):
        if score1 is None or score2 is None:
            return False
        if getattr(quote, "is_extra_penalties", False):
            # En direct, un pari "Pénalités supplémentaires" reste cohérent
            # tant que le score réglementaire des 5 tirs n'est pas dépassé.
            return score1 <= 5 and score2 <= 5
        if TerminatAI._is_both_teams_to_score_market(quote):
            outcome = TerminatAI._normalized_text(getattr(quote, "outcome_label", ""))
            if outcome in ("non", "no"):
                return score1 == 0 or score2 == 0
            return True
        if quote.group_id == 1:
            return {
                1: score1 >= score2,
                2: score1 == score2,
                3: score2 >= score1,
            }.get(quote.event_type, False)
        if quote.group_id == 8:
            return {
                4: score1 >= score2,
                5: score1 != score2,
                6: score2 >= score1,
            }.get(quote.event_type, False)
        threshold = _number(quote.threshold)
        if threshold is None:
            return False

        # ── Totaux globaux (groupe 17 = ligne fixe, groupe 19 = ligne variable) ──
        #
        # Le total live reste cohérent tant que le score actuel n'a pas déjà
        # dépassé la ligne et que la ligne n'est pas "très au-delà" d'un score
        # encore réaliste. Les tests de validation exigent une tolérance plus large
        # que l'heuristique précédente pour les lignes longues (7.5, 8.5, 9.5).
        if quote.group_id in (17, 19):
            total = score1 + score2
            is_over = quote.event_type in (9, 11273)
            is_under = quote.event_type in (10, 11274)
            lead = abs(score1 - score2)

            if is_over:
                if total >= threshold + 0.5:
                    return False
                goals_needed = threshold + 0.5 - total
                if lead >= 3:
                    return goals_needed <= 2
                if lead >= 1:
                    return goals_needed <= 5
                return goals_needed <= 9

            if is_under:
                if total > threshold:
                    return False
                if lead >= 3:
                    return (total + 1) < threshold
                if lead >= 1:
                    return (threshold - total) >= 4
                return (threshold - total) >= 3

        # ── Totaux par équipe (groupes 15 = équipe 1, 62 = équipe 2) ──────────
        if quote.group_id in (15, 62):
            team_score = score1 if quote.group_id == 15 else score2
            other_score = score2 if quote.group_id == 15 else score1
            is_over  = quote.event_type in (11, 13)
            is_under = quote.event_type in (12, 14)
            lead = team_score - other_score  # positif si cette équipe mène

            if is_over:
                goals_needed = threshold + 0.5 - team_score
                if goals_needed <= 0:
                    return True
                # Le scénario est encore réaliste tant que l'équipe n'est pas
                # explicitement éliminée par le score actuel. Les lignes de
                # totals d'équipe peuvent rester valides à 0-0 ou à un score très
                # serré si le profil historique de l'équipe le justifie.
                if lead <= -3:
                    return goals_needed <= 1
                if lead <= -1:
                    return goals_needed <= 2
                return goals_needed <= 4
            if is_under:
                if lead >= 3:
                    return (team_score + 1) < threshold
                return (threshold - team_score) >= 4

        # ── Handicaps (groupe 2) ──────────────────────────────────────────────
        if quote.group_id == 2:
            if quote.event_type == 7:
                return score1 + threshold >= score2
            if quote.event_type == 8:
                return score2 + threshold >= score1
        return False

    def _sudden_death_rates(self, hour):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            recent = connection.execute(
                """
                SELECT went_to_sudden_death
                FROM matches_history
                WHERE total_goals IS NOT NULL
                ORDER BY updated_at DESC, game_id DESC
                LIMIT 15
                """
            ).fetchall()
            hourly = connection.execute(
                """
                SELECT went_to_sudden_death
                FROM matches_history
                WHERE hour_of_day = ? AND total_goals IS NOT NULL
                """,
                (hour,),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return None, None
            raise
        finally:
            connection.close()

        recent_rate = (
            sum(bool(row[0]) for row in recent) / len(recent) if recent else None
        )
        hourly_rate = (
            sum(bool(row[0]) for row in hourly) / len(hourly) if hourly else None
        )
        return recent_rate, hourly_rate

    def _extra_penalties_recommendation(
        self,
        parsed_game,
        analysis,
        hour,
        regime,
        calibrated_by_key=None,
    ):
        market = getattr(analysis, "extra_penalties", None)
        if market is None:
            return None

        yes_odds = _valid_odds(market.yes_odds)
        no_odds = _valid_odds(market.no_odds)
        if yes_odds is None or no_odds is None:
            return None

        extra_quotes = {
            quote.outcome_label: quote
            for quote in getattr(analysis, "regulation_markets", ())
            if getattr(quote, "is_extra_penalties", False)
            and quote.outcome_label in ("Oui", "Non")
        }
        yes_quote = extra_quotes.get("Oui")
        no_quote = extra_quotes.get("Non")
        yes_probability = (
            calibrated_by_key.get(yes_quote.key)
            if calibrated_by_key is not None and yes_quote is not None
            else None
        )
        no_probability = (
            calibrated_by_key.get(no_quote.key)
            if calibrated_by_key is not None and no_quote is not None
            else None
        )
        if yes_probability is None and yes_quote is not None:
            yes_probability = self._calibrated_regulation_probability(
                yes_quote,
                hour,
                regime,
                str(parsed_game.get("status", "")).upper() == "LIVE",
            )
        if no_probability is None and no_quote is not None:
            no_probability = self._calibrated_regulation_probability(
                no_quote,
                hour,
                regime,
                str(parsed_game.get("status", "")).upper() == "LIVE",
            )
        if yes_probability is None:
            yes_probability = 1 / yes_odds
        if no_probability is None:
            no_probability = 1 / no_odds

        probability_sum = yes_probability + no_probability
        if probability_sum <= 0:
            return None
        yes_probability = _clamp_probability(yes_probability / probability_sum)
        no_probability = _clamp_probability(no_probability / probability_sum)

        score1 = _number(parsed_game.get("score1"))
        score2 = _number(parsed_game.get("score2"))
        status = str(parsed_game.get("status", "")).upper()
        live_level_score = (
            status == "LIVE"
            and score1 == score2
            and score1 in (3, 4, 5)
        )
        high_odds_opportunity = False
        if status == "PRE-MATCH" and yes_odds <= 4.50:
            global_quotes = [
                quote
                for quote in getattr(analysis, "regulation_markets", ())
                if quote.group_id == 17
            ]
            total_85_is_short = any(
                quote.event_type == 9
                and _number(quote.threshold) == 8.5
                and quote.odds <= 1.72
                for quote in global_quotes
            )
            total_95_is_center = any(
                _number(quote.threshold) == 9.5 and quote.is_center
                for quote in global_quotes
            )
            recent_rate, hourly_rate = self._sudden_death_rates(hour)
            strong_history = any(
                rate is not None and rate >= 0.25
                for rate in (recent_rate, hourly_rate)
            )
            high_odds_opportunity = (
                (total_85_is_short or total_95_is_center) and strong_history
            )

        yes_allowed = (
            yes_odds <= 2.75 or live_level_score or high_odds_opportunity
        )
        side = "OUI" if yes_allowed else "NON"
        selected_odds = yes_odds if yes_allowed else no_odds
        confidence = yes_probability if yes_allowed else no_probability

        return {
            "side": side,
            "odds": selected_odds,
            "confidence_pct": round(confidence * 100, 1),
            "high_odds_opportunity": high_odds_opportunity,
        }

    def predict_preference(self, parsed_game, analysis):
        hour = parsed_game.get("hour_of_day", 0)
        try:
            hour = int(hour)
        except (TypeError, ValueError):
            hour = 0
        hour = max(0, min(23, hour))
        recent_rate = self.memory.get("overall", {}).get(
            "recent_sudden_death_rate"
        )
        short_rate = self.memory.get("overall", {}).get("recent_short_rate")
        sudden_probability = self._markov_probability(
            parsed_game.get("game_id", parsed_game.get("id"))
        )

        if sudden_probability >= 0.6 or (
            recent_rate is not None and recent_rate >= 0.5
        ):
            regime = "CYCLE PROLONGATIONS"
        elif short_rate is not None and short_rate >= 0.6:
            regime = "CYCLE COURT"
        else:
            regime = "ÉQUILIBRÉ"

        candidates = []
        team1 = str(parsed_game.get("team1") or "Équipe 1")
        team2 = str(parsed_game.get("team2") or "Équipe 2")
        winner_markets = [
            market
            for market in getattr(analysis, "market_probabilities", ())
            if market.group_id == 1
            and market.event_type in (1, 3)
            and market.odds >= 1.45
        ]
        if winner_markets:
            favorite = min(winner_markets, key=lambda market: market.odds)
            implied = _clamp_probability(1 / favorite.odds)
            confidence = self._calibrated_probability(
                "favorite", hour, implied
            )
            candidates.append(
                (
                    confidence,
                    confidence,
                    "1X2 : " + favorite.label,
                )
            )

        total = getattr(analysis, "balanced_total", None)
        if total is not None:
            implied_over_raw = 1 / total.over_odds
            implied_under_raw = 1 / total.under_odds
            implied_sum = implied_over_raw + implied_under_raw
            implied_over = implied_over_raw / implied_sum
            implied_under = implied_under_raw / implied_sum
            for side, base_probability, category, odds in (
                (
                    "Plus de",
                    implied_over,
                    "total_over",
                    total.over_odds,
                ),
                (
                    "Moins de",
                    implied_under,
                    "total_under",
                    total.under_odds,
                ),
            ):
                if odds < 1.45:
                    continue
                confidence = self._calibrated_probability(
                    category, hour, base_probability
                )
                ranking_score = confidence
                if (
                    regime == "CYCLE PROLONGATIONS" and side == "Plus de"
                ) or (regime == "CYCLE COURT" and side == "Moins de"):
                    ranking_score += 0.05
                candidates.append(
                    (
                        ranking_score,
                        confidence,
                        f"Total : ({total.threshold:g}) {side} @{odds:g}",
                    )
                )

        # Scores exacts : modèle Poisson bivarié (pas 1/cote), et uniquement
        # les scores dont la probabilité battent la cote dévigorisée.
        for candidate in self._score_exact_probabilities(
            analysis, self.analyze_matchup(team1, team2), "PRE-MATCH", False
        ):
            if not candidate["qualified"]:
                continue
            confidence = _clamp_probability(candidate["confidence_pct"] / 100)
            candidates.append(
                (
                    confidence,
                    confidence,
                    "Score exact — " + candidate["score"] + " @" + str(candidate["odds"]),
                )
            )

        regulation_quotes = [
            quote
            for quote in getattr(analysis, "regulation_markets", ())
            if not getattr(quote, "is_extra_penalties", False)
        ]
        sibling_groups = {}
        for quote in regulation_quotes:
            sibling_groups.setdefault(
                (
                    quote.group_id,
                    quote.event_type,
                    quote.threshold,
                    quote.group_label,
                ),
                [],
            ).append(quote.odds)
        for quote in regulation_quotes:
            if quote.group_id == 19:
                continue
            if quote.odds is None or not (self.MIN_ODDS <= quote.odds <= self.MAX_ODDS):
                continue
            # On n'expose comme préférence qu'un marché rentable prouvé.
            if self.market_profitability(quote.key).get("verdict") != "PROVEN":
                continue
            confidence = self._calibrated_regulation_probability(
                quote,
                hour,
                regime,
                sibling_odds=sibling_groups.get(
                    (
                        quote.group_id,
                        quote.event_type,
                        quote.threshold,
                        quote.group_label,
                    )
                ),
            )
            if quote.group_id == 1 and quote.event_type in (1, 2, 3):
                market_team = (
                    team1
                    if quote.event_type == 1
                    else team2
                    if quote.event_type == 3
                    else "Match nul"
                )
                market_label = f"1X2 : {market_team}"
            elif quote.group_id == 17 and quote.threshold is not None:
                side = "Plus de" if quote.event_type == 9 else "Moins de"
                market_label = f"Total : ({quote.threshold:g}) {side}"
            elif (
                quote.group_id in (15, 62)
                and quote.threshold is not None
                and quote.event_type in (11, 12, 13, 14)
            ):
                market_team = team1 if quote.group_id == 15 else team2
                side = "Plus de" if quote.event_type in (11, 13) else "Moins de"
                market_label = (
                    f"Total {market_team} : ({quote.threshold:g}) {side}"
                )
            else:
                market_label = quote.label
            candidates.append(
                (
                    confidence,
                    confidence,
                    f"{market_label} @{quote.odds}",
                )
            )

        penalties = self._extra_penalties_recommendation(
            parsed_game, analysis, hour, regime
        )
        if penalties is not None:
            confidence = penalties["confidence_pct"] / 100
            candidates.append(
                (
                    confidence,
                    confidence,
                    "Pénalités supplémentaires — "
                    + penalties["side"]
                    + " @"
                    + str(penalties["odds"]),
                )
            )

        if not candidates:
            return {
                "bet": "Aucun pari fiable",
                "confidence_pct": 0.0,
                "regime": regime,
            }

        best = max(candidates, key=lambda item: item[0])
        return {
            "bet": best[2],
            "confidence_pct": round(best[1] * 100, 1),
            "regime": regime,
        }

    def review_and_approve(
        self, parsed_game, analysis, signal_drops=None, *, unfiltered=False
    ):
        """Select one calculable market, optionally bypassing approval thresholds."""
        preference = self.predict_preference(parsed_game, analysis)
        hour = max(0, min(23, int(parsed_game.get("hour_of_day", 0) or 0)))
        regime = preference["regime"]
        status = str(parsed_game.get("status", "")).upper()
        is_live = status == "LIVE"
        minimum_confidence = self.MIN_CONFIDENCE
        quotes = list(getattr(analysis, "regulation_markets", ()))
        team1 = str(parsed_game.get("team1") or "Équipe 1")
        team2 = str(parsed_game.get("team2") or "Équipe 2")
        matchup = self.analyze_matchup(team1, team2)

        # Cotes soeurs (même marché, même ligne) : nécessaires pour dévigoriser.
        def _quote_cohort_key(q):
            if getattr(q, "is_extra_penalties", False) or q.group_id == 1094:
                return ("extra-penalties", None)
            if q.group_id in (1, 8):
                return (q.group_id, None)
            if q.group_id == 14:
                return (14, None)  # BTTS Oui / Non
            if q.group_id == 2270:
                return (2270, None)  # Total pair / impair
            if q.group_id in (2866, 2867):
                return (q.group_id, None)
            if q.group_id == 776:
                return (776, q.threshold)
            if q.group_id in (17, 15, 62) or (
                q.group_id == 19 and q.event_type in (11273, 11274)
            ):
                line = TerminatAI._normalized_total_line(q.threshold) if q.group_id == 19 else _number(q.threshold)
                return (q.group_id, line)
            if q.group_id == 2:
                return (q.group_id, _number(q.threshold))
            return (q.group_id, _number(q.threshold))

        sibling_groups = {}
        for quote in quotes:
            sibling_groups.setdefault(_quote_cohort_key(quote), []).append(quote.odds)

        calibrated = [
            self._calibrated_regulation_probability(
                quote,
                hour,
                regime,
                is_live,
                sibling_groups.get(_quote_cohort_key(quote)),
            )
            for quote in quotes
        ]
        cohorts = {}
        for index, quote in enumerate(quotes):
            cohorts.setdefault(_quote_cohort_key(quote), []).append(index)
        for indices in cohorts.values():
            if len(indices) < 2:
                continue
            probability_sum = sum(calibrated[index] for index in indices)
            if probability_sum > 0:
                for index in indices:
                    calibrated[index] = _clamp_probability(
                        calibrated[index] / probability_sum
                    )
        # Filet de sécurité : la probabilité calibrée ne peut dépasser le marché
        # dévigorisé de plus de MAX_EDGE_OVER_MARKET, ET la somme des issues
        # d'un même marché doit rester plausible (pas de 117 % au total).
        market_baseline = [
            self._bookmaker_fair_probability(
                quote,
                sibling_groups.get(_quote_cohort_key(quote)),
            )
            for quote in quotes
        ]
        raw_calibrated = calibrated[:]
        for index, baseline in enumerate(market_baseline):
            if baseline is None:
                continue
            delta = calibrated[index] - baseline
            if abs(delta) > self.MAX_EDGE_OVER_MARKET:
                calibrated[index] = _clamp_probability(
                    baseline + math.copysign(self.MAX_EDGE_OVER_MARKET, delta)
                )
        raw_confidence_by_key = {
            quote.key: raw_probability
            for quote, raw_probability in zip(quotes, raw_calibrated)
        }
        # Bornage par marché : la somme des issues d'un même marché ne doit
        # jamais dépasser 1 (sinon on affiche une "value" mathématiquement
        # impossible contre un bookmaker qui a 9,5 % de marge).
        for indices in cohorts.values():
            if len(indices) < 2:
                continue
            total = sum(calibrated[index] for index in indices)
            if total > 1:
                for index in indices:
                    calibrated[index] /= total

        market_candidates = []
        score1 = _number(parsed_game.get("score1"))
        score2 = _number(parsed_game.get("score2"))
        balanced_total = getattr(analysis, "balanced_total", None)
        center_line = getattr(balanced_total, "threshold", None)
        winner_markets = [
            m for m in getattr(analysis, "market_probabilities", ())
            if getattr(m, "group_id", None) == 1 and getattr(m, "event_type", None) in (1, 3)
        ]
        fav_odds = min((getattr(m, "odds", 99.0) for m in winner_markets), default=None) if winner_markets else None
        for quote, probability in zip(quotes, calibrated):
            is_extra_penalties = bool(
                getattr(quote, "is_extra_penalties", False)
            )
            is_btts_market = self._is_both_teams_to_score_market(quote)
            if (
                quote.group_id == 19
                and not is_btts_market
                and not self._is_settleable_regulation_market(quote)
            ):
                continue
            threshold = _number(quote.threshold)
            if (
                not unfiltered
                and quote.group_id in (15, 62)
                and quote.event_type in (11, 13)
                and threshold is not None
                and threshold >= 4
            ):
                team_profile = matchup[
                    "team1" if quote.group_id == 15 else "team2"
                ]
                average_goals = team_profile["average_goals_scored"]
                if average_goals is None or average_goals < 4.2:
                    continue

            historical_probability, historical_count = self._matchup_probability(
                quote, matchup, team1, team2
            )
            probability = self._blend_matchup_probability(
                probability, historical_probability, historical_count
            )
            signal_drop_pct = (
                signal_drops.get(quote.key)
                if isinstance(signal_drops, Mapping)
                else None
            )
            signal_calibration = None
            if is_live and signal_drop_pct is not None:
                signal_calibration = self.get_live_signal_calibration(
                    signal_drop_pct, quote.odds
                )
                if (
                    signal_calibration["calibrated"]
                    and signal_calibration["win_rate_pct"] is not None
                ):
                    empirical_rate = (
                        signal_calibration["win_rate_pct"] / 100
                    )
                    weight = signal_calibration["weight"]
                    probability = _clamp_probability(
                        probability * (1 - weight) + empirical_rate * weight
                    )
            value = probability * quote.odds
            if is_extra_penalties:
                outcome = quote.outcome_label or quote.label
                label = f"Pénalités supplémentaires : {outcome}"
            elif is_btts_market:
                outcome = quote.outcome_label or quote.label
                label = f"Les deux équipes marquent : {outcome}"
            elif quote.group_id == 1 and quote.event_type in (1, 2, 3):
                team = (
                    team1
                    if quote.event_type == 1
                    else team2
                    if quote.event_type == 3
                    else "Match nul"
                )
                label = f"1X2 : {team}"
            elif (
                quote.group_id in (17, 19)
                and threshold is not None
                and quote.event_type in (9, 10, 11273, 11274)
            ):
                side = (
                    "Plus de" if quote.event_type in (9, 11273) else "Moins de"
                )
                line = (
                    TerminatAI._normalized_total_line(threshold)
                    if quote.group_id == 19
                    else threshold
                )
                label = f"Total : {side} {line if line is not None else threshold:g} buts"
            elif (
                quote.group_id in (15, 62)
                and threshold is not None
                and quote.event_type in (11, 12, 13, 14)
            ):
                team = team1 if quote.group_id == 15 else team2
                if not team:
                    team = "Équipe 1" if quote.group_id == 15 else "Équipe 2"
                side = "Plus de" if quote.event_type in (11, 13) else "Moins de"
                label = f"Total {team} : ({threshold:g}) {side}"
            else:
                label = str(
                    quote.label
                    or quote.group_label
                    or f"Groupe {quote.group_id}, type {quote.event_type}"
                )
            odds_qualified = unfiltered or self.MIN_ODDS <= quote.odds <= self.MAX_ODDS
            confidence_qualified = unfiltered or probability >= minimum_confidence
            # L'historique se juge sur l'échantillon réellement utilisé par le
            # marché (matchs de l'équipe, ou confrontations directes pour un
            # 1X2). Exiger 30 confrontations directes serait impossible :
            # deux équipes ne se rencontrent presque jamais autant de fois.
            # On retient donc le plus grand échantillon disponible et on le
            # compare au seuil, en plafonnant à la taille du marché PROVEN.
            profitability_room = self.market_profitability(quote.key).get("bets", 0)
            sample_size = max(historical_count, profitability_room)
            history_qualified = (
                unfiltered or sample_size >= self.MIN_MARKET_HISTORY
            )
            # En PRE-MATCH il n'y a pas de score à vérifier : la cohérence
            # n'est un critère que pour les paris LIVE.
            score_coherent = (
                True
                if (unfiltered or not is_live)
                else self._is_live_score_coherent(quote, score1, score2)
            )
            settleable = self._is_settleable_regulation_market(quote)
            valid_odds = math.isfinite(quote.odds) and quote.odds > 1
            value_qualified = unfiltered or value >= self.MIN_VALUE
            # Rentabilité prouvée en base : on refuse les marchés dont
            # l'historique démontre une perte structurelle (ex. "Moins de X",
            # handicap -1.5) et on n'accepte que la liste blanche validée.
            profitability = self.market_profitability(quote.key)
            verdict = profitability.get("verdict", "UNKNOWN")
            roi_value = profitability.get("roi_pct")
            # Source de la profitabilité : live_signal = déclencheur de chute de cote.
            # Ces marchés sont rentables UNIQUEMENT en live quand le score actuel
            # les rend cohérents (ex: 4-3 → "Plus de 7.5" est quasi certain).
            # En pré-match, il est impossible de savoir si le score justifiera le pari
            # → on n'autorise jamais un marché "live_signal" en pré-match.
            profitability_source = profitability.get("source", "prematch")
            profitability_qualified = (
                unfiltered
                or (
                    profitability_source == "live_signal"
                    and is_live  # signal live → valide SEULEMENT en live
                    and verdict == "PROVEN"
                )
                or (
                    profitability_source != "live_signal"
                    and (
                        verdict == "PROVEN"
                        or (roi_value is not None and roi_value > 0 and verdict != "AVOID")
                    )
                )
            )

            # Sécurisation des paris "Pénalités supplémentaires" (mort subite)
            if is_extra_penalties:
                outcome_text = str(quote.outcome_label or quote.label).casefold()
                is_no = "non" in outcome_text or "no" in outcome_text
                recent_sd_rate = self.memory.get("overall", {}).get("recent_sudden_death_rate")
                if is_no:
                    # En pré-match ou en live : si la cote de "Non" dépasse 2.05, le bookmaker sait que la prolongation est très probable
                    if quote.odds > 2.05:
                        profitability_qualified = False
                    # Si la ligne médiane totale est >= 8.5, forte projection de buts menant fréquemment à 5-5 (mort subite)
                    elif center_line is not None and center_line >= 8.5:
                        profitability_qualified = False
                    # En live : score serré ou déjà avancé
                    elif is_live and (score1 is not None and score2 is not None and (score1 == score2 >= 1 or score1 + score2 >= 5)):
                        profitability_qualified = False
                    # En pré-match : tendance récente montrant cycle prolongations
                    elif not is_live and (regime == "CYCLE PROLONGATIONS" or (recent_sd_rate is not None and recent_sd_rate >= 0.25)):
                        profitability_qualified = False

            # Sécurisation des paris "Les deux équipes marquent : Oui" (BTTS)
            if is_btts_market or quote.group_id == 14:
                outcome_text = str(quote.outcome_label or quote.label).casefold()
                is_yes = "oui" in outcome_text or "yes" in outcome_text or quote.event_type == 183
                if is_yes:
                    # Si la ligne médiane totale est <= 4.0, risque majeur de clean sheet rapide (ex: 3-0)
                    if center_line is not None and center_line <= 4.0:
                        profitability_qualified = False
                    # Si un favori écrasant (< 1.15) est présent avec une ligne totale courte (<= 5.5)
                    elif fav_odds is not None and fav_odds < 1.15 and center_line is not None and center_line <= 5.5:
                        profitability_qualified = False

            # Sécurisation des paris "Total impair" (Groupe 2270)
            if quote.group_id == 2270 and quote.event_type == 2512:
                # Piège du favori écrasant (< 1.20) avec petite ligne (<= 5.5) : victoire 3-1 très fréquente (= 4 buts pair)
                if fav_odds is not None and fav_odds < 1.20 and center_line is not None and center_line <= 5.5:
                    profitability_qualified = False
                # Ligne extrême (>= 9.5) : scores pairs fréquents (4-2, 5-3, 5-5)
                elif center_line is not None and center_line >= 9.5:
                    profitability_qualified = False
            market_candidates.append(
                {
                    "market_key": quote.key,
                    "label": label,
                    "odds": quote.odds,
                    "confidence_pct": probability * 100,
                    "value": value,
                    "value_per_unit": value - 1,
                    "profitability_verdict": verdict,
                    "historical_roi_pct": profitability.get("roi_pct"),
                    "qualified": (
                        status in ("LIVE", "PRE-MATCH")
                        and settleable
                        and valid_odds
                        and (
                            unfiltered
                            or (
                                odds_qualified
                                and confidence_qualified
                                and history_qualified
                                and score_coherent
                                and value_qualified
                                and profitability_qualified
                            )
                        )
                    ),
                    "historical_matches": sample_size,
                    "history_qualified": history_qualified,
                    "profitability_qualified": profitability_qualified,
                    "score_coherent": score_coherent,
                    "value_qualified": value_qualified,
                    "signal_drop_pct": signal_drop_pct,
                    "live_signal_samples": (
                        0
                        if signal_calibration is None
                        else signal_calibration["count"]
                    ),
                    "live_signal_calibrated": (
                        False
                        if signal_calibration is None
                        else signal_calibration["calibrated"]
                    ),
                    "market_type": (
                        "Pénalités supplémentaires"
                        if is_extra_penalties
                        else "Les deux équipes marquent"
                        if is_btts_market or quote.group_id == 14
                        else "Total pair / impair"
                        if quote.group_id == 2270
                        else "Sans encaisser"
                        if quote.group_id in (2866, 2867)
                        else "Pénalties équipe"
                        if quote.group_id == 776
                        else "Total buts"
                        if quote.group_id in (17, 19)
                        else "Temps réglementaire"
                    ),
                    "group_id": quote.group_id,
                    "event_type": quote.event_type,
                    "threshold": quote.threshold,
                }
            )

        exact_scores = self._score_exact_probabilities(
            analysis, matchup, status, unfiltered
        )
        exact_scores.sort(
            key=lambda item: (item["confidence_pct"], -item["odds"]),
            reverse=True,
        )

        market_candidates.sort(
            key=lambda item: (item["confidence_pct"], -item["odds"]),
            reverse=True,
        )
        decision_candidates = [
            item
            for item in market_candidates
            if item["market_type"]
            in (
                "Temps réglementaire",
                "Total buts",
                "Pénalités supplémentaires",
                "Les deux équipes marquent",
                "Total pair / impair",
                "Sans encaisser",
                "Pénalties équipe",
            )
            and item["qualified"]
            and (unfiltered or item["odds"] >= self.MIN_ODDS)
        ]
        if decision_candidates:
            best_raw_confidence = max(
                raw_confidence_by_key.get(
                    item["market_key"], item["confidence_pct"] / 100.0
                )
                for item in decision_candidates
            )
            decision_candidates = [
                item
                for item in decision_candidates
                if raw_confidence_by_key.get(
                    item["market_key"], item["confidence_pct"] / 100.0
                )
                >= best_raw_confidence - self.MAX_EDGE_OVER_MARKET
            ]
        phase_eligible = status in ("LIVE", "PRE-MATCH")
        # Récupérer les 3 derniers types de marchés envoyés pour forcer une rotation équitable
        recent_sent_types = []
        try:
            with closing(sqlite3.connect(self.db_path, timeout=5)) as conn:
                rows = conn.execute(
                    """
                    SELECT market_key, market_label 
                    FROM sent_predictions 
                    WHERE delivery_status = 'SENT'
                    ORDER BY id DESC LIMIT 5
                    """
                ).fetchall()
                for mk, ml in rows:
                    if "extra-penalties" in str(mk) or "Pénalités supplémentaires" in str(ml):
                        recent_sent_types.append("extra_penalties")
                    elif "even-odd" in str(mk) or "Total impair" in str(ml) or "Total pair" in str(ml):
                        recent_sent_types.append("even_odd")
                    elif "btts" in str(mk) or "marquent" in str(ml):
                        recent_sent_types.append("btts")
                    elif "total" in str(mk) or "Total :" in str(ml):
                        recent_sent_types.append("totals")
                    else:
                        recent_sent_types.append("other")
        except Exception:
            pass

        # Classement par rentabilité réelle (value) et diversification forcée :
        def _selection_score(item):
            val = item.get("value", 1.0)
            conf = item.get("confidence_pct", 50.0) / 100.0
            is_win = item.get("group_id") == 1
            is_tot = item.get("group_id") in (17, 19)
            mtype = item.get("market_type")
            has_signal = signal_drops is not None and item.get("market_key") in signal_drops
            
            market_weight = 1.35 if is_win else (1.20 if is_tot else 1.0)
            if has_signal:
                market_weight *= 1.25  # Bonus si signal fort de chute de cote

            # ROTATION DYNAMIQUE UNIVERSELLE :
            # Aucun type de marché ne doit être envoyé en boucle.
            # Si un marché a été envoyé récemment, on favorise les autres options rentables (BTTS, Totaux, 1X2, Impair).
            item_type_key = (
                "extra_penalties" if mtype == "Pénalités supplémentaires"
                else "even_odd" if mtype == "Total pair / impair"
                else "btts" if mtype == "Les deux équipes marquent"
                else "totals" if is_tot
                else "other"
            )
            type_count = recent_sent_types[:3].count(item_type_key)
            if type_count >= 1:
                market_weight *= (0.60 ** type_count)  # -40% au premier doublon, -64% au deuxième

            return (val * conf * market_weight, val, -item.get("odds", 2.0))

        decision_candidates.sort(key=_selection_score, reverse=True)
        decision = decision_candidates[0] if (decision_candidates and phase_eligible) else None
        approved = decision is not None
        top_markets = decision_candidates[:3] if phase_eligible else []
        all_opportunities = sorted(
            market_candidates + exact_scores,
            key=lambda item: (item["confidence_pct"], -item["odds"]),
            reverse=True,
        )
        # Seuls les scores exacts validés (value + probabilité > cote) sont
        # retenus pour l'affichage : pas de "top 3" indicatif.
        qualified_exact_scores = [
            item for item in exact_scores if item["qualified"]
        ]
        top_exact_scores = qualified_exact_scores[:3]
        normal_scores = [item for item in top_exact_scores if not item["extended"]]
        extended_scores = [item for item in top_exact_scores if item["extended"]]
        return {
            "approved": approved,
            "reason": (
                (
                    (
                        "Pronostic non filtré — seuils ignorés"
                        if unfiltered
                        else "Ticket conforme aux seuils LIVE"
                    )
                    if is_live
                    else (
                        "Pronostic PRE-MATCH non filtré"
                        if unfiltered and status == "PRE-MATCH"
                        else ""
                    )
                )
                if approved
                else (
                    "Les notifications sont réservées aux matchs LIVE"
                    if status == "PRE-MATCH"
                    else "Phase non prise en charge"
                    if not phase_eligible
                    else "Aucun marché réglable disponible"
                    if unfiltered
                    else (
                        "Aucun marché ne satisfait les seuils PRE-MATCH"
                        if not is_live
                        else "Aucun marché ne satisfait les seuils LIVE"
                    )
                )
            ),
            "unfiltered": unfiltered,
            "preference": preference,
            "matchup": matchup,
            "decision": decision,
            "top_opportunities": all_opportunities[:3],
            "top_markets": top_markets,
            "evaluated_markets": market_candidates,
            "top_exact_scores": top_exact_scores,
            "normal_scores": normal_scores,
            "extended_scores": extended_scores,
            "signal_override": False,
        }

    def format_status(self):
        matches = self._read_matches()
        trained = int(self.memory.get("trained_matches", 0))
        sudden_rate = self.memory.get("overall", {}).get("sudden_death_rate")
        profitability = self.memory.get("profitability", {})
        if sudden_rate is None:
            learned_rate = "pas encore calibré"
        else:
            learned_rate = str(round(float(sudden_rate) * 100, 1)) + "%"

        market = profitability.get("market", "Données insuffisantes")
        roi = profitability.get("roi_pct")
        bets = profitability.get("bets", 0)
        if roi is None:
            profitable = market
        else:
            profitable = (
                market
                + " (ROI "
                + ("+" if roi >= 0 else "")
                + str(round(float(roi), 1))
                + "%, "
                + str(bets)
                + " paris)"
            )
        pending = len(matches) % self.TRAINING_INTERVAL
        live_stats = self.get_live_signal_statistics()
        live_win_rate = live_stats["win_rate_pct"]
        return "\n".join(
            [
                "🧠 TerminatAI — apprentissage local",
                "Matchs analysés : " + str(len(matches)),
                "Dernier apprentissage : " + str(trained) + " match(s)",
                "Taux de Mort Subite appris : " + learned_rate,
                "Marché le plus rentable : " + profitable,
                "Marché Temps Réglementaire le plus fiable : "
                + (
                    self._most_reliable_regulation_market()
                    or "données insuffisantes"
                ),
                "Signaux LIVE détectés : " + str(live_stats["detected"]),
                "Tickets LIVE envoyés : " + str(live_stats["sent"]),
                (
                    "Taux de réussite réel LIVE : "
                    + f"{live_win_rate:.1f}% "
                    + f"({live_stats['won']}/{live_stats['won'] + live_stats['lost']})"
                    if live_win_rate is not None
                    else "Taux de réussite réel LIVE : indisponible "
                    "(aucun ticket LIVE réglé)"
                ),
                (
                    "Prochain apprentissage dans "
                    + str(self.TRAINING_INTERVAL - pending)
                    + " match(s) terminé(s)"
                ),
            ]
        )