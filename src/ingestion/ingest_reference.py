"""Ingest slow-changing reference data: team conference/division, player positions.

    python -m src.ingestion.ingest_reference               # current season
    python -m src.ingestion.ingest_reference --season 2025-26

31 API calls total (1 standings + 30 rosters), so this is run occasionally
rather than nightly.
"""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg2

from src.ingestion import nba_client
from src.ingestion.config import get_database_url
from src.ingestion.ingest_games import _to_rows, _upsert

log = logging.getLogger("ingest-ref")

DEFAULT_SEASON = "2025-26"

TEAMS_COLUMNS = {
    "TeamID": "team_id", "season": "season", "TeamCity": "team_city",
    "TeamName": "team_name", "Conference": "conference", "Division": "division",
}

ROSTER_COLUMNS = {
    "PLAYER_ID": "player_id", "TeamID": "team_id", "season": "season",
    "PLAYER": "player_name", "POSITION": "position", "NUM": "jersey_num",
    "HEIGHT": "height", "WEIGHT": "weight", "AGE": "age",
}


def load_teams(conn, season: str) -> list[int]:
    """Write conference/division to stg_teams. Returns the team ids."""
    standings = nba_client.fetch_standings(season).assign(season=season)

    columns, rows = _to_rows(standings, TEAMS_COLUMNS, key=["team_id", "season"])
    _upsert(conn, "staging.stg_teams", columns, rows, key=["team_id", "season"])
    conn.commit()
    log.info("stg_teams: upserted %d rows", len(rows))

    return standings["TeamID"].tolist()


def load_rosters(conn, team_ids: list[int], season: str) -> list[int]:
    """Write one roster per team to stg_players. Returns failed team ids."""
    failures: list[int] = []

    for i, team_id in enumerate(team_ids, start=1):
        try:
            roster = nba_client.fetch_team_roster(team_id, season).assign(season=season)
            columns, rows = _to_rows(roster, ROSTER_COLUMNS,
                                     key=["player_id", "team_id", "season"])
            _upsert(conn, "staging.stg_players", columns, rows,
                    key=["player_id", "team_id", "season"])
            conn.commit()
            log.info("[%d/%d] team %d: %d players", i, len(team_ids), team_id, len(rows))
        except Exception as exc:
            conn.rollback()
            failures.append(team_id)
            log.error("[%d/%d] team %d FAILED: %s: %s",
                      i, len(team_ids), team_id, type(exc).__name__, exc)

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default=DEFAULT_SEASON, help='e.g. "2025-26"')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    with psycopg2.connect(get_database_url()) as conn:
        team_ids = load_teams(conn, args.season)
        failures = load_rosters(conn, team_ids, args.season)

    if failures:
        log.error("%d team(s) failed: %s", len(failures), failures)
        return 1

    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
