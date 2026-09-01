"""Ingest NBA box scores into the staging tables.

    python -m nba_tracker.ingestion.ingest_games --catch-up       # the scheduled job
    python -m nba_tracker.ingestion.ingest_games                  # yesterday
    python -m nba_tracker.ingestion.ingest_games 2026-01-15       # one specific date
    python -m nba_tracker.ingestion.ingest_games --season 2025-26 # backfill a season

--catch-up is what the scheduled job runs: it works out the current season,
lists its games in one call, and fetches only the ones the warehouse does not
already have. That makes the job self-healing rather than punctual — if it does
not run for three days, the next run collects all three. A job that only ever
looks at yesterday turns every missed night into a permanent hole.

Idempotent throughout: every write is an upsert on the table's natural key, so
re-running any date updates rows in place and never duplicates them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from nba_tracker.ingestion import nba_client
from nba_tracker.ingestion.config import current_season, get_database_url, is_in_scope

log = logging.getLogger("ingest")


# --- column mappings ---------------------------------------------------------
# Left: the column in the API payload. Right: our staging column.
# The API's *_PCT columns are deliberately absent — they report 0.0 for zero
# attempts, so an 0-for-0 night would read as "0% shooting". Derived downstream.

GAMES_COLUMNS = {
    "GAME_ID": "game_id", "TEAM_ID": "team_id", "SEASON_ID": "season_id",
    "GAME_DATE": "game_date", "TEAM_ABBREVIATION": "team_abbreviation",
    "TEAM_NAME": "team_name", "MATCHUP": "matchup", "WL": "wl",
    "MIN": "min", "PTS": "pts", "FGM": "fgm", "FGA": "fga",
    "FG3M": "fg3m", "FG3A": "fg3a", "FTM": "ftm", "FTA": "fta",
    "OREB": "oreb", "DREB": "dreb", "REB": "reb", "AST": "ast",
    "STL": "stl", "BLK": "blk", "TOV": "tov", "PF": "pf",
    "PLUS_MINUS": "plus_minus",
}

# V3 renamed everything to camelCase — note `foulsPersonal` and `personId`.
PLAYER_BOX_COLUMNS = {
    "gameId": "game_id", "personId": "player_id", "teamId": "team_id",
    "teamTricode": "team_tricode", "firstName": "first_name",
    "familyName": "family_name", "position": "position", "comment": "comment",
    "minutes": "minutes",
    "fieldGoalsMade": "fgm", "fieldGoalsAttempted": "fga",
    "threePointersMade": "fg3m", "threePointersAttempted": "fg3a",
    "freeThrowsMade": "ftm", "freeThrowsAttempted": "fta",
    "reboundsOffensive": "oreb", "reboundsDefensive": "dreb",
    "reboundsTotal": "reb", "assists": "ast", "steals": "stl",
    "blocks": "blk", "turnovers": "turnovers", "foulsPersonal": "pf",
    "points": "pts", "plusMinusPoints": "plus_minus",
}


def _clean(value):
    """Make one pandas value safe for psycopg2.

    NaN/NaT become NULL, and blank strings become NULL so a DNP's empty
    `minutes` lands as NULL rather than an empty string. numpy scalars are
    unwrapped to Python types, which psycopg2 can adapt.
    """
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if hasattr(value, "item"):      # numpy int64 / float64 -> int / float
        return value.item()
    return value


def _to_rows(df: pd.DataFrame, mapping: dict[str, str], key: list[str]):
    """Map API columns to staging columns and return (columns, rows).

    Deduplicates on the primary key: Postgres rejects an ON CONFLICT DO UPDATE
    whose batch touches the same row twice ("cannot affect row a second time"),
    and the API does return duplicate rows for some games.
    """
    missing = set(mapping) - set(df.columns)
    if missing:
        raise KeyError(f"payload is missing expected columns: {sorted(missing)}")

    renamed = df[list(mapping)].rename(columns=mapping)
    renamed = renamed.drop_duplicates(subset=key, keep="last")

    columns = list(renamed.columns)
    rows = [tuple(_clean(v) for v in record) for record in renamed.itertuples(index=False)]
    return columns, rows


def _upsert(conn, table: str, columns: list[str], rows: list[tuple], key: list[str]) -> int:
    """Insert rows, updating any that already exist. Returns rows written.

    This is what makes re-running a date safe: the natural key collides and the
    existing row is refreshed in place.
    """
    if not rows:
        return 0

    updatable = [c for c in columns if c not in key]
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)

    sql = f"""
        INSERT INTO {table} ({", ".join(columns)})
        VALUES %s
        ON CONFLICT ({", ".join(key)}) DO UPDATE
        SET {assignments}, ingested_at = now()
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=500)
    return len(rows)


