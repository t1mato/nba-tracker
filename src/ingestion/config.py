"""Shared configuration for the ingestion layer.

Everything tunable lives here so the pipeline code stays about *what* it does,
not *where* things are or *how long* to wait.
"""

import datetime as dt
import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = two levels up from this file (src/ingestion/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load .env from the project root explicitly. Without the path, python-dotenv
# guesses from the caller's stack frame, which breaks when the script is invoked
# from another directory — as the GitHub Actions runner will do.
load_dotenv(PROJECT_ROOT / ".env")


def get_database_url() -> str:
    """The one connection string every layer uses.

    Read lazily via a function rather than a module-level constant so an import
    never explodes at collection time (pytest imports this module).
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env, or export it.\n"
            "Local default: postgresql://nba:nba_dev_password@localhost:5432/nba_tracker"
        )
    return url


# --- stats.nba.com politeness -----------------------------------------------
# nba_api wraps undocumented endpoints. Hammering them gets you rate-limited or
# IP-blocked, and a backfill makes ~1300 calls, so these matter.

REQUEST_DELAY_SECONDS = 0.8   # between successive calls
REQUEST_TIMEOUT_SECONDS = 60  # per call; the endpoint is genuinely slow sometimes
MAX_RETRIES = 4               # attempts per call before giving up
BACKOFF_BASE_SECONDS = 2.0    # exponential: 2s, 4s, 8s ...


# The month a new NBA season begins. October, and stable enough to be a
# constant rather than a lookup.
SEASON_START_MONTH = 10


# --- which games belong in the warehouse ------------------------------------
# The first three characters of a game_id encode its type. Verified against the
# full 2025-26 season (1401 games).
GAME_TYPE_BY_PREFIX = {
    "001": "preseason",      # incl. exhibitions vs non-NBA clubs (foreign team
                             # ids, duplicate partial rows) — excluded
    "002": "regular",        # 1230 games
    "003": "allstar",        # fake rosters (Stripes / Stars / World) — excluded
    "004": "playoffs",       # 85 games
    "005": "playin",         # 6 games
    "006": "nba_cup_final",  # 1 game; real teams, but excluded from
                             # regular-season aggregates downstream
}

# What we actually ingest. Preseason and All-Star are noise for this warehouse.
IN_SCOPE_PREFIXES = frozenset({"002", "004", "005", "006"})


# --- which season are we in ---------------------------------------------------

def current_season(today: dt.date | None = None) -> str:
    """The NBA season containing `today`, as "2025-26".

    Seasons start in October and run into June, so the calendar year alone is
    not the answer: 2026-03-01 belongs to 2025-26, not 2026-27.

    The scheduled job derives the season rather than taking it as a parameter.
    A hardcoded season is a time bomb — it works until October and then quietly
    ingests nothing, which is the exact failure this pipeline is built to avoid.

    Offseason dates (July through September) resolve to the season that just
    finished, so the job keeps finding no new games until the new one tips off.
    That is correct: there is genuinely nothing to fetch.
    """
    today = today or dt.date.today()
    start_year = today.year if today.month >= SEASON_START_MONTH else today.year - 1
    # "2025-26"; matches the label 020_load_dimensions.sql derives, including
    # the century roll ("2099-00", the NBA's own convention).
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def game_type(game_id: str) -> str:
    """'0022500578' -> 'regular'. Unknown prefixes pass through labelled."""
    return GAME_TYPE_BY_PREFIX.get(game_id[:3], f"unknown_{game_id[:3]}")


def is_in_scope(game_id: str) -> bool:
    """True if this game belongs in the warehouse."""
    return game_id[:3] in IN_SCOPE_PREFIXES
