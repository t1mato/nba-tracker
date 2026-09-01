"""Thin, polite wrapper around the nba_api endpoints we use.

Responsibility: *get the data*. It does not know the database exists.

Two things this layer exists to handle:
  1. Throttling — stats.nba.com is undocumented and will rate-limit or block a
     client that hammers it. A season backfill is ~1300 calls.
  2. Flakiness — the endpoint intermittently times out or returns a non-JSON
     body. A single blip should not kill a 20-minute backfill.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time

import pandas as pd
from nba_api.stats.endpoints import boxscoretraditionalv3, leaguegamefinder

from src.ingestion.config import (
    BACKOFF_BASE_SECONDS,
    MAX_RETRIES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
)

log = logging.getLogger(__name__)

# Timestamp of the last outbound request, so _throttle can space calls out
# without sleeping longer than necessary.
_last_request_at: float = 0.0


def _throttle() -> None:
    """Ensure at least REQUEST_DELAY_SECONDS since the previous request.

    Sleeps only the remaining time rather than a flat delay, so slow calls
    (which already took a second) aren't penalised twice.
    """
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    remaining = REQUEST_DELAY_SECONDS - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_request_at = time.monotonic()


# Failures worth retrying: network trouble, and the HTML/empty bodies
# stats.nba.com returns when it is unhappy (which surface as JSON errors).
_RETRYABLE = (
    OSError,                  # includes requests' ConnectionError / Timeout
    json.JSONDecodeError,
    ValueError,               # pandas/nba_api raise this on malformed payloads
)


def _call(label: str, fn):
    """Run one API call with throttling and exponential backoff.

    `fn` is a zero-arg callable so it can be re-invoked on each attempt.
    Raises the last exception if every attempt fails — a caller ingesting many
    games can then decide whether to skip this one or abort.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            return fn()
        except _RETRYABLE as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            wait = BACKOFF_BASE_SECONDS ** attempt
            log.warning(
                "%s failed (attempt %d/%d): %s: %s — retrying in %.0fs",
                label, attempt, MAX_RETRIES, type(exc).__name__, exc, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"{label} failed after {MAX_RETRIES} attempts") from last_error


def fetch_games_for_date(game_date: dt.date) -> pd.DataFrame:
    """Every NBA game on one date, as team-level rows (2 per game).

    This is the game-discovery step and the source of game_date, matchup and
    win/loss — none of which appear in the box score payload.
    """
    stamp = game_date.strftime("%m/%d/%Y")  # nba_api wants MM/DD/YYYY
    log.info("fetching game list for %s", game_date.isoformat())

    return _call(
        f"leaguegamefinder({game_date.isoformat()})",
        lambda: leaguegamefinder.LeagueGameFinder(
            date_from_nullable=stamp,
            date_to_nullable=stamp,
            league_id_nullable="00",   # "00" = NBA, excluding G-League / WNBA
            timeout=REQUEST_TIMEOUT_SECONDS,
        ).get_data_frames()[0],
    )


def fetch_games_for_season(season: str) -> pd.DataFrame:
    """Every game in a season, e.g. "2025-26". One call, no pagination.

    Used by the backfill so it can discover ~1400 games up front instead of
    making one discovery call per calendar day.
    """
    log.info("fetching full game list for season %s", season)

    return _call(
        f"leaguegamefinder(season={season})",
        lambda: leaguegamefinder.LeagueGameFinder(
            season_nullable=season,
            league_id_nullable="00",
            timeout=REQUEST_TIMEOUT_SECONDS,
        ).get_data_frames()[0],
    )


def fetch_player_box(game_id: str) -> pd.DataFrame:
    """Player stat lines for one game (~26 rows, DNPs included).

    Uses BoxScoreTraditionalV3. V2 is deprecated and returns zero rows with its
    columns intact — it fails silently, so it must not be used here.
    """
    return _call(
        f"boxscorev3({game_id})",
        lambda: boxscoretraditionalv3.BoxScoreTraditionalV3(
            game_id=game_id,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ).get_data_frames()[0],
    )