def _already_ingested(conn) -> set[str]:
    """game_ids that already have player rows — used to resume a backfill."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT game_id FROM staging.stg_player_box")
        return {row[0] for row in cur.fetchall()}


def load_games(conn, games_df: pd.DataFrame) -> list[str]:
    """Write the game list to stg_games. Returns the in-scope game_ids."""
    if games_df.empty:
        return []

    games_df = games_df.copy()
    games_df["GAME_DATE"] = pd.to_datetime(games_df["GAME_DATE"]).dt.date

    # Drop preseason and All-Star: exhibitions against non-NBA clubs carry
    # foreign team ids, and All-Star rosters aren't real teams.
    in_scope = games_df[games_df["GAME_ID"].map(is_in_scope)]
    skipped = len(games_df) - len(in_scope)
    if skipped:
        log.info("skipped %d out-of-scope team rows (preseason / all-star)", skipped)

    if in_scope.empty:
        return []

    columns, rows = _to_rows(in_scope, GAMES_COLUMNS, key=["game_id", "team_id"])
    written = _upsert(conn, "staging.stg_games", columns, rows, key=["game_id", "team_id"])
    conn.commit()
    log.info("stg_games: upserted %d rows", written)

    return sorted(in_scope["GAME_ID"].unique())


def load_box_scores(conn, game_ids: list[str], skip_existing: bool = False) -> tuple[int, list[str]]:
    """Fetch and load one box score per game. Returns (rows_written, failures).

    Each game commits on its own, so a failure part-way through a long backfill
    keeps everything already loaded and the run can simply be re-run.
    """
    if skip_existing:
        done = _already_ingested(conn)
        remaining = [g for g in game_ids if g not in done]
        if len(remaining) < len(game_ids):
            log.info("resuming: %d of %d games already ingested",
                     len(game_ids) - len(remaining), len(game_ids))
        game_ids = remaining

    total_rows = 0
    failures: list[str] = []

    for i, game_id in enumerate(game_ids, start=1):
        try:
            box = nba_client.fetch_player_box(game_id)
            columns, rows = _to_rows(box, PLAYER_BOX_COLUMNS, key=["game_id", "player_id"])
            written = _upsert(conn, "staging.stg_player_box", columns, rows,
                              key=["game_id", "player_id"])
            conn.commit()
            total_rows += written
            log.info("[%d/%d] %s: %d player rows", i, len(game_ids), game_id, written)
        except Exception as exc:
            # One bad game must not abort a 1300-game backfill. Report at the end.
            conn.rollback()
            failures.append(game_id)
            log.error("[%d/%d] %s FAILED: %s: %s",
                      i, len(game_ids), game_id, type(exc).__name__, exc)

    return total_rows, failures


def ingest_date(conn, game_date: dt.date) -> list[str]:
    """Ingest every completed game on one date."""
    games_df = nba_client.fetch_games_for_date(game_date)
    if games_df.empty:
        log.info("no games found for %s", game_date.isoformat())
        return []

    game_ids = load_games(conn, games_df)
    log.info("%d in-scope games on %s", len(game_ids), game_date.isoformat())

    _, failures = load_box_scores(conn, game_ids)
    return failures


def ingest_season(conn, season: str) -> list[str]:
    """Backfill a whole season. One discovery call, then one call per game."""
    games_df = nba_client.fetch_games_for_season(season)
    game_ids = load_games(conn, games_df)
    log.info("%d in-scope games in season %s", len(game_ids), season)

    _, failures = load_box_scores(conn, game_ids, skip_existing=True)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("date", nargs="?", help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--season", help='backfill a whole season, e.g. "2025-26"')
    parser.add_argument("--catch-up", action="store_true",
                        help="fetch every game of the current season the warehouse lacks")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    with psycopg2.connect(get_database_url()) as conn:
        if args.catch_up:
            # Derive the season rather than hardcoding it: a literal "2025-26"
            # would keep working right up until October and then silently
            # ingest nothing.
            season = current_season()
            log.info("catch-up for season %s", season)
            failures = ingest_season(conn, season)
        elif args.season:
            failures = ingest_season(conn, args.season)
        else:
            # Default to yesterday: the scheduled job ingests completed games,
            # and today's are still in progress.
            target = (dt.date.fromisoformat(args.date) if args.date
                      else dt.date.today() - dt.timedelta(days=1))
            failures = ingest_date(conn, target)

    if failures:
        log.error("%d game(s) failed: %s", len(failures), ", ".join(failures))
        return 1

    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
