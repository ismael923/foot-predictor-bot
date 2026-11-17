import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import telegram_bot
from telegram_bot import (
    CycleStatistics,
    GameEventProcessor,
    OddsSignal,
    OddsSignalTracker,
    SignalReport,
    ExactScorePrediction,
    ExtraPenaltiesPrediction,
    GameAnalysis,
    MarketQuote,
    MarketProbability,
    TeamTotalPrediction,
    TotalBalance,
    analyze_game_event,
    format_prediction_message,
    parse_game_event,
)


def approving_ai():
    brain = unittest.mock.Mock()
    brain.review_and_approve.return_value = {
        "approved": True,
        "reason": "test approval",
        "decision": {
            "market_key": "winner:1",
            "label": "Équipe locale gagne",
            "odds": 1.6,
            "confidence_pct": 85.0,
            "value": 1.36,
            "group_id": 1,
            "event_type": 1,
            "threshold": None,
        },
        "matchup": {
            "h2h": {
                "matches_played": 4,
                "average_score_team1": 4.5,
                "average_score_team2": 3.5,
            }
        },
        "preference": {
            "bet": "Test",
            "confidence_pct": 60.0,
            "regime": "ÉQUILIBRÉ",
        },
        "top_markets": [],
        "normal_scores": [],
        "extended_scores": [],
        "top_exact_scores": [],
        "signal_override": False,
    }
    return brain


def exact_score_match(
    game_id: int,
    odds: float,
    status: str = "PRE-MATCH",
    score: tuple[int, int] = (4, 3),
) -> dict:
    outcome_index = 0 if score[0] >= score[1] else 1
    score_events = [
        {
            "type": 731,
            "cf": odds,
            "eventParams": {
                "params": [str(max(score)), str(min(score))]
            },
        }
    ]
    outcomes = [[], []]
    outcomes[outcome_index] = score_events
    match = {
        "gameId": game_id,
        "status": status,
        "eventGroups": [
            {
                "groupId": 136,
                "events": outcomes,
            }
        ],
    }
    if status == "LIVE":
        match["SC"] = {"FS": {"S1": 2, "S2": 2}}
    return match


def total_match(game_id: int, odds: float) -> dict:
    return {
        "gameId": game_id,
        "status": "LIVE",
        "SC": {"FS": {"S1": 2, "S2": 2}},
        "eventGroups": [
            {
                "groupId": 19,
                "events": [
                    [
                        {
                            "type": 11273,
                            "parameter": 4,
                            "cf": odds,
                            "eventParams": {"params": ["(4)"]},
                        }
                    ]
                ],
            }
        ],
    }


def game_event(
    game_id: int,
    *,
    odds: float,
    time_sec: float = 120,
    live: bool = False,
    score: tuple[int, int] = (0, 0),
    exact_scores: list[tuple[int, int, float]] | None = None,
) -> dict:
    if exact_scores is None:
        exact_scores = [(4, 3, odds)]
    outcomes = [[], []]
    for score_1, score_2, price in exact_scores:
        high_score, low_score = max(score_1, score_2), min(score_1, score_2)
        outcome_index = 0 if score_1 >= score_2 else 1
        outcomes[outcome_index].append(
            {
                "type": 731,
                "cf": price,
                "parameter": f"{high_score}.00{low_score}",
                "eventParams": {
                    "params": [str(high_score), str(low_score)]
                },
            }
        )
    return {
        "id": game_id,
        "nonStarted": not live,
        "statusLineStr": "Début dans 2 minutes",
        "timer": {"timeRun": live, "timeSec": time_sec},
        "scores": {
            "scoreOpp1": score[0],
            "scoreOpp2": score[1],
            "fullScore": {"S1": score[0], "S2": score[1]},
        },
        "eventGroups": [
            {
                "groupId": 1,
                "events": [
                    [
                        {"type": 1, "cf": 1.55},
                        {"type": 2, "cf": 3.2},
                        {"type": 3, "cf": 2.28},
                    ]
                ],
            },
            {
                "groupId": 136,
                "events": outcomes,
            },
            {
                "groupId": 19,
                "events": [
                    [
                        {"type": 11273, "parameter": 4, "cf": 2.1},
                        {"type": 11274, "parameter": 4, "cf": 1.9},
                    ]
                ],
            },
        ],
    }


def flat_match(
    game_id: int,
    *,
    time_run: bool = False,
    time_sec: float = 120,
    score: tuple[int, int] = (0, 0),
    over_odds: float = 2.1,
) -> dict:
    return {
        "id": game_id,
        "opponent1": {"fullName": "Équipe locale"},
        "opponent2": {"fullName": "Équipe visiteuse"},
        "scores": {
            "fullScore": f"{score[0]}-{score[1]}",
            "scoreOpp1": score[0],
            "scoreOpp2": score[1],
            "statusLineStr": "Événement de test",
            "timer": {"timeRun": time_run, "timeSec": time_sec},
        },
        "events": [
            {"groupId": 1, "type": 1, "cf": 2.0},
            {"groupId": 1, "type": 2, "cf": 3.2},
            {"groupId": 1, "type": 3, "cf": 3.6},
            {"groupId": 17, "type": 9, "parameter": 2.5, "cf": over_odds},
            {"groupId": 17, "type": 10, "parameter": 2.5, "cf": 1.8},
            {"groupId": 2, "type": 7, "parameter": -1.5, "cf": 1.95},
            {"groupId": 2, "type": 8, "parameter": 1.5, "cf": 1.85},
        ],
    }


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps({"ok": True}).encode("utf-8")


class FakeAsyncResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self.payload


class FakeTelegramSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.get_calls = []
        self.post_calls = []

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return FakeAsyncResponse(self.payload)

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeAsyncResponse(self.payload)


class OddsSignalTrackerTests(unittest.TestCase):
    def test_registers_commands_in_telegram_menu(self):
        session = FakeTelegramSession({"ok": True, "result": True})

        asyncio.run(telegram_bot._register_telegram_commands(session, "token"))

        url, request = session.post_calls[0]
        self.assertEqual(
            url, "https://api.telegram.org/bottoken/setMyCommands"
        )
        self.assertEqual(
            [command["command"] for command in request["json"]["commands"]],
            ["pause", "resume", "status", "stats", "ai"],
        )

    def test_get_updates_uses_telegram_long_poll_and_offset(self):
        session = FakeTelegramSession({"ok": True, "result": [{"update_id": 25}]})

        updates = asyncio.run(
            telegram_bot._poll_telegram_updates(session, "token", 26)
        )

        self.assertEqual(updates, [{"update_id": 25}])
        url, request = session.get_calls[0]
        self.assertEqual(url, "https://api.telegram.org/bottoken/getUpdates")
        self.assertEqual(request["params"]["timeout"], 30)
        self.assertEqual(request["params"]["offset"], 26)
        self.assertEqual(
            json.loads(request["params"]["allowed_updates"]), ["message"]
        )
    def test_detects_qualifying_exact_score_drop_once(self):
        tracker = OddsSignalTracker()
        self.assertFalse(tracker.update(exact_score_match(10, 10.0), timestamp=100).STRONG_SIGNAL)

        report = tracker.update(exact_score_match(10, 8.4), timestamp=115)
        self.assertTrue(report.STRONG_SIGNAL)
        self.assertAlmostEqual(report.signals[0].drop_pct, 16.0)
        self.assertFalse(tracker.update(exact_score_match(10, 8.4), timestamp=118).STRONG_SIGNAL)

    def test_detects_high_odds_exact_score_drops_without_a_ceiling(self):
        tracker = OddsSignalTracker()
        tracker.update(
            exact_score_match(11, 20.0, "LIVE", score=(7, 6)),
            timestamp=100,
        )
        high_odds_report = tracker.update(
            exact_score_match(11, 16.0, "LIVE", score=(7, 6)),
            timestamp=105,
        )
        self.assertTrue(high_odds_report.STRONG_SIGNAL)
        self.assertAlmostEqual(high_odds_report.signals[0].drop_pct, 20.0)

        tracker = OddsSignalTracker()
        tracker.update(
            exact_score_match(12, 35.0, "LIVE", score=(12, 11)),
            timestamp=100,
        )
        report = tracker.update(
            exact_score_match(12, 29.0, "LIVE", score=(12, 11)),
            timestamp=105,
        )
        self.assertTrue(report.STRONG_SIGNAL)

    def test_filters_unreachable_live_exact_score(self):
        tracker = OddsSignalTracker()
        tracker.update(exact_score_match(13, 10.0, "LIVE"), timestamp=100)
        unreachable = exact_score_match(13, 8.0, "LIVE")
        unreachable["SC"]["FS"] = {"S1": 5, "S2": 4}
        report = tracker.update(unreachable, timestamp=105)
        self.assertFalse(report.STRONG_SIGNAL)

    def test_does_not_compare_samples_outside_the_window(self):
        tracker = OddsSignalTracker()
        tracker.update(exact_score_match(13, 10.0), timestamp=100)
        report = tracker.update(exact_score_match(13, 8.0), timestamp=126.01)
        self.assertFalse(report.STRONG_SIGNAL)

    def test_detects_total_market_drop_during_live_match(self):
        tracker = OddsSignalTracker()
        tracker.update(total_match(15, 2.0), timestamp=100)
        report = tracker.update(total_match(15, 1.65), timestamp=105)
        self.assertTrue(report.STRONG_SIGNAL)
        self.assertEqual(report.signals[0].market, "Plus de 4 buts")

    def test_send_message_prepends_signal_badge(self):
        report = SignalReport(
            (OddsSignal("14", "Score exact 4-3", 10.0, 8.4, 16.0),)
        )
        with (
            patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123"},
            ),
            patch("telegram_bot.urlopen", return_value=FakeResponse()) as urlopen,
        ):
            self.assertTrue(telegram_bot.send_message("Pronostic", report))

        request = urlopen.call_args.args[0]
        payload = parse_qs(request.data.decode("utf-8"))
        self.assertTrue(payload["text"][0].startswith("🚨 <b>SIGNAL FORT</b> (Chute de cote -16.0%)"))


