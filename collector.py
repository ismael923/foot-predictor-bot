"""Collecteur HTTP et local adapté pour FC24. Penalty (1xbet.sn) et Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp
import requests

try:
    from curl_cffi import requests as curl_requests  # pyright: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - optional dependency for browser-like TLS
    curl_requests = None

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 5.0
DEFAULT_CHAMPIONSHIP_ID = 2627439
MIRROR_DOMAINS = (
    "https://1xbet.com",
    "https://1xbet.sn",
    "https://1xbet.mobi",
)
DB_NAME = "penalty_matches.db"
HISTORY_DB_NAME = "penalty_history.db"
HISTORY_DB_PATH = Path(__file__).with_name(HISTORY_DB_NAME)

LEFT_MENU_PATH = "/service-api/main-live-feed/v3/leftMenuSports"
GAME_EVENTS_PATH = "/service-api/main-live-feed/v3/gameEvents"
TOP_GAMES_BY_STAT_PATH = "/service-api/main-live-feed/v3/topGamesByStat"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "x-app-n": "__BETTING_APP__",
    "x-svc-source": "__BETTING_APP__",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Connection": "keep-alive",
}

COOKIES = {
    "lng": "fr",
    "platform_type": "desktop",
    "cur": "XOF",
    "v3fr": "1",
}


class EventProcessor(Protocol):
    def process_game_event(self, event: Mapping[str, Any], /) -> bool: ...


class InvalidFeedResponseError(RuntimeError):
    """Erreur lorsque la réponse n'est pas un JSON valide."""


class FeedAccessError(RuntimeError):
    """Erreur d'accès au flux HTTP."""


FeedRequestError = FeedAccessError


