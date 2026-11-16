import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import unused_port

from collector import (
    APICollector,
    DEFAULT_CHAMPIONSHIP_ID,
    DEFAULT_POLL_INTERVAL_SEC,
    FeedAccessError,
    FeedRequestError,
    MatchHistoryRepository,
    GAME_EVENTS_PATH,
    LEFT_MENU_PATH,
    InvalidFeedResponseError,
    LocalFileCollector,
    TOP_GAMES_BY_STAT_PATH,
    _RetryableHTTPStatus,
    _iter_game_events,
)


class FakeProcessor:
    def __init__(self):
        self.events = []

    def process_game_event(self, event) -> bool:
        self.events.append(event)
        return False


class MatchHistoryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "penalty_history.db"
        self.repository = MatchHistoryRepository(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _record(self, match):
        self.repository.record_match(
            match,
            fav_team="Home",
            fav_odds=1.7,
            center_total_line=8.5,
            over_odds=1.6,
            under_odds=2.1,
        )

    def test_upserts_match_and_saves_finished_score_and_markets(self):
        with patch("collector.datetime") as mocked_datetime:
            mocked_datetime.now.return_value.hour = 7
            self._record(
                {
                    "game_id": 42,
                    "match_number": "55",
                    "team1": "Home",
                    "team2": "Away",
                    "status": "LIVE",
                    "score1": 4,
                    "score2": 4,
                }
            )
            mocked_datetime.now.return_value.hour = 13
            self._record(
                {
                    "game_id": 42,
                    "match_number": "55",
                    "team1": "Home",
                    "team2": "Away",
                    "status": "FINISHED",
                    "score1": 6,
                    "score2": 5,
                }
            )

        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                """
                SELECT game_id, hour_of_day, status, final_score1, final_score2,
                       total_goals, went_to_sudden_death, fav_team, fav_odds,
                       center_total_line, over_odds, under_odds
                FROM matches_history
                """
            ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0],
            ("42", 7, "FINISHED", 6, 5, 11, 1, "Home", 1.7, 8.5, 1.6, 2.1),
        )

    def test_saves_regulation_market_quotes_and_preserves_prematch_odds(self):
        opening_quotes = [
            {
                "key": "winner:1",
                "group_id": 1,
                "event_type": 1,
                "label": "Home",
                "odds": 1.8,
            },
            {
                "key": "double-chance:4",
                "group_id": 8,
                "event_type": 4,
                "label": "1X",
                "odds": 1.2,
            },
            {
                "key": "total:9:7.5",
                "group_id": 17,
                "event_type": 9,
                "threshold": 7.5,
                "label": "Plus de 7.5 buts",
                "is_center": True,
                "odds": 1.9,
            },
            {
                "key": "group-19:1",
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
                "group_label": "Pénalités supplémentaires",
                "outcome_label": "Oui",
                "is_extra_penalties": True,
                "odds": 2.0,
            },
        ]
        match = {
            "game_id": 55,
            "team1": "Home",
            "team2": "Away",
            "status": "PRE-MATCH",
            "score1": 0,
            "score2": 0,
        }
        self.repository.record_match(
            match,
            fav_team="Home",
            fav_odds=1.8,
            center_total_line=7.5,
            over_odds=1.9,
            under_odds=1.9,
            regulation_markets=opening_quotes,
            cycle_regime="CYCLE COURT",
        )
        self.repository.record_match(
            {**match, "status": "LIVE", "score1": 2, "score2": 1},
            fav_team="Home",
            fav_odds=1.6,
            center_total_line=7.5,
            over_odds=1.7,
            under_odds=2.1,
            regulation_markets=[
                {**opening_quotes[2], "odds": 1.7}
            ],
            cycle_regime="CYCLE PROLONGATIONS",
        )
        self.repository.record_match(
            {**match, "status": "FINISHED", "score1": 4, "score2": 3},
            fav_team="Home",
            fav_odds=1.6,
            center_total_line=7.5,
            over_odds=1.7,
            under_odds=2.1,
        )

        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                """
                SELECT market_key, odds, prematch_odds, status, cycle_regime,
                       is_center
                FROM regulation_markets_history
                WHERE game_id = '55'
                ORDER BY market_key
                """
            ).fetchall()

        self.assertEqual(len(rows), len(opening_quotes))
        market_rows = {row[0]: row[1:] for row in rows}
        self.assertEqual(
            market_rows["total:9:7.5"],
            (1.7, 1.9, "FINISHED", "CYCLE COURT", 1),
        )
        self.assertEqual(
            market_rows["double-chance:4"],
            (1.2, 1.2, "FINISHED", "CYCLE COURT", 0),
        )
        self.assertEqual(market_rows["total:9:7.5"][-1], 1)

    def test_records_and_settles_exact_score_odds_for_each_phase(self):
        prematch = {
            "game_id": 56,
            "team1": "Home",
            "team2": "Away",
            "status": "PRE-MATCH",
        }
        predictions = (
            SimpleNamespace(score=(4, 3), odds=9.2, probability_pct=10.9),
            SimpleNamespace(score=(5, 4), odds=12.0, probability_pct=8.3),
        )
        self.assertEqual(
            self.repository.record_exact_score_predictions(
                prematch, predictions
            ),
            2,
        )
        self.repository.record_exact_score_predictions(
            prematch,
            (
                SimpleNamespace(
                    score=(4, 3), odds=8.5, probability_pct=11.8
                ),
                predictions[1],
            ),
        )
        self.repository.record_exact_score_predictions(
            {**prematch, "status": "LIVE"},
            (
                SimpleNamespace(
                    score=(4, 3), odds=7.0, probability_pct=14.3
                ),
            ),
        )
        self._record(
            {
                **prematch,
                "status": "FINISHED",
                "score1": 4,
                "score2": 3,
            }
        )

        self.assertEqual(
            self.repository.settle_exact_score_predictions(
                {
                    "game_id": 56,
                    "status": "FINISHED",
                    "score1": 4,
                    "score2": 3,
                }
            ),
            3,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                """
                SELECT phase, predicted_score1, predicted_score2, odds,
                       prediction_status, final_score1, final_score2,
                       ai_displayed
                FROM exact_score_predictions
                WHERE game_id = '56'
                ORDER BY phase, predicted_score1, predicted_score2
                """
            ).fetchall()

        self.assertEqual(
            rows,
            [
                ("LIVE", 4, 3, 7.0, "WON", 4, 3, 1),
                ("PRE-MATCH", 4, 3, 9.2, "WON", 4, 3, 1),
                ("PRE-MATCH", 5, 4, 12.0, "LOST", 4, 3, 1),
            ],
        )

    def test_cycle_statistics_classify_recent_outcomes_and_current_hour(self):
        with patch("collector.datetime") as mocked_datetime:
            mocked_datetime.now.return_value.hour = 10
            for game_id, score in enumerate(
                ((3, 2), (4, 3), (3, 3), (5, 5)), 1
            ):
                self._record(
                    {
                        "game_id": game_id,
                        "team1": "Home",
                        "team2": "Away",
                        "status": "FINISHED",
                        "score1": score[0],
                        "score2": score[1],
                    }
                )

        stats = self.repository.get_cycle_statistics(hour_of_day=10)

        self.assertEqual(stats["regime"], "CYCLE PROLONGATIONS (Over / Mort Subite)")
        self.assertEqual(stats["recent_count"], 4)
        self.assertEqual(stats["recent_sudden_death_pct"], 25)
        self.assertEqual(stats["hour_match_count"], 4)
        self.assertEqual(stats["hour_sudden_death_pct"], 25)
        self.assertEqual(stats["recent_average_goals"], 7.0)

    def test_recent_statistics_use_only_finished_matches(self):
        self._record(
            {
                "game_id": 1,
                "team1": "Home",
                "team2": "Away",
                "status": "LIVE",
                "score1": 2,
                "score2": 1,
            }
        )

        self.assertEqual(self.repository.get_match_count(), 1)
        self.assertEqual(self.repository.get_recent_matches(), [])
        self.assertEqual(
            self.repository.get_cycle_statistics()["recent_count"], 0
        )

    def test_live_signal_snapshots_are_settled_and_count_only_sent_tickets(self):
        live_match = {
            "game_id": 88,
            "match_number": "12",
            "team1": "Home",
            "team2": "Away",
            "status": "LIVE",
            "score1": 3,
            "score2": 3,
        }
        snapshot_ids = {}
        for key, event_type in (("over", 9), ("under", 10)):
            snapshot_ids[key] = self.repository.record_live_signal_snapshot(
                live_match,
                trigger_time_sec=75,
                score1_at_trigger=3,
                score2_at_trigger=3,
                drop_pct=22.5,
                market_key=key,
                market_label=f"Total (7.5) {key}",
                odds_at_trigger=1.65,
                estimated_prob=0.7,
                group_id=17,
                event_type=event_type,
                threshold=7.5,
            )
        self.repository.update_live_signal_snapshot(
            snapshot_ids["under"], approved=True
        )

        self._record(
            {
                **live_match,
                "status": "FINISHED",
                "score1": 4,
                "score2": 3,
            }
        )

        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                """
                SELECT market_key, approved, final_score1, final_score2, outcome
                FROM live_signal_snapshots
                WHERE game_id = '88'
                ORDER BY market_key
                """
            ).fetchall()
        stats = self.repository.get_live_signal_statistics()

        self.assertEqual(
            rows,
            [
                ("over", 0, 4, 3, "LOST"),
                ("under", 1, 4, 3, "WON"),
            ],
        )
        self.assertEqual(stats["detected"], 2)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["win_rate_pct"], 100.0)


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def test_api_defaults_to_five_second_polling(self):
        self.assertEqual(DEFAULT_POLL_INTERVAL_SEC, 5.0)
        self.assertEqual(DEFAULT_CHAMPIONSHIP_ID, 2627439)

    def test_uses_region_feed_parameters(self):
        collector = APICollector("https://api.example", FakeProcessor())

        self.assertEqual(collector._left_menu_params["gr"], "448")
        self.assertEqual(collector._left_menu_params["ref"], "161")
        self.assertEqual(
            collector._left_menu_params["selectedMs"],
            "1.85.2627439,2.85.2627439,10.85.2627439",
        )
        self.assertEqual(
            collector._game_events_params,
            {
                "cfView": "3",
                "countEvents": "250",
                "fcountry": "165",
                "gameId": "",
                "gr": "448",
                "grMode": "4",
                "lng": "fr",
                "marketType": "1",
                "ref": "161",
            },
        )
        self.assertEqual(
            collector._top_games_params,
            {
                "cfView": "3",
                "excludeSportIds": "66",
                "fcountry": "165",
                "gr": "448",
                "lng": "fr",
                "ref": "161",
            },
        )
        self.assertEqual(collector._headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(collector._headers["x-app-n"], "__BETTING_APP__")
        self.assertEqual(collector._headers["x-svc-source"], "__BETTING_APP__")

    def test_handles_missing_opponent_objects(self):
        collector = APICollector("https://api.example", FakeProcessor())
        response = Mock(
            status_code=200,
            text="{}",
        )
        response.json.return_value = [
            {
                "id": DEFAULT_CHAMPIONSHIP_ID,
                "games": [
                    {
                        "id": 101,
                        "opponent1": None,
                        "opponent2": {"fullName": "Arsenal"},
                        "video": {"enabled": True},
                        "scores": {"timer": None},
                    }
                ],
            }
        ]
        with patch.object(collector.session, "get", return_value=response):
            matches = collector._fetch_matches_list()

        self.assertEqual(matches[0]["team1"], "Équipe 1")
        self.assertEqual(matches[0]["team2"], "Arsenal")

    def test_extracts_single_and_wrapped_game_events(self):
        first = {"id": 1, "scores": {}}
        second = {"id": 2, "scores": {}}
        payload = {"data": {"gameEvents": [first, second]}}
        self.assertEqual(list(_iter_game_events(payload)), [first, second])

    def test_configures_standard_authentication_headers(self):
        with patch.dict(
            os.environ,
            {"API_BEARER_TOKEN": "bearer-value", "API_KEY": "key-value"},
        ):
            collector = APICollector("https://api.example/feed", FakeProcessor())

        self.assertEqual(
            collector._headers,
            {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "x-app-n": "__BETTING_APP__",
                "x-svc-source": "__BETTING_APP__",
                "Authorization": "Bearer bearer-value",
                "X-API-Key": "key-value",
            },
        )

    async def test_honors_retry_after_for_rate_limited_response(self):
        for status in (429, 503):
            async def rate_limited(_request, response_status=status):
                return web.Response(
                    status=response_status, headers={"Retry-After": "4"}
                )

            app = web.Application()
            app.router.add_get("/feed", rate_limited)
            runner = web.AppRunner(app)
            await runner.setup()
            port = unused_port()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            collector = APICollector(
                f"http://127.0.0.1:{port}/feed", FakeProcessor()
            )
            try:
                async with ClientSession() as session:
                    with self.assertRaises(_RetryableHTTPStatus) as raised:
                        await collector._fetch_json(session)
            finally:
                await runner.cleanup()

            self.assertEqual(raised.exception.status, status)
            self.assertEqual(raised.exception.retry_after, 4)

    async def test_ignores_no_content_and_not_modified_responses(self):
            async def no_content(_request):
                return web.Response(status=204)

            async def not_modified(_request):
                return web.Response(status=304)

            app = web.Application()
            app.router.add_get("/empty", no_content)
            app.router.add_get("/cached", not_modified)
            runner = web.AppRunner(app)
            await runner.setup()
            port = unused_port()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            try:
                async with ClientSession() as session:
                    for path in ("empty", "cached"):
                        collector = APICollector(
                            f"http://127.0.0.1:{port}/{path}", FakeProcessor()
                        )
                        self.assertIsNone(await collector._fetch_json(session))
            finally:
                await runner.cleanup()

    async def test_fails_fast_when_feed_denies_access(self):
            async def forbidden(_request):
                return web.Response(status=403)

            app = web.Application()
            app.router.add_get("/feed", forbidden)
            runner = web.AppRunner(app)
            await runner.setup()
            port = unused_port()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            collector = APICollector(
                f"http://127.0.0.1:{port}/feed", FakeProcessor()
            )
            try:
                async with ClientSession() as session:
                    with self.assertRaises(FeedAccessError) as raised:
                        await collector._fetch_json(session)
            finally:
                await runner.cleanup()

            self.assertIn("HTTP 403", str(raised.exception))

    async def test_fails_fast_when_feed_rejects_request_parameters(self):
            async def bad_request(_request):
                return web.Response(status=400)

            app = web.Application()
            app.router.add_get("/feed", bad_request)
            runner = web.AppRunner(app)
            await runner.setup()
            port = unused_port()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            collector = APICollector(
                f"http://127.0.0.1:{port}/feed", FakeProcessor()
            )
            try:
                async with ClientSession() as session:
                    with self.assertRaises(FeedRequestError) as raised:
                        await collector._fetch_json(session)
            finally:
                await runner.cleanup()

            self.assertIn("HTTP 400", str(raised.exception))

    async def test_discovers_imminent_league_matches_and_attaches_metadata(self):
            processor = FakeProcessor()
            requested_game_ids = []
            requested_query_strings = []
            discovered_games = [
                {
                    "id": 101,
                    "dopInfo": "87",
                    "video": {"enabled": False},
                    "opponent1": {"fullName": "Real Madrid"},
                    "opponent2": {"fullName": "Arsenal"},
                    "scores": {"timer": {"timeRun": False, "timeSec": 120}},
                },
                {
                    "id": 102,
                    "dopInfo": "88",
                    "video": {"enabled": True},
                    "opponent1": {"name": "Milan"},
                    "opponent2": {"shortName": "Ajax"},
                    "scores": {"timer": {"timeRun": False, "timeSec": 600}},
                },
                {
                    "id": 103,
                    "dopInfo": "89",
                    "video": {"enabled": False},
                    "statusLineStr": "Fin du match",
                    "opponent1": {"fullName": "Team 3"},
                    "opponent2": {"fullName": "Team 4"},
                    "scores": {"timer": {"timeRun": False, "timeSec": 600}},
                },
            ]

            async def left_menu(_request):
                return web.json_response(
                    [
                        {
                            "sports": [
                                {
                                    "ligas": [
                                        {
                                            "id": DEFAULT_CHAMPIONSHIP_ID,
                                            "name": "FC24. Penalty",
                                            "games": discovered_games,
                                        },
                                        {
                                            "id": 999,
                                            "games": [
                                                {
                                                    "id": 104,
                                                    "scores": {
                                                        "timer": {
                                                            "timeRun": False,
                                                            "timeSec": 90,
                                                        }
                                                    },
                                                }
                                            ],
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                )

            async def game_events(request):
                game_id = request.query["gameId"]
                requested_game_ids.append(game_id)
                requested_query_strings.append(request.rel_url.query_string)
                return web.json_response(
                    {
                        "id": int(game_id),
                        "scores": {
                            "scoreOpp1": 0,
                            "scoreOpp2": 0,
                            "fullScore": "0-0",
                        },
                    }
                )

            app = web.Application()
            app.router.add_get(LEFT_MENU_PATH, left_menu)
            app.router.add_get(GAME_EVENTS_PATH, game_events)
            runner = web.AppRunner(app)
            await runner.setup()
            port = unused_port()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            collector = APICollector(
                f"http://127.0.0.1:{port}/",
                processor,
                championship_id=DEFAULT_CHAMPIONSHIP_ID,
            )
            try:
                async with ClientSession() as session:
                    await collector._poll_league(session)
            finally:
                await runner.cleanup()

            self.assertEqual(requested_game_ids, ["101", "102", "103"])
            self.assertEqual(
                requested_query_strings,
                [
                    "cfView=3&countEvents=250&fcountry=165&gameId=101&gr=448&grMode=4&lng=fr&marketType=1&ref=161",
                    "cfView=3&countEvents=250&fcountry=165&gameId=102&gr=448&grMode=4&lng=fr&marketType=1&ref=161",
                    "cfView=3&countEvents=250&fcountry=165&gameId=103&gr=448&grMode=4&lng=fr&marketType=1&ref=161",
                ],
            )
            self.assertEqual(len(processor.events), 3)
            first_event, second_event, finished_event = processor.events
            self.assertEqual(first_event["match_number"], "87")
            self.assertEqual(first_event["team1"], "Real Madrid")
            self.assertEqual(first_event["team2"], "Arsenal")
            self.assertFalse(first_event["video_enabled"])
            self.assertEqual(second_event["team1"], "Milan")
            self.assertEqual(second_event["team2"], "Ajax")
            self.assertTrue(finished_event["is_finished"])
            self.assertEqual(finished_event["status"], "FINISHED")
            self.assertTrue(second_event["video_enabled"])

    def test_api_collector_rejects_non_fc24_championship(self):
        with self.assertRaisesRegex(ValueError, "2627439"):
            APICollector(
                "https://example.test/feed",
                FakeProcessor(),
                championship_id=123,
            )

    async def test_adds_metadata_without_mutating_read_only_payload(self):
        processor = FakeProcessor()
        collector = APICollector("https://api.example", processor)
        payload = MappingProxyType({"id": 101, "scores": {}})
        match = {
            "id": 101,
            "match_number": "87",
            "team1": "Real Madrid",
            "team2": "Arsenal",
            "video_enabled": False,
        }

        with patch.object(
            collector,
            "_fetch_json",
            new=AsyncMock(return_value=payload),
        ):
            await collector._fetch_and_process_match(match, None)

        self.assertEqual(
            processor.events[0],
            {
                "id": 101,
                "scores": {},
                "match_number": "87",
                "team1": "Real Madrid",
                "team2": "Arsenal",
                "video_enabled": False,
            },
        )
        self.assertEqual(dict(payload), {"id": 101, "scores": {}})

    async def test_replaces_placeholder_team_names_with_feed_metadata(self):
        processor = FakeProcessor()
        collector = APICollector("https://api.example", processor)
        payload = {
            "id": 101,
            "team1": "Équipe locale",
            "team2": "Équipe visiteuse",
            "scores": {},
        }
        match = {
            "id": 101,
            "team1": "Arsenal",
            "team2": "Barcelona",
        }

        with patch.object(
            collector,
            "_fetch_json",
            new=AsyncMock(return_value=payload),
        ):
            await collector._fetch_and_process_match(match, None)

        self.assertEqual(processor.events[0]["team1"], "Arsenal")
        self.assertEqual(processor.events[0]["team2"], "Barcelona")

    def test_legacy_poll_replaces_placeholder_team_names_with_feed_metadata(self):
        processor = FakeProcessor()
        collector = APICollector("https://api.example", processor)
        payload = {
            "id": 101,
            "team1": "Équipe locale",
            "team2": "Équipe visiteuse",
            "scores": {},
        }
        match = {"id": 101, "team1": "Arsenal", "team2": "Barcelona"}

        with patch.object(collector, "_request_with_fallback", return_value=payload):
            self.assertTrue(collector._fetch_and_process_match_legacy(match))

        self.assertEqual(processor.events[0]["team1"], "Arsenal")
        self.assertEqual(processor.events[0]["team2"], "Barcelona")

    async def test_continues_polling_after_match_event_request_is_rejected(self):
        collector = APICollector("https://api.example", FakeProcessor())
        payload = {
            "id": DEFAULT_CHAMPIONSHIP_ID,
            "games": [
                {"id": 101, "scores": {"timer": {"timeRun": True}}},
                {"id": 102, "scores": {"timer": {"timeRun": True}}},
            ],
        }
        with (
            patch.object(collector, "_fetch_json", new=AsyncMock(return_value=payload)),
            patch.object(
                collector,
                "_fetch_and_process_match",
                new=AsyncMock(
                    side_effect=[
                        FeedRequestError("HTTP 400"),
                        False,
                    ]
                ),
            ) as fetch_match,
        ):
            matches = await collector._poll_league(None)

        self.assertEqual([match["id"] for match in matches], [101, 102])
        self.assertEqual(fetch_match.await_count, 2)

    async def test_returns_empty_league_list_when_feed_is_invalid_json(self):
        collector = APICollector("https://api.example", FakeProcessor())
        with patch.object(
            collector,
            "_fetch_json",
            new=AsyncMock(side_effect=InvalidFeedResponseError("HTTP 200 invalid JSON")),
        ):
            matches = await collector._poll_league(None)

        self.assertEqual(matches, [])

    async def test_uses_top_games_fallback_for_rejected_fc24_match(self):
        processor = FakeProcessor()
        collector = APICollector("https://api.example", processor)
        left_menu = {
            "id": DEFAULT_CHAMPIONSHIP_ID,
            "games": [
                {
                    "id": 101,
                    "opponent1": {"fullName": "Barcelona"},
                    "opponent2": {"fullName": "Bayern"},
                    "scores": {"timer": {"timeRun": True}},
                }
            ],
        }
        fallback_game = {
            "id": 101,
            "liga": {"id": DEFAULT_CHAMPIONSHIP_ID},
            "opponent1": {"fullName": "Barcelona"},
            "opponent2": {"fullName": "Bayern"},
            "event": {"groupId": 2, "type": 7, "cf": 1.89},
            "scores": {
                "scoreOpp1": 1,
                "scoreOpp2": 0,
                "currentPeriod": 1,
                "timer": {"timeRun": True, "timeSec": 42},
            },
        }
        with (
            patch.object(
                collector,
                "_fetch_json",
                new=AsyncMock(
                    side_effect=[
                        left_menu,
                        [{"events": [fallback_game], "id": 1001}],
                    ]
                ),
            ) as fetch_json,
            patch.object(
                collector,
                "_fetch_and_process_match",
                new=AsyncMock(side_effect=FeedRequestError("HTTP 400")),
            ),
        ):
            matches = await collector._poll_league(None)

        self.assertEqual([match["id"] for match in matches], [101])
        self.assertEqual(processor.events, [fallback_game])
        self.assertEqual(fetch_json.await_args_list[1].args[1], f"https://api.example{TOP_GAMES_BY_STAT_PATH}")

    async def test_rejects_successful_html_response_as_invalid_feed(self):
        async def html_response(_request):
            return web.Response(text="<html>homepage</html>", content_type="text/html")

        app = web.Application()
        app.router.add_get("/feed", html_response)
        runner = web.AppRunner(app)
        await runner.setup()
        port = unused_port()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        collector = APICollector(
            f"http://127.0.0.1:{port}/feed", FakeProcessor()
        )
        try:
            async with ClientSession() as session:
                with self.assertRaises(InvalidFeedResponseError) as raised:
                    await collector._fetch_json(session)
        finally:
            await runner.cleanup()

        self.assertIn("HTTP 200", str(raised.exception))
        self.assertIn("pas du JSON", str(raised.exception))

    async def test_local_file_collector_processes_all_events_once(self):
        events = [
            {"id": 1, "scores": {}, "liga": {"id": DEFAULT_CHAMPIONSHIP_ID}},
            {"id": 2, "scores": {}, "liga": {"id": DEFAULT_CHAMPIONSHIP_ID}},
        ]
        processor = FakeProcessor()
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample_events.json"
            file_path.write_text(json.dumps(events), encoding="utf-8")
            await LocalFileCollector(processor, file_path).run()

        self.assertEqual(processor.events, events)

    async def test_local_file_collector_ignores_events_outside_fc24_penalty(self):
        events = [
            {"id": 1, "scores": {}, "liga": {"id": 1}},
            {"id": 2, "scores": {}},
            {"id": 3, "scores": {}, "liga": {"id": DEFAULT_CHAMPIONSHIP_ID}},
        ]
        processor = FakeProcessor()
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample_events.json"
            file_path.write_text(json.dumps(events), encoding="utf-8")
            await LocalFileCollector(processor, file_path).run()

        self.assertEqual([event["id"] for event in processor.events], [3])

    async def test_default_sample_file_is_ready_for_local_mode(self):
        processor = FakeProcessor()
        await LocalFileCollector(processor).run()

        self.assertEqual(len(processor.events), 1)
        self.assertEqual(processor.events[0]["id"], 770161562)


if __name__ == "__main__":
    unittest.main()