class GameEventProcessorTests(unittest.TestCase):
    def test_records_and_settles_exact_score_candidates_from_match_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from collector import MatchHistoryRepository

            repository = MatchHistoryRepository(
                Path(temp_dir) / "penalty_history.db"
            )
            brain = approving_ai()
            brain.maybe_train.return_value = False
            brain.review_and_approve.return_value["top_exact_scores"] = [
                {
                    "score": "4-3",
                    "odds": 3.0,
                    "confidence_pct": 33.3,
                },
                {
                    "score": "5-4",
                    "odds": 5.0,
                    "confidence_pct": 20.0,
                },
            ]
            processor = GameEventProcessor(
                sender=unittest.mock.Mock(),
                history_repository=repository,
                ai_brain=brain,
            )
            predictions = [(4, 3, 3.0), (5, 4, 5.0)]

            processor.process_game_event(
                game_event(950, odds=3.0, exact_scores=predictions)
            )
            finished = game_event(
                950,
                odds=3.0,
                exact_scores=predictions,
                score=(4, 3),
            )
            finished["isFinished"] = True
            processor.process_game_event(finished)

            with closing(sqlite3.connect(repository.db_path)) as connection:
                rows = connection.execute(
                    """
                    SELECT predicted_score1, predicted_score2, odds,
                           prediction_status, final_score1, final_score2
                    FROM exact_score_predictions
                    WHERE game_id = '950' AND phase = 'PRE-MATCH'
                    ORDER BY predicted_score1, predicted_score2
                    """
                ).fetchall()

        self.assertEqual(
            rows,
            [
                (4, 3, 3.0, "WON", 4, 3),
                (5, 4, 5.0, "LOST", 4, 3),
            ],
        )

    def test_extracts_all_requested_regulation_market_families(self):
        payload = game_event(71, odds=9.0)
        payload["eventGroups"].extend(
            [
                {
                    "groupId": 8,
                    "events": [[
                        {"type": 4, "cf": 1.25},
                        {"type": 5, "cf": 1.45},
                        {"type": 6, "cf": 1.8},
                    ]],
                },
                {
                    "groupId": 2,
                    "name": "Handicap",
                    "events": [[
                        {"type": 7, "parameter": -1.5, "cf": 1.95, "isCenter": True},
                        {"type": 8, "parameter": 1.5, "cf": 1.85, "isCenter": True},
                    ]],
                },
                {
                    "groupId": 17,
                    "events": [[
                        {"type": 9, "parameter": line, "cf": 1.9}
                        for line in (5.5, 6.5, 7.5, 8.5, 9.5, 11.5)
                    ] + [
                        {"type": 10, "parameter": line, "cf": 1.9}
                        for line in (5.5, 6.5, 7.5, 8.5, 9.5, 11.5)
                    ]],
                },
                {
                    "groupId": 15,
                    "events": [[
                        {"type": 11, "parameter": 3.5, "cf": 1.8},
                        {"type": 12, "parameter": 3.5, "cf": 2.0},
                    ]],
                },
                {
                    "groupId": 62,
                    "events": [[
                        {"type": 13, "parameter": 2.5, "cf": 1.75},
                        {"type": 14, "parameter": 2.5, "cf": 2.05},
                    ]],
                },
                {
                    "groupId": 19,
                    "name": "Les deux équipes marquent",
                    "events": [[
                        {"type": 1, "name": "Oui", "cf": 1.7},
                        {"type": 2, "name": "Non", "cf": 2.1},
                    ]],
                },
                {
                    "groupId": 77,
                    "name": "Pénalités supplémentaires",
                    "events": [[
                        {"type": 1, "name": "Oui", "cf": 2.0},
                        {"type": 2, "name": "Non", "cf": 1.8},
                    ]],
                },
                {
                    "groupId": 88,
                    "name": "Premier pénalty manqué",
                    "events": [[
                        {"type": 1, "name": "Oui", "cf": 1.9},
                        {"type": 2, "name": "Non", "cf": 1.8},
                    ]],
                },
            ]
        )

        analysis = analyze_game_event(parse_game_event(payload))
        quotes = analysis.regulation_markets
        keys = {quote.key for quote in quotes}

        self.assertIn("winner:1", keys)
        self.assertIn("winner:2", keys)
        self.assertIn("winner:3", keys)
        self.assertTrue({"double-chance:4", "double-chance:5", "double-chance:6"} <= keys)
        self.assertIn("handicap:7:-1.5", keys)
        self.assertIn("handicap:8:1.5", keys)
        self.assertEqual(
            sum(quote.group_id == 17 for quote in quotes),
            12,
        )
        self.assertEqual(sum(quote.group_id == 15 for quote in quotes), 2)
        self.assertEqual(sum(quote.group_id == 62 for quote in quotes), 2)
        self.assertTrue(any(quote.group_id == 19 and quote.event_type == 1 for quote in quotes))
        self.assertEqual(
            {quote.outcome_label for quote in quotes if quote.group_id == 88},
            {"Oui", "Non"},
        )
        self.assertEqual(
            {quote.outcome_label for quote in quotes if quote.is_extra_penalties},
            {"Oui", "Non"},
        )

    def test_detects_extra_penalties_from_numeric_binary_market_ids(self):
        payload = game_event(73, odds=9.0)
        payload["eventGroups"].append(
            {
                "groupId": 21,
                "events": [[
                    {"type": 180, "parameter": None, "cf": 1.8},
                    {"type": 181, "parameter": None, "cf": 2.0},
                ]],
            }
        )

        analysis = analyze_game_event(parse_game_event(payload))
        numeric_quotes = [
            quote
            for quote in analysis.regulation_markets
            if quote.is_extra_penalties
        ]

        self.assertIsNotNone(analysis.extra_penalties)
        self.assertEqual(analysis.extra_penalties.yes_odds, 1.8)
        self.assertEqual(analysis.extra_penalties.no_odds, 2.0)
        self.assertEqual(
            {quote.outcome_label for quote in numeric_quotes},
            {"Oui", "Non"},
        )

    def test_detects_numeric_yes_no_pair_inside_group_with_third_issue(self):
        payload = game_event(74, odds=9.0)
        payload["eventGroups"].append(
            {
                "groupId": 1094,
                "events": [[
                    {"type": 2210, "parameter": None, "cf": 4.81},
                    {"type": 2211, "parameter": None, "cf": 2.59},
                    {"type": 2212, "parameter": None, "cf": 2.025},
                ]],
            }
        )

        analysis = analyze_game_event(parse_game_event(payload))
        numeric_quotes = [
            quote
            for quote in analysis.regulation_markets
            if quote.group_id == 1094
        ]

        self.assertIsNotNone(analysis.extra_penalties)
        self.assertEqual(analysis.extra_penalties.yes_odds, 4.81)
        self.assertEqual(analysis.extra_penalties.no_odds, 2.59)
        self.assertEqual(len(numeric_quotes), 2)
        self.assertEqual(
            {quote.outcome_label for quote in numeric_quotes},
            {"Oui", "Non"},
        )
        message = format_prediction_message(
            {
                "game_id": 74,
                "status": "PRE-MATCH",
                "team1": "Home",
                "team2": "Away",
            },
            analysis,
        )
        penalties_section = message.split(
            "⏱️ PÉNALITÉS SUPPLÉMENTAIRES", maxsplit=1
        )[1].split("🎯 SCORES", maxsplit=1)[0]
        self.assertIn("<b>Oui</b> @4.81", penalties_section)
        self.assertIn("<b>Non</b> @2.59", penalties_section)
        self.assertNotIn("Indisponible", penalties_section)

    def test_training_completion_sends_a_summary_to_the_configured_sender(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from collector import MatchHistoryRepository

            repository = MatchHistoryRepository(Path(temp_dir) / "history.db")
            brain = unittest.mock.Mock()
            brain.maybe_train.return_value = True
            brain.format_training_summary.return_value = (
                "🧠 TerminatAI — cycle d’apprentissage terminé"
            )
            sender = unittest.mock.Mock()
            processor = GameEventProcessor(
                sender=sender,
                history_repository=repository,
                ai_brain=brain,
            )
            analysis = GameAnalysis(
                exact_scores=(),
                balanced_total=None,
                regulation_markets=(
                    MarketQuote(
                        key="winner:1",
                        label="Home",
                        group_id=1,
                        event_type=1,
                        odds=1.8,
                    ),
                ),
            )
            match = {
                "game_id": 72,
                "team1": "Home",
                "team2": "Away",
                "status": "FINISHED",
                "score1": 2,
                "score2": 1,
            }

            processor._record_history(match, analysis)

            brain.maybe_train.assert_called_once_with()
            sender.assert_called_once_with(
                "🧠 TerminatAI — cycle d’apprentissage terminé"
            )

            with closing(sqlite3.connect(repository.db_path)) as connection:
                saved_quote = connection.execute(
                    "SELECT prematch_odds FROM regulation_markets_history"
                ).fetchone()
            self.assertEqual(saved_quote, (None,))

    def test_prematch_sends_one_unfiltered_ticket_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from collector import MatchHistoryRepository

            repository = MatchHistoryRepository(
                Path(temp_dir) / "penalty_history.db"
            )
            sender = unittest.mock.Mock()
            brain = approving_ai()
            brain.maybe_train.return_value = False
            processor = GameEventProcessor(
                sender=sender,
                history_repository=repository,
                ai_brain=brain,
            )

            self.assertTrue(
                processor.process_game_event(
                    game_event(73, odds=9.0, time_sec=120)
                )
            )
            self.assertFalse(
                processor.process_game_event(
                    game_event(73, odds=9.0, time_sec=90)
                )
            )
            sender.assert_called_once()
            brain.review_and_approve.assert_called_once()
            with closing(sqlite3.connect(repository.db_path)) as connection:
                status = connection.execute(
                    "SELECT status FROM matches_history WHERE game_id = '73'"
                ).fetchone()
            self.assertTrue(repository.has_sent_prediction(73, "PRE-MATCH"))
            self.assertFalse(repository.has_sent_prediction(73, "LIVE"))

        self.assertEqual(status, ("PRE-MATCH",))

    def test_parses_gameevents_status_countdown_and_scores(self):
        pre_match = parse_game_event(game_event(21, odds=9.0, time_sec=90))
        self.assertEqual(pre_match["game_id"], 21)
        self.assertEqual(pre_match["status"], "PRE-MATCH")
        self.assertEqual(pre_match["statusLineStr"], "Début dans 2 minutes")
        self.assertEqual(pre_match["timeSec"], 90)
        self.assertEqual(pre_match["score1"], 0)
        self.assertEqual(pre_match["full_score"], {"S1": 0, "S2": 0})

        timer_pre_match = game_event(22, odds=9.0, live=True)
        timer_pre_match["nonStarted"] = False
        timer_pre_match["timer"]["timeRun"] = False
        self.assertEqual(parse_game_event(timer_pre_match)["status"], "PRE-MATCH")

    def test_parses_live_feed_timer_and_status_from_scores(self):
        payload = {
            "id": 770413473,
            "nonStarted": False,
            "scores": {
                "scoreOpp1": 1,
                "scoreOpp2": 0,
                "fullScore": "1-0",
                "timer": {"timeRun": True, "timeSec": 42},
                "statusLineStr": "En cours",
            },
            "eventGroups": [],
        }

        parsed = parse_game_event(payload)

        self.assertEqual(parsed["status"], "LIVE")
        self.assertEqual(parsed["timeSec"], 42)
        self.assertEqual(parsed["statusLineStr"], "En cours")
        self.assertEqual((parsed["score1"], parsed["score2"]), (1, 0))

    def test_parses_flat_top_games_match_and_its_single_market(self):
        payload = {
            "id": 770410844,
            "opponent1": {"fullName": "Genoa"},
            "opponent2": {"fullName": "Lecce"},
            "event": {
                "groupId": 2,
                "type": 7,
                "parameter": -2.0,
                "cf": 2.01,
            },
            "scores": {
                "fullScore": "2-0",
                "scoreOpp1": 2,
                "scoreOpp2": 0,
                "currentPeriod": 2,
                "currentPeriodName": "2ème mi-temps",
                "timer": {"timeSec": 3967, "timeRun": False},
                "statusLineStr": "66 minutes",
            },
        }

        parsed = parse_game_event(payload)
        analysis = analyze_game_event(parsed)

        self.assertEqual(parsed["status"], "LIVE")
        self.assertEqual(parsed["statusLineStr"], "66 minutes")
        self.assertEqual((parsed["score1"], parsed["score2"]), (2, 0))
        self.assertEqual(len(analysis.market_probabilities), 1)
        self.assertEqual(analysis.market_probabilities[0].odds, 2.01)

    def test_parse_game_event_accepts_game_id_and_score_fallback(self):
        payload = {
            "gameId": 77,
            "status": "LIVE",
            "SC": {"FS": {"S1": 2, "S2": 1}},
            "eventGroups": [{"groupId": 136, "events": [[{"type": 731, "cf": 5.0, "eventParams": {"params": ["4", "3"]}}]]}],
        }
        parsed = parse_game_event(payload)
        self.assertEqual(parsed["game_id"], 77)
        self.assertEqual(parsed["score1"], 2)
        self.assertEqual(parsed["score2"], 1)
        self.assertEqual(parsed["status"], "LIVE")

    def test_exact_score_parameter_respects_winning_team_sublist(self):
        score_events = [
            {"type": 731, "parameter": "5.004", "cf": 1.97},
            {"type": 731, "parameter": 4.003, "cf": 3.02},
            {"type": 731, "parameter": "4.002", "cf": 5.88},
        ]
        home_win = analyze_game_event(
            {
                "status": "PRE-MATCH",
                "eventGroups": [
                    {"groupId": 136, "events": [score_events, []]}
                ],
            }
        )
        away_win = analyze_game_event(
            {
                "status": "PRE-MATCH",
                "eventGroups": [
                    {"groupId": 136, "events": [[], score_events]}
                ],
            }
        )

        self.assertEqual(
            {prediction.score for prediction in home_win.exact_scores},
            {(5, 4), (4, 3), (4, 2)},
        )
        self.assertEqual(
            {prediction.score for prediction in away_win.exact_scores},
            {(4, 5), (3, 4), (2, 4)},
        )

    def test_extracts_every_exact_score_from_both_outcome_columns(self):
        home_events = [
            {
                "type": 731,
                "cf": 2.0,
                "eventParams": {"params": ["0", "3"]},
            },
            {
                "type": 731,
                "cf": 35.0,
                "parameter": "4.003",
                "eventParams": {"params": ["10", "9"]},
            },
            {
                "type": 731,
                "cf": 20.0,
                "eventParams": {"params": ["12", "11"]},
            },
            {
                "type": 731,
                "cf": 18.0,
                "eventParams": {"params": ["20", "19"]},
            },
            {
                "type": 731,
                "cf": 12.0,
                "eventParams": {"params": ["9", "8"]},
            },
        ]
        away_events = [
            {
                "type": 731,
                "cf": 2.1,
                "eventParams": {"params": ["0", "3"]},
            },
            {
                "type": 731,
                "cf": 17.0,
                "eventParams": {"params": ["6", "7"]},
            },
            {
                "type": 731,
                "cf": 11.0,
                "eventParams": {"params": ["8", "7"]},
            },
            {
                "type": 731,
                "cf": 10.0,
                "eventParams": {"params": ["9", "8"]},
            },
            {
                "type": 731,
                "cf": 14.0,
                "eventParams": {"params": ["12", "11"]},
            },
            {
                "type": 731,
                "cf": 20.0,
                "eventParams": {"params": ["20", "19"]},
            },
        ]

        analysis = analyze_game_event(
            {
                "status": "PRE-MATCH",
                "eventGroups": [
                    {
                        "groupId": 136,
                        "events": [home_events, away_events],
                    }
                ],
            }
        )

        self.assertEqual(len(analysis.exact_scores), 11)
        self.assertEqual(
            {prediction.score for prediction in analysis.exact_scores},
            {
                (3, 0),
                (0, 3),
                (10, 9),
                (12, 11),
                (11, 12),
                (20, 19),
                (19, 20),
                (9, 8),
                (6, 7),
                (7, 8),
                (8, 9),
            },
        )

    def test_extracts_unknown_market_groups_and_skips_blocked_or_issue_events(self):
        analysis = analyze_game_event(
            {
                "status": "PRE-MATCH",
                "team1": "Home",
                "team2": "Away",
                "eventGroups": [
                    {
                        "groupId": 901,
                        "groupName": "Total pair",
                        "events": [
                            [
                                {
                                    "type": 1,
                                    "name": "Pair",
                                    "cf": 1.8,
                                    "isCenter": True,
                                },
                                {"type": 2, "name": "Impair", "cf": 2.0},
                                {
                                    "type": 3,
                                    "name": "Bloqué",
                                    "cf": 3.0,
                                    "blocked": True,
                                },
                                {"type": 4, "name": "Issue 4", "cf": 4.0},
                            ]
                        ],
                    }
                ],
            }
        )

        self.assertEqual(
            [quote.label for quote in analysis.regulation_markets],
            ["Total pair: Pair", "Total pair: Impair"],
        )
        self.assertTrue(analysis.regulation_markets[0].is_center)

    def test_group_one_odds_display_the_favorite(self):
        payload = game_event(45, odds=9.0)
        payload["opponent1"] = {"fullName": "Bayern Munich"}
        payload["opponent2"] = {"fullName": "Juventus"}
        parsed = parse_game_event(payload)
        analysis = analyze_game_event(parsed)

        winner_markets = {
            market.event_type: (market.label, market.odds)
            for market in analysis.market_probabilities
            if market.group_id == 1
        }
        self.assertEqual(winner_markets[1], ("Bayern Munich", 1.55))
        self.assertEqual(winner_markets[3], ("Juventus", 2.28))
        self.assertIn(
            "<b>Bayern Munich</b> ⭐ Favori\n@1.55",
            format_prediction_message(parsed, analysis),
        )

    def test_parse_game_event_assigns_default_team_names_when_missing(self):
        payload = {
            "id": 88,
            "nonStarted": True,
            "statusLineStr": "Match test",
            "timer": {"timeRun": False, "timeSec": 120},
            "scores": {"scoreOpp1": 0, "scoreOpp2": 0, "fullScore": {"S1": 0, "S2": 0}},
            "eventGroups": [],
        }
        parsed = parse_game_event(payload)
        self.assertEqual(parsed["team1"], "Équipe locale")
        self.assertEqual(parsed["team2"], "Équipe visiteuse")

    def test_parse_game_event_prefers_real_opponent_names_over_placeholders(self):
        payload = game_event(880, odds=9.0)
        payload.update(
            {
                "team1": "Équipe locale",
                "team2": "Équipe visiteuse",
                "opponent1": {"name": "Paris FC"},
                "opponent2": {"shortName": "Lyon"},
            }
        )

        parsed = parse_game_event(payload)

        self.assertEqual(parsed["team1"], "Paris FC")
        self.assertEqual(parsed["team2"], "Lyon")

    def test_parse_game_event_detects_explicit_finished_status(self):
        payload = game_event(
            89,
            odds=4.0,
            live=True,
            time_sec=300,
            score=(6, 5),
        )
        payload["statusLineStr"] = "Fin du match"

        parsed = parse_game_event(payload)

        self.assertEqual(parsed["status"], "FINISHED")
        self.assertTrue(parsed["is_finished"])

    def test_run_collector_defaults_to_local_sample_when_feed_url_missing(self):
        with patch.dict(os.environ, {"DATA_SOURCE": "", "FEED_API_URL": ""}, clear=True):
            with (
                patch(
                    "collector.LocalFileCollector.run", new_callable=AsyncMock
                ) as run,
                patch(
                    "telegram_bot._run_command_listener", new_callable=AsyncMock
                ) as command_listener,
                patch("collector.MatchHistoryRepository"),
                patch("my_ai_brain.TerminatAI") as ai_class,
            ):
                asyncio.run(telegram_bot._run_collector())
        run.assert_awaited_once()
        command_listener.assert_awaited_once()
        ai_class.return_value.start_auto_training.assert_called_once()
        ai_class.return_value.stop_auto_training.assert_called_once_with()

    def test_paused_processor_keeps_analyzing_without_sending_alerts(self):
        sender = unittest.mock.Mock()
        tracker = unittest.mock.Mock(wraps=OddsSignalTracker())
        processor = GameEventProcessor(sender=sender, tracker=tracker)
        processor.is_paused = True

        with patch("telegram_bot.time.monotonic", side_effect=[100, 105]):
            self.assertFalse(
                processor.process_game_event(
                    game_event(29, odds=9.0, time_sec=120)
                )
            )
            self.assertFalse(
                processor.process_game_event(
                    game_event(30, odds=10.0, live=True, time_sec=10)
                )
            )
            self.assertFalse(
                processor.process_game_event(
                    game_event(30, odds=8.0, live=True, time_sec=15)
                )
            )

        sender.assert_not_called()
        self.assertEqual(tracker.update.call_count, 2)
        self.assertEqual(processor.processed_match_count, 2)

    def test_authorized_telegram_commands_update_pause_and_status(self):
        processor = GameEventProcessor(sender=lambda *_args: None)
        processor.process_game_event(game_event(31, odds=9.0, time_sec=120))
        update = {
            "message": {
                "chat": {"id": 123},
                "text": "/pause@prediction_bot",
            }
        }
        session = object()

        with patch(
            "telegram_bot._send_telegram_text", new_callable=AsyncMock
        ) as send:
            asyncio.run(
                telegram_bot._handle_telegram_update(
                    update, processor, "123", "token", session
                )
            )
            self.assertTrue(processor.is_paused)
            send.assert_awaited_once_with(
                session,
                "token",
                "123",
                "⏸️ Alertes mises en pause. Envoyez /resume pour reprendre.",
            )

            send.reset_mock()
            update["message"]["text"] = "/status"
            asyncio.run(
                telegram_bot._handle_telegram_update(
                    update, processor, "123", "token", session
                )
            )
            status_reply = send.await_args.args[3]
            self.assertIn("⏸️ En pause", status_reply)
            self.assertIn("Matchs traités durant la session : 1", status_reply)
            self.assertIn(f"{telegram_bot.SIGNAL_DROP_PCT:g}%", status_reply)
            self.assertIn(f"{telegram_bot.SIGNAL_WINDOW_SEC:g} s", status_reply)

            send.reset_mock()
            update["message"]["text"] = "/resume"
            asyncio.run(
                telegram_bot._handle_telegram_update(
                    update, processor, "123", "token", session
                )
            )

        self.assertFalse(processor.is_paused)
        self.assertEqual(
            send.await_args.args[3],
            "▶️ Alertes réactivées ! Surveillance FC24 Penalty en cours.",
        )

    def test_telegram_commands_ignore_updates_outside_authorized_chat(self):
        processor = GameEventProcessor()
        update = {
            "message": {
                "chat": {"id": 456},
                "text": "/pause",
            }
        }

        with patch(
            "telegram_bot._send_telegram_text", new_callable=AsyncMock
        ) as send:
            asyncio.run(
                telegram_bot._handle_telegram_update(
                    update, processor, "123", "token", object()
                )
            )

        self.assertFalse(processor.is_paused)
        send.assert_not_awaited()

    def test_stats_command_replies_with_database_summary(self):
        processor = GameEventProcessor()
        update = {
            "message": {
                "chat": {"id": 123},
                "text": "/stats",
            }
        }
        session = object()

        with (
            patch(
                "telegram_bot._format_stats_message",
                return_value="📊 Matchs enregistrés : 2",
            ) as format_stats,
            patch(
                "telegram_bot._send_telegram_text", new_callable=AsyncMock
            ) as send,
        ):
            asyncio.run(
                telegram_bot._handle_telegram_update(
                    update, processor, "123", "token", session
                )
            )

        format_stats.assert_called_once_with()
        send.assert_awaited_once_with(
            session, "token", "123", "📊 Matchs enregistrés : 2"
        )

    def test_ai_command_replies_with_local_learning_status(self):
        brain = unittest.mock.Mock()
        brain.format_status.return_value = "🧠 TerminatAI — apprentissage local"
        processor = GameEventProcessor(ai_brain=brain)
        update = {
            "message": {
                "chat": {"id": 123},
                "text": "/ai",
            }
        }
        session = object()

        with patch(
            "telegram_bot._send_telegram_text", new_callable=AsyncMock
        ) as send:
            asyncio.run(
                telegram_bot._handle_telegram_update(
                    update, processor, "123", "token", session
                )
            )

        brain.format_status.assert_called_once_with()
        send.assert_awaited_once_with(
            session,
            "token",
            "123",
            "🧠 TerminatAI — apprentissage local",
        )

    def test_prediction_does_not_render_the_legacy_ai_block(self):
        message = format_prediction_message(
            {
                "game_id": 42,
                "status": "PRE-MATCH",
                "team1": "Home",
                "team2": "Away",
            },
            GameAnalysis(exact_scores=(), balanced_total=None),
        )

        self.assertNotIn("🧠 IA TERMINATAI", message)

    def test_prediction_includes_matchup_and_terminatai_decision(self):
        message = format_prediction_message(
            {
                "game_id": 43,
                "status": "PRE-MATCH",
                "team1": "Home",
                "team2": "Away",
            },
            GameAnalysis(exact_scores=(), balanced_total=None),
            ai_review={
                "approved": True,
                "preference": {
                    "bet": "CHOIX CONTRADICTOIRE @8.5",
                    "confidence_pct": 100.0,
                    "regime": "CYCLE COURT",
                },
                "matchup": {
                    "team1": {
                        "matches": 2,
                        "average_goals_scored": 4.0,
                        "win_rate_pct": 50.0,
                        "sudden_death_rate_pct": 25.0,
                    },
                    "team2": {
                        "matches": 3,
                        "average_goals_scored": 3.0,
                        "win_rate_pct": 33.3,
                        "sudden_death_rate_pct": 0.0,
                    },
                    "h2h": {
                        "matches_played": 1,
                        "average_score_team1": 4.0,
                        "average_score_team2": 3.0,
                    },
                },
                "decision": {
                    "market_type": "Temps réglementaire",
                    "label": "Total Home (3.5) Plus",
                    "odds": 1.8,
                    "confidence_pct": 60.0,
                    "value": 1.08,
                },
                "top_markets": [
                    {
                        "label": "Total Home (3.5) Plus",
                        "odds": 1.8,
                        "confidence_pct": 60.0,
                        "value": 1.08,
                    }
                ],
                "top_exact_scores": [],
            },
        )

        self.assertIn("📊 CONFRONTATIONS DIRECTES", message)
        self.assertIn("1 matchs · score moyen 4.0 - 3.0", message)
        self.assertIn("🧠 DÉCISION IA", message)
        self.assertIn("<b>👉 Total Home (3.5) Plus</b>\n✅ Cote : @1.8", message)
        self.assertIn("Confiance : 60.0%", message)
        self.assertNotIn("CHOIX CONTRADICTOIRE", message)
        self.assertNotIn("🧠 IA TERMINATAI", message)
        self.assertLess(message.index("DÉCISION IA"), message.index("COTES 1X2"))
        self.assertLess(
            message.index("CONFRONTATIONS DIRECTES"), message.index("TENDANCE")
        )

    def test_stats_read_distinct_matches_and_latest_score_per_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from collector import MatchHistoryRepository

            database = Path(temp_dir) / "penalty_history.db"
            repository = MatchHistoryRepository(database)
            for game_id, teams, score in (
                (1, ("A", "B"), (0, 0)),
                (2, ("C", "D"), (0, 1)),
                (3, ("E", "F"), (1, 1)),
                (4, ("G", "H"), (2, 1)),
                (5, ("I", "J"), (1, 2)),
                (6, ("K", "L"), (3, 2)),
            ):
                repository.record_match(
                    {
                        "game_id": game_id,
                        "team1": teams[0],
                        "team2": teams[1],
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
            repository.record_match(
                {
                    "game_id": 1,
                    "team1": "A",
                    "team2": "B",
                    "status": "FINISHED",
                    "score1": 1,
                    "score2": 0,
                },
                fav_team=None,
                fav_odds=None,
                center_total_line=None,
                over_odds=None,
                under_odds=None,
            )

            message = telegram_bot._format_stats_message(database)

        self.assertIn("Matchs suivis : 6", message)
        self.assertIn("Bilan des 15 derniers matchs terminés : 6 match(s)", message)
        self.assertIn("A 1-0 B", message)
        self.assertIn("K 3-2 L", message)
        self.assertNotIn("A 0-0 B", message)

    def test_sent_live_ticket_is_settled_and_counted_in_real_win_rate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from collector import MatchHistoryRepository

            repository = MatchHistoryRepository(
                Path(temp_dir) / "penalty_history.db"
            )
            sender = unittest.mock.Mock()
            tracker = unittest.mock.Mock()
            tracker.update.return_value = SignalReport(
                (OddsSignal("94", "winner:1", 2.0, 1.6, 20.0),)
            )
            brain = approving_ai()
            brain.maybe_train.return_value = False
            processor = GameEventProcessor(
                sender=sender,
                tracker=tracker,
                history_repository=repository,
                ai_brain=brain,
            )

            self.assertTrue(
                processor.process_game_event(
                    game_event(94, odds=9.0, live=True, score=(1, 0))
                )
            )
            self.assertTrue(repository.has_sent_prediction(94))
            sender.assert_called_once()
            self.assertIn(
                "🚨 SIGNAL FORT (Chute de cote -20.0%)",
                sender.call_args.args[0],
            )
            self.assertIn("🔴 LIVE · Score : 1 - 0", sender.call_args.args[0])
            self.assertIn("🧠 DÉCISION IA", sender.call_args.args[0])
            self.assertIn("🎯 À MISER", sender.call_args.args[0])
            self.assertNotIn("indicatif non filtré", sender.call_args.args[0])
            finished = game_event(
                94, odds=9.0, live=False, score=(2, 0)
            )
            finished["isFinished"] = True
            self.assertFalse(processor.process_game_event(finished))

            losing_sender = unittest.mock.Mock()
            losing_tracker = unittest.mock.Mock()
            losing_tracker.update.return_value = SignalReport(
                (OddsSignal("95", "winner:1", 2.0, 1.6, 20.0),)
            )
            losing_brain = approving_ai()
            losing_brain.maybe_train.return_value = False
            losing_processor = GameEventProcessor(
                sender=losing_sender,
                tracker=losing_tracker,
                history_repository=repository,
                ai_brain=losing_brain,
            )
            self.assertTrue(
                losing_processor.process_game_event(
                    game_event(95, odds=9.0, live=True, score=(1, 0))
                )
            )
            losing_finished = game_event(
                95, odds=9.0, live=False, score=(0, 1)
            )
            losing_finished["isFinished"] = True
            self.assertFalse(losing_processor.process_game_event(losing_finished))

            stats = repository.get_prediction_statistics()
            live_stats = repository.get_live_signal_statistics()
            message = telegram_bot._format_stats_message(repository.db_path)

        self.assertEqual(stats["won"], 1)
        self.assertEqual(stats["lost"], 1)
        self.assertEqual(stats["win_rate_pct"], 50.0)
        self.assertIn("Taux de réussite réel : 50.0%", message)
        self.assertEqual(live_stats["detected"], 2)
        self.assertEqual(live_stats["sent"], 2)
        self.assertEqual(live_stats["won"], 1)
        self.assertEqual(live_stats["lost"], 1)
        self.assertIn("Signaux détectés : 2", message)
        self.assertIn("Tickets envoyés : 2", message)
        self.assertIn("Taux de réussite réel : 50.0% (1/2)", message)

    def test_processor_persists_finished_score_without_sending_prediction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            from collector import MatchHistoryRepository

            repository = MatchHistoryRepository(
                Path(temp_dir) / "penalty_history.db"
            )
            sender = unittest.mock.Mock()
            processor = GameEventProcessor(
                sender=sender, history_repository=repository
            )
            payload = game_event(
                90,
                odds=4.0,
                live=True,
                time_sec=300,
                score=(6, 5),
            )
            payload["isFinished"] = True

            self.assertFalse(processor.process_game_event(payload))

            recent = repository.get_recent_matches()

        sender.assert_not_called()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0][0], "90")
        self.assertEqual(recent[0][3:7], (6, 5, "Équipe visiteuse", 11))

    def test_live_template_keeps_total_and_extended_scores(self):
        analysis = GameAnalysis(
            exact_scores=(
                ExactScorePrediction((6, 5), 2.0, 50.0),
                ExactScorePrediction((5, 6), 2.5, 40.0),
                ExactScorePrediction((7, 6), 3.0, 30.0),
            ),
            balanced_total=TotalBalance(
                threshold=8.5,
                over_odds=1.6,
                under_odds=2.2,
                over_probability_pct=62.5,
                under_probability_pct=45.5,
                selected_side="Moins de",
                selected_odds=2.2,
                selected_probability_pct=55.0,
            ),
            extra_penalties=ExtraPenaltiesPrediction(2.0, 1.8, "Non", 1.8),
        )
        match = {
            "game_id": 91,
            "status": "LIVE",
            "team1": "Home",
            "team2": "Away",
            "score1": 2,
            "score2": 1,
        }

        message = format_prediction_message(
            match, analysis, CycleStatistics(current_hour=12)
        )

        self.assertIn("🔴 LIVE · Score : 2 - 1", message)
        self.assertNotIn("ALERTE MORT SUBITE", message)
        self.assertIn("<b>🌐 TOTAL 8.5</b>\n<b>Moins de 8.5</b>\n@2.2 (55.0%)", message)
        self.assertIn("Recommandé : <b>Oui</b>", message)
        self.assertIn("🔥 SCORES PROLONGÉS", message)
        self.assertIn("🥇 6-5", message)
        self.assertNotIn("Aucun score exact", message)

    def test_extra_penalties_recommendation_uses_only_the_strict_yes_conditions(self):
        cases = (
            ("PRE-MATCH", 0, 0, 2.75, "OUI"),
            ("PRE-MATCH", 3, 3, 5.16, "NON"),
            ("LIVE", 3, 3, 5.16, "OUI"),
            ("LIVE", 4, 4, 5.16, "OUI"),
            ("LIVE", 5, 5, 5.16, "OUI"),
            ("LIVE", 6, 6, 5.16, "NON"),
        )
        for status, score1, score2, yes_odds, expected in cases:
            with self.subTest(status=status, score=(score1, score2)):
                message = format_prediction_message(
                    {
                        "game_id": 92,
                        "status": status,
                        "team1": "Home",
                        "team2": "Away",
                        "score1": score1,
                        "score2": score2,
                    },
                    GameAnalysis(
                        exact_scores=(),
                        balanced_total=None,
                        extra_penalties=ExtraPenaltiesPrediction(
                            yes_odds, 2.54, "Oui", yes_odds
                        ),
                    ),
                )

                self.assertIn(f"Recommandé : <b>{expected.title()}</b>", message)

    def test_prematch_high_odds_penalties_require_all_three_signals(self):
        cases = (
            (
                MarketQuote(
                    "over-8.5",
                    "Total Global (8.5) Plus de",
                    17,
                    9,
                    1.72,
                    8.5,
                ),
                CycleStatistics(recent_sudden_death_pct=25.0),
                4.5,
                "OUI",
                True,
            ),
            (
                MarketQuote(
                    "center-9.5",
                    "Total Global (9.5) Moins de",
                    17,
                    10,
                    1.9,
                    9.5,
                    is_center=True,
                ),
                CycleStatistics(hour_sudden_death_pct=25.0),
                4.5,
                "OUI",
                True,
            ),
            (
                MarketQuote(
                    "over-8.5",
                    "Total Global (8.5) Plus de",
                    17,
                    9,
                    1.73,
                    8.5,
                ),
                CycleStatistics(recent_sudden_death_pct=25.0),
                4.5,
                "NON",
                False,
            ),
            (
                MarketQuote(
                    "over-8.5",
                    "Total Global (8.5) Plus de",
                    17,
                    9,
                    1.72,
                    8.5,
                ),
                CycleStatistics(recent_sudden_death_pct=24.9),
                4.5,
                "NON",
                False,
            ),
            (
                MarketQuote(
                    "over-8.5",
                    "Total Global (8.5) Plus de",
                    17,
                    9,
                    1.72,
                    8.5,
                ),
                CycleStatistics(recent_sudden_death_pct=25.0),
                4.51,
                "NON",
                False,
            ),
        )
        for line_quote, stats, yes_odds, expected, is_opportunity in cases:
            with self.subTest(line=line_quote.key, yes_odds=yes_odds):
                message = format_prediction_message(
                    {"game_id": 93, "status": "PRE-MATCH"},
                    GameAnalysis(
                        exact_scores=(),
                        balanced_total=None,
                        extra_penalties=ExtraPenaltiesPrediction(
                            yes_odds, 2.54, "Oui", yes_odds
                        ),
                        regulation_markets=(line_quote,),
                    ),
                    stats,
                )

                self.assertIn(f"Recommandé : <b>{expected.title()}</b>", message)
                self.assertEqual(
                    "🔥 OPPORTUNITÉ COTE ÉLEVÉE" in message,
                    is_opportunity,
                )

    def test_short_cycle_formats_under_market_and_extended_scores(self):
        analysis = GameAnalysis(
            exact_scores=(
                ExactScorePrediction((3, 1), 2.0, 50.0),
                ExactScorePrediction((4, 2), 3.0, 30.0),
                ExactScorePrediction((6, 5), 4.0, 20.0),
            ),
            balanced_total=TotalBalance(
                threshold=7.5,
                over_odds=2.1,
                under_odds=1.7,
                over_probability_pct=47.6,
                under_probability_pct=58.8,
                selected_side="Plus de",
                selected_odds=2.1,
                selected_probability_pct=52.0,
            ),
        )

        message = format_prediction_message(
            {
                "game_id": 92,
                "status": "PRE-MATCH",
                "team1": "Home",
                "team2": "Away",
                "score1": 0,
                "score2": 0,
            },
            analysis,
            CycleStatistics(regime="CYCLE COURT (Under)", current_hour=12),
        )

        self.assertIn("<b>🌐 TOTAL 7.5</b>\n<b>Moins de 7.5</b>\n@1.7", message)
        self.assertIn("1️⃣ 3-1", message)
        self.assertIn("🔥 SCORES PROLONGÉS", message)

    def test_live_analysis_preserves_all_available_exact_scores(self):
        match = parse_game_event(
            game_event(
                23,
                odds=8.0,
                live=True,
                score=(2, 1),
                exact_scores=[
                    (1, 3, 4.0),
                    (2, 1, 10.0),
                    (3, 3, 7.0),
                    (3, 0, 5.0),
                ],
            )
        )
        analysis = analyze_game_event(match)
        self.assertEqual(
            {item.score for item in analysis.exact_scores},
            {(1, 3), (2, 1), (3, 3), (3, 0)},
        )
        self.assertAlmostEqual(analysis.exact_scores[0].probability_pct, 25.0)
        self.assertIsNone(analysis.balanced_total)

    def test_analyzes_regulation_totals_and_extra_penalties(self):
        payload = game_event(
            230,
            odds=9.0,
            exact_scores=[(4, 3, 2.0)],
        )
        payload["eventGroups"].extend(
            [
                {
                    "groupId": 17,
                    "events": [
                        {"type": 9, "parameter": "5.5", "cf": 1.91},
                        {"type": 10, "parameter": "5.5", "cf": 1.91},
                        {
                            "type": 9,
                            "parameter": "7.5",
                            "cf": 1.95,
                            "isCenter": True,
                        },
                        {
                            "type": 10,
                            "parameter": "7.5",
                            "cf": 1.87,
                            "isCenter": True,
                        },
                    ],
                },
                {
                    "groupId": 15,
                    "events": [
                        {
                            "type": 11,
                            "parameter": "3.5",
                            "cf": 1.75,
                            "isCenter": True,
                        },
                        {
                            "type": 12,
                            "parameter": "3.5",
                            "cf": 2.1,
                            "isCenter": True,
                        },
                    ],
                },
                {
                    "groupId": 62,
                    "events": [
                        {
                            "type": 13,
                            "parameter": "2.5",
                            "cf": 2.0,
                            "isCenter": True,
                        },
                        {
                            "type": 14,
                            "parameter": "2.5",
                            "cf": 1.6,
                            "isCenter": True,
                        },
                    ],
                },
                {
                    "groupId": 77,
                    "name": "Pénalités supplémentaires",
                    "events": [
                        {"type": 1, "name": "Oui", "cf": 2.1},
                        {"type": 2, "name": "Non", "cf": 1.7},
                    ],
                },
            ]
        )

        analysis = analyze_game_event(parse_game_event(payload))

        self.assertIsNotNone(analysis.balanced_total)
        self.assertEqual(analysis.balanced_total.threshold, 7.5)
        self.assertEqual(
            [(item.team_number, item.threshold, item.selected_side) for item in analysis.team_totals],
            [(1, 3.5, "Plus"), (2, 2.5, "Moins")],
        )
        self.assertIsNotNone(analysis.extra_penalties)
        self.assertEqual(analysis.extra_penalties.yes_odds, 2.1)
        self.assertEqual(analysis.extra_penalties.no_odds, 1.7)
        self.assertEqual(analysis.extra_penalties.likely_outcome, "Non")
        self.assertEqual(len(analysis.market_probabilities), 3)

    def test_prematch_sends_once_without_live_signal(self):
        sent_messages = []
        brain = approving_ai()
        processor = GameEventProcessor(
            sender=lambda text, *_: sent_messages.append(text),
            ai_brain=brain,
        )

        self.assertTrue(
            processor.process_game_event(game_event(24, odds=9.0, time_sec=59))
        )
        self.assertFalse(
            processor.process_game_event(game_event(24, odds=9.0, time_sec=120))
        )
        self.assertFalse(
            processor.process_game_event(game_event(24, odds=9.0, time_sec=90))
        )
        self.assertEqual(len(sent_messages), 1)
        self.assertIn("PRE-MATCH", sent_messages[0])
        self.assertIn("✅ Pari validé par Terminat-AI", sent_messages[0])
        self.assertIn("🎯 À MISER", sent_messages[0])
        self.assertNotIn("indicatif non filtré", sent_messages[0])
        brain.review_and_approve.assert_called_once()

    def test_prematch_sends_when_mock_ai_approves(self):
        sender = unittest.mock.Mock()
        brain = approving_ai()
        processor = GameEventProcessor(sender=sender, ai_brain=brain)

        self.assertTrue(
            processor.process_game_event(game_event(240, odds=9.0, time_sec=120))
        )

        brain.review_and_approve.assert_called_once()
        # Aucun mode indicatif : les seuils sont toujours appliqués.
        self.assertFalse(brain.review_and_approve.call_args.kwargs["unfiltered"])
        sender.assert_called_once()

    def test_sends_once_in_each_phase_and_persists_both(self):
        from collector import MatchHistoryRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = MatchHistoryRepository(
                Path(temp_dir) / "penalty_history.db"
            )
            sender = unittest.mock.Mock()
            processor = GameEventProcessor(
                sender=sender,
                history_repository=repository,
                ai_brain=approving_ai(),
                single_prediction_per_match=False,
            )

            self.assertTrue(
                processor.process_game_event(
                    game_event(2401, odds=9.0, time_sec=120)
                )
            )
            self.assertFalse(
                processor.process_game_event(
                    game_event(2401, odds=9.0, time_sec=90)
                )
            )
            self.assertTrue(
                processor.process_game_event(
                    game_event(2401, odds=9.0, time_sec=120, live=True)
                )
            )
            self.assertFalse(
                processor.process_game_event(
                    game_event(2401, odds=9.0, time_sec=90, live=True)
                )
            )

            self.assertEqual(sender.call_count, 2)
            self.assertTrue(repository.has_sent_prediction(2401, "PRE-MATCH"))
            self.assertTrue(repository.has_sent_prediction(2401, "LIVE"))

    def test_single_prediction_per_match_blocks_duplicate_live_alert_after_prematch(self):
        from collector import MatchHistoryRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = MatchHistoryRepository(
                Path(temp_dir) / "penalty_history.db"
            )
            sender = unittest.mock.Mock()
            processor = GameEventProcessor(
                sender=sender,
                history_repository=repository,
                ai_brain=approving_ai(),
                single_prediction_per_match=True,
            )

            self.assertTrue(
                processor.process_game_event(
                    game_event(3001, odds=9.0, time_sec=120)
                )
            )
            self.assertEqual(sender.call_count, 1)

            # Le match passe en LIVE : doit être bloqué pour éviter le doublon
            self.assertFalse(
                processor.process_game_event(
                    game_event(3001, odds=9.0, time_sec=120, live=True)
                )
            )
            self.assertEqual(sender.call_count, 1)
            self.assertTrue(repository.has_sent_prediction(3001, "PRE-MATCH"))
            self.assertFalse(repository.has_sent_prediction(3001, "LIVE"))

    def test_migrates_existing_sent_predictions_to_live_phase(self):
        from collector import MatchHistoryRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "penalty_history.db"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE sent_predictions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        game_id TEXT NOT NULL UNIQUE,
                        market_key TEXT NOT NULL,
                        market_label TEXT NOT NULL,
                        group_id INTEGER NOT NULL,
                        event_type INTEGER NOT NULL,
                        threshold REAL,
                        odds REAL NOT NULL,
                        confidence_pct REAL NOT NULL,
                        delivery_status TEXT NOT NULL DEFAULT 'SENDING',
                        prediction_status TEXT NOT NULL DEFAULT 'PENDING',
                        final_score1 INTEGER,
                        final_score2 INTEGER,
                        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        settled_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO sent_predictions (
                        game_id, market_key, market_label, group_id,
                        event_type, odds, confidence_pct, delivery_status
                    )
                    VALUES ('2402', 'winner:1', 'Home wins', 1, 1, 1.8, 60, 'SENT')
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX idx_sent_predictions_status
                    ON sent_predictions(prediction_status, sent_at DESC)
                    """
                )

            repository = MatchHistoryRepository(db_path)
            self.assertTrue(repository.has_sent_prediction(2402, "LIVE"))
            self.assertFalse(repository.has_sent_prediction(2402, "PRE-MATCH"))
            self.assertTrue(
                repository.reserve_sent_prediction(
                    {
                        "game_id": 2402,
                        "status": "PRE-MATCH",
                    },
                    {
                        "market_key": "winner:1",
                        "label": "Home wins",
                        "group_id": 1,
                        "event_type": 1,
                        "odds": 1.8,
                        "confidence_pct": 60,
                    },
                )
            )

    def test_processor_blocks_prediction_when_terminatai_rejects(self):
        sender = unittest.mock.Mock()
        brain = unittest.mock.Mock()
        brain.review_and_approve.return_value = {
            "approved": False,
            "reason": "Aucun marché qualifié",
        }
        brain.maybe_train.return_value = False
        tracker = unittest.mock.Mock()
        tracker.update.return_value = SignalReport(
            (OddsSignal("241", "winner:1", 2.0, 1.5, 25.0),)
        )
        processor = GameEventProcessor(
            sender=sender, tracker=tracker, ai_brain=brain
        )

        self.assertFalse(
            processor.process_game_event(
                game_event(241, odds=9.0, time_sec=120, live=True)
            )
        )

        brain.review_and_approve.assert_called_once()
        self.assertEqual(
            brain.review_and_approve.call_args.args[0]["game_id"], 241
        )
        self.assertEqual(len(brain.review_and_approve.call_args.args), 3)
        self.assertEqual(
            brain.review_and_approve.call_args.args[2], {"winner:1": 25.0}
        )
        self.assertFalse(brain.review_and_approve.call_args.kwargs["unfiltered"])
        sender.assert_not_called()

    def test_rejected_live_signal_is_snapshotted_and_settled(self):
        from collector import MatchHistoryRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = MatchHistoryRepository(
                Path(temp_dir) / "penalty_history.db"
            )
            sender = unittest.mock.Mock()
            brain = unittest.mock.Mock()
            brain.review_and_approve.return_value = {
                "approved": False,
                "reason": "Aucun marché qualifié",
            }
            brain.maybe_train.return_value = False
            tracker = unittest.mock.Mock()
            tracker.update.return_value = SignalReport(
                (
                    OddsSignal(
                        "242",
                        "winner:1",
                        2.0,
                        1.5,
                        25.0,
                        market_key="winner:1",
                    ),
                )
            )
            processor = GameEventProcessor(
                sender=sender,
                tracker=tracker,
                history_repository=repository,
                ai_brain=brain,
            )

            self.assertFalse(
                processor.process_game_event(
                    game_event(242, odds=9.0, time_sec=120, live=True)
                )
            )
            finished = game_event(
                242, odds=9.0, live=False, score=(0, 1)
            )
            finished["isFinished"] = True
            self.assertFalse(processor.process_game_event(finished))

            with closing(sqlite3.connect(repository.db_path)) as connection:
                row = connection.execute(
                    """
                    SELECT approved, final_score1, final_score2, outcome
                    FROM live_signal_snapshots
                    WHERE game_id = '242'
                    """
                ).fetchone()
            stats = repository.get_live_signal_statistics()
            stats_message = telegram_bot._format_stats_message(
                repository.db_path
            )

        self.assertEqual(row, (0, 0, 1, "LOST"))
        self.assertEqual(stats["detected"], 1)
        self.assertEqual(stats["sent"], 0)
        self.assertIn("Signaux détectés : 1", stats_message)
        self.assertIn("Tickets envoyés : 0", stats_message)
        sender.assert_not_called()

    def test_live_sends_once_without_waiting_for_signal(self):
        sent_messages = []
        processor = GameEventProcessor(
            sender=lambda text, signal=None: sent_messages.append((text, signal)),
            ai_brain=approving_ai(),
        )
        first = game_event(25, odds=10.0, live=True, score=(2, 2))
        second = game_event(25, odds=8.4, live=True, score=(2, 2))
        with patch("telegram_bot.time.monotonic", side_effect=[100, 105]):
            self.assertTrue(processor.process_game_event(first))
            self.assertFalse(processor.process_game_event(second))
        self.assertEqual(len(sent_messages), 1)
        self.assertFalse(sent_messages[0][1].STRONG_SIGNAL)
        self.assertIn(
            "🔴 LIVE · Score : 2 - 2",
            sent_messages[0][0],
        )
        self.assertIn(
            "🏟 <b>ÉQUIPE LOCALE vs ÉQUIPE VISITEUSE</b>",
            sent_messages[0][0],
        )
        self.assertIn("✅ Pari validé par Terminat-AI", sent_messages[0][0])
        self.assertIn("🎯 À MISER", sent_messages[0][0])
        self.assertNotIn("indicatif non filtré", sent_messages[0][0])

        high_score_processor = GameEventProcessor(
            sender=lambda text, signal=None: sent_messages.append((text, signal)),
            ai_brain=approving_ai(),
        )
        high_score_first = game_event(26, odds=10.0, live=True, score=(3, 2))
        high_score_next = game_event(26, odds=8.4, live=True, score=(3, 2))
        with patch("telegram_bot.time.monotonic", side_effect=[200, 205]):
            self.assertTrue(high_score_processor.process_game_event(high_score_first))
            self.assertFalse(high_score_processor.process_game_event(high_score_next))
        self.assertEqual(len(sent_messages), 2)

    def test_live_does_not_wait_for_ten_seconds_or_signal(self):
        sent_messages = []
        processor = GameEventProcessor(
            sender=lambda text, signal=None: sent_messages.append((text, signal)),
            ai_brain=approving_ai(),
        )
        early_first = game_event(
            27, odds=10.0, live=True, time_sec=5, score=(0, 0)
        )
        early_drop = game_event(
            27, odds=8.4, live=True, time_sec=9, score=(0, 0)
        )
        threshold_sample = game_event(
            27, odds=8.4, live=True, time_sec=10, score=(0, 0)
        )
        qualifying_drop = game_event(
            27, odds=7.0, live=True, time_sec=12, score=(0, 0)
        )

        with patch(
            "telegram_bot.time.monotonic",
            side_effect=[100, 102, 104, 106],
        ):
            self.assertTrue(processor.process_game_event(early_first))
            self.assertFalse(processor.process_game_event(early_drop))
            self.assertFalse(processor.process_game_event(threshold_sample))
            self.assertFalse(processor.process_game_event(qualifying_drop))

        self.assertEqual(len(sent_messages), 1)
        self.assertFalse(sent_messages[0][1].STRONG_SIGNAL)

    def test_sudden_death_highlights_most_likely_extended_scores(self):
        parsed = parse_game_event(
            game_event(
                28,
                odds=9.0,
                live=True,
                time_sec=20,
                score=(4, 4),
                exact_scores=[
                    (5, 4, 1.8),
                    (4, 5, 2.0),
                    (6, 5, 3.0),
                    (5, 6, 2.5),
                    (7, 6, 4.0),
                    (8, 7, 5.0),
                    (9, 8, 6.0),
                    (10, 9, 7.0),
                    (11, 10, 34.0),
                ],
            )
        )
        analysis = analyze_game_event(parsed)
        message = format_prediction_message(parsed, analysis)

        self.assertEqual(len(analysis.exact_scores), 9)
        self.assertIn("🔴 LIVE · Score : 4 - 4", message)
        self.assertIn("🔥 SCORES PROLONGÉS", message)
        self.assertIn("🥇 5-6", message)
        self.assertIn("🥈 6-5", message)
        self.assertIn("🥉 7-6", message)
        self.assertNotIn("5-4 │", message)
        self.assertNotIn("@34", message)

    def test_formats_live_message_with_the_requested_layout(self):
        analysis = GameAnalysis(
            exact_scores=(
                ExactScorePrediction((3, 4), 1.97, 50.8),
                ExactScorePrediction((2, 3), 3.02, 33.1),
                ExactScorePrediction((1, 3), 5.88, 17.0),
            ),
            balanced_total=TotalBalance(
                threshold=3,
                over_odds=1.55,
                under_odds=2.28,
                over_probability_pct=64.5,
                under_probability_pct=43.9,
                selected_side="Plus de",
                selected_odds=1.55,
                selected_probability_pct=59.6,
            ),
            market_probabilities=(
                MarketProbability(1, 1, "Bayern Munich", 1.55, 64.5),
                MarketProbability(1, 2, "Match nul", 3.2, 31.3),
                MarketProbability(1, 3, "Juventus", 2.28, 43.9),
            ),
            team_totals=(
                TeamTotalPrediction(1, 3.5, "Plus", 1.75),
                TeamTotalPrediction(2, 2.5, "Moins", 2.05),
            ),
            extra_penalties=ExtraPenaltiesPrediction(2.1, 1.7, "Non", 1.7),
        )
        message = format_prediction_message(
            {
                "game_id": 770428836,
                "status": "LIVE",
                "team1": "Bayern Munich",
                "team2": "Juventus",
                "match_number": 55,
                "score1": 1,
                "score2": 1,
            },
            analysis,
            CycleStatistics(
                regime="CYCLE MIXTE",
                recent_count=15,
                recent_sudden_death_pct=20.0,
                recent_average_goals=7.2,
                current_hour=10,
                hour_match_count=4,
                hour_sudden_death_pct=25.0,
            ),
        )

        self.assertTrue(message.startswith("🔴 LIVE · Score : 1 - 1"))
        self.assertIn("🏟 <b>BAYERN MUNICH vs JUVENTUS</b>\n#55 · ID 770428836", message)
        self.assertIn("<b>⚽ COTES 1X2</b>", message)
        self.assertIn("<b>🌐 TOTAL 3</b>\n<b>Plus de 3</b>", message)
        self.assertIn("<b>🏠 TOTAL Bayern Munich (3.5)</b>\n<b>Plus</b>", message)
        self.assertIn("<b>✈️ TOTAL Juventus (2.5)</b>\n<b>Moins</b>", message)
        self.assertIn("<b>Oui</b> @2.1", message)
        self.assertIn("Recommandé : <b>Oui</b>", message)
        self.assertIn("<b>🎯 SCORES EXACTS VALIDÉS</b>", message)
        self.assertIn("<b>📈 TENDANCE · 15 DERNIERS MATCHS</b>", message)
        self.assertLess(message.index("COTES 1X2"), message.index("TOTAL 3"))
        self.assertLess(message.index("TOTAL 3"), message.index("PÉNALITÉS"))
        self.assertLess(
            message.index("PÉNALITÉS"), message.index("SCORES EXACTS VALIDÉS")
        )
        self.assertLess(
            message.index("SCORES EXACTS VALIDÉS"), message.index("TENDANCE")
        )

    def test_processes_root_list_sends_each_prematch_match_once(self):
        sent_messages = []
        brain = approving_ai()
        processor = GameEventProcessor(
            sender=lambda text, *_: sent_messages.append(text),
            ai_brain=brain,
        )
        matches = [flat_match(31), flat_match(32)]

        self.assertTrue(processor.process_game_event(matches))
        self.assertFalse(processor.process_game_event(matches))
        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(brain.review_and_approve.call_count, 2)

    def test_formats_discovered_match_number_and_full_team_names(self):
        sent_messages = []
        processor = GameEventProcessor(
            sender=lambda text, *_: sent_messages.append(text),
            ai_brain=approving_ai(),
        )
        first = flat_match(34, time_run=True, over_odds=2.1)
        second = flat_match(34, time_run=True, over_odds=1.7)
        first.update(
            {
                "team1": "Real Madrid",
                "team2": "Arsenal",
                "match_number": "87",
            }
        )
        second.update(
            {
                "team1": "Real Madrid",
                "team2": "Arsenal",
                "match_number": "87",
            }
        )

        with patch("telegram_bot.time.monotonic", side_effect=[100, 105]):
            self.assertTrue(processor.process_game_event(first))
            self.assertFalse(processor.process_game_event(second))
        message = sent_messages[0]
        self.assertIn(
            "🏟 <b>REAL MADRID vs ARSENAL</b>\n#87 · ID 34",
            message,
        )
        self.assertIn("1X2", message)

    def test_flat_live_markets_trigger_one_strong_signal(self):
        sent_messages = []
        processor = GameEventProcessor(
            sender=lambda text, signal=None: sent_messages.append((text, signal)),
            ai_brain=approving_ai(),
        )
        first = flat_match(33, time_run=True, score=(1, 1), over_odds=2.1)
        second = flat_match(33, time_run=True, score=(1, 1), over_odds=1.7)
        with patch("telegram_bot.time.monotonic", side_effect=[300, 305]):
            self.assertTrue(processor.process_game_event(first))
            self.assertFalse(processor.process_game_event(second))

        self.assertEqual(len(sent_messages), 1)
        self.assertFalse(sent_messages[0][1].STRONG_SIGNAL)
        self.assertIn("✅ Pari validé par Terminat-AI", sent_messages[0][0])
        self.assertIn("🎯 À MISER", sent_messages[0][0])
        self.assertNotIn("indicatif non filtré", sent_messages[0][0])


if __name__ == "__main__":
    unittest.main()