class MatchHistoryRepository:
    """Persist match history, sent tickets, outcomes, and cycle statistics."""

    class _TrackedConnection:
        def __init__(self, connection: sqlite3.Connection, registry: set[sqlite3.Connection]) -> None:
            self._connection = connection
            self._registry = registry

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

        def __enter__(self) -> "MatchHistoryRepository._TrackedConnection":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                if exc_type is None:
                    self._connection.commit()
                else:
                    self._connection.rollback()
            finally:
                self.close()

        def close(self) -> None:
            if self._connection is None:
                return
            connection = self._connection
            self._connection = None
            self._registry.discard(connection)
            connection.close()

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        initialize: bool = True,
    ) -> None:
        self.db_path = HISTORY_DB_PATH if db_path is None else Path(db_path)
        self._open_connections: set[sqlite3.Connection] = set()
        if initialize:
            self._initialize()

    def _connect(self, *args: Any, **kwargs: Any) -> "MatchHistoryRepository._TrackedConnection":
        connection = sqlite3.connect(*args, **kwargs)
        self._open_connections.add(connection)
        return self._TrackedConnection(connection, self._open_connections)

    def __enter__(self) -> "MatchHistoryRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        while self._open_connections:
            connection = self._open_connections.pop()
            try:
                connection.close()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect(self.db_path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS matches_history (
                    game_id TEXT UNIQUE NOT NULL,
                    match_number TEXT,
                    team1 TEXT,
                    team2 TEXT,
                    hour_of_day INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    final_score1 INTEGER,
                    final_score2 INTEGER,
                    total_goals INTEGER,
                    went_to_sudden_death INTEGER,
                    fav_team TEXT,
                    fav_odds REAL,
                    center_total_line REAL,
                    over_odds REAL,
                    under_odds REAL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS regulation_markets_history (
                    game_id TEXT NOT NULL,
                    market_key TEXT NOT NULL,
                    group_id INTEGER NOT NULL,
                    event_type INTEGER NOT NULL,
                    threshold REAL,
                    label TEXT NOT NULL,
                    group_label TEXT NOT NULL,
                    outcome_label TEXT NOT NULL,
                    is_extra_penalties INTEGER NOT NULL DEFAULT 0,
                    is_center INTEGER NOT NULL DEFAULT 0,
                    odds REAL NOT NULL,
                    prematch_odds REAL,
                    status TEXT NOT NULL,
                    hour_of_day INTEGER NOT NULL,
                    cycle_regime TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (game_id, market_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exact_score_predictions (
                    game_id TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('PRE-MATCH', 'LIVE')),
                    predicted_score1 INTEGER NOT NULL,
                    predicted_score2 INTEGER NOT NULL,
                    odds REAL NOT NULL,
                    probability_pct REAL NOT NULL,
                    hour_of_day INTEGER NOT NULL,
                    prediction_status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (prediction_status IN ('PENDING', 'WON', 'LOST')),
                    final_score1 INTEGER,
                    final_score2 INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    settled_at TEXT,
                    ai_displayed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (
                        game_id, phase, predicted_score1, predicted_score2
                    )
                )
                """
            )
            exact_score_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(exact_score_predictions)"
                )
            }
            if "ai_displayed" not in exact_score_columns:
                connection.execute(
                    """
                    ALTER TABLE exact_score_predictions
                    ADD COLUMN ai_displayed INTEGER NOT NULL DEFAULT 0
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_exact_score_predictions_training
                ON exact_score_predictions(
                    prediction_status, ai_displayed, phase, odds
                )
                """
            )
            market_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(regulation_markets_history)"
                )
            }
            if "is_center" not in market_columns:
                connection.execute(
                    """
                    ALTER TABLE regulation_markets_history
                    ADD COLUMN is_center INTEGER NOT NULL DEFAULT 0
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_matches_history_recent
                ON matches_history(updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_matches_history_hour
                ON matches_history(hour_of_day, updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_matches_history_team1
                ON matches_history(team1 COLLATE NOCASE, updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_matches_history_team2
                ON matches_history(team2 COLLATE NOCASE, updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_regulation_markets_game
                ON regulation_markets_history(game_id, group_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'LIVE',
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
                    settled_at TEXT,
                    UNIQUE(game_id, phase)
                )
                """
            )
            prediction_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(sent_predictions)"
                )
            }
            if "phase" not in prediction_columns:
                connection.execute(
                    "DROP INDEX IF EXISTS idx_sent_predictions_status"
                )
                connection.execute(
                    "ALTER TABLE sent_predictions RENAME TO sent_predictions_legacy"
                )
                connection.execute(
                    """
                    CREATE TABLE sent_predictions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        game_id TEXT NOT NULL,
                        phase TEXT NOT NULL DEFAULT 'LIVE',
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
                        settled_at TEXT,
                        UNIQUE(game_id, phase)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO sent_predictions (
                        id, game_id, phase, market_key, market_label, group_id,
                        event_type, threshold, odds, confidence_pct,
                        delivery_status, prediction_status, final_score1,
                        final_score2, sent_at, settled_at
                    )
                    SELECT
                        id, game_id, 'LIVE', market_key, market_label, group_id,
                        event_type, threshold, odds, confidence_pct,
                        delivery_status, prediction_status, final_score1,
                        final_score2, sent_at, settled_at
                    FROM sent_predictions_legacy
                    """
                )
                connection.execute("DROP TABLE sent_predictions_legacy")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sent_predictions_status
                ON sent_predictions(prediction_status, sent_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS live_signal_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL,
                    match_number TEXT,
                    team1 TEXT,
                    team2 TEXT,
                    trigger_time_sec REAL,
                    score1_at_trigger INTEGER,
                    score2_at_trigger INTEGER,
                    drop_pct REAL NOT NULL,
                    market_key TEXT NOT NULL,
                    market_label TEXT NOT NULL,
                    odds_at_trigger REAL NOT NULL,
                    estimated_prob REAL,
                    approved INTEGER NOT NULL DEFAULT 0
                        CHECK (approved IN (0, 1)),
                    final_score1 INTEGER,
                    final_score2 INTEGER,
                    outcome TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (outcome IN ('WON', 'LOST', 'VOID', 'PENDING')),
                    group_id INTEGER,
                    event_type INTEGER,
                    threshold REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_live_signal_snapshots_category
                ON live_signal_snapshots(drop_pct, odds_at_trigger, outcome)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_live_signal_snapshots_game
                ON live_signal_snapshots(game_id, outcome)
                """
            )

    def record_match(
        self,
        match: Mapping[str, Any],
        *,
        fav_team: str | None,
        fav_odds: float | None,
        center_total_line: float | None,
        over_odds: float | None,
        under_odds: float | None,
        regulation_markets: Iterable[Mapping[str, Any]] = (),
        cycle_regime: str | None = None,
    ) -> None:
        game_id = match.get("game_id", match.get("gameId", match.get("id")))
        if game_id is None:
            raise ValueError("Un identifiant de match est requis pour l'historique")

        score1 = _history_int(match.get("score1"))
        score2 = _history_int(match.get("score2"))
        is_finished = (
            str(match.get("status", "")).upper() == "FINISHED"
            or match.get("is_finished") is True
        )
        final_score1 = score1 if is_finished else None
        final_score2 = score2 if is_finished else None
        total_goals = (
            score1 + score2
            if is_finished and score1 is not None and score2 is not None
            else None
        )
        went_to_sudden_death = (
            int(
                (score1 >= 5 and score2 >= 5) or total_goals >= 10
            )
            if total_goals is not None and score1 is not None and score2 is not None
            else None
        )
        match_number = match.get("match_number", match.get("dopInfo"))

        market_rows = list(regulation_markets)
        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            connection.execute(
                """
                INSERT INTO matches_history (
                    game_id, match_number, team1, team2, hour_of_day, status,
                    final_score1, final_score2, total_goals, went_to_sudden_death,
                    fav_team, fav_odds, center_total_line, over_odds, under_odds,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(game_id) DO UPDATE SET
                    match_number = COALESCE(excluded.match_number, matches_history.match_number),
                    team1 = COALESCE(excluded.team1, matches_history.team1),
                    team2 = COALESCE(excluded.team2, matches_history.team2),
                    status = excluded.status,
                    final_score1 = COALESCE(excluded.final_score1, matches_history.final_score1),
                    final_score2 = COALESCE(excluded.final_score2, matches_history.final_score2),
                    total_goals = COALESCE(excluded.total_goals, matches_history.total_goals),
                    went_to_sudden_death = COALESCE(
                        excluded.went_to_sudden_death,
                        matches_history.went_to_sudden_death
                    ),
                    fav_team = COALESCE(excluded.fav_team, matches_history.fav_team),
                    fav_odds = COALESCE(excluded.fav_odds, matches_history.fav_odds),
                    center_total_line = COALESCE(
                        excluded.center_total_line,
                        matches_history.center_total_line
                    ),
                    over_odds = COALESCE(excluded.over_odds, matches_history.over_odds),
                    under_odds = COALESCE(excluded.under_odds, matches_history.under_odds),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    str(game_id),
                    None if match_number is None else str(match_number),
                    match.get("team1"),
                    match.get("team2"),
                    datetime.now().hour,
                    str(match.get("status", "UNKNOWN")),
                    final_score1,
                    final_score2,
                    total_goals,
                    went_to_sudden_death,
                    fav_team,
                    fav_odds,
                    center_total_line,
                    over_odds,
                    under_odds,
                ),
            )
            for quote in market_rows:
                market_key = quote.get("key")
                group_id = _history_int(quote.get("group_id"))
                event_type = _history_int(quote.get("event_type"))
                odds = _history_float(quote.get("odds"))
                if (
                    not isinstance(market_key, str)
                    or not market_key
                    or group_id is None
                    or event_type is None
                    or odds is None
                    or odds <= 1
                ):
                    continue
                quote_status = str(match.get("status", "UNKNOWN"))
                connection.execute(
                    """
                    INSERT INTO regulation_markets_history (
                        game_id, market_key, group_id, event_type, threshold,
                        label, group_label, outcome_label, is_extra_penalties,
                        is_center, odds, prematch_odds, status, hour_of_day,
                        cycle_regime, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(game_id, market_key) DO UPDATE SET
                        group_id = excluded.group_id,
                        event_type = excluded.event_type,
                        threshold = excluded.threshold,
                        label = excluded.label,
                        group_label = excluded.group_label,
                        outcome_label = excluded.outcome_label,
                        is_extra_penalties = excluded.is_extra_penalties,
                        is_center = excluded.is_center,
                        odds = excluded.odds,
                        prematch_odds = CASE
                            WHEN excluded.status = 'PRE-MATCH'
                            THEN COALESCE(
                                regulation_markets_history.prematch_odds,
                                excluded.odds
                            )
                            ELSE regulation_markets_history.prematch_odds
                        END,
                        cycle_regime = COALESCE(
                            regulation_markets_history.cycle_regime,
                            excluded.cycle_regime
                        ),
                        status = excluded.status,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        str(game_id),
                        market_key,
                        group_id,
                        event_type,
                        _history_float(quote.get("threshold")),
                        str(quote.get("label") or market_key),
                        str(quote.get("group_label") or ""),
                        str(quote.get("outcome_label") or ""),
                        int(bool(quote.get("is_extra_penalties"))),
                        int(bool(quote.get("is_center"))),
                        odds,
                        odds if quote_status == "PRE-MATCH" else None,
                        quote_status,
                        datetime.now().hour,
                        cycle_regime,
                    ),
                )
            connection.execute(
                """
                UPDATE regulation_markets_history
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE game_id = ?
                """,
                (str(match.get("status", "UNKNOWN")), str(game_id)),
            )
        if is_finished:
            self.settle_live_signal_snapshots(match)

    def record_exact_score_predictions(
        self,
        match: Mapping[str, Any],
        predictions: Iterable[Any],
    ) -> int:
        game_id = match.get("game_id", match.get("gameId", match.get("id")))
        phase = str(match.get("status", "")).upper()
        if game_id is None or phase not in ("PRE-MATCH", "LIVE"):
            raise ValueError("Match PRE-MATCH/LIVE requis pour enregistrer les scores exacts")

        rows = []
        hour = _history_int(match.get("hour_of_day"))
        if hour is None:
            hour = datetime.now().hour
        for prediction in predictions:
            if isinstance(prediction, Mapping):
                score = prediction.get("score")
                odds = _history_float(prediction.get("odds"))
                probability_pct = _history_float(
                    prediction.get(
                        "confidence_pct", prediction.get("probability_pct")
                    )
                )
            else:
                score = getattr(prediction, "score", None)
                odds = _history_float(getattr(prediction, "odds", None))
                probability_pct = _history_float(
                    getattr(prediction, "probability_pct", None)
                )
            if isinstance(score, str):
                score_parts = score.split("-", 1)
                if len(score_parts) == 2:
                    score = tuple(score_parts)
            if (
                not isinstance(score, tuple)
                or len(score) != 2
                or any((val := _history_int(value)) is None or val < 0 for value in score)
                or odds is None
                or odds <= 1
                or probability_pct is None
                or not 0 <= probability_pct <= 100
            ):
                continue
            rows.append(
                (
                    str(game_id),
                    phase,
                    int(score[0]),
                    int(score[1]),
                    odds,
                    probability_pct,
                    hour,
                )
            )

        if not rows:
            return 0
        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            connection.executemany(
                """
                INSERT INTO exact_score_predictions (
                    game_id, phase, predicted_score1, predicted_score2,
                    odds, probability_pct, hour_of_day, ai_displayed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(
                    game_id, phase, predicted_score1, predicted_score2
                ) DO NOTHING
                """,
                rows,
            )
            return connection.total_changes

    def settle_exact_score_predictions(
        self, match: Mapping[str, Any]
    ) -> int:
        if str(match.get("status", "")).upper() != "FINISHED":
            raise ValueError("Les scores exacts ne peuvent être réglés qu'à FINISHED")
        score1 = _history_int(match.get("score1"))
        score2 = _history_int(match.get("score2"))
        game_id = match.get("game_id", match.get("gameId", match.get("id")))
        if score1 is None or score2 is None or game_id is None:
            raise ValueError("Score final invalide pour le règlement des scores exacts")

        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE exact_score_predictions
                SET prediction_status = CASE
                        WHEN predicted_score1 = ? AND predicted_score2 = ?
                        THEN 'WON' ELSE 'LOST'
                    END,
                    final_score1 = ?,
                    final_score2 = ?,
                    settled_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE game_id = ? AND prediction_status = 'PENDING'
                """,
                (score1, score2, score1, score2, str(game_id)),
            )
        return cursor.rowcount

    def record_live_signal_snapshot(
        self,
        match: Mapping[str, Any],
        *,
        trigger_time_sec: float | None,
        score1_at_trigger: int | None,
        score2_at_trigger: int | None,
        drop_pct: float,
        market_key: str,
        market_label: str,
        odds_at_trigger: float,
        estimated_prob: float | None,
        group_id: int | None = None,
        event_type: int | None = None,
        threshold: float | None = None,
    ) -> int:
        game_id = match.get("game_id", match.get("gameId", match.get("id")))
        drop = _history_float(drop_pct)
        odds = _history_float(odds_at_trigger)
        probability = (
            None if estimated_prob is None else _history_float(estimated_prob)
        )
        if (
            game_id is None
            or not market_key
            or not market_label
            or drop is None
            or drop <= 0
            or odds is None
            or odds <= 1
            or (probability is not None and not 0 <= probability <= 1)
        ):
            raise ValueError("Instantané de signal LIVE invalide")

        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO live_signal_snapshots (
                    game_id, match_number, team1, team2, trigger_time_sec,
                    score1_at_trigger, score2_at_trigger, drop_pct, market_key,
                    market_label, odds_at_trigger, estimated_prob, approved,
                    group_id, event_type, threshold
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    str(game_id),
                    (
                        None
                        if match.get("match_number", match.get("dopInfo")) is None
                        else str(match.get("match_number", match.get("dopInfo")))
                    ),
                    match.get("team1"),
                    match.get("team2"),
                    _history_float(trigger_time_sec),
                    _history_int(score1_at_trigger),
                    _history_int(score2_at_trigger),
                    drop,
                    market_key,
                    market_label,
                    odds,
                    probability,
                    _history_int(group_id),
                    _history_int(event_type),
                    _history_float(threshold),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite n'a pas retourné l'identifiant du signal")
            return int(cursor.lastrowid)

    def update_live_signal_snapshot(
        self,
        snapshot_id: int,
        *,
        estimated_prob: float | None = None,
        approved: bool | None = None,
    ) -> None:
        probability = (
            None if estimated_prob is None else _history_float(estimated_prob)
        )
        if probability is not None and not 0 <= probability <= 1:
            raise ValueError("La probabilité estimée doit être comprise entre 0 et 1")
        if approved is None and estimated_prob is None:
            raise ValueError("Aucune mise à jour d'instantané n'a été fournie")

        updates = []
        values: list[Any] = []
        if estimated_prob is not None:
            updates.append("estimated_prob = ?")
            values.append(probability)
        if approved is not None:
            updates.append("approved = ?")
            values.append(int(approved))
        values.append(snapshot_id)
        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            cursor = connection.execute(
                "UPDATE live_signal_snapshots SET "
                + ", ".join(updates)
                + " WHERE id = ?",
                values,
            )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Instantané LIVE {snapshot_id} introuvable pour mise à jour"
            )

    def settle_live_signal_snapshots(
        self, match: Mapping[str, Any]
    ) -> int:
        if str(match.get("status", "")).upper() != "FINISHED":
            raise ValueError("Les signaux LIVE ne peuvent être réglés qu'à FINISHED")
        score1 = _history_int(match.get("score1"))
        score2 = _history_int(match.get("score2"))
        game_id = match.get("game_id", match.get("gameId", match.get("id")))
        if score1 is None or score2 is None or game_id is None:
            raise ValueError("Score final invalide pour le règlement du signal LIVE")

        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            snapshots = connection.execute(
                """
                SELECT id, group_id, event_type, threshold
                FROM live_signal_snapshots
                WHERE game_id = ? AND outcome = 'PENDING'
                """,
                (str(game_id),),
            ).fetchall()
            updates = [
                (
                    self._prediction_result(
                        group_id,
                        event_type,
                        threshold,
                        score1,
                        score2,
                    ),
                    score1,
                    score2,
                    snapshot_id,
                )
                for snapshot_id, group_id, event_type, threshold in snapshots
            ]
            connection.executemany(
                """
                UPDATE live_signal_snapshots
                SET outcome = ?, final_score1 = ?, final_score2 = ?
                WHERE id = ? AND outcome = 'PENDING'
                """,
                updates,
            )
        return len(updates)

    def get_live_signal_statistics(self) -> dict[str, int | float | None]:
        try:
            with closing(self._connect(self.db_path, timeout=5)) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*),
                           SUM(approved = 1),
                           SUM(approved = 1 AND outcome = 'WON'),
                           SUM(approved = 1 AND outcome = 'LOST'),
                           SUM(approved = 1 AND outcome = 'VOID'),
                           SUM(approved = 1 AND outcome = 'PENDING')
                    FROM live_signal_snapshots
                    """
                ).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error).lower():
                raise
            row = (0, 0, 0, 0, 0, 0)
        detected, sent, wins, losses, voids, pending = row or (0, 0, 0, 0, 0, 0)
        wins = int(wins or 0)
        losses = int(losses or 0)
        settled = wins + losses
        return {
            "detected": int(detected or 0),
            "sent": int(sent or 0),
            "won": wins,
            "lost": losses,
            "void": int(voids or 0),
            "pending": int(pending or 0),
            "win_rate_pct": wins / settled * 100 if settled else None,
        }

    def get_cycle_statistics(self, hour_of_day: int | None = None) -> dict[str, Any]:
        current_hour = datetime.now().hour if hour_of_day is None else hour_of_day
        with closing(self._connect(self.db_path, timeout=5)) as connection:
            recent = connection.execute(
                """
                SELECT total_goals, went_to_sudden_death
                FROM matches_history
                WHERE total_goals IS NOT NULL
                ORDER BY updated_at DESC, game_id DESC
                LIMIT 15
                """
            ).fetchall()
            hour_rows = connection.execute(
                """
                SELECT went_to_sudden_death
                FROM matches_history
                WHERE hour_of_day = ? AND total_goals IS NOT NULL
                """,
                (current_hour,),
            ).fetchall()

        recent_count = len(recent)
        recent_sudden = sum(bool(row[1]) for row in recent)
        hour_count = len(hour_rows)
        hour_sudden = sum(bool(row[0]) for row in hour_rows)
        short_count = sum(row[0] <= 7 for row in recent)
        over_count = sum(row[0] >= 9 for row in recent)

        if recent and (recent_sudden > 0 or over_count > recent_count / 2):
            regime = "CYCLE PROLONGATIONS (Over / Mort Subite)"
        elif recent and short_count > recent_count / 2:
            regime = "CYCLE COURT (Under)"
        else:
            regime = "CYCLE MIXTE"

        return {
            "regime": regime,
            "recent_count": recent_count,
            "recent_sudden_death_pct": (
                recent_sudden / recent_count * 100 if recent_count else 0.0
            ),
            "recent_average_goals": (
                sum(row[0] for row in recent) / recent_count if recent_count else None
            ),
            "current_hour": current_hour,
            "hour_match_count": hour_count,
            "hour_sudden_death_pct": (
                hour_sudden / hour_count * 100 if hour_count else 0.0
            ),
        }

    def get_recent_matches(self, limit: int = 15) -> list[tuple[Any, ...]]:
        with closing(self._connect(self.db_path, timeout=5)) as connection:
            return connection.execute(
                """
                SELECT game_id, match_number, team1, final_score1, final_score2,
                       team2, total_goals, went_to_sudden_death, updated_at
                FROM matches_history
                WHERE total_goals IS NOT NULL
                ORDER BY updated_at DESC, game_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def get_match_count(self) -> int:
        with closing(self._connect(self.db_path, timeout=5)) as connection:
            result = connection.execute(
                "SELECT COUNT(*) FROM matches_history"
            ).fetchone()
        return int(result[0] if result is not None else 0)

    def has_sent_prediction(
        self, game_id: str | int, phase: str = "LIVE"
    ) -> bool:
        normalized_phase = str(phase).upper()
        if normalized_phase not in ("LIVE", "PRE-MATCH"):
            raise ValueError("Phase de pronostic invalide")
        with closing(self._connect(self.db_path, timeout=5)) as connection:
            row = connection.execute(
                "SELECT 1 FROM sent_predictions WHERE game_id = ? AND phase = ?",
                (str(game_id), normalized_phase),
            ).fetchone()
        return row is not None

    def reserve_sent_prediction(
        self,
        match: Mapping[str, Any],
        decision: Mapping[str, Any],
    ) -> bool:
        if str(match.get("status", "")).upper() not in ("LIVE", "PRE-MATCH"):
            raise ValueError(
                "Un ticket ne peut être enregistré que pour un match LIVE ou PRE-MATCH"
            )
        game_id = match.get("game_id", match.get("gameId", match.get("id")))
        phase = str(match.get("status", "")).upper()
        group_id = _history_int(decision.get("group_id"))
        event_type = _history_int(decision.get("event_type"))
        odds = _history_float(decision.get("odds"))
        confidence = _history_float(decision.get("confidence_pct"))
        market_key = decision.get("market_key")
        label = decision.get("label")
        if (
            game_id is None
            or not isinstance(market_key, str)
            or not market_key
            or not isinstance(label, str)
            or not label
            or group_id is None
            or event_type is None
            or odds is None
            or odds <= 1
            or confidence is None
            or not 0 <= confidence <= 100
        ):
            raise ValueError("Informations du ticket invalides")

        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO sent_predictions (
                    game_id, phase, market_key, market_label, group_id,
                    event_type, threshold, odds, confidence_pct, delivery_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SENDING')
                ON CONFLICT(game_id, phase) DO NOTHING
                """,
                (
                    str(game_id),
                    phase,
                    market_key,
                    label,
                    group_id,
                    event_type,
                    _history_float(decision.get("threshold")),
                    odds,
                    confidence,
                ),
            )
        return cursor.rowcount == 1

    def mark_prediction_sent(
        self, game_id: str | int, phase: str = "LIVE"
    ) -> None:
        normalized_phase = str(phase).upper()
        if normalized_phase not in ("LIVE", "PRE-MATCH"):
            raise ValueError("Phase de pronostic invalide")
        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE sent_predictions
                SET delivery_status = 'SENT'
                WHERE game_id = ? AND phase = ? AND delivery_status = 'SENDING'
                """,
                (str(game_id), normalized_phase),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Impossible de confirmer l'envoi du ticket du match {game_id}"
            )

    def settle_sent_predictions(self, match: Mapping[str, Any]) -> int:
        if str(match.get("status", "")).upper() != "FINISHED":
            raise ValueError("Les tickets ne peuvent être réglés qu'à FINISHED")
        score1 = _history_int(match.get("score1"))
        score2 = _history_int(match.get("score2"))
        game_id = match.get("game_id", match.get("gameId", match.get("id")))
        if score1 is None or score2 is None or game_id is None:
            raise ValueError("Score final invalide pour le règlement du ticket")

        with closing(self._connect(self.db_path, timeout=5)) as connection, connection:
            predictions = connection.execute(
                """
                SELECT id, group_id, event_type, threshold
                FROM sent_predictions
                WHERE game_id = ? AND delivery_status = 'SENT'
                  AND prediction_status = 'PENDING'
                """,
                (str(game_id),),
            ).fetchall()
            updates = [
                (
                    self._prediction_result(
                        group_id,
                        event_type,
                        threshold,
                        score1,
                        score2,
                    ),
                    score1,
                    score2,
                    prediction_id,
                )
                for prediction_id, group_id, event_type, threshold in predictions
            ]
            connection.executemany(
                """
                UPDATE sent_predictions
                SET prediction_status = ?, final_score1 = ?, final_score2 = ?,
                    settled_at = CURRENT_TIMESTAMP
                WHERE id = ? AND delivery_status = 'SENT'
                  AND prediction_status = 'PENDING'
                """,
                updates,
            )
        return len(updates)

    @staticmethod
    def _prediction_result(
        group_id: int,
        event_type: int,
        threshold: float | None,
        score1: int,
        score2: int,
    ) -> str:
        if group_id == 1:
            won = {
                1: score1 > score2,
                2: score1 == score2,
                3: score2 > score1,
            }.get(event_type)
            if won is None:
                return "VOID"
            return "WON" if won else "LOST"
        if group_id == 8:
            won = {
                4: score1 >= score2,
                5: score1 != score2,
                6: score2 >= score1,
            }.get(event_type)
            if won is None:
                return "VOID"
            return "WON" if won else "LOST"
        if group_id == 2 and threshold is not None:
            difference = (
                score1 + threshold - score2
                if event_type == 7
                else score2 + threshold - score1
                if event_type == 8
                else None
            )
            if difference is not None:
                if difference == 0:
                    return "VOID"
                return "WON" if difference > 0 else "LOST"
        if group_id == 17 and threshold is not None:
            total = score1 + score2
            if event_type == 9:
                if total == threshold:
                    return "VOID"
                return "WON" if total > threshold else "LOST"
            if event_type == 10:
                if total == threshold:
                    return "VOID"
                return "WON" if total < threshold else "LOST"
        if group_id == 19 and threshold is not None:
            total = score1 + score2
            th = float(threshold)
            real_threshold = (th + 0.5) if th.is_integer() else th
            if event_type == 11273:
                if total == real_threshold:
                    return "VOID"
                return "WON" if total > real_threshold else "LOST"
            if event_type == 11274:
                if total == real_threshold:
                    return "VOID"
                return "WON" if total < real_threshold else "LOST"
        if group_id in (15, 62) and threshold is not None:
            team_score = score1 if group_id == 15 else score2
            if event_type in (11, 13):
                if team_score == threshold:
                    return "VOID"
                return "WON" if team_score > threshold else "LOST"
            if event_type in (12, 14):
                if team_score == threshold:
                    return "VOID"
                return "WON" if team_score < threshold else "LOST"
        if group_id in (1094, 21, 65, 2769) or event_type in (2210, 2211, 180, 181):
            total = score1 + score2
            went_to_sudden_death = (score1 >= 5 and score2 >= 5) or total >= 10
            if event_type in (2210, 180):
                return "WON" if went_to_sudden_death else "LOST"
            if event_type in (2211, 181):
                return "WON" if not went_to_sudden_death else "LOST"

        # ── Group 14: Les deux équipes marquent (BTTS) ─────────────────────
        # event_type 183 = Oui, event_type 182 = Non
        if group_id == 14:
            btts = (score1 > 0 and score2 > 0)
            if event_type == 183:
                return "WON" if btts else "LOST"
            if event_type == 182:
                return "WON" if not btts else "LOST"

        # ── Group 2270: Total pair / impair ───────────────────────────────
        # event_type 2511 = Pair, event_type 2512 = Impair
        if group_id == 2270:
            total = score1 + score2
            is_even = (total % 2 == 0)
            if event_type == 2511:
                return "WON" if is_even else "LOST"
            if event_type == 2512:
                return "WON" if not is_even else "LOST"

        # ── Group 2866 & 2867: Équipe gagne sans prendre de but (Clean sheet win)
        # 2866: Équipe 1 (200=Oui, 205=Non)
        # 2867: Équipe 2 (195=Oui, 210=Non)
        if group_id == 2866:
            clean_win = (score1 > score2 and score2 == 0)
            if event_type == 200:
                return "WON" if clean_win else "LOST"
            if event_type == 205:
                return "WON" if not clean_win else "LOST"
        if group_id == 2867:
            clean_win = (score2 > score1 and score1 == 0)
            if event_type == 195:
                return "WON" if clean_win else "LOST"
            if event_type == 210:
                return "WON" if not clean_win else "LOST"

        # ── Group 776: Équipe marque ses penalties ────────────────────────
        # 1422 / 1423 : Équipe 1 (Plus / Moins de 4.5 ou 4 penalties marqués)
        # 1424 / 1425 : Équipe 2 (Plus / Moins de 4.5 ou 4 penalties marqués)
        if group_id == 776:
            thresh = 3.5 if threshold is None else threshold
            if event_type == 1422:
                return "WON" if score1 > thresh else "LOST"
            if event_type == 1423:
                return "WON" if score1 < thresh else "LOST"
            if event_type == 1424:
                return "WON" if score2 > thresh else "LOST"
            if event_type == 1425:
                return "WON" if score2 < thresh else "LOST"

        return "VOID"

    def get_prediction_statistics(self) -> dict[str, int | float | None]:
        with closing(self._connect(self.db_path, timeout=5)) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*),
                       SUM(prediction_status = 'WON'),
                       SUM(prediction_status = 'LOST'),
                       SUM(prediction_status = 'VOID'),
                       SUM(prediction_status = 'PENDING')
                FROM sent_predictions
                WHERE delivery_status = 'SENT'
                """
            ).fetchone()
            uncertain_row = connection.execute(
                """
                SELECT COUNT(*) FROM sent_predictions
                WHERE delivery_status = 'SENDING'
                """
            ).fetchone()
        total, wins, losses, voids, pending = row or (0, 0, 0, 0, 0)
        wins = int(wins or 0)
        losses = int(losses or 0)
        settled = wins + losses
        return {
            "sent": int(total or 0),
            "won": wins,
            "lost": losses,
            "void": int(voids or 0),
            "pending": int(pending or 0),
            "delivery_uncertain": int(
                uncertain_row[0] if uncertain_row is not None else 0
            ),
            "win_rate_pct": wins / settled * 100 if settled else None,
        }


def _history_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _history_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _feed_team_name(game: Mapping[str, Any], team_number: int) -> str | None:
    for key in (
        f"opponent{team_number}",
        f"team{team_number}",
        f"opponent{team_number}Name",
        f"team{team_number}Name",
    ):
        value = game.get(key)
        if isinstance(value, Mapping):
            value = next(
                (
                    value[name_key]
                    for name_key in (
                        "fullName",
                        "name",
                        "shortName",
                        "nameEn",
                        "nameFr",
                        "nameRu",
                        "title",
                    )
                    if isinstance(value.get(name_key), str)
                    and value[name_key].strip()
                ),
                None,
            )
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip()
        if name.casefold() in {
            "équipe 1",
            "équipe 2",
            "équipe locale",
            "équipe visiteuse",
        }:
            continue
        return name
    return None


def _is_finished_game(game: Mapping[str, Any]) -> bool:
    if any(
        game.get(key) is True
        for key in ("isFinished", "finished", "isEnded", "ended", "completed")
    ):
        return True
    labels = " ".join(
        str(game.get(key, ""))
        for key in ("status", "gameStatus", "state", "statusName", "statusLineStr")
    ).lower()
    finished_labels = (
        "finished",
        "ended",
        "complete",
        "final",
        "terminé",
        "termine",
        "fin du match",
    )
    return any(label in labels for label in finished_labels) or labels.strip() == "ft"


def _iter_game_events(payload: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        if "id" in payload and "scores" in payload:
            yield payload
            return
        for key in ("gameEvents", "gameEvent", "events", "data", "result", "results"):
            if key in payload:
                yield from _iter_game_events(payload[key])
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_game_events(item)


def _is_fc24_penalty_event(event: Mapping[str, Any]) -> bool:
    league_id = event.get("championship_id", event.get("league_id"))
    if league_id is None:
        for key in ("liga", "league", "championship"):
            league = event.get(key)
            if isinstance(league, Mapping):
                league_id = league.get("id")
                break
    return str(league_id) == str(DEFAULT_CHAMPIONSHIP_ID)


class _RetryableHTTPStatus(RuntimeError):
    def __init__(self, status: int, retry_after: int | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        message = f"HTTP {status} a exigé une nouvelle tentative"
        if retry_after is not None:
            message = f"{message} (Retry-After: {retry_after}s)"
        super().__init__(message)


class APICollector:
    """Collecteur en direct et pré-match pour FC24. Penalty avec sauvegarde SQLite."""

    def __init__(
        self,
        feed_api_url: str,
        processor: EventProcessor,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        championship_id: int = DEFAULT_CHAMPIONSHIP_ID,
    ) -> None:
        if str(championship_id) != str(DEFAULT_CHAMPIONSHIP_ID):
            raise ValueError(
                f"Seule la ligue FC24. Penalty ({DEFAULT_CHAMPIONSHIP_ID}) est autorisée"
            )
        parsed = urlsplit(feed_api_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("FEED_API_URL doit être une URL HTTP ou HTTPS valide")
        self._feed_url = feed_api_url
        self._base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._mirror_domains = self._candidate_domains(feed_api_url)
        self._processor = processor
        self._poll_interval_sec = poll_interval_sec
        self._championship_id = DEFAULT_CHAMPIONSHIP_ID
        self._left_menu_params = {
            "fcountry": "165",
            "gr": "448",
            "lng": "fr",
            "ref": "161",
            "selectedMs": (
                f"1.85.{self._championship_id},"
                f"2.85.{self._championship_id},"
                f"10.85.{self._championship_id}"
            ),
        }
        self._game_events_params = {
            "cfView": "3",
            "countEvents": "250",
            "fcountry": "165",
            "gameId": "",
            "gr": "448",
            "grMode": "4",
            "lng": "fr",
            "marketType": "1",
            "ref": "161",
        }
        self._top_games_params = {
            "cfView": "3",
            "excludeSportIds": "66",
            "fcountry": "165",
            "gr": "448",
            "lng": "fr",
            "ref": "161",
        }
        self._headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "x-app-n": "__BETTING_APP__",
            "x-svc-source": "__BETTING_APP__",
        }

        self.session = self._create_session()
        self._warmup_cookies()

        bearer_token = os.getenv("API_BEARER_TOKEN", "").strip()
        api_key = os.getenv("API_KEY", "").strip()
        if bearer_token:
            self.session.headers["Authorization"] = f"Bearer {bearer_token}"
            self._headers["Authorization"] = f"Bearer {bearer_token}"
        if api_key:
            self.session.headers["X-API-Key"] = api_key
            self._headers["X-API-Key"] = api_key

        self._init_db()

    def _create_session(self):
        if curl_requests is not None:
            session = curl_requests.Session(impersonate="chrome131")
        else:
            session = requests.Session()
        session.headers.update(HEADERS)
        session.cookies.update(COOKIES)
        return session

    def _candidate_domains(self, current_url: str) -> list[str]:
        parsed = urlsplit(current_url)
        current_domain = f"{parsed.scheme}://{parsed.netloc}"
        ordered = [current_domain]
        host = parsed.netloc.lower()
        if "127.0.0.1" in host or "localhost" in host:
            return ordered
        for mirror in MIRROR_DOMAINS:
            if mirror not in ordered and mirror != current_domain:
                ordered.append(mirror)
        return ordered

    def _warmup_cookies(self) -> None:
        warmup_headers = {
            **HEADERS,
            "Referer": "https://1xbet.com/fr",
        }
        for domain in self._mirror_domains:
            if "127.0.0.1" in domain or "localhost" in domain:
                continue
            try:
                warmup_headers["Referer"] = f"{domain}/fr"
                self.session.get(f"{domain}/fr", headers=warmup_headers, timeout=15)
            except Exception as exc:  # pragma: no cover - best effort warm-up
                logger.debug("Warmup cookie failed for %s: %s", domain, exc)

    def _request_with_fallback(self, url: str, *, params: Mapping[str, Any] | None = None, timeout: int = 10):
        last_error = None
        candidate_domains = [self._base_url] if "127.0.0.1" in self._base_url or "localhost" in self._base_url else self._mirror_domains
        for domain in candidate_domains:
            request_url = url if url.startswith("http") else f"{domain}{url}"
            try:
                response = self.session.get(request_url, params=params, headers=HEADERS, timeout=timeout)
                if response.status_code in (403, 429):
                    logger.warning("Le domaine %s a bloqué la demande (HTTP %s)", domain, response.status_code)
                    self.session = self._create_session()
                    self._warmup_cookies()
                    continue
                if response.status_code == 200 and response.text and not response.text.lstrip().startswith("<"):
                    try:
                        return response.json()
                    except ValueError as exc:
                        last_error = exc
                        logger.warning("JSON invalide sur %s pour %s: %s", domain, request_url, exc)
                        continue
                if response.status_code == 200:
                    last_error = RuntimeError("Réponse HTML au lieu de JSON")
                    logger.warning("Réponse HTML détectée sur %s pour %s", domain, request_url)
                    continue
                last_error = RuntimeError(f"HTTP {response.status_code}")
                logger.warning("Échec HTTP %s sur %s", response.status_code, request_url)
            except Exception as exc:  # pragma: no cover - network failures are environment-dependent
                last_error = exc
                logger.warning("Erreur réseau sur %s : %s", domain, exc)
                self.session = self._create_session()
                self._warmup_cookies()
        if last_error is not None:
            raise last_error
        return None

    def _init_db(self) -> None:
        with closing(sqlite3.connect(DB_NAME)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS penalty_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER,
                    equipe_1 TEXT,
                    equipe_2 TEXT,
                    chrono_sec INTEGER,
                    score_global TEXT,
                    score_id1 INTEGER,
                    score_id2 INTEGER,
                    timestamp_enregistrement INTEGER,
                    UNIQUE (match_id, chrono_sec)
                )
                """
            )
            conn.commit()

    def _save_snapshot(
        self, match_id: int, eq1: str, eq2: str, chrono: int, score_str: str, s1: int, s2: int
    ) -> None:
        with closing(sqlite3.connect(DB_NAME)) as conn, conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO penalty_matches (
                        match_id, equipe_1, equipe_2, chrono_sec,
                        score_global, score_id1, score_id2, timestamp_enregistrement
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (match_id, chrono_sec) DO NOTHING
                    """,
                    (match_id, eq1, eq2, chrono, score_str, s1, s2, int(time.time())),
                )
                conn.commit()
            except sqlite3.Error as exc:
                logger.debug("Erreur SQLite : %s", exc)

    async def _fetch_json(
        self,
        session,
        url: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any | None:
        request_url = self._feed_url if url is None else url
        if hasattr(self.session, "cookies"):
            try:
                cookie_dict = {key: value for key, value in self.session.cookies.items()}
                if cookie_dict:
                    session.cookie_jar.update_cookies(cookie_dict)
            except Exception:  # pragma: no cover - best effort only
                pass

        last_error: Exception | None = None
        candidate_urls = [request_url]
        parsed = urlsplit(request_url)
        host = parsed.netloc.lower()
        if parsed.scheme and parsed.netloc and "127.0.0.1" not in host and "localhost" not in host:
            base_domain = f"{parsed.scheme}://{parsed.netloc}"
            for mirror in self._mirror_domains:
                if mirror != base_domain and mirror not in candidate_urls:
                    candidate_urls.append(f"{mirror}{parsed.path}")
        for attempt_url in candidate_urls:
            request_headers = dict(self._headers)
            try:
                base_domain = urlsplit(attempt_url).scheme + "://" + urlsplit(attempt_url).netloc
                request_headers["Referer"] = f"{base_domain}/fr"
                async with session.get(
                    attempt_url, params=params, headers=request_headers
                ) as response:
                    status = response.status
                    if status in (204, 304):
                        return None
                    if status in (429, 503):
                        retry_after_header = response.headers.get("Retry-After")
                        try:
                            retry_after = (
                                int(retry_after_header)
                                if retry_after_header is not None
                                else None
                            )
                        except ValueError:
                            retry_after = None
                        raise _RetryableHTTPStatus(status, retry_after)
                    if status == 403:
                        raise FeedAccessError(f"Accès refusé au flux (HTTP {status})")
                    if status == 400:
                        raise FeedRequestError(f"Paramètres rejetés par le flux (HTTP {status})")
                    if status != 200:
                        last_error = FeedAccessError(f"Erreur HTTP {status} sur le flux")
                        continue
                    text = await response.text()
                    if not text.strip() or text.lstrip().startswith("<"):
                        last_error = InvalidFeedResponseError(
                            f"Réponse HTTP {status} ne contient pas du JSON valide"
                        )
                        continue
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as exc:
                        last_error = InvalidFeedResponseError(
                            f"Réponse HTTP {status} ne contient pas du JSON valide"
                        )
                        logger.warning("JSON invalide sur %s : %s", attempt_url, exc)
                        continue
            except _RetryableHTTPStatus:
                raise
            except (FeedAccessError, FeedRequestError, InvalidFeedResponseError):
                raise
            except Exception as exc:
                last_error = InvalidFeedResponseError(
                    f"Réponse HTTP 200 ne contient pas du JSON valide : {exc}"
                )
                logger.warning("Erreur réseau sur %s : %s", attempt_url, exc)
                continue
        if last_error is not None:
            raise last_error
        return None

    def _fetch_matches_list(self) -> list[dict[str, Any]]:
        url = f"{self._base_url}{LEFT_MENU_PATH}"
        try:
            data = self._request_with_fallback(
                url,
                params=self._left_menu_params,
                timeout=10,
            )
            if data is None:
                return []
        except Exception as exc:
            logger.warning("Échec de récupération leftMenuSports via les miroirs : %s", exc)
            return []

        matches: list[dict[str, Any]] = []

        def extract_games(node: Any) -> None:
            if isinstance(node, Mapping):
                if str(node.get("id")) == str(self._championship_id) and isinstance(node.get("games"), list):
                    for g in node["games"]:
                        if not isinstance(g, Mapping) or not g.get("id"):
                            continue
                        scores = g.get("scores", {})
                        timer = scores.get("timer") if isinstance(scores, Mapping) else None
                        if not isinstance(timer, Mapping):
                            timer = {}
                        time_sec = int(timer.get("timeSec", 999))
                        time_run = bool(timer.get("timeRun", False))
                        video_ok = isinstance(g.get("video"), Mapping) and g["video"].get("enabled") is True
                        finished = _is_finished_game(g)
                        team1 = _feed_team_name(g, 1)
                        team2 = _feed_team_name(g, 2)
                        if time_run or video_ok or time_sec <= 240 or finished:
                            display_team1 = team1 or "Équipe 1"
                            display_team2 = team2 or "Équipe 2"
                            match = {
                                "id": int(g["id"]),
                                "equipe1": display_team1,
                                "equipe2": display_team2,
                                "dopInfo": str(g.get("dopInfo", "")),
                                "is_live": time_run,
                                "match_number": str(g.get("dopInfo", "")),
                                "team1": display_team1,
                                "team2": display_team2,
                                "video_enabled": video_ok,
                                "is_finished": finished,
                                "status": "FINISHED" if finished else g.get("status", ""),
                            }
                            matches.append(match)
                for v in node.values():
                    extract_games(v)
            elif isinstance(node, list):
                for item in node:
                    extract_games(item)

        try:
            extract_games(data)
            return matches
        except Exception as exc:
            logger.error("Erreur réseau leftMenuSports : %s", exc)
            return []

    async def _poll_league(self, session) -> list[dict[str, Any]]:
        url = f"{self._base_url}{LEFT_MENU_PATH}"
        try:
            payload = await self._fetch_json(session, url, params=self._left_menu_params)
        except (FeedAccessError, FeedRequestError, InvalidFeedResponseError) as exc:
            logger.warning("Flux FC24 Penalty indisponible lors du polling : %s", exc)
            return []
        if payload is None:
            return []
        matches: list[dict[str, Any]] = []
        seen: set[int] = set()

        def extract_games(node: Any) -> None:
            if isinstance(node, Mapping):
                game_entries = node.get("games")
                if str(node.get("id")) == str(self._championship_id) and isinstance(game_entries, list):
                    for game in game_entries:
                        if not isinstance(game, Mapping) or not game.get("id"):
                            continue
                        game_id = int(game["id"])
                        if game_id in seen:
                            continue
                        scores = game.get("scores", {})
                        timer = scores.get("timer") if isinstance(scores, Mapping) else None
                        if not isinstance(timer, Mapping):
                            timer = {}
                        video = game.get("video")
                        video_enabled = (
                            isinstance(video, Mapping) and video.get("enabled") is True
                        )
                        finished = _is_finished_game(game)
                        time_sec = timer.get("timeSec", 999)
                        time_run = bool(timer.get("timeRun", False))
                        try:
                            imminent = int(time_sec) <= 240
                        except (TypeError, ValueError):
                            imminent = False
                        if not (time_run or video_enabled or imminent or finished):
                            continue
                        seen.add(game_id)
                        team1 = _feed_team_name(game, 1)
                        team2 = _feed_team_name(game, 2)
                        match = {
                            "id": game_id,
                            "match_number": str(game.get("dopInfo", "")),
                            "video_enabled": video_enabled,
                            "is_live": time_run,
                            "is_finished": finished,
                            "status": "FINISHED" if finished else game.get("status", ""),
                            "statusLineStr": "",
                        }
                        if team1 is not None:
                            match["team1"] = team1
                        if team2 is not None:
                            match["team2"] = team2
                        matches.append(match)
                for value in node.values():
                    extract_games(value)
            elif isinstance(node, list):
                for item in node:
                    extract_games(item)

        extract_games(payload)
        rejected_match_ids: set[int] = set()
        for match in matches:
            try:
                await self._fetch_and_process_match(match, session)
            except (FeedAccessError, FeedRequestError, InvalidFeedResponseError) as exc:
                rejected_match_ids.add(match["id"])
                logger.warning(
                    "Flux d'événements indisponible pour le match %s : %s",
                    match["id"],
                    exc,
                )
        if rejected_match_ids:
            await self._fetch_top_games_fallback(rejected_match_ids, session)
        return matches

    async def _fetch_top_games_fallback(self, match_ids: set[int], session) -> None:
        url = f"{self._base_url}{TOP_GAMES_BY_STAT_PATH}"
        payload = await self._fetch_json(session, url, params=self._top_games_params)
        if payload is None:
            return

        for game in _iter_game_events(payload):
            try:
                game_id = int(game["id"])
            except (KeyError, TypeError, ValueError):
                continue
            league = game.get("liga")
            if (
                game_id not in match_ids
                or not isinstance(league, Mapping)
                or str(league.get("id")) != str(self._championship_id)
            ):
                continue
            self._processor.process_game_event(game)

    async def _fetch_and_process_match(self, match: dict[str, Any], session) -> bool:
        url = f"{self._base_url}{GAME_EVENTS_PATH}"
        params = dict(self._game_events_params)
        params["gameId"] = str(match["id"])
        payload = await self._fetch_json(session, url, params=params)
        if payload is None:
            return False
        if not isinstance(payload, Mapping):
            return False
        payload = dict(payload)

        if match.get("match_number"):
            payload.setdefault("match_number", str(match["match_number"]))
        if match.get("team1") and _feed_team_name(payload, 1) is None:
            payload["team1"] = match["team1"]
        if match.get("team2") and _feed_team_name(payload, 2) is None:
            payload["team2"] = match["team2"]
        if match.get("video_enabled") is not None:
            payload.setdefault("video_enabled", bool(match["video_enabled"]))
        if match.get("is_finished") is True:
            payload["is_finished"] = True
            payload["status"] = "FINISHED"

        return bool(self._processor.process_game_event(payload)) is not False

    async def _poll_sync(self) -> int:
        # Compatibility wrapper for the non-async bot entry point and the async loop.
        return 0

    def _fetch_and_process_match_legacy(self, match: dict[str, Any]) -> bool:
        url = f"{self._base_url}{GAME_EVENTS_PATH}"
        params = {
            "cfView": "3",
            "countEvents": "250",
            "fcountry": "165",
            "gameId": str(match["id"]),
            "gr": "448",
            "grMode": "4",
            "lng": "fr",
            "marketType": "1",
            "ref": "161",
        }
        try:
            data = self._request_with_fallback(url, params=params, timeout=10)
            if data is None or not isinstance(data, Mapping):
                return False
            payload = dict(data)
            if match.get("team1") and _feed_team_name(payload, 1) is None:
                payload["team1"] = match["team1"]
            if match.get("team2") and _feed_team_name(payload, 2) is None:
                payload["team2"] = match["team2"]
            self._processor.process_game_event(payload)
            return True
        except Exception as exc:
            logger.error("Erreur réseau match %s : %s", match["id"], exc)
            return False

    def _poll(self) -> int:
        all_matches = self._fetch_matches_list()
        processed_count = 0
        for match in all_matches:
            if self._fetch_and_process_match_legacy(match):
                processed_count += 1
            time.sleep(0.4)
        return processed_count

    async def run(self) -> None:
        logger.info(
            "Collecteur FC24. Penalty actif (%s), intervalle %.1f s",
            self._base_url,
            self._poll_interval_sec,
        )
        while True:
            try:
                processed_count = await asyncio.to_thread(self._poll)
                logger.info("Cycle terminé : %d match(s) FC24. Penalty analysé(s)", processed_count)
                await asyncio.sleep(self._poll_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Erreur dans la boucle du collecteur : %s", exc)
                await asyncio.sleep(self._poll_interval_sec)


class LocalFileCollector:
    """Mode de test local sur un fichier JSON."""

    def __init__(self, processor: EventProcessor, file_path: str | Path | None = None) -> None:
        self._processor = processor
        self._path = Path(file_path) if file_path is not None else Path(__file__).with_name("sample_events.json")

    async def run(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"Fichier local introuvable : {self._path}")
        payload = json.loads(self._path.read_text(encoding="utf-8-sig"))
        for ev in _iter_game_events(payload):
            if _is_fc24_penalty_event(ev):
                self._processor.process_game_event(ev)
        logger.info("Fichier local traité avec succès")
