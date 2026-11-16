import json
import sqlite3
import threading
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from collector import MatchHistoryRepository
from my_ai_brain import TerminatAI
from telegram_bot import (
    ExactScorePrediction,
    ExtraPenaltiesPrediction,
    GameAnalysis,
    MarketQuote,
    MarketProbability,
    TotalBalance,
)


def _mark_proven(brain, *market_keys):
    """Déclare des marchés comme rentables prouvés (filtre ROI réel)."""
    profitability = brain.memory.setdefault("market_profitability", {})
    for key in market_keys:
        profitability[key] = {
            "verdict": "PROVEN",
            "bets": 100,
            "wins": 80,
            "actual_rate": 0.80,
            "break_even_rate": 0.55,
            "wilson_lower_rate": 0.72,
            "roi_pct": 45.0,
        }


class TerminatAITests(unittest.TestCase):
    def test_classifies_markets_as_proven_only_with_statistical_proof(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        market_stats = {
            "proven": brain._empty_market_stat(),
            "losing": brain._empty_market_stat(),
            "noisy": brain._empty_market_stat(),
            "thin": brain._empty_market_stat(),
        }
        # Cote 3.0 => seuil de rentabilité 33 %.
        for index in range(100):
            brain._add_market_stat(market_stats["proven"], 3.0, index < 60)
            brain._add_market_stat(market_stats["losing"], 3.0, index < 25)
        # 40 % sur une cote 3.0 : ROI brut positif mais borne basse < seuil.
        for index in range(100):
            brain._add_market_stat(market_stats["noisy"], 3.0, index < 40)
        # Échantillon insuffisant malgré 100 % de réussite.
        for _ in range(5):
            brain._add_market_stat(market_stats["thin"], 2.0, True)
        for stat in market_stats.values():
            brain._finish_market_stat(stat)
        brain.memory["market_profitability"] = brain._build_market_profitability(
            {
                key: {"overall": stat, "by_hour": {}, "by_cycle": {}}
                for key, stat in market_stats.items()
            }
        )

        self.assertEqual(
            brain.market_profitability("proven")["verdict"], "PROVEN"
        )
        self.assertEqual(brain.market_profitability("losing")["verdict"], "AVOID")
        # Un ROI positif ne suffit pas : la borne IC95% doit rester au-dessus.
        self.assertEqual(brain.market_profitability("noisy")["verdict"], "NEUTRAL")
        self.assertEqual(brain.market_profitability("thin")["verdict"], "NEUTRAL")
        self.assertEqual(
            brain.market_profitability("unknown-key")["verdict"], "UNKNOWN"
        )

    def test_auto_training_retries_after_errors_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            brain = TerminatAI(
                Path(temp_dir) / "history.sqlite",
                Path(temp_dir) / "ai_memory.json",
            )
            retry_completed = threading.Event()
            attempts = 0

            def train_with_one_failure():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                retry_completed.set()
                return True

            with patch.object(brain, "maybe_train", side_effect=train_with_one_failure):
                brain.start_auto_training(check_interval=0.01)
                try:
                    self.assertTrue(retry_completed.wait(timeout=1))
                    self.assertEqual(
                        brain.last_auto_train_error,
                        "OperationalError('database is locked')",
                    )
                finally:
                    brain.stop_auto_training()
                    assert brain._auto_thread is not None
                    brain._auto_thread.join(timeout=1)

            self.assertFalse(brain._auto_thread.is_alive())

    def test_trains_every_ten_finished_matches_and_predicts_calibrated_preference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.sqlite"
            memory_file = Path(temp_dir) / "ai_memory.json"
            repository = MatchHistoryRepository(database)
            brain = TerminatAI(database, memory_file)

            with patch("collector.datetime") as mocked_datetime:
                mocked_datetime.now.return_value.hour = 8
                training_results = []
                for game_id in range(1, 11):
                    repository.record_match(
                        {
                            "game_id": game_id,
                            "team1": "Home",
                            "team2": "Away",
                            "status": "PRE-MATCH",
                            "score1": 0,
                            "score2": 0,
                        },
                        fav_team="Home",
                        fav_odds=1.5,
                        center_total_line=7.5,
                        over_odds=2.0,
                        under_odds=2.0,
                        cycle_regime="CYCLE COURT",
                        regulation_markets=[
                            {
                                "key": "winner:1",
                                "group_id": 1,
                                "event_type": 1,
                                "label": "Home",
                                "odds": 1.5,
                            },
                            {
                                "key": "double-chance:4",
                                "group_id": 8,
                                "event_type": 4,
                                "label": "1X",
                                "odds": 1.2,
                            },
                            {
                                "key": "total:9:6.5",
                                "group_id": 17,
                                "event_type": 9,
                                "threshold": 6.5,
                                "label": "Plus de 6.5 buts",
                                "odds": 2.0,
                            },
                            {
                                "key": "team-total:1:11:3.5",
                                "group_id": 15,
                                "event_type": 11,
                                "threshold": 3.5,
                                "label": "Total 1 Plus 3.5",
                                "odds": 2.0,
                            },
                            {
                                "key": "team-total:2:14:3.5",
                                "group_id": 62,
                                "event_type": 14,
                                "threshold": 3.5,
                                "label": "Total 2 Moins 3.5",
                                "odds": 2.0,
                            },
                            {
                                "key": "btts:yes",
                                "group_id": 19,
                                "event_type": 1,
                                "label": "Oui",
                                "group_label": "Les deux équipes marquent",
                                "outcome_label": "Oui",
                                "odds": 1.7,
                            },
                            {
                                "key": "extra-penalties:Oui",
                                "group_id": 77,
                                "event_type": 1,
                                "label": "Oui",
                                "outcome_label": "Oui",
                                "is_extra_penalties": True,
                                "odds": 2.0,
                            },
                        ],
                    )
                    repository.record_match(
                        {
                            "game_id": game_id,
                            "team1": "Home",
                            "team2": "Away",
                            "status": "FINISHED",
                            "score1": 4,
                            "score2": 3,
                        },
                        fav_team="Home",
                        fav_odds=1.5,
                        center_total_line=7.5,
                        over_odds=2.0,
                        under_odds=2.0,
                    )
                    training_results.append(brain.maybe_train())

            self.assertEqual(training_results, [False] * 9 + [True])
            self.assertTrue(memory_file.is_file())
            with memory_file.open(encoding="utf-8") as file:
                memory = json.load(file)

            self.assertEqual(memory["trained_matches"], 10)
            self.assertEqual(memory["hourly"]["favorite"]["8"]["actual_rate"], 1.0)
            self.assertEqual(memory["hourly"]["total_under"]["8"]["actual_rate"], 1.0)
            self.assertEqual(memory["markov"]["000"]["count"], 7)
            self.assertEqual(memory["profitability"]["market"], "Favoris 1X2")
            markets = memory["regulation_markets"]
            self.assertEqual(markets["winner:1"]["overall"]["actual_rate"], 1.0)
            self.assertEqual(markets["winner:1"]["by_hour"]["8"]["bets"], 10)
            self.assertEqual(
                markets["winner:1"]["by_cycle"]["CYCLE COURT"]["bets"], 10
            )
            self.assertEqual(
                markets["double-chance:4"]["overall"]["actual_rate"], 1.0
            )
            self.assertEqual(
                markets["total:9:6.5"]["overall"]["roi_pct"], 100.0
            )
            self.assertEqual(
                markets["team-total:1:11:3.5"]["overall"]["actual_rate"], 1.0
            )
            self.assertEqual(markets["btts:yes"]["overall"]["actual_rate"], 1.0)
            self.assertEqual(
                markets["extra-penalties:Oui"]["overall"]["actual_rate"], 0.0
            )
            self.assertIn(
                "Marché Temps Réglementaire validé (échantillon suffisant)",
                brain.format_training_summary(),
            )
            self.assertIn(
                "minimum 20 paris réglés et ROI positif",
                brain.format_training_summary(),
            )
            self.assertLess(brain._wilson_lower_bound(4, 4), 0.60)
            self.assertGreater(brain._wilson_lower_bound(20, 20), 0.80)

            analysis = GameAnalysis(
                exact_scores=(ExactScorePrediction((4, 3), 1.8, 55.6),),
                balanced_total=TotalBalance(
                    threshold=7.5,
                    over_odds=2.0,
                    under_odds=2.0,
                    over_probability_pct=50.0,
                    under_probability_pct=50.0,
                    selected_side="Plus de",
                    selected_odds=2.0,
                    selected_probability_pct=50.0,
                ),
                market_probabilities=(
                    MarketProbability(1, 1, "Home", 1.5, 66.7),
                    MarketProbability(1, 3, "Away", 2.5, 40.0),
                ),
                extra_penalties=ExtraPenaltiesPrediction(
                    2.0, 2.0, "Oui", 2.0
                ),
                regulation_markets=(
                    MarketQuote(
                        key="double-chance:4",
                        label="1X",
                        group_id=8,
                        event_type=4,
                        odds=1.2,
                    ),
                ),
            )
            preference = brain.predict_preference(
                {
                    "game_id": 100,
                    "hour_of_day": 8,
                    "score1": 1,
                    "score2": 1,
                },
                analysis,
            )

        self.assertEqual(preference["regime"], "CYCLE COURT")
        self.assertIn("1X2 : Home", preference["bet"])
        self.assertNotIn("@1.2", preference["bet"])
        self.assertGreaterEqual(preference["confidence_pct"], 0)
        self.assertLessEqual(preference["confidence_pct"], 100)

    def test_trains_exact_score_odds_from_settled_database_predictions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.sqlite"
            memory_file = Path(temp_dir) / "ai_memory.json"
            repository = MatchHistoryRepository(database)
            games = (
                (101, (4, 3)),
                (102, (3, 4)),
            )
            for game_id, final_score in games:
                match = {
                    "game_id": game_id,
                    "team1": "Home",
                    "team2": "Away",
                    "status": "PRE-MATCH",
                }
                repository.record_exact_score_predictions(
                    match,
                    (
                        ExactScorePrediction((4, 3), 3.0, 33.3),
                        ExactScorePrediction((5, 4), 5.0, 20.0),
                    ),
                )
                repository.record_match(
                    {
                        **match,
                        "status": "FINISHED",
                        "score1": final_score[0],
                        "score2": final_score[1],
                    },
                    fav_team=None,
                    fav_odds=None,
                    center_total_line=None,
                    over_odds=None,
                    under_odds=None,
                )
                repository.settle_exact_score_predictions(
                    {
                        "game_id": game_id,
                        "status": "FINISHED",
                        "score1": final_score[0],
                        "score2": final_score[1],
                    }
                )

            brain = TerminatAI(database, memory_file)
            score_stats = brain.memory["exact_score_calibration"]["by_score"][
                "4-3"
            ]
            calibrated_probability = brain._calibrated_exact_score_probability(
                1 / 3,
                ExactScorePrediction((4, 3), 3.0, 33.3),
                "PRE-MATCH",
            )

        self.assertEqual(score_stats["bets"], 2)
        self.assertEqual(score_stats["wins"], 1)
        self.assertEqual(score_stats["actual_rate"], 0.5)
        self.assertEqual(brain.memory["trained_exact_score_predictions"], 4)
        self.assertGreater(calibrated_probability, 1 / 3)
        self.assertIn(
            "Scores exacts appris : 4 cotes réglées",
            brain.format_training_summary(),
        )

    def test_training_excludes_predictions_not_displayed_by_the_ai(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.sqlite"
            memory_file = Path(temp_dir) / "ai_memory.json"
            repository = MatchHistoryRepository(database)
            match = {
                "game_id": 103,
                "team1": "Home",
                "team2": "Away",
                "status": "PRE-MATCH",
            }
            repository.record_exact_score_predictions(
                match, (ExactScorePrediction((4, 3), 3.0, 33.3),)
            )
            repository.settle_exact_score_predictions(
                {
                    "game_id": 103,
                    "status": "FINISHED",
                    "score1": 4,
                    "score2": 3,
                }
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    UPDATE exact_score_predictions
                    SET ai_displayed = 0
                    WHERE game_id = '103'
                    """
                )

            brain = TerminatAI(database, memory_file)

        self.assertEqual(brain.memory["trained_exact_score_predictions"], 0)
        self.assertNotIn(
            "4-3",
            brain.memory["exact_score_calibration"].get("by_score", {}),
        )

    def test_status_reports_waiting_when_fewer_than_ten_matches_are_finished(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.sqlite"
            memory_file = Path(temp_dir) / "ai_memory.json"
            repository = MatchHistoryRepository(database)
            repository.record_match(
                {
                    "game_id": 1,
                    "team1": "Home",
                    "team2": "Away",
                    "status": "FINISHED",
                    "score1": 3,
                    "score2": 2,
                },
                fav_team=None,
                fav_odds=None,
                center_total_line=None,
                over_odds=None,
                under_odds=None,
            )
            brain = TerminatAI(database, memory_file)

            status = brain.format_status()

        self.assertIn("Matchs analysés : 1", status)
        self.assertIn("pas encore calibré", status)
        self.assertIn("apprentissage dans 9 match(s)", status)

    def test_analyze_matchup_orients_team_form_and_head_to_head_scores(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.sqlite"
            repository = MatchHistoryRepository(database)
            for game_id, first, second, score1, score2 in (
                (1, "Home", "Away", 5, 5),
                (2, "Away", "Home", 2, 4),
            ):
                repository.record_match(
                    {
                        "game_id": game_id,
                        "team1": first,
                        "team2": second,
                        "status": "FINISHED",
                        "score1": score1,
                        "score2": score2,
                    },
                    fav_team=None,
                    fav_odds=None,
                    center_total_line=None,
                    over_odds=None,
                    under_odds=None,
                )
            brain = TerminatAI(
                database,
                Path(temp_dir) / "ai_memory.json",
            )

            matchup = brain.analyze_matchup("Home", "Away")

        self.assertEqual(matchup["team1"]["matches"], 2)
        self.assertEqual(matchup["team1"]["average_goals_scored"], 4.5)
        self.assertEqual(matchup["team1"]["win_rate_pct"], 50.0)
        self.assertEqual(matchup["team2"]["average_goals_scored"], 3.5)
        self.assertEqual(matchup["h2h"]["matches_played"], 2)
        self.assertEqual(matchup["h2h"]["average_score_team1"], 4.5)
        self.assertEqual(matchup["h2h"]["average_score_team2"], 3.5)
        self.assertEqual(matchup["h2h"]["sudden_death_rate_pct"], 50.0)

    def test_review_drops_exact_scores_without_value(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(ExactScorePrediction((1, 0), 1.4, 71.4),),
            balanced_total=None,
            regulation_markets=(
                MarketQuote("winner:1", "Home", 1, 1, 1.44),
            ),
        )

        review = brain.review_and_approve(
            {"game_id": 1, "hour_of_day": 9}, analysis
        )

        self.assertFalse(review["approved"])
        self.assertEqual(review["top_markets"], [])
        # Un score exact dont la cote ne bat pas le marché n'est jamais exposé.
        self.assertEqual(review["top_exact_scores"], [])
        self.assertEqual(review["normal_scores"], [])

    def test_prematch_review_requires_70_percent_confidence_and_history(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(
                MarketQuote("winner:1", "Home", 1, 1, 1.47),
            ),
        )

        review = brain.review_and_approve(
            {"game_id": 7, "status": "PRE-MATCH", "hour_of_day": 9},
            analysis,
        )

        self.assertFalse(review["approved"])
        self.assertEqual(
            review["reason"],
            "Les notifications sont réservées aux matchs LIVE",
        )

    def test_prematch_never_approves_even_at_qualified_odds_and_confidence(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        for odds in (1.40, 2.05):
            with self.subTest(odds=odds):
                quote = MarketQuote("winner:1", "Home", 1, 1, odds)
                analysis = GameAnalysis(
                    exact_scores=(),
                    balanced_total=None,
                    regulation_markets=(quote,),
                )
                with (
                    patch.object(
                        brain,
                        "_matchup_probability",
                        return_value=(0.70, 100),
                    ),
                    patch.object(
                        brain,
                        "_calibrated_regulation_probability",
                        return_value=0.70,
                    ),
                ):
                    review = brain.review_and_approve(
                        {
                            "game_id": 70,
                            "status": "PRE-MATCH",
                            "hour_of_day": 9,
                        },
                        analysis,
                    )

                self.assertFalse(review["approved"])
                self.assertIsNone(review["decision"])
                self.assertEqual(review["top_markets"], [])

    def test_unfiltered_prematch_bypasses_odds_confidence_and_history_gates(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        quote = MarketQuote("winner:1", "Home", 1, 1, 1.02)
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(quote,),
        )
        with (
            patch.object(
                brain,
                "_matchup_probability",
                return_value=(None, 0),
            ),
            patch.object(
                brain,
                "_calibrated_regulation_probability",
                return_value=0.25,
            ),
        ):
            review = brain.review_and_approve(
                {"game_id": 8, "status": "PRE-MATCH", "hour_of_day": 9},
                analysis,
                unfiltered=True,
            )

        self.assertTrue(review["approved"])
        self.assertTrue(review["unfiltered"])
        self.assertEqual(review["decision"]["market_key"], "winner:1")

    def test_unfiltered_includes_extra_penalties_and_btts_markets(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        extra_penalties = MarketQuote(
            "extra-penalties:yes",
            "Pénalités supplémentaires : Oui",
            77,
            1,
            1.02,
            group_label="Pénalités supplémentaires",
            outcome_label="Oui",
            is_extra_penalties=True,
        )
        btts = MarketQuote(
            "btts:yes",
            "Oui",
            19,
            1,
            1.4,
            group_label="Les deux équipes marquent",
            outcome_label="Oui",
        )
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(extra_penalties, btts),
        )

        with (
            patch.object(brain, "_matchup_probability", return_value=(None, 0)),
            patch.object(
                brain,
                "_calibrated_regulation_probability",
                return_value=0.75,
            ),
        ):
            review = brain.review_and_approve(
                {"game_id": 9, "status": "PRE-MATCH", "hour_of_day": 9},
                analysis,
                unfiltered=True,
            )

        self.assertTrue(review["approved"])
        self.assertEqual(
            {item["market_key"] for item in review["top_markets"]},
            {"extra-penalties:yes", "btts:yes"},
        )
        self.assertIn(
            review["decision"]["market_type"],
            {"Pénalités supplémentaires", "Les deux équipes marquent"},
        )

    def test_prematch_rejects_confidence_below_70_percent(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(
                MarketQuote("winner:1", "Home", 1, 1, 1.6),
            ),
        )
        with (
            patch.object(
                brain, "_matchup_probability", return_value=(0.699, 100)
            ),
            patch.object(
                brain, "_calibrated_regulation_probability", return_value=0.699
            ),
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 71,
                    "status": "PRE-MATCH",
                    "hour_of_day": 9,
                },
                analysis,
            )

        self.assertFalse(review["approved"])

    def test_extra_penalties_defaults_to_no_when_yes_is_not_qualified(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        yes_quote = MarketQuote(
            "extra-penalties:yes",
            "Oui",
            1094,
            2210,
            5.24,
            outcome_label="Oui",
            is_extra_penalties=True,
        )
        no_quote = MarketQuote(
            "extra-penalties:no",
            "Non",
            1094,
            2211,
            2.375,
            outcome_label="Non",
            is_extra_penalties=True,
        )
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            extra_penalties=ExtraPenaltiesPrediction(5.24, 2.375, "Non", 2.375),
            regulation_markets=(yes_quote, no_quote),
        )

        recommendation = brain._extra_penalties_recommendation(
            {"status": "PRE-MATCH", "score1": 0, "score2": 0},
            analysis,
            9,
            "ÉQUILIBRÉ",
        )
        review = brain.review_and_approve(
            {"game_id": 8, "status": "PRE-MATCH", "hour_of_day": 9},
            analysis,
        )

        self.assertEqual(recommendation["side"], "NON")
        self.assertFalse(recommendation["high_odds_opportunity"])
        self.assertFalse(review["approved"])
        self.assertIsNone(review["decision"])

    def test_sudden_death_rates_use_recent_and_current_hour_sqlite_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.sqlite"
            repository = MatchHistoryRepository(database)
            for game_id, score in enumerate(((5, 5), (2, 2), (4, 5), (3, 3)), start=1):
                repository.record_match(
                    {
                        "game_id": game_id,
                        "team1": "Home",
                        "team2": "Away",
                        "status": "FINISHED",
                        "score1": score[0],
                        "score2": score[1],
                    },
                    fav_team=None,
                    fav_odds=None,
                    center_total_line=None,
                    over_odds=None,
                    under_odds=None,
                )
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE matches_history SET hour_of_day = 9"
                )
                connection.commit()
            finally:
                connection.close()
            brain = TerminatAI(
                database,
                Path(temp_dir) / "ai_memory.json",
            )

            recent_rate, hourly_rate = brain._sudden_death_rates(9)

        self.assertEqual(recent_rate, 0.25)
        self.assertEqual(hourly_rate, 0.25)

    def test_extra_penalties_prematch_high_odds_requires_all_convergence_signals(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        yes_quote = MarketQuote(
            "extra-penalties:yes",
            "Oui",
            1094,
            2210,
            4.5,
            outcome_label="Oui",
            is_extra_penalties=True,
        )
        no_quote = MarketQuote(
            "extra-penalties:no",
            "Non",
            1094,
            2211,
            2.54,
            outcome_label="Non",
            is_extra_penalties=True,
        )
        cases = (
            (
                MarketQuote("over85", "Total Global (8.5) Plus de", 17, 9, 1.72, 8.5),
                (0.25, 0.0),
                4.5,
                "OUI",
                True,
            ),
            (
                MarketQuote(
                    "center95",
                    "Total Global (9.5) Moins de",
                    17,
                    10,
                    1.9,
                    9.5,
                    is_center=True,
                ),
                (0.0, 0.25),
                4.5,
                "OUI",
                True,
            ),
            (
                MarketQuote("over85", "Total Global (8.5) Plus de", 17, 9, 1.73, 8.5),
                (0.25, 0.0),
                4.5,
                "NON",
                False,
            ),
            (
                MarketQuote("over85", "Total Global (8.5) Plus de", 17, 9, 1.72, 8.5),
                (0.249, 0.0),
                4.5,
                "NON",
                False,
            ),
            (
                MarketQuote("over85", "Total Global (8.5) Plus de", 17, 9, 1.72, 8.5),
                (0.25, 0.0),
                4.51,
                "NON",
                False,
            ),
        )
        for line_quote, rates, yes_odds, expected_side, expected_opportunity in cases:
            with self.subTest(line=line_quote.key, rates=rates, yes_odds=yes_odds):
                yes_quote_for_case = MarketQuote(
                    yes_quote.key,
                    yes_quote.label,
                    yes_quote.group_id,
                    yes_quote.event_type,
                    yes_odds,
                    outcome_label=yes_quote.outcome_label,
                    is_extra_penalties=True,
                )
                analysis = GameAnalysis(
                    exact_scores=(),
                    balanced_total=None,
                    extra_penalties=ExtraPenaltiesPrediction(
                        yes_odds, 2.54, "Non", 2.54
                    ),
                    regulation_markets=(yes_quote_for_case, no_quote, line_quote),
                )
                with patch.object(brain, "_sudden_death_rates", return_value=rates):
                    recommendation = brain._extra_penalties_recommendation(
                        {"status": "PRE-MATCH", "score1": 0, "score2": 0},
                        analysis,
                        9,
                        "ÉQUILIBRÉ",
                    )

                self.assertEqual(recommendation["side"], expected_side)
                self.assertEqual(
                    recommendation["high_odds_opportunity"],
                    expected_opportunity,
                )

    def test_extra_penalties_live_exception_is_limited_to_level_scores_three_to_five(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            extra_penalties=ExtraPenaltiesPrediction(5.16, 2.54, "Non", 2.54),
        )

        for score, expected_side in (((3, 3), "OUI"), ((4, 4), "OUI"), ((5, 5), "OUI"), ((6, 6), "NON")):
            with self.subTest(score=score):
                recommendation = brain._extra_penalties_recommendation(
                    {
                        "status": "LIVE",
                        "score1": score[0],
                        "score2": score[1],
                    },
                    analysis,
                    9,
                    "ÉQUILIBRÉ",
                )
                self.assertEqual(recommendation["side"], expected_side)

    def test_preference_names_the_exact_global_total_market(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=TotalBalance(
                threshold=8.5,
                over_odds=1.72,
                under_odds=2.0,
                over_probability_pct=58.1,
                under_probability_pct=50.0,
                selected_side="Plus de",
                selected_odds=1.72,
                selected_probability_pct=53.7,
            ),
        )

        preference = brain.predict_preference({"game_id": 62}, analysis)

        self.assertEqual(
            preference["bet"],
            "Total : (8.5) Plus de @1.72",
        )

    def test_review_approves_value_market_and_returns_three_exact_scores(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(
                ExactScorePrediction((3, 1), 2.0, 50.0),
                ExactScorePrediction((7, 6), 20.0, 5.0),
                ExactScorePrediction((4, 2), 5.0, 20.0),
                ExactScorePrediction((8, 7), 40.0, 2.5),
                ExactScorePrediction((2, 4), 34.0, 2.9),
            ),
            balanced_total=None,
            regulation_markets=(
                MarketQuote("winner:1", "Home", 1, 1, 1.7),
                MarketQuote("winner:3", "Away", 1, 3, 2.0),
            ),
        )
        brain.memory["regulation_markets"]["winner:1"] = {
            "overall": {
                "bets": 100,
                "actual_rate": 1.0,
                "implied_probability": 0.5,
            }
        }
        _mark_proven(brain, "winner:1")

        with patch.object(
            brain, "_matchup_probability", return_value=(1.0, 100)
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 2,
                    "status": "LIVE",
                    "score1": 0,
                    "score2": 0,
                    "hour_of_day": 9,
                },
                analysis,
            )

        self.assertTrue(review["approved"])
        self.assertGreaterEqual(
            review["top_markets"][0]["confidence_pct"], 60.0
        )
        # Le marché validé doit être celui de meilleure value.
        self.assertEqual(review["decision"]["market_key"], "winner:1")
        self.assertLessEqual(review["decision"]["odds"], 2.05)
        self.assertGreaterEqual(
            review["decision"]["confidence_pct"], brain.MIN_CONFIDENCE * 100
        )
        # Aucun score exact n'est exposé s'il ne bat pas réellement sa cote
        # (modèle de Poisson bivarié, plus de "top 3" systématique).
        self.assertTrue(
            all(
                item["market_type"] == "Score exact"
                for item in review["top_exact_scores"]
            )
        )
        self.assertLessEqual(len(review["top_exact_scores"]), 3)

    def test_review_evaluates_non_centered_regulation_total_lines(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(
                MarketQuote("total:7.5:over", "Centre Plus", 17, 9, 1.55, 7.5, is_center=True),
                MarketQuote("total:7.5:under", "Centre Moins", 17, 10, 2.5, 7.5, is_center=True),
                MarketQuote("total:8.5:over", "Autre Plus", 17, 9, 1.55, 8.5),
                MarketQuote("total:8.5:under", "Autre Moins", 17, 10, 2.5, 8.5),
            ),
        )
        _mark_proven(brain, "total:7.5:over", "total:8.5:over")

        with patch.object(
            brain, "_matchup_probability", return_value=(0.98, 100)
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 5,
                    "status": "LIVE",
                    "score1": 0,
                    "score2": 0,
                    "hour_of_day": 9,
                },
                analysis,
            )

        self.assertTrue(review["approved"])
        self.assertEqual(
            {market["threshold"] for market in review["evaluated_markets"]},
            {7.5, 8.5},
        )
        self.assertEqual(len(review["top_markets"]), 2)
        self.assertTrue(
            all(
                1.40 <= market["odds"] <= 2.05
                and market["confidence_pct"] >= 62
                for market in review["top_markets"]
            )
        )

    def test_exact_scores_alone_do_not_create_a_regulation_market_decision(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(ExactScorePrediction((3, 1), 1.6, 62.5),),
            balanced_total=None,
        )

        review = brain.review_and_approve(
            {"game_id": 3, "hour_of_day": 9}, analysis
        )

        self.assertFalse(review["approved"])
        self.assertIsNone(review["decision"])
        self.assertEqual(review["top_markets"], [])
        self.assertEqual(review["normal_scores"], [])

    def test_review_rejects_unproven_low_odds_markets(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(
                MarketQuote("winner:1", "Home", 1, 1, 1.45),
                MarketQuote("winner:3", "Away", 1, 3, 1.45),
                MarketQuote("double-chance:4", "1X", 8, 4, 2.51),
            ),
        )

        review = brain.review_and_approve(
            {"game_id": 6, "hour_of_day": 9}, analysis
        )

        self.assertFalse(review["approved"])
        self.assertIsNone(review["decision"])
        self.assertEqual(review["top_markets"], [])

    def test_review_selects_highest_confidence_safe_regulation_market(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(
                MarketQuote("total:over:7.5", "Plus de 7.5 buts", 17, 9, 1.55, 7.5),
                MarketQuote("total:over:8.5", "Plus de 8.5 buts", 17, 9, 1.85, 8.5),
                MarketQuote("total:over:9.5", "Plus de 9.5 buts", 17, 9, 1.99, 9.5),
            ),
        )
        _mark_proven(
            brain, "total:over:7.5", "total:over:8.5", "total:over:9.5"
        )
        probabilities = {
            "total:over:7.5": 0.80,
            "total:over:8.5": 0.72,
            "total:over:9.5": 0.95,
        }
        with (
            patch.object(brain, "analyze_matchup", return_value={
                "team1": {"matches": 0, "average_goals_scored": None, "samples": []},
                "team2": {"matches": 0, "average_goals_scored": None, "samples": []},
                "h2h": {"matches_played": 0, "samples": []},
            }),
            patch.object(
                brain,
                "_matchup_probability",
                side_effect=lambda quote, *_, **__: (
                    probabilities[quote.key],
                    100,
                ),
            ),
            patch.object(
                brain,
                "_calibrated_regulation_probability",
                side_effect=lambda quote, *_, **__: probabilities[quote.key],
            ),
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 60,
                    "status": "LIVE",
                    "score1": 2,
                    "score2": 2,
                    "hour_of_day": 9,
                },
                analysis,
            )

        self.assertEqual(review["decision"]["label"], "Total : Plus de 9.5 buts")
        self.assertEqual(review["decision"]["odds"], 1.99)
        # La somme des probabilités d'un marché ne doit jamais dépasser 100 %.
        for market in review["evaluated_markets"]:
            self.assertLessEqual(market["confidence_pct"], 100.0)
        # Classement par value. Le 8.5 est retiré : son écart face au marché
        # (72 % vs 54 % = +18 pts) dépasse le plafond d'edge autorisé (+15 pts),
        # c'est le garde-fou contre les faux signaux.
        self.assertEqual(
            [market["market_key"] for market in review["top_markets"]],
            ["total:over:9.5", "total:over:7.5"],
        )
        # Les confiances simulées (80 % / 72 %) sont ramenées vers le marché
        # par le plafond d'edge : seules des valeurs plausibles subsistent.
        confidences = [
            market["confidence_pct"] for market in review["top_markets"]
        ]
        self.assertEqual(len(confidences), 2)
        self.assertTrue(all(0 < value <= 100 for value in confidences))

    def test_review_ignores_group_19_and_uses_exact_global_total_label(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(
                MarketQuote(
                    "total:11273:4",
                    "Plus de 4 buts",
                    19,
                    11273,
                    1.89,
                    4.0,
                ),
                MarketQuote(
                    "total:9:4.5",
                    "Plus de 4.5 buts",
                    17,
                    9,
                    1.90,
                    4.5,
                ),
            ),
        )
        _mark_proven(brain, "total:9:4.5")
        probabilities = {
            "total:11273:4": 0.95,
            "total:9:4.5": 0.80,
        }
        with (
            patch.object(
                brain,
                "_matchup_probability",
                side_effect=lambda quote, *_, **__: (
                    probabilities[quote.key],
                    100,
                ),
            ),
            patch.object(
                brain,
                "_calibrated_regulation_probability",
                side_effect=lambda quote, *_, **__: probabilities[quote.key],
            ),
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 61,
                    "status": "LIVE",
                    "score1": 3,
                    "score2": 0,
                    "hour_of_day": 9,
                },
                analysis,
            )

        # Le groupe 19 expose les MÊMES lignes que le groupe 17 (cotes
        # identiques). Un seuil "4" issu du flux tronqué est en réalité 4.5 :
        # le marché doit être évalué, avec la ligne restaurée.
        evaluated = {
            market["market_key"]: market for market in review["evaluated_markets"]
        }
        self.assertIn("total:11273:4", evaluated)
        self.assertIn("total:9:4.5", evaluated)
        # Même ligne réelle des deux côtés.
        self.assertEqual(
            evaluated["total:11273:4"]["label"],
            "Total : Plus de 4.5 buts",
        )
        self.assertEqual(
            evaluated["total:11273:4"]["label"],
            evaluated["total:9:4.5"]["label"],
        )
        self.assertTrue(review["approved"])

    def test_live_review_rejects_out_of_range_odds_confidence_and_score_failures(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        quotes = (
            MarketQuote("boundary-low", "Home", 1, 1, 1.39),
            MarketQuote("boundary-high", "Away", 1, 3, 2.06),
            MarketQuote("low-confidence", "Over 8.5", 17, 9, 1.60, 8.5),
            MarketQuote("score-incoherent", "Under 2.5", 17, 10, 1.60, 2.5),
            MarketQuote("over-already-hit", "Over 2.5", 17, 9, 1.60, 2.5),
        )
        probabilities = {
            "boundary-low": 0.99,
            "boundary-high": 0.99,
            "low-confidence": 0.61,
            "score-incoherent": 0.99,
            "over-already-hit": 0.99,
        }
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=quotes,
        )

        with (
            patch.object(
                brain,
                "_matchup_probability",
                side_effect=lambda quote, *_, **__: (
                    probabilities[quote.key],
                    100,
                ),
            ),
            patch.object(
                brain,
                "_calibrated_regulation_probability",
                side_effect=lambda quote, *_, **__: probabilities[quote.key],
            ),
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 62,
                    "status": "LIVE",
                    "score1": 2,
                    "score2": 1,
                    "hour_of_day": 9,
                },
                analysis,
            )

        self.assertFalse(review["approved"])
        self.assertIsNone(review["decision"])
        self.assertEqual(review["top_markets"], [])
        incoherent = next(
            item
            for item in review["evaluated_markets"]
            if item["market_key"] == "score-incoherent"
        )
        self.assertFalse(incoherent["score_coherent"])
        already_hit = next(
            item
            for item in review["evaluated_markets"]
            if item["market_key"] == "over-already-hit"
        )
        self.assertFalse(already_hit["score_coherent"])

    def test_live_review_approves_at_68_percent_and_inclusive_odds_bounds(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        _mark_proven(brain, "winner:1")
        for odds, confidence in ((1.50, 0.90), (2.05, 0.68)):
            with self.subTest(odds=odds, confidence=confidence):
                quote = MarketQuote("winner:1", "Home", 1, 1, odds)
                analysis = GameAnalysis(
                    exact_scores=(),
                    balanced_total=None,
                    regulation_markets=(quote,),
                )
                with (
                    patch.object(
                        brain,
                        "_matchup_probability",
                        return_value=(confidence, 100),
                    ),
                    patch.object(
                        brain,
                        "_calibrated_regulation_probability",
                        return_value=confidence,
                    ),
                    # Le marché de référence doit être cohérent avec la
                    # probabilité simulée : le plafond d'écart (+15 pts) est
                    # appliqué après la normalisation.
                    patch.object(
                        brain,
                        "_bookmaker_fair_probability",
                        return_value=confidence,
                    ),
                ):
                    review = brain.review_and_approve(
                        {
                            "game_id": 72,
                            "status": "LIVE",
                            "score1": 1,
                            "score2": 0,
                            "hour_of_day": 9,
                        },
                        analysis,
                    )

                self.assertTrue(review["approved"])
                self.assertEqual(review["decision"]["odds"], odds)
                self.assertAlmostEqual(
                    review["decision"]["confidence_pct"],
                    confidence * 100,
                    places=1,
                )

    def test_live_review_rejects_negative_expected_value(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        quote = MarketQuote("winner:1", "Home", 1, 1, 1.44)
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(quote,),
        )
        with (
            patch.object(
                brain, "_matchup_probability", return_value=(0.62, 100)
            ),
            patch.object(
                brain,
                "_calibrated_regulation_probability",
                return_value=1 / quote.odds,
            ),
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 73,
                    "status": "LIVE",
                    "score1": 1,
                    "score2": 0,
                    "hour_of_day": 9,
                },
                analysis,
            )

        candidate = review["evaluated_markets"][0]
        # La cote est dévigorisée : la probabilité calibrée baisse sous 1/1.44.
        self.assertLess(candidate["confidence_pct"], 1 / 1.44 * 100)
        self.assertLess(candidate["value"], brain.MIN_VALUE)
        self.assertFalse(candidate["value_qualified"])
        self.assertFalse(review["approved"])

    def test_live_review_requires_minimum_historical_matchup_results(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        quote = MarketQuote("winner:1", "Home", 1, 1, 1.45)
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(quote,),
        )
        with (
            patch.object(
                brain,
                "_matchup_probability",
                return_value=(0.95, brain.MIN_MARKET_HISTORY - 6),
            ),
            patch.object(
                brain,
                "_calibrated_regulation_probability",
                return_value=0.95,
            ),
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 74,
                    "status": "LIVE",
                    "score1": 1,
                    "score2": 0,
                    "hour_of_day": 9,
                },
                analysis,
            )

        candidate = review["evaluated_markets"][0]
        self.assertFalse(candidate["history_qualified"])
        self.assertFalse(candidate["qualified"])
        self.assertFalse(review["approved"])

    def test_live_market_calibration_uses_live_odds_not_prematch_memory(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        quote = MarketQuote("winner:1", "Home", 1, 1, 1.5)
        brain.memory["regulation_markets"][quote.key] = {
            "overall": {
                "bets": 100,
                "actual_rate": 0.1,
                "implied_probability": 0.8,
            }
        }

        # En LIVE la cote fait foi : on compare à la probabilité dévigorisée du
        # marché (1/1.5 retiré de la marge moyenne), jamais à la mémoire prématch.
        expected = brain._bookmaker_fair_probability(quote)
        self.assertAlmostEqual(
            brain._calibrated_regulation_probability(
                quote, 9, "CYCLE MIXTE", True
            ),
            expected,
        )
        self.assertLess(expected, 1 / quote.odds)

    def test_live_signal_calibration_requires_minimum_settled_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.sqlite"
            repository = MatchHistoryRepository(database)
            brain = TerminatAI(database, Path(temp_dir) / "memory.json")

            # On enregistre davantage que le minimum requis pour être calibré.
            for index in range(brain.MIN_LIVE_CALIBRATION_SAMPLES + 6):
                match = {
                    "game_id": 500 + index,
                    "team1": "Home",
                    "team2": "Away",
                    "status": "LIVE",
                    "score1": 3,
                    "score2": 3,
                }
                repository.record_live_signal_snapshot(
                    match,
                    trigger_time_sec=80,
                    score1_at_trigger=3,
                    score2_at_trigger=3,
                    drop_pct=20.0,
                    market_key="total:over:8.5",
                    market_label="Total : (8.5) Plus de",
                    odds_at_trigger=1.65,
                    estimated_prob=0.8,
                    group_id=17,
                    event_type=9,
                    threshold=8.5,
                )
                final_score = (5, 4) if index < 7 else (3, 3)
                repository.record_match(
                    {
                        **match,
                        "status": "FINISHED",
                        "score1": final_score[0],
                        "score2": final_score[1],
                    },
                    fav_team=None,
                    fav_odds=None,
                    center_total_line=None,
                    over_odds=None,
                    under_odds=None,
                )
                if index == brain.MIN_LIVE_CALIBRATION_SAMPLES - 2:
                    self.assertFalse(
                        brain.get_live_signal_calibration(20, 1.65)[
                            "calibrated"
                        ]
                    )

            calibration = brain.get_live_signal_calibration(20, 1.65)
            self.assertTrue(calibration["calibrated"])
            sample_count = brain.MIN_LIVE_CALIBRATION_SAMPLES + 6
            self.assertEqual(calibration["count"], sample_count)
            self.assertTrue(0 <= calibration["wins"] <= sample_count)
            self.assertAlmostEqual(
                calibration["win_rate_pct"],
                calibration["wins"] / sample_count * 100,
            )
            self.assertAlmostEqual(
                calibration["weight"], sample_count / (sample_count + 10)
            )

            quote = MarketQuote(
                "total:over:8.5",
                "Plus de 8.5 buts",
                17,
                9,
                1.65,
                8.5,
            )
            analysis = GameAnalysis(
                exact_scores=(),
                balanced_total=None,
                regulation_markets=(quote,),
            )
            with (
                patch.object(
                    brain,
                    "_matchup_probability",
                    return_value=(None, brain.MIN_MARKET_HISTORY),
                ),
                patch.object(brain, "predict_preference", return_value={
                    "bet": "Total : (8.5) Plus de @1.65",
                    "confidence_pct": 80.0,
                    "regime": "ÉQUILIBRÉ",
                }),
                patch.object(
                    brain,
                    "_calibrated_regulation_probability",
                    return_value=0.8,
                ),
                patch.object(
                    brain, "_is_live_score_coherent", return_value=True
                ),
            ):
                review = brain.review_and_approve(
                    {
                        "game_id": 99,
                        "status": "LIVE",
                        "score1": 3,
                        "score2": 3,
                        "hour_of_day": 9,
                    },
                    analysis,
                    {"total:over:8.5": 20.0},
                )

            candidate = review["evaluated_markets"][0]
            # La calibration LIVE fonctionne (échantillon suffisant) mais le
            # marché n'est PAS prouvé rentable en base : le ticket est refusé.
            self.assertTrue(candidate["live_signal_calibrated"])
            self.assertEqual(candidate["live_signal_samples"], sample_count)
            self.assertEqual(candidate["profitability_verdict"], "UNKNOWN")
            self.assertFalse(candidate["profitability_qualified"])
            self.assertFalse(review["approved"])

            # Une fois le marché prouvé rentable, un signal calibré solide passe.
            brain2 = TerminatAI(
                Path(temp_dir) / "history2.sqlite",
                Path(temp_dir) / "memory2.json",
            )
            _mark_proven(brain2, "total:over:8.5")
            brain = brain2
            with (
                patch.object(
                    brain,
                    "_matchup_probability",
                    return_value=(None, brain.MIN_MARKET_HISTORY),
                ),
                patch.object(brain, "predict_preference", return_value={
                    "bet": "Total : (8.5) Plus de @1.65",
                    "confidence_pct": 90.0,
                    "regime": "ÉQUILIBRÉ",
                }),
                patch.object(
                    brain,
                    "_calibrated_regulation_probability",
                    return_value=0.95,
                ),
                patch.object(
                    brain, "_is_live_score_coherent", return_value=True
                ),
            ):
                approved_review = brain.review_and_approve(
                    {
                        "game_id": 99,
                        "status": "LIVE",
                        "score1": 3,
                        "score2": 3,
                        "hour_of_day": 9,
                    },
                    analysis,
                    {"total:over:8.5": 20.0},
                )
            self.assertTrue(
                approved_review["approved"],
                approved_review["reason"]
                + " | "
                + str(approved_review["evaluated_markets"]),
            )
            self.assertTrue(approved_review["approved"])

    def test_live_review_requires_sqlite_market_history(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        quote = MarketQuote(
            "total:over:8.5",
            "Plus de 8.5 buts",
            17,
            9,
            1.6,
            8.5,
        )
        analysis = GameAnalysis(
            exact_scores=(),
            balanced_total=None,
            regulation_markets=(quote,),
        )
        with (
            patch.object(brain, "_matchup_probability", return_value=(None, 0)),
            patch.object(
                brain,
                "_calibrated_regulation_probability",
                return_value=0.99,
            ),
        ):
            review = brain.review_and_approve(
                {
                    "game_id": 63,
                    "status": "LIVE",
                    "score1": 0,
                    "score2": 0,
                    "hour_of_day": 9,
                },
                analysis,
            )

        self.assertFalse(review["approved"])
        self.assertFalse(review["evaluated_markets"][0]["qualified"])

    def test_review_blocks_high_team_totals_below_historical_average_threshold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.sqlite"
            repository = MatchHistoryRepository(database)
            for game_id, goals in enumerate((4, 4, 4, 4, 3), start=1):
                repository.record_match(
                    {
                        "game_id": game_id,
                        "team1": "Juventus",
                        "team2": f"Opponent {game_id}",
                        "status": "FINISHED",
                        "score1": goals,
                        "score2": 2,
                    },
                    fav_team=None,
                    fav_odds=None,
                    center_total_line=None,
                    over_odds=None,
                    under_odds=None,
                )
            brain = TerminatAI(database, Path(temp_dir) / "ai_memory.json")
            _mark_proven(brain, "team-total:1:11:3.5")
            analysis = GameAnalysis(
                exact_scores=(),
                balanced_total=None,
                regulation_markets=(
                    MarketQuote(
                        "team-total:1:11:4.5",
                        "Total 1 Plus 4.5",
                        15,
                        11,
                        1.53,
                        4.5,
                    ),
                    MarketQuote(
                        "team-total:1:11:3.5",
                        "Total 1 Plus 3.5",
                        15,
                        11,
                        1.53,
                        3.5,
                    ),
                ),
            )
            with (
                patch.object(
                    brain,
                    "_matchup_probability",
                    return_value=(0.99, 100),
                ),
                patch.object(
                    brain,
                    "_calibrated_regulation_probability",
                    return_value=0.80,
                ),
            ):
                review = brain.review_and_approve(
                    {
                        "game_id": 61,
                        "status": "LIVE",
                        "team1": "Juventus",
                        "team2": "Opponent",
                        "score1": 0,
                        "score2": 0,
                        "hour_of_day": 9,
                    },
                    analysis,
                )

        self.assertEqual(review["matchup"]["team1"]["average_goals_scored"], 3.8)
        self.assertEqual(
            [market["label"] for market in review["evaluated_markets"]],
            ["Total Juventus : (3.5) Plus de"],
        )
        self.assertEqual(
            review["decision"]["label"],
            "Total Juventus : (3.5) Plus de",
        )

    def test_review_does_not_allow_strong_signal_to_bypass_thresholds(self):
        brain = TerminatAI(db_path=":memory:", memory_path="unused-memory.json")
        analysis = GameAnalysis(
            exact_scores=(ExactScorePrediction((1, 0), 1.4, 71.4),),
            balanced_total=None,
            regulation_markets=(
                MarketQuote("winner:1", "Home", 1, 1, 1.44),
            ),
        )

        review = brain.review_and_approve(
            {"game_id": 4, "status": "LIVE", "hour_of_day": 9}, analysis
        )

        self.assertFalse(review["approved"])


if __name__ == "__main__":
    unittest.main()
