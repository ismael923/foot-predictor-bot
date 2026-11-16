"""Telegram sender and rolling odds-drop signal detector.

Signal settings are loaded from this folder's .env file:
    SIGNAL_DROP_PCT=15.0
    SIGNAL_WINDOW_SEC=15
"""

import asyncio
import json
import logging
import math
import os
import re
import socket
import sqlite3
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import aiohttp

TELEGRAM_API = "https://api.telegram.org"
logger = logging.getLogger(__name__)


def _temporarily_force_ipv4() -> Callable[[], None]:
    """Force IPv4 resolution for the duration of the call to avoid Telegram timeouts on IPv6 routes."""
    original_getaddrinfo = socket.getaddrinfo

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0:
            infos = original_getaddrinfo(host, port, socket.AF_UNSPEC, type, proto, flags)
            v4 = [info for info in infos if info[0] == socket.AF_INET]
            if v4:
                return v4
        return original_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = ipv4_only

    def restore() -> None:
        socket.getaddrinfo = original_getaddrinfo

    return restore


def _load_env_file() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key:
            raise ValueError(f"Ligne .env invalide : {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file()


def _read_positive_setting(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    try:
        value = default if raw_value is None else float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un nombre positif") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} doit être un nombre positif")
    return value


SIGNAL_DROP_PCT = _read_positive_setting("SIGNAL_DROP_PCT", 15.0)
SIGNAL_WINDOW_SEC = _read_positive_setting("SIGNAL_WINDOW_SEC", 15.0)
TELEGRAM_COMMANDS = [
    {"command": "pause", "description": "Mettre les alertes en pause"},
    {"command": "resume", "description": "Reprendre les alertes"},
    {"command": "status", "description": "Voir l'état du bot"},
    {"command": "stats", "description": "Voir les statistiques des matchs"},
    {"command": "ai", "description": "Voir l'apprentissage de TerminatAI"},
]


@dataclass(frozen=True)
class OddsSample:
    timestamp: float
    odds: float


@dataclass(frozen=True)
class OddsSignal:
    game_id: str
    market: str
    old_odds: float
    current_odds: float
    drop_pct: float
    market_key: str = ""


@dataclass(frozen=True)
class SignalReport:
    signals: tuple[OddsSignal, ...]

    @property
    def STRONG_SIGNAL(self) -> bool:
        return bool(self.signals)


@dataclass(frozen=True)
class MarketQuote:
    key: str
    label: str
    group_id: int
    event_type: int
    odds: float
    threshold: float | None = None
    score: tuple[int, int] | None = None
    is_center: bool = False
    group_label: str = ""
    outcome_label: str = ""
    is_extra_penalties: bool = False


@dataclass(frozen=True)
class ExactScorePrediction:
    score: tuple[int, int]
    odds: float
    probability_pct: float


@dataclass(frozen=True)
class TotalBalance:
    threshold: float
    over_odds: float
    under_odds: float
    over_probability_pct: float
    under_probability_pct: float
    selected_side: str
    selected_odds: float
    selected_probability_pct: float


@dataclass(frozen=True)
class MarketProbability:
    group_id: int
    event_type: int
    label: str
    odds: float
    probability_pct: float


@dataclass(frozen=True)
class TeamTotalPrediction:
    team_number: int
    threshold: float
    selected_side: str
    selected_odds: float


@dataclass(frozen=True)
class ExtraPenaltiesPrediction:
    yes_odds: float
    no_odds: float
    likely_outcome: str
    likely_odds: float


@dataclass(frozen=True)
class GameAnalysis:
    exact_scores: tuple[ExactScorePrediction, ...]
    balanced_total: TotalBalance | None
    market_probabilities: tuple[MarketProbability, ...] = ()
    team_totals: tuple[TeamTotalPrediction, ...] = ()
    extra_penalties: ExtraPenaltiesPrediction | None = None
    regulation_markets: tuple[MarketQuote, ...] = ()


@dataclass(frozen=True)
class CycleStatistics:
    regime: str = "CYCLE MIXTE"
    recent_count: int = 0
    recent_sudden_death_pct: float = 0.0
    recent_average_goals: float | None = None
    current_hour: int = 0
    hour_match_count: int = 0
    hour_sudden_death_pct: float = 0.0


@dataclass
class MatchDeliveryState:
    prematch_alert_sent: bool = False
    live_alert_sent: bool = False


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _team_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in (
            "fullName",
            "name",
            "shortName",
            "nameEn",
            "nameFr",
            "nameRu",
            "title",
        ):
            name = value.get(key)
            if isinstance(name, str) and name.strip():
                value = name
                break
        else:
            return None
    if not isinstance(value, str):
        return None
    name = value.strip()
    normalized = _normalize_market_text(name)
    if not name or normalized in {
        "equipe 1",
        "equipe 2",
        "equipe locale",
        "equipe visiteuse",
    }:
        return None
    return name


def _match_team_name(
    data: Mapping[str, Any], team_number: int, fallback: str
) -> str:
    for key in (
        f"opponent{team_number}",
        f"team{team_number}",
        f"opponent{team_number}Name",
        f"team{team_number}Name",
    ):
        name = _team_name(data.get(key))
        if name is not None:
            return name
    return fallback


def _as_finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iter_events(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_events(item)


def _exact_score_components(event: Mapping[str, Any]) -> tuple[int, int] | None:
    event_params = event.get("eventParams")
    score_params = (
        event_params.get("params")
        if isinstance(event_params, Mapping)
        else None
    )
    if isinstance(score_params, (list, tuple)) and len(score_params) >= 2:
        score_1 = _as_int(score_params[0])
        score_2 = _as_int(score_params[1])
        if score_1 is not None and score_2 is not None:
            return score_1, score_2

    parameter = event.get("parameter")
    if parameter is not None:
        match = re.fullmatch(r"(\d+)\.00(\d+)", str(parameter).strip())
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


_MARKET_TEXT_KEYS = (
    "name",
    "fullName",
    "shortName",
    "title",
    "groupName",
    "label",
    "description",
    "nameEn",
    "nameRu",
    "nameFr",
)


def _market_text(item: Mapping[str, Any]) -> str:
    return " ".join(
        str(item[key])
        for key in _MARKET_TEXT_KEYS
        if isinstance(item.get(key), str)
    )


def _normalize_market_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _is_extra_penalties_market(text: str) -> bool:
    normalized = _normalize_market_text(text)
    return any(
        phrase in normalized
        for phrase in ("sudden death", "mort subite", "extra penalty", "additional penalty")
    ) or (
        "penal" in normalized
        and any(
            marker in normalized
            for marker in ("supplement", "additional", "extra", "overtime")
        )
    )


def _yes_no_outcome(event: Mapping[str, Any], index: int) -> str:
    words = set(re.findall(r"[a-z]+", _normalize_market_text(_market_text(event))))
    if words.intersection({"yes", "oui"}):
        return "Oui"
    if words.intersection({"no", "non"}):
        return "Non"
    event_type = _as_int(event.get("type"))
    if event_type == 1:
        return "Oui"
    if event_type == 2:
        return "Non"
    return "Oui" if index == 0 else "Non"


def _is_blocked_or_unreadable(event: Mapping[str, Any]) -> bool:
    if event.get("blocked") is True:
        return True
    label = _market_text(event).strip()
    return bool(re.match(r"^issue(?:\s|$)", label, flags=re.IGNORECASE))


def _unique_market_keys(markets: list[MarketQuote]) -> list[MarketQuote]:
    occurrences: dict[str, int] = {}
    unique_markets = []
    for market in markets:
        occurrences[market.key] = occurrences.get(market.key, 0) + 1
        if occurrences[market.key] > 1:
            market = replace(
                market,
                key=f"{market.key}:{occurrences[market.key]}",
            )
        unique_markets.append(market)
    return unique_markets


def _event_threshold(event: Mapping[str, Any]) -> float | None:
    threshold = _as_finite_float(event.get("parameter"))
    if threshold is not None:
        return threshold
    params = event.get("eventParams")
    if isinstance(params, Mapping):
        values = params.get("params")
        if isinstance(values, (list, tuple)):
            # 1xBet code une ligne 6.5 sous la forme params=[6, 5]. Lire
            # seulement le premier nombre donnait 6.0, une ligne qui n'existe
            # pas : il faut recomposer la partie décimale.
            if len(values) >= 2:
                whole = _as_finite_float(values[0])
                decimal = _as_finite_float(values[1])
                if whole is not None and decimal is not None:
                    fraction = int(decimal)
                    if fraction >= 0:
                        digits = 1
                        while fraction >= 10 ** digits:
                            digits += 1
                        return whole + fraction / (10 ** digits)
            for value in values:
                threshold = _as_finite_float(value)
                if threshold is not None:
                    return threshold
    return None


def _is_finished_match(data: Mapping[str, Any]) -> bool:
    sources = [data]
    for key in ("event", "scores"):
        value = data.get(key)
        if isinstance(value, Mapping):
            sources.append(value)
    if any(
        source.get(key) is True
        for source in sources
        for key in (
            "isFinished",
            "is_finished",
            "finished",
            "isEnded",
            "ended",
            "completed",
        )
    ):
        return True

    finished_labels = {
        "complete",
        "completed",
        "ended",
        "final",
        "finished",
        "ft",
        "termine",
        "termines",
    }
    for source in sources:
        for key in ("status", "gameStatus", "state", "statusName", "statusLineStr"):
            value = source.get(key)
            if isinstance(value, str):
                words = set(
                    re.findall(r"[a-z]+", _normalize_market_text(value))
                )
                if words.intersection(finished_labels) or "fin du match" in _normalize_market_text(value):
                    return True
    return False


def _extract_markets(
    match: Mapping[str, Any],
) -> list[MarketQuote]:
    markets: list[MarketQuote] = []
    event_groups = match.get("eventGroups", [])
    if not isinstance(event_groups, list) or not event_groups:
        return _unique_market_keys(_extract_flat_markets(match))

    for group in event_groups:
        if not isinstance(group, Mapping):
            continue
        group_id = _as_int(group.get("groupId"))
        raw_events = group.get("events", [])
        group_label = _market_text(group)
        group_events = list(_iter_events(raw_events))
        numeric_penalty_types = {180, 181, 2210, 2211}
        numeric_penalty_group_ids = {21, 65, 1094, 2769}
        numeric_penalty_outcomes = [
            event
            for event in group_events
            if _as_int(event.get("type")) in numeric_penalty_types
            and event.get("parameter") is None
            and not _is_blocked_or_unreadable(event)
        ]
        numeric_penalty_group = (
            group_id in numeric_penalty_group_ids
            or bool(numeric_penalty_outcomes)
        ) and len(numeric_penalty_outcomes) == 2
        if not numeric_penalty_group and group_id in numeric_penalty_group_ids:
            numeric_penalty_group = len(group_events) == 2 and all(
                event.get("parameter") is None
                and not _is_blocked_or_unreadable(event)
                for event in group_events
            )
        events_to_parse = (
            numeric_penalty_outcomes
            if numeric_penalty_group and len(numeric_penalty_outcomes) == 2
            else group_events
        )
        combined_label = " ".join(
            (group_label, *(_market_text(event) for event in events_to_parse))
        )

        if group_id == 136:
            if not isinstance(raw_events, (list, tuple)):
                continue
            for outcome_index, outcome_events in enumerate(raw_events):
                for event in _iter_events(outcome_events):
                    if (
                        _as_int(event.get("type")) != 731
                        or _is_blocked_or_unreadable(event)
                    ):
                        continue
                    odds = _as_finite_float(event.get("cf"))
                    components = _exact_score_components(event)
                    if odds is None or odds <= 0 or components is None:
                        continue
                    high_score, low_score = max(components), min(components)
                    score_pair = (
                        (high_score, low_score)
                        if outcome_index == 0
                        else (low_score, high_score)
                    )
                    markets.append(
                        MarketQuote(
                            key=f"score:{score_pair[0]}:{score_pair[1]}",
                            label=f"Score exact {score_pair[0]}-{score_pair[1]}",
                            group_id=group_id,
                            event_type=731,
                            odds=odds,
                            score=score_pair,
                        )
                    )
            continue

        for event_index, event in enumerate(events_to_parse):
            if _is_blocked_or_unreadable(event):
                continue
            odds = _as_finite_float(event.get("cf"))
            if odds is None or odds <= 0:
                continue

            event_type = _as_int(event.get("type"))
            threshold = _event_threshold(event)
            is_center = event.get("isCenter") is True
            event_label = _market_text(event).strip()
            is_extra_penalties = _is_extra_penalties_market(
                f"{group_label} {event_label}"
            ) or numeric_penalty_group
            if is_extra_penalties:
                outcome_label = _yes_no_outcome(event, event_index)
                market_label = group_label or "Pénalités supplémentaires"
                markets.append(
                    MarketQuote(
                        key=f"extra-penalties:{group_id}:{event_type}:{outcome_label}",
                        label=f"{market_label}: {outcome_label}",
                        group_id=-1 if group_id is None else group_id,
                        event_type=event_type or event_index + 1,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=group_label,
                        outcome_label=outcome_label,
                        is_extra_penalties=True,
                    )
                )
                continue

            if group_id == 1 and event_type in (1, 2, 3):
                team1 = str(match.get("team1") or "Équipe 1")
                team2 = str(match.get("team2") or "Équipe 2")
                label = {1: team1, 2: "Match nul", 3: team2}[event_type]
                markets.append(
                    MarketQuote(
                        key=f"winner:{event_type}",
                        label=label,
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        is_center=is_center,
                        group_label=group_label,
                    )
                )
            elif group_id == 8 and event_type in (4, 5, 6):
                label = {4: "1X", 5: "12", 6: "2X"}[event_type]
                markets.append(
                    MarketQuote(
                        key=f"double-chance:{event_type}",
                        label=label,
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        is_center=is_center,
                        group_label=group_label,
                    )
                )
            elif group_id == 2 and event_type in (7, 8):
                if threshold is None:
                    continue
                team = str(match.get("team1") or "Équipe 1") if event_type == 7 else str(
                    match.get("team2") or "Équipe 2"
                )
                markets.append(
                    MarketQuote(
                        key=f"handicap:{event_type}:{threshold:g}",
                        label=f"Handicap {team} ({threshold:+g})",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=group_label,
                    )
                )
            elif group_id == 19 and event_type in (11273, 11274):
                if threshold is None:
                    continue
                side = "Plus de" if event_type == 11273 else "Moins de"
                threshold_label = (
                    str(int(threshold)) if threshold.is_integer() else f"{threshold:g}"
                )
                markets.append(
                    MarketQuote(
                        key=f"total:{event_type}:{threshold:g}",
                        label=f"{side} {threshold_label} buts",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=group_label,
                    )
                )
            elif group_id == 19:
                parity_market = bool(
                    re.search(
                        r"\b(pair|impair|even|odd)\b",
                        _normalize_market_text(
                            f"{group_label} {event_label}"
                        ),
                    )
                )
                outcome_label = event_label if parity_market else ""
                if not parity_market and (
                    event_type in (1, 2)
                    or re.search(
                        r"\b(yes|oui|no|non)\b",
                        _normalize_market_text(event_label),
                    )
                ):
                    outcome_label = _yes_no_outcome(event, 0)
                label = event_label or f"Groupe 19, type {event_type}"
                threshold_key = "" if threshold is None else f":{threshold:g}"
                markets.append(
                    MarketQuote(
                        key=f"group-19:{event_type}{threshold_key}",
                        label=label,
                        group_id=group_id,
                        event_type=event_type or 0,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=group_label,
                        outcome_label=outcome_label,
                    )
                )
            elif group_id == 17 and event_type in (9, 10):
                if threshold is None:
                    continue
                side = "Plus de" if event_type == 9 else "Moins de"
                markets.append(
                    MarketQuote(
                        key=f"total:{event_type}:{threshold:g}",
                        label=f"{side} {threshold:g} buts",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=group_label,
                    )
                )
            elif group_id == 15 and event_type in (11, 12):
                if threshold is None:
                    continue
                side = "Plus" if event_type == 11 else "Moins"
                markets.append(
                    MarketQuote(
                        key=f"team-total:1:{event_type}:{threshold:g}",
                        label=f"Total 1 {side} {threshold:g}",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=group_label,
                    )
                )
            elif group_id == 62 and event_type in (13, 14):
                if threshold is None:
                    continue
                side = "Plus" if event_type == 13 else "Moins"
                markets.append(
                    MarketQuote(
                        key=f"team-total:2:{event_type}:{threshold:g}",
                        label=f"Total 2 {side} {threshold:g}",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=group_label,
                    )
                )
            elif group_id == 14 and event_type in (182, 183):
                outcome_label = "Oui" if event_type == 183 else "Non"
                markets.append(
                    MarketQuote(
                        key=f"btts:{event_type}:{outcome_label}",
                        label=f"Deux équipes vont marquer : {outcome_label}",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        is_center=is_center,
                        group_label="Deux équipes vont marquer",
                        outcome_label=outcome_label,
                    )
                )
            elif group_id == 2270 and event_type in (2511, 2512):
                outcome_label = "Total pair" if event_type == 2511 else "Total impair"
                markets.append(
                    MarketQuote(
                        key=f"even-odd:{event_type}:{outcome_label}",
                        label=outcome_label,
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        is_center=is_center,
                        group_label="Total pair / impair",
                        outcome_label=outcome_label,
                    )
                )
            elif group_id == 2866 and event_type in (200, 205):
                team1 = str(match.get("team1") or "Équipe 1")
                outcome_label = "Oui" if event_type == 200 else "Non"
                markets.append(
                    MarketQuote(
                        key=f"clean-sheet-win:1:{event_type}:{outcome_label}",
                        label=f"{team1} gagne sans prendre de but : {outcome_label}",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        is_center=is_center,
                        group_label=f"{team1} gagne sans prendre de but",
                        outcome_label=outcome_label,
                    )
                )
            elif group_id == 2867 and event_type in (195, 210):
                team2 = str(match.get("team2") or "Équipe 2")
                outcome_label = "Oui" if event_type == 195 else "Non"
                markets.append(
                    MarketQuote(
                        key=f"clean-sheet-win:2:{event_type}:{outcome_label}",
                        label=f"{team2} gagne sans prendre de but : {outcome_label}",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        is_center=is_center,
                        group_label=f"{team2} gagne sans prendre de but",
                        outcome_label=outcome_label,
                    )
                )
            elif group_id == 776 and event_type in (1422, 1423, 1424, 1425):
                team1 = str(match.get("team1") or "Équipe 1")
                team2 = str(match.get("team2") or "Équipe 2")
                if event_type in (1422, 1423):
                    side = "Plus" if event_type == 1422 else "Moins"
                    team = team1
                else:
                    side = "Plus" if event_type == 1424 else "Moins"
                    team = team2
                markets.append(
                    MarketQuote(
                        key=f"team-pens:{group_id}:{event_type}",
                        label=f"{team} penalties marqués {side} de 4",
                        group_id=group_id,
                        event_type=event_type,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=f"{team} va marquer ses pénalties",
                    )
                )
            else:
                outcome_label = event_label
                if not outcome_label and event_type in (1, 2):
                    outcome_label = _yes_no_outcome(event, event_index)
                label = (
                    f"{group_label}: {outcome_label}".strip(": ")
                    if group_label
                    else outcome_label
                )
                if not label:
                    label = f"Groupe {group_id or 'inconnu'}, type {event_type or event_index + 1}"
                markets.append(
                    MarketQuote(
                        key=(
                            f"market:{group_id}:{event_type or 0}:"
                            f"{threshold if threshold is not None else ''}:"
                            f"{outcome_label or event_index}"
                        ),
                        label=label,
                        group_id=-1 if group_id is None else group_id,
                        event_type=event_type or 0,
                        odds=odds,
                        threshold=threshold,
                        is_center=is_center,
                        group_label=group_label,
                        outcome_label=outcome_label,
                    )
                )
    return _unique_market_keys(markets)


def _extract_flat_markets(match: Mapping[str, Any]) -> list[MarketQuote]:
    events = match.get("events", [])
    if not isinstance(events, list):
        return []

    markets: list[MarketQuote] = []
    team1 = str(match.get("team1") or "Équipe 1")
    team2 = str(match.get("team2") or "Équipe 2")
    for event_index, event in enumerate(_iter_events(events)):
        if _is_blocked_or_unreadable(event):
            continue
        group_id = _as_int(event.get("groupId"))
        event_type = _as_int(event.get("type"))
        odds = _as_finite_float(event.get("cf"))
        if (
            event_type is None
            or odds is None
            or odds <= 0
        ):
            continue

        threshold = _event_threshold(event)
        group_label = _market_text(event.get("group", {})) if isinstance(event.get("group"), Mapping) else ""
        event_label = _market_text(event).strip()
        is_extra_penalties = _is_extra_penalties_market(
            f"{group_label} {event_label}"
        )
        if is_extra_penalties:
            outcome_label = _yes_no_outcome(event, event_index)
            markets.append(
                MarketQuote(
                    key=f"extra-penalties:{group_id}:{event_type}:{outcome_label}",
                    label=f"{group_label}: {outcome_label}".strip(": "),
                    group_id=-1 if group_id is None else group_id,
                    event_type=event_type,
                    odds=odds,
                    threshold=threshold,
                    group_label=group_label,
                    outcome_label=outcome_label,
                    is_extra_penalties=True,
                )
            )
            continue
        if group_id == 1:
            label = {
                1: team1,
                2: "Match nul",
                3: team2,
            }.get(event_type, f"1X2 type {event_type}")
        elif group_id == 8 and event_type in (4, 5, 6):
            label = {4: "1X", 5: "12", 6: "2X"}[event_type]
        elif group_id == 2 and event_type in (7, 8):
            team = team1 if event_type == 7 else team2
            parameter_label = (
                f" ({threshold:+g})" if threshold is not None else ""
            )
            label = f"Handicap {team}{parameter_label}"
        elif group_id == 19:
            label = event_label or f"Groupe 19, type {event_type}"
        elif group_id == 17 and event_type in (9, 10):
            side = "Plus de" if event_type == 9 else "Moins de"
            label = (
                f"{side} {threshold:g} buts"
                if threshold is not None
                else f"Total type {event_type}"
            )
        else:
            outcome_label = event_label
            if not outcome_label and event_type in (1, 2):
                outcome_label = _yes_no_outcome(event, event_index)
            label = (
                f"{group_label}: {outcome_label}".strip(": ")
                if group_label
                else outcome_label
            )
            if not label:
                label = f"Groupe {group_id or 'inconnu'}, type {event_type}"

        threshold_key = "" if threshold is None else f":{threshold:g}"
        markets.append(
            MarketQuote(
                key=(
                    f"flat:{group_id}:{event_type}{threshold_key}:{event_index}"
                ),
                label=label,
                group_id=-1 if group_id is None else group_id,
                event_type=event_type,
                odds=odds,
                threshold=threshold,
                is_center=event.get("isCenter") is True,
                group_label=group_label,
                outcome_label=(
                    _yes_no_outcome(event, event_index)
                    if event_type in (1, 2)
                    else event_label
                ),
            )
        )
    return _unique_market_keys(markets)


def _live_score(match: Mapping[str, Any]) -> tuple[bool, tuple[int, int] | None]:
    score_1 = _as_int(match.get("score1"))
    score_2 = _as_int(match.get("score2"))
    score_data = match.get("SC")
    is_live = str(match.get("status", "")).upper() == "LIVE" or isinstance(
        score_data, Mapping
    )
    if score_1 is not None and score_2 is not None:
        return is_live, (score_1, score_2)
    if not isinstance(score_data, Mapping):
        return is_live, None

    full_score = score_data.get("FS")
    if not isinstance(full_score, Mapping):
        return is_live, None
    score_1 = _as_int(full_score.get("S1"))
    score_2 = _as_int(full_score.get("S2"))
    if score_1 is None or score_2 is None:
        return is_live, None
    return is_live, (score_1, score_2)


def parse_game_event(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize gameEvents and flattened top-game responses."""
    if ("events" in data or "event" in data) and not data.get("eventGroups"):
        return _parse_flat_match(data)

    game_id = data.get("id", data.get("gameId"))
    if game_id is None:
        raise ValueError("gameEvents doit contenir un identifiant de match")

    scores = data.get("scores")
    score_data = data.get("SC")
    if not isinstance(scores, Mapping):
        scores = {}
        if isinstance(score_data, Mapping):
            full_score = score_data.get("FS")
            if isinstance(full_score, Mapping):
                scores = {
                    "scoreOpp1": full_score.get("S1", 0),
                    "scoreOpp2": full_score.get("S2", 0),
                    "fullScore": full_score,
                }

    score_1 = _as_int(scores.get("scoreOpp1"))
    score_2 = _as_int(scores.get("scoreOpp2"))
    if score_1 is None or score_2 is None:
        if isinstance(score_data, Mapping):
            full_score = score_data.get("FS")
            if isinstance(full_score, Mapping):
                score_1 = _as_int(full_score.get("S1"))
                score_2 = _as_int(full_score.get("S2"))
        if score_1 is None or score_2 is None:
            raise ValueError("gameEvents.scores contient un score invalide")
    full_score = scores.get("fullScore")
    if full_score is None and isinstance(score_data, Mapping):
        full_score = score_data.get("FS")

    timer = data.get("timer", scores.get("timer"))
    if not isinstance(timer, Mapping):
        timer = {}
    is_finished = _is_finished_match(data)
    status = (
        "FINISHED"
        if is_finished
        else (
            "PRE-MATCH"
            if data.get("nonStarted") is True or timer.get("timeRun") is False
            else "LIVE"
        )
    )

    team1 = _match_team_name(data, 1, "Équipe locale")
    team2 = _match_team_name(data, 2, "Équipe visiteuse")

    parsed = dict(data)
    parsed.update(
        {
            "game_id": game_id,
            "gameId": game_id,
            "team1": str(team1),
            "team2": str(team2),
            "status": status,
            "is_finished": is_finished,
            "statusLineStr": data.get(
                "statusLineStr", scores.get("statusLineStr", "")
            ),
            "timeSec": _as_finite_float(timer.get("timeSec")),
            "score1": score_1,
            "score2": score_2,
            "full_score": full_score,
            "SC": {"FS": {"S1": score_1, "S2": score_2}}
            if status == "LIVE"
            else None,
            "eventGroups": data.get("eventGroups", []),
        }
    )
    return parsed


def _parse_flat_match(data: Mapping[str, Any]) -> dict[str, Any]:
    game_id = data["id"]
    scores = data.get("scores")
    if not isinstance(scores, Mapping):
        raise ValueError("Le match doit contenir un objet scores")

    score_1 = _as_int(scores.get("scoreOpp1", 0))
    score_2 = _as_int(scores.get("scoreOpp2", 0))
    if score_1 is None or score_2 is None:
        raise ValueError("scores.scoreOpp1/scoreOpp2 doit être numérique")

    timer = scores.get("timer")
    if not isinstance(timer, Mapping):
        raise ValueError("scores.timer doit être un objet")
    time_run = timer.get("timeRun")
    is_finished = _is_finished_match(data)
    if is_finished:
        status = "FINISHED"
    elif isinstance(data.get("event"), Mapping):
        if data.get("nonStarted") is True:
            status = "PRE-MATCH"
        elif time_run is True or (
            scores.get("currentPeriod") is not None
            or scores.get("currentPeriodName")
        ):
            status = "LIVE"
        else:
            status = "PRE-MATCH"
    elif time_run is True:
        status = "LIVE"
    elif time_run is False:
        status = "PRE-MATCH"
    else:
        raise ValueError("scores.timer.timeRun doit être un booléen")

    team1 = _match_team_name(data, 1, "Équipe 1")
    team2 = _match_team_name(data, 2, "Équipe 2")

    parsed = dict(data)
    if not isinstance(parsed.get("events"), list):
        event = parsed.get("event")
        parsed["events"] = [event] if isinstance(event, Mapping) else []
    parsed.update(
        {
            "game_id": game_id,
            "gameId": game_id,
            "team1": str(team1),
            "team2": str(team2),
            "status": status,
            "is_finished": is_finished,
            "statusLineStr": scores.get(
                "statusLineStr", data.get("statusLineStr", "")
            ),
            "timeSec": _as_finite_float(timer.get("timeSec")),
            "score1": score_1,
            "score2": score_2,
            "full_score": scores.get("fullScore", f"{score_1}-{score_2}"),
            "SC": {"FS": {"S1": score_1, "S2": score_2}}
            if status == "LIVE"
            else None,
            "eventGroups": [],
        }
    )
    return parsed


def _is_high_odds_penalty_opportunity(
    match: Mapping[str, Any],
    analysis: GameAnalysis,
    cycle_stats: CycleStatistics,
) -> bool:
    extra_penalties = analysis.extra_penalties
    if (
        str(match.get("status", "")).upper() != "PRE-MATCH"
        or extra_penalties is None
        or extra_penalties.yes_odds > 4.50
    ):
        return False

    global_quotes = [
        quote
        for quote in analysis.regulation_markets
        if quote.group_id == 17
    ]
    total_85_is_short = any(
        quote.event_type == 9
        and quote.threshold == 8.5
        and quote.odds <= 1.72
        for quote in global_quotes
    )
    total_95_is_center = any(
        quote.threshold == 9.5 and quote.is_center
        for quote in global_quotes
    )
    strong_history = any(
        math.isfinite(rate) and rate >= 25.0
        for rate in (
            cycle_stats.hour_sudden_death_pct,
            cycle_stats.recent_sudden_death_pct,
        )
    )
    return (total_85_is_short or total_95_is_center) and strong_history


def analyze_game_event(match: Mapping[str, Any]) -> GameAnalysis:
    exact_scores: list[ExactScorePrediction] = []
    totals: dict[tuple[int, float], dict[int, MarketQuote]] = {}
    team_total_lines: dict[int, dict[float, dict[int, MarketQuote]]] = {}
    extra_penalties_outcomes: dict[str, MarketQuote] = {}
    market_probabilities: list[MarketProbability] = []

    quotes = _extract_markets(match)
    for quote in quotes:
        if quote.group_id == 136 and quote.score is not None:
            exact_scores.append(
                ExactScorePrediction(
                    score=quote.score,
                    odds=quote.odds,
                    probability_pct=100 / quote.odds,
                )
            )
        elif quote.is_extra_penalties and quote.outcome_label in ("Oui", "Non"):
            extra_penalties_outcomes[quote.outcome_label] = quote
        elif quote.group_id == 17 and quote.threshold is not None:
            total_type = {9: 11273, 10: 11274}.get(quote.event_type)
            if total_type is not None:
                totals.setdefault((17, quote.threshold), {})[total_type] = quote
        elif quote.group_id == 15 and quote.threshold is not None:
            team_total_lines.setdefault(15, {}).setdefault(
                quote.threshold, {}
            )[quote.event_type] = quote
        elif quote.group_id == 62 and quote.threshold is not None:
            team_total_lines.setdefault(62, {}).setdefault(
                quote.threshold, {}
            )[quote.event_type] = quote
        elif quote.group_id in (1, 2):
            market_probabilities.append(
                MarketProbability(
                    group_id=quote.group_id,
                    event_type=quote.event_type,
                    label=quote.label,
                    odds=quote.odds,
                    probability_pct=100 / quote.odds,
                )
            )

    exact_scores.sort(key=lambda prediction: prediction.probability_pct, reverse=True)

    balanced_total = None
    candidates: list[
        tuple[float, float, MarketQuote, MarketQuote, float, float]
    ] = []
    for (total_group_id, threshold), outcomes in totals.items():
        if total_group_id != 17:
            continue
        over = outcomes.get(11273)
        under = outcomes.get(11274)
        if over is None or under is None:
            continue
        over_probability = 100 / over.odds
        under_probability = 100 / under.odds
        probability_sum = over_probability + under_probability
        normalized_over = over_probability / probability_sum * 100
        normalized_under = under_probability / probability_sum * 100
        balance_gap = abs(over.odds - under.odds)
        candidates.append(
            (
                0 if over.is_center or under.is_center else 1,
                balance_gap,
                over,
                under,
                normalized_over,
                normalized_under,
            )
        )
    if candidates:
        _, _, over, under, normalized_over, normalized_under = min(
            candidates, key=lambda candidate: (candidate[0], candidate[1])
        )
        over_probability = 100 / over.odds
        under_probability = 100 / under.odds
        selected_over = normalized_over >= normalized_under
        balanced_total = TotalBalance(
            threshold=over.threshold if over.threshold is not None else 0,
            over_odds=over.odds,
            under_odds=under.odds,
            over_probability_pct=over_probability,
            under_probability_pct=under_probability,
            selected_side="Plus de" if selected_over else "Moins de",
            selected_odds=over.odds if selected_over else under.odds,
            selected_probability_pct=max(normalized_over, normalized_under),
        )

    team_totals: list[TeamTotalPrediction] = []
    for group_id, team_number, over_type, under_type in (
        (15, 1, 11, 12),
        (62, 2, 13, 14),
    ):
        centered_lines = [
            (threshold, outcomes)
            for threshold, outcomes in team_total_lines.get(group_id, {}).items()
            if any(quote.is_center for quote in outcomes.values())
        ]
        if not centered_lines:
            continue

        def line_gap(item: tuple[float, dict[int, MarketQuote]]) -> float:
            over = item[1].get(over_type)
            under = item[1].get(under_type)
            if over is None or under is None:
                return math.inf
            return abs(over.odds - under.odds)

        threshold, outcomes = min(
            centered_lines,
            key=line_gap,
        )
        options = [
            (side, outcomes[event_type])
            for side, event_type in (("Plus", over_type), ("Moins", under_type))
            if event_type in outcomes
        ]
        if options:
            selected_side, selected_quote = min(options, key=lambda option: option[1].odds)
            team_totals.append(
                TeamTotalPrediction(
                    team_number=team_number,
                    threshold=threshold,
                    selected_side=selected_side,
                    selected_odds=selected_quote.odds,
                )
            )

    extra_penalties = None
    yes_quote = extra_penalties_outcomes.get("Oui")
    no_quote = extra_penalties_outcomes.get("Non")
    if yes_quote is not None and no_quote is not None:
        likely_quote = min((yes_quote, no_quote), key=lambda quote: quote.odds)
        extra_penalties = ExtraPenaltiesPrediction(
            yes_odds=yes_quote.odds,
            no_odds=no_quote.odds,
            likely_outcome=likely_quote.outcome_label,
            likely_odds=likely_quote.odds,
        )

    return GameAnalysis(
        tuple(exact_scores),
        balanced_total,
        tuple(market_probabilities),
        tuple(team_totals),
        extra_penalties,
        tuple(
            quote
            for quote in quotes
            if quote.group_id != 136
        ),
    )


class OddsSignalTracker:
    """Keep 15-second odds history per match and report newly triggered drops."""

    def __init__(self) -> None:
        self._history: dict[str, dict[str, deque[OddsSample]]] = {}
        self._active_signals: set[tuple[str, str]] = set()

    def _prune(self, now: float) -> None:
        for game_id, market_history in list(self._history.items()):
            for market, samples in list(market_history.items()):
                while samples and now - samples[0].timestamp > SIGNAL_WINDOW_SEC:
                    samples.popleft()
                if not samples:
                    del market_history[market]
            if not market_history:
                del self._history[game_id]

        retained = {
            (game_id, market)
            for game_id, market_history in self._history.items()
            for market in market_history
        }
        self._active_signals.intersection_update(retained)

    def update(
        self, match: Mapping[str, Any], *, timestamp: float | None = None
    ) -> SignalReport:
        game_value = match.get("gameId")
        if game_value is None:
            raise ValueError("Le match doit contenir gameId")
        game_id = str(game_value)
        now = time.monotonic() if timestamp is None else timestamp
        if not math.isfinite(now):
            raise ValueError("timestamp doit être un nombre fini")
        self._prune(now)

        is_live, current_score = _live_score(match)
        game_history = self._history.setdefault(game_id, {})
        seen_markets: set[str] = set()
        signals: list[OddsSignal] = []

        for quote in _extract_markets(match):
            if quote.group_id == 1:
                continue
            market_key = quote.key
            seen_markets.add(market_key)
            samples = game_history.setdefault(market_key, deque())
            valid_score = (
                not is_live
                or quote.score is None
                or (
                    current_score is not None
                    and current_score[0] <= quote.score[0]
                    and current_score[1] <= quote.score[1]
                )
            )
            candidates = []
            if valid_score:
                for sample in samples:
                    elapsed = now - sample.timestamp
                    if 0 < elapsed <= SIGNAL_WINDOW_SEC and sample.odds > quote.odds:
                        drop_pct = (sample.odds - quote.odds) / sample.odds * 100
                        if drop_pct >= SIGNAL_DROP_PCT:
                            candidates.append((drop_pct, sample.odds))

            signal_key = (game_id, market_key)
            if candidates:
                if signal_key not in self._active_signals:
                    drop_pct, old_odds = max(candidates, key=lambda item: item[0])
                    signals.append(
                        OddsSignal(
                            game_id=game_id,
                            market=quote.label,
                            old_odds=old_odds,
                            current_odds=quote.odds,
                            drop_pct=drop_pct,
                            market_key=quote.key,
                        )
                    )
                self._active_signals.add(signal_key)
            else:
                self._active_signals.discard(signal_key)

            samples.append(OddsSample(timestamp=now, odds=quote.odds))

        for active_game, market in tuple(self._active_signals):
            if active_game == game_id and market not in seen_markets:
                self._active_signals.discard((active_game, market))

        return SignalReport(tuple(signals))


def send_message(text: str, signal: SignalReport | None = None) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant dans .env")

    if signal is not None and signal.STRONG_SIGNAL:
        largest_drop = max(item.drop_pct for item in signal.signals)
        if not text.startswith("🚨 SIGNAL FORT ("):
            text = (
                f"🚨 <b>SIGNAL FORT</b> "
                f"(Chute de cote -{largest_drop:.1f}%)\n\n{text}"
            )

    restore_getaddrinfo = _temporarily_force_ipv4()
    try:
        for attempt in range(1, 4):
            try:
                request = Request(
                    f"{TELEGRAM_API}/bot{token}/sendMessage",
                    data=urlencode(
                        {
                            "chat_id": chat_id,
                            "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        }
                    ).encode("utf-8"),
                )
                with urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))

                if payload.get("ok"):
                    logger.info("Notification Telegram envoyée")
                    return True

                error_code = payload.get("error_code", 500)
                description = payload.get("description", "Erreur inconnue")
                logger.warning(
                    "Échec Telegram (%s : %s) - tentative %s/3",
                    error_code,
                    description,
                    attempt,
                )
                if error_code in (400, 401, 403):
                    return False
            except HTTPError as exc:
                error_code = exc.code
                body = exc.read().decode("utf-8", errors="replace")
                logger.warning(
                    "HTTP Telegram %s - tentative %s/3 : %s",
                    error_code,
                    attempt,
                    body[:200],
                )
                if error_code in (400, 401, 403):
                    return False
            except URLError as exc:
                logger.warning(
                    "Erreur réseau Telegram - tentative %s/3 : %s",
                    attempt,
                    exc,
                )
            except Exception as exc:  # pragma: no cover - défensive fallback
                logger.warning(
                    "Erreur Telegram imprévue - tentative %s/3 : %s",
                    attempt,
                    exc,
                )

            if attempt < 3:
                time.sleep(2 * attempt)

        logger.error("Impossible d'envoyer le message Telegram après 3 tentatives.")
        return False
    finally:
        restore_getaddrinfo()


def _format_stats_message(db_path: Path | None = None) -> str:
    from collector import HISTORY_DB_PATH, MatchHistoryRepository

    database = HISTORY_DB_PATH if db_path is None else db_path
    if not database.is_file():
        return "📊 Aucun historique de matchs enregistré."

    try:
        repository = MatchHistoryRepository(database, initialize=False)
        total_matches = repository.get_match_count()
        recent_matches = repository.get_recent_matches(15)
        cycle_stats = repository.get_cycle_statistics()
        prediction_stats = repository.get_prediction_statistics()
        live_signal_stats = repository.get_live_signal_statistics()
    except (OSError, sqlite3.Error):
        logger.exception("Impossible de lire la base de statistiques Telegram")
        return "⚠️ Impossible de lire les statistiques enregistrées."

    lines = [
        "📊 STATISTIQUES FC24 PENALTY",
        f"Matchs suivis : {total_matches}",
        (
            f"Bilan des 15 derniers matchs terminés : "
            f"{len(recent_matches)} match(s)"
        ),
        (
            f"Mort Subite récente : "
            f"{cycle_stats['recent_sudden_death_pct']:.1f}%"
        ),
        (
            "Moyenne récente : "
            f"{cycle_stats['recent_average_goals']:.1f} buts"
            if cycle_stats["recent_average_goals"] is not None
            else "Moyenne récente : indisponible"
        ),
        (
            f"Heure locale {cycle_stats['current_hour']:02d}h : "
            f"Mort Subite {cycle_stats['hour_sudden_death_pct']:.1f}% "
            f"({cycle_stats['hour_match_count']} match(s))"
        ),
        "Derniers scores terminés :",
    ]
    if not recent_matches:
        lines.append("Aucun match terminé enregistré.")
    else:
        for _, match_number, team1, score1, score2, team2, *_ in recent_matches:
            match_prefix = f"#{match_number} · " if match_number else ""
            lines.append(
                f"• {match_prefix}{team1 or 'Équipe 1'} "
                f"{score1}-{score2} {team2 or 'Équipe 2'}"
            )
    wins = int(prediction_stats["won"] or 0)
    losses = int(prediction_stats["lost"] or 0)
    settled_predictions = wins + losses
    win_rate = prediction_stats["win_rate_pct"]
    lines.extend(
        [
            "",
            "🎯 Tickets envoyés",
            f"Envoyés : {prediction_stats['sent']}",
            (
                f"Gagnés : {wins} · "
                f"Perdus : {losses} · "
                f"Annulés : {prediction_stats['void']} · "
                f"En attente : {prediction_stats['pending']} · "
                f"Envois à confirmer : "
                f"{prediction_stats['delivery_uncertain']}"
            ),
            (
                f"Taux de réussite réel : "
                f"{win_rate:.1f}%"
                if settled_predictions and win_rate is not None
                else "Taux de réussite réel : indisponible (aucun ticket réglé)"
            ),
        ]
    )
    live_win_rate = live_signal_stats["win_rate_pct"]
    live_wins = int(live_signal_stats["won"] or 0)
    live_losses = int(live_signal_stats["lost"] or 0)
    lines.extend(
        [
            "",
            "📡 Signaux LIVE",
            f"Signaux détectés : {live_signal_stats['detected']}",
            f"Tickets envoyés : {live_signal_stats['sent']}",
            (
                "Taux de réussite réel : "
                f"{live_win_rate:.1f}% "
                f"({live_wins}/{live_wins + live_losses})"
                if live_win_rate is not None
                else "Taux de réussite réel : indisponible "
                "(aucun ticket LIVE réglé)"
            ),
        ]
    )
    return "\n".join(lines)


async def _send_telegram_text(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: str,
    text: str,
) -> None:
    async with session.post(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
    ) as response:
        payload = await response.json(content_type=None)
    if response.status >= 400 or not isinstance(payload, Mapping) or not payload.get("ok"):
        raise RuntimeError("Échec de réponse à une commande Telegram")


async def _register_telegram_commands(
    session: aiohttp.ClientSession, token: str
) -> None:
    async with session.post(
        f"{TELEGRAM_API}/bot{token}/setMyCommands",
        json={"commands": TELEGRAM_COMMANDS},
    ) as response:
        payload = await response.json(content_type=None)
    if response.status >= 400 or not isinstance(payload, Mapping) or not payload.get("ok"):
        raise RuntimeError("Échec d'enregistrement des commandes Telegram")
    logger.info("Commandes Telegram enregistrées dans le menu")


async def _poll_telegram_updates(
    session: aiohttp.ClientSession,
    token: str,
    offset: int | None,
) -> list[Any]:
    params: dict[str, Any] = {
        "timeout": 30,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = offset

    async with session.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates",
        params=params,
    ) as response:
        payload = await response.json(content_type=None)
    if response.status >= 400 or not isinstance(payload, Mapping) or not payload.get("ok"):
        raise RuntimeError("Échec de récupération des commandes Telegram")

    updates = payload.get("result")
    if not isinstance(updates, list):
        raise ValueError("Réponse getUpdates invalide")
    return updates


async def _handle_telegram_update(
    update: Mapping[str, Any],
    processor: "GameEventProcessor",
    authorized_chat_id: str,
    token: str,
    session: aiohttp.ClientSession,
) -> None:
    message = update.get("message")
    if not isinstance(message, Mapping):
        return
    chat = message.get("chat")
    if not isinstance(chat, Mapping) or str(chat.get("id")) != authorized_chat_id:
        return

    text = message.get("text")
    if not isinstance(text, str) or not text.startswith("/"):
        return
    command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()

    if command == "/pause":
        processor.is_paused = True
        reply = "⏸️ Alertes mises en pause. Envoyez /resume pour reprendre."
    elif command == "/resume":
        processor.is_paused = False
        reply = "▶️ Alertes réactivées ! Surveillance FC24 Penalty en cours."
    elif command == "/status":
        bot_status = "⏸️ En pause" if processor.is_paused else "🟢 Actif"
        reply = (
            f"🤖 Bot : {bot_status}\n"
            f"Matchs traités durant la session : {processor.processed_match_count}\n"
            f"Seuil SIGNAL_DROP_PCT : {SIGNAL_DROP_PCT:g}%\n"
            f"Fenêtre SIGNAL_WINDOW_SEC : {SIGNAL_WINDOW_SEC:g} s"
        )
    elif command == "/stats":
        reply = await asyncio.to_thread(_format_stats_message)
    elif command == "/ai":
        if processor.ai_brain is None:
            reply = "🧠 TerminatAI n'est pas initialisé."
        else:
            reply = await asyncio.to_thread(processor.ai_brain.format_status)
    else:
        return

    await _send_telegram_text(session, token, authorized_chat_id, reply)


async def _run_command_listener(processor: "GameEventProcessor") -> None:
    _load_env_file()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être définis")

    offset: int | None = None
    timeout = aiohttp.ClientTimeout(total=35)
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        commands_registered = False
        while True:
            if not commands_registered:
                try:
                    await _register_telegram_commands(session, token)
                    commands_registered = True
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    logger.warning(
                        "Enregistrement des commandes Telegram indisponible (%s)",
                        type(exc).__name__,
                    )
                    await asyncio.sleep(2)
                    continue

            try:
                updates = await _poll_telegram_updates(session, token, offset)
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                RuntimeError,
                ValueError,
            ) as exc:
                logger.warning(
                    "Pause écoute Telegram suite à erreur temporaire : %s",
                    exc,
                )
                await asyncio.sleep(5)
                continue
            except Exception as exc:  # pragma: no cover - garde-fou défensif
                logger.warning(
                    "Pause écoute Telegram suite à erreur inattendue : %s",
                    exc,
                )
                await asyncio.sleep(5)
                continue

            for update in updates:
                if not isinstance(update, Mapping):
                    continue
                update_id = _as_int(update.get("update_id"))
                if update_id is not None:
                    offset = max(offset or 0, update_id + 1)
                try:
                    await _handle_telegram_update(
                        update, processor, chat_id, token, session
                    )
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    logger.error(
                        "Échec de traitement d'une commande Telegram (%s)",
                        type(exc).__name__,
                    )


def format_prediction_message(
    match: Mapping[str, Any],
    analysis: GameAnalysis,
    cycle_stats: CycleStatistics | None = None,
    ai_review: Mapping[str, Any] | None = None,
    signal: SignalReport | None = None,
) -> str:
    if cycle_stats is None:
        cycle_stats = CycleStatistics(current_hour=datetime.now().hour)
    game_id = escape(str(match["game_id"]))
    status = str(match.get("status", "")).upper()
    is_prematch = status == "PRE-MATCH"
    team1_name = str(match.get("team1") or "Équipe 1")
    team2_name = str(match.get("team2") or "Équipe 2")
    team1 = escape(team1_name)
    team2 = escape(team2_name)
    match_number = match.get("match_number")
    if match_number is None:
        match_number = match.get("dopInfo")
    match_identity = (
        f"#{escape(str(match_number))} · ID {game_id}"
        if match_number is not None and str(match_number).strip()
        else f"ID {game_id}"
    )
    lines: list[str] = []

    # ── EN-TÊTE ──────────────────────────────────────────────
    if is_prematch:
        lines.append("⚽ <b>FC24 PENALTY · PRE-MATCH</b>")
        lines.append(f"🏟 <b>{escape(team1_name.upper())} vs "
                     f"{escape(team2_name.upper())}</b>")
        lines.append(f"🔖 {match_identity}")
        time_sec = match.get("timeSec")
        if time_sec is not None:
            remaining = max(0, math.ceil(time_sec))
            minutes, seconds = divmod(remaining, 60)
            countdown = (
                f"{minutes} min {seconds} s" if minutes else f"{seconds} s"
            )
            lines.append(f"⏳ Coup d'envoi dans <b>{countdown}</b>")
    else:
        if signal is not None and signal.STRONG_SIGNAL:
            largest_drop = max(item.drop_pct for item in signal.signals)
            lines.append(
                f"🚨 <b>SIGNAL FORT</b> (Chute de cote <b>-{largest_drop:.1f}%</b>)"
            )
        lines.append("🔴 <b>FC24 PENALTY · EN DIRECT</b>")
        lines.append(f"🏟 <b>{escape(team1_name.upper())} "
                     f"{match['score1']} - {match['score2']} "
                     f"{escape(team2_name.upper())}</b>")
        lines.append(f"🔖 {match_identity}")

    lines.append("")

    # ── DÉCISION IA ──────────────────────────────────────────
    matchup: Mapping[str, Any] = {}
    top_markets: list[Any] = []
    if ai_review is not None:
        matchup = ai_review.get("matchup", {})
        top_markets = list(ai_review.get("top_markets", []))[:3]
        decision = ai_review.get("decision")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("🧠 <b>DÉCISION TERMINAT-AI</b>")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        if isinstance(decision, Mapping):
            confidence = decision['confidence_pct']
            roi = decision.get("historical_roi_pct")
            lines.append("")
            lines.append(f"🎯 <b>PARI CONSEILLÉ :</b>")
            lines.append(f"👉 <b>{escape(str(decision['label']))}</b>")
            lines.append(f"💎 Cote : <b>@{decision['odds']:g}</b>")
            lines.append(f"📊 Confiance : <b>{confidence:.1f}%</b>")
            if isinstance(roi, (int, float)):
                lines.append(f"📈 Rentabilité historique : <b>{roi:+.1f}%</b>")
        else:
            lines.append("❌ Aucun pari fiable validé")

    lines.append("")
    lines.append("🤖 <i>Terminat-AI · FC24 Penalty</i>")

    return "\n".join(lines)


class GameEventProcessor:
    """Parse gameEvents and enforce one ticket per game."""

    def __init__(
        self,
        sender: Callable[..., Any] | None = None,
        tracker: OddsSignalTracker | None = None,
        history_repository: Any | None = None,
        ai_brain: Any | None = None,
    ) -> None:
        self._sender = send_message if sender is None else sender
        self._tracker = OddsSignalTracker() if tracker is None else tracker
        self._history_repository = history_repository
        self.ai_brain = ai_brain
        self._delivery_state: dict[str, MatchDeliveryState] = {}
        self.is_paused = False
        self._processed_match_ids: set[str] = set()

    @property
    def processed_match_count(self) -> int:
        return len(self._processed_match_ids)

    def _cycle_statistics(self) -> CycleStatistics:
        if self._history_repository is None:
            return CycleStatistics(current_hour=datetime.now().hour)
        return CycleStatistics(**self._history_repository.get_cycle_statistics())

    def _record_history(self, match: Mapping[str, Any], analysis: GameAnalysis) -> None:
        if self._history_repository is None:
            return

        outcomes = {
            market.event_type: market
            for market in analysis.market_probabilities
            if market.group_id == 1 and market.event_type in (1, 3)
        }
        home, away = outcomes.get(1), outcomes.get(3)
        if home is None or away is None or home.odds == away.odds:
            favorite_team = favorite_odds = None
        elif home.odds < away.odds:
            favorite_team, favorite_odds = match.get("team1"), home.odds
        else:
            favorite_team, favorite_odds = match.get("team2"), away.odds

        total = analysis.balanced_total
        cycle_regime = self._cycle_statistics().regime
        self._history_repository.record_match(
            match,
            fav_team=favorite_team,
            fav_odds=favorite_odds,
            center_total_line=None if total is None else total.threshold,
            over_odds=None if total is None else total.over_odds,
            under_odds=None if total is None else total.under_odds,
            regulation_markets=(
                {
                    "key": quote.key,
                    "group_id": quote.group_id,
                    "event_type": quote.event_type,
                    "threshold": quote.threshold,
                    "label": quote.label,
                    "group_label": quote.group_label,
                    "outcome_label": quote.outcome_label,
                    "is_extra_penalties": quote.is_extra_penalties,
                    "is_center": quote.is_center,
                    "odds": quote.odds,
                }
                for quote in analysis.regulation_markets
            ),
            cycle_regime=cycle_regime,
        )
        if match["status"] == "FINISHED":
            self._history_repository.settle_sent_predictions(match)
            self._history_repository.settle_exact_score_predictions(match)
            if self.ai_brain is not None and self.ai_brain.maybe_train():
                logger.info("Cycle d'apprentissage terminé:\n%s", self.ai_brain.format_training_summary())

    def _format_prediction(
        self,
        match: Mapping[str, Any],
        analysis: GameAnalysis,
        ai_review: Mapping[str, Any],
        signal: SignalReport,
    ) -> str:
        return format_prediction_message(
            match,
            analysis,
            self._cycle_statistics(),
            ai_review,
            signal,
        )

    def _review_prediction(
        self,
        match: Mapping[str, Any],
        analysis: GameAnalysis,
        signal_drops: Mapping[str, float] | None = None,
        *,
        unfiltered: bool = False,
    ) -> Mapping[str, Any] | None:
        if self.ai_brain is None:
            return None
        cycle_stats = self._cycle_statistics()
        prediction_match = dict(match)
        prediction_match["hour_of_day"] = cycle_stats.current_hour
        return self.ai_brain.review_and_approve(
            prediction_match,
            analysis,
            signal_drops,
            unfiltered=unfiltered,
        )

    def process_game_event(
        self, data: Mapping[str, Any] | list[Any]
    ) -> bool:
        if isinstance(data, list):
            sent_any = False
            for index, item in enumerate(data):
                if not isinstance(item, Mapping):
                    raise ValueError(f"Match invalide à l'index {index}")
                sent_any = self.process_game_event(item) or sent_any
            return sent_any
        if not isinstance(data, Mapping):
            raise TypeError("gameEvents doit être un objet ou une liste de matchs")

        match = parse_game_event(data)
        analysis = analyze_game_event(match)
        game_id = str(match["game_id"])
        self._processed_match_ids.add(game_id)
        state = self._delivery_state.setdefault(game_id, MatchDeliveryState())
        self._record_history(match, analysis)

        phase = match["status"]
        if phase == "FINISHED":
            return False

        if phase not in ("PRE-MATCH", "LIVE"):
            return False

        is_live = phase == "LIVE"
        phase_alert_sent = (
            state.live_alert_sent if is_live else state.prematch_alert_sent
        )
        if phase_alert_sent or (
            self._history_repository is not None
            and self._history_repository.has_sent_prediction(game_id, phase)
        ):
            return False

        signal = self._tracker.update(match) if is_live else SignalReport(())

        quotes = {quote.key: quote for quote in analysis.regulation_markets}
        signal_drops: dict[str, float] = {}
        snapshot_ids: dict[str, int] = {}
        for item in signal.signals:
            market_key = item.market_key or item.market
            quote = quotes.get(market_key)
            if quote is None:
                quote = next(
                    (
                        market
                        for market in analysis.regulation_markets
                        if market.label == item.market
                    ),
                    None,
                )
                if quote is not None:
                    market_key = quote.key
            signal_drops[market_key] = item.drop_pct
            if self._history_repository is not None:
                odds = (
                    quote.odds
                    if quote is not None
                    else item.current_odds
                )
                snapshot_ids[market_key] = (
                    self._history_repository.record_live_signal_snapshot(
                        match,
                        trigger_time_sec=match["timeSec"],
                        score1_at_trigger=match["score1"],
                        score2_at_trigger=match["score2"],
                        drop_pct=item.drop_pct,
                        market_key=market_key,
                        market_label=(
                            quote.label if quote is not None else item.market
                        ),
                        odds_at_trigger=odds,
                        estimated_prob=1 / odds,
                        group_id=None if quote is None else quote.group_id,
                        event_type=None if quote is None else quote.event_type,
                        threshold=None if quote is None else quote.threshold,
                    )
                )

        # Aucun mode « indicatif » : un message n'est envoyé que si un pari
        # satisfait réellement tous les seuils (cote, confiance, value,
        # historique et rentabilité prouvée).
        review = self._review_prediction(
            match,
            analysis,
            signal_drops or None,
        )
        if review is None:
            return False
        review = dict(review)
        review["unfiltered"] = False
        for market in review.get("evaluated_markets", []):
            if not isinstance(market, Mapping):
                continue
            market_key = market.get("market_key")
            if not isinstance(market_key, str):
                continue
            snapshot_id = snapshot_ids.get(market_key)
            probability_pct = market.get("confidence_pct")
            if (
                self._history_repository is not None
                and snapshot_id is not None
                and isinstance(probability_pct, (int, float))
                and math.isfinite(probability_pct)
            ):
                self._history_repository.update_live_signal_snapshot(
                    snapshot_id,
                    estimated_prob=probability_pct / 100,
                )

        decision = review.get("decision")
        decision_key = (
            decision.get("market_key")
            if isinstance(decision, Mapping)
            else None
        )
        snapshot_id = (
            snapshot_ids.get(decision_key)
            if isinstance(decision_key, str)
            else None
        )
        if (
            self.is_paused
            or review.get("approved") is not True
            or not isinstance(decision, Mapping)
        ):
            return False

        message = self._format_prediction(match, analysis, review, signal)
        if (
            self._history_repository is not None
            and not self._history_repository.reserve_sent_prediction(
                match, decision
            )
        ):
            if is_live:
                state.live_alert_sent = True
            else:
                state.prematch_alert_sent = True
            return False
        # Reserve before I/O to avoid duplicates after an ambiguous timeout.
        if is_live:
            state.live_alert_sent = True
        else:
            state.prematch_alert_sent = True
        if self._history_repository is not None:
            self._history_repository.record_exact_score_predictions(
                match,
                review.get("top_exact_scores", ()),
            )
        send_result = self._sender(message, signal)
        if send_result is False:
            raise RuntimeError("L'envoi du ticket Telegram a échoué")
        if self._history_repository is not None and snapshot_id is not None:
            self._history_repository.update_live_signal_snapshot(
                snapshot_id,
                approved=True,
            )
        if self._history_repository is not None:
            self._history_repository.mark_prediction_sent(game_id, phase)
        return True


async def _run_collector() -> None:
    _load_env_file()
    from collector import (
        DEFAULT_POLL_INTERVAL_SEC,
        APICollector,
        LocalFileCollector,
        MatchHistoryRepository,
    )
    from my_ai_brain import TerminatAI

    history_repository = MatchHistoryRepository()
    ai_brain = TerminatAI(db_path=str(history_repository.db_path))
    processor = GameEventProcessor(
        history_repository=history_repository,
        ai_brain=ai_brain,
    )
    if getattr(ai_brain, "training_completed_on_load", False) is True:
        logger.info("%s", ai_brain.format_training_summary())
    data_source = os.getenv("DATA_SOURCE", "").strip().upper()
    feed_api_url = os.getenv("FEED_API_URL", "").strip()
    if not data_source and feed_api_url:
        data_source = "API"
    if not data_source:
        data_source = "LOCAL"

    if data_source == "LOCAL":
        collector = LocalFileCollector(processor)
    elif data_source == "API":
        if not feed_api_url:
            raise RuntimeError("FEED_API_URL doit être défini en mode API")
        poll_interval = _read_positive_setting(
            "POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC
        )
        collector = APICollector(
            feed_api_url=feed_api_url,
            processor=processor,
            poll_interval_sec=poll_interval,
        )
    else:
        raise ValueError("DATA_SOURCE doit être API ou LOCAL")

    ai_brain.start_auto_training(
        on_trained=lambda: logger.info(
            "%s", ai_brain.format_training_summary()
        )
    )
    try:
        await asyncio.gather(collector.run(), _run_command_listener(processor))
    finally:
        ai_brain.stop_auto_training()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from collector import (
        FeedAccessError,
        FeedRequestError,
        InvalidFeedResponseError,
    )

    try:
        asyncio.run(_run_collector())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Arrêt demandé par l'utilisateur")
    except (FeedAccessError, FeedRequestError, InvalidFeedResponseError) as exc:
        logging.getLogger("collector").error("%s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
