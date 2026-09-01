"""Tests for the ingestion layer.

These are deliberately pure: no database, no network. Everything here exercises
the mapping and cleaning logic that sits between the API payload and the INSERT,
which is where the subtle bugs live.
"""

from __future__ import annotations

import datetime as dt
import time

import numpy as np
import pandas as pd
import pytest

from src.ingestion import nba_client
from src.ingestion.config import (
    GAME_TYPE_BY_PREFIX,
    current_season,
    game_type,
    is_in_scope,
)
from src.ingestion.ingest_games import (
    GAMES_COLUMNS,
    PLAYER_BOX_COLUMNS,
    _clean,
    _to_rows,
)


# --- game type filtering -----------------------------------------------------

class TestGameTypeFilter:
    """The game_id prefix decides what belongs in the warehouse."""

    @pytest.mark.parametrize("game_id,expected", [
        ("0012500009", "preseason"),
        ("0022500578", "regular"),
        ("0032500041", "allstar"),
        ("0042500101", "playoffs"),
        ("0052500201", "playin"),
        ("0062500001", "nba_cup_final"),
    ])
    def test_decodes_every_known_prefix(self, game_id, expected):
        assert game_type(game_id) == expected

    def test_unknown_prefix_is_labelled_not_crashed(self):
        # A new game type should be visible in the data, not blow up ingestion.
        assert game_type("0992500001") == "unknown_099"

    @pytest.mark.parametrize("game_id,expected", [
        ("0022500578", True),    # regular season
        ("0042500101", True),    # playoffs
        ("0052500201", True),    # play-in
        ("0062500001", True),    # NBA Cup final
        ("0012500009", False),   # preseason: exhibitions vs non-NBA clubs
        ("0032500041", False),   # all-star: rosters aren't real teams
    ])
    def test_scope(self, game_id, expected):
        assert is_in_scope(game_id) is expected

    def test_every_prefix_has_a_scope_decision(self):
        # Guards against adding a prefix to the map and forgetting to decide.
        for prefix in GAME_TYPE_BY_PREFIX:
            assert isinstance(is_in_scope(f"{prefix}2500001"), bool)


# --- value cleaning ----------------------------------------------------------

class TestClean:
    """_clean makes one pandas value safe for psycopg2."""

    @pytest.mark.parametrize("value", [None, np.nan, float("nan"), pd.NA, pd.NaT])
    def test_missing_values_become_null(self, value):
        assert _clean(value) is None

    @pytest.mark.parametrize("value", ["", "   ", "\t"])
    def test_blank_strings_become_null(self, value):
        # A DNP's empty `minutes` must land as NULL, not an empty string —
        # otherwise "did they play" queries silently match everyone.
        assert _clean(value) is None

    def test_real_strings_survive_and_are_stripped(self):
        assert _clean("26:41") == "26:41"
        assert _clean("  ORL  ") == "ORL"

    def test_zero_is_kept_not_nulled(self):
        # Regression guard: 0 is falsy but a real stat line. A player who scored
        # 0 points is not a player with unknown points.
        assert _clean(0) == 0
        assert _clean(np.int64(0)) == 0
        assert _clean(0.0) == 0.0

    def test_numpy_scalars_are_unwrapped_to_python_types(self):
        # psycopg2 cannot adapt numpy types directly.
        result = _clean(np.int64(26))
        assert result == 26 and isinstance(result, int)

        result = _clean(np.float64(-7.5))
        assert result == -7.5 and isinstance(result, float)


# --- payload -> rows ---------------------------------------------------------

def _player_box_frame(**overrides) -> pd.DataFrame:
    """One minimal BoxScoreTraditionalV3-shaped row."""
    row = {col: 0 for col in PLAYER_BOX_COLUMNS}
    row.update({
        "gameId": "0022500578", "personId": 1630532, "teamId": 1610612753,
        "teamTricode": "ORL", "firstName": "Franz", "familyName": "Wagner",
        "position": "F", "comment": "", "minutes": "26:41",
    })
    row.update(overrides)
    return pd.DataFrame([row])


class TestToRows:
    """_to_rows renames API columns to staging columns and dedupes the batch."""

    def test_maps_camelcase_payload_to_staging_columns(self):
        columns, rows = _to_rows(_player_box_frame(), PLAYER_BOX_COLUMNS,
                                 key=["game_id", "player_id"])

        assert "player_id" in columns and "personId" not in columns
        # The renames most easily got wrong:
        assert "turnovers" in columns          # not "TO"
        assert "pf" in columns                 # from foulsPersonal
        assert len(rows) == 1

        record = dict(zip(columns, rows[0]))
        assert record["game_id"] == "0022500578"
        assert record["player_id"] == 1630532
        assert record["minutes"] == "26:41"

    def test_percentage_columns_are_not_carried_through(self):
        # They report 0.0 for zero attempts, so an 0-for-0 night would read as
        # "0% shooting". Derived downstream instead.
        columns, _ = _to_rows(_player_box_frame(), PLAYER_BOX_COLUMNS,
                              key=["game_id", "player_id"])
        assert not [c for c in columns if "pct" in c or "percentage" in c.lower()]

    def test_dnp_row_lands_with_null_minutes(self):
        frame = _player_box_frame(minutes="", comment="DNP - Coach's Decision")
        columns, rows = _to_rows(frame, PLAYER_BOX_COLUMNS, key=["game_id", "player_id"])

        record = dict(zip(columns, rows[0]))
        assert record["minutes"] is None
        assert record["comment"] == "DNP - Coach's Decision"

    def test_duplicate_keys_are_collapsed(self):
        # Postgres rejects an ON CONFLICT DO UPDATE whose batch touches the same
        # row twice ("cannot affect row a second time"), and the API really does
        # return duplicate rows for some games.
        frame = pd.concat([
            _player_box_frame(points=10),
            _player_box_frame(points=99),
        ], ignore_index=True)

        columns, rows = _to_rows(frame, PLAYER_BOX_COLUMNS, key=["game_id", "player_id"])

        assert len(rows) == 1, "duplicate primary keys must be collapsed"
        assert dict(zip(columns, rows[0]))["pts"] == 99, "keep=last should win"

    def test_distinct_players_are_not_collapsed(self):
        frame = pd.concat([
            _player_box_frame(personId=1),
            _player_box_frame(personId=2),
        ], ignore_index=True)
        _, rows = _to_rows(frame, PLAYER_BOX_COLUMNS, key=["game_id", "player_id"])
        assert len(rows) == 2

    def test_missing_payload_column_fails_loudly(self):
        # If the API drops or renames a column, we want a named error — not a
        # silent NULL column discovered weeks later in the dashboard.
        frame = _player_box_frame().drop(columns=["points"])

        with pytest.raises(KeyError, match="points"):
            _to_rows(frame, PLAYER_BOX_COLUMNS, key=["game_id", "player_id"])

    def test_games_mapping_covers_the_staging_columns(self):
        frame = pd.DataFrame([{col: 0 for col in GAMES_COLUMNS}])
        columns, rows = _to_rows(frame, GAMES_COLUMNS, key=["game_id", "team_id"])

        assert {"game_id", "team_id", "game_date", "matchup", "wl"} <= set(columns)
        assert len(rows) == 1


# --- retry behaviour ---------------------------------------------------------

class TestCallRetries:
    """_call must survive a flaky endpoint without stalling a backfill."""

    @pytest.fixture(autouse=True)
    def _no_real_sleeping(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(nba_client, "_last_request_at", 0.0)

    def test_returns_immediately_on_success(self):
        assert nba_client._call("probe", lambda: "payload") == "payload"

    def test_retries_then_succeeds(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("stats.nba.com hung up")
            return "payload"

        assert nba_client._call("probe", flaky) == "payload"
        assert len(attempts) == 3

    def test_gives_up_after_max_retries(self, monkeypatch):
        monkeypatch.setattr(nba_client, "MAX_RETRIES", 3)
        attempts = []

        def always_fails():
            attempts.append(1)
            raise TimeoutError("timed out")

        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            nba_client._call("probe", always_fails)

        assert len(attempts) == 3

    def test_original_error_is_preserved_as_the_cause(self):
        # A backfill that fails needs the real reason, not just our wrapper.
        def always_fails():
            raise ConnectionError("connection reset")

        with pytest.raises(RuntimeError) as excinfo:
            nba_client._call("probe", always_fails)

        assert isinstance(excinfo.value.__cause__, ConnectionError)

    def test_malformed_json_is_retried(self):
        # stats.nba.com returns HTML when unhappy, which surfaces as a JSON error.
        import json
        attempts = []

        def bad_json_once():
            attempts.append(1)
            if len(attempts) == 1:
                raise json.JSONDecodeError("Expecting value", "<html>", 0)
            return "payload"

        assert nba_client._call("probe", bad_json_once) == "payload"
        assert len(attempts) == 2


# --- season derivation -------------------------------------------------------

class TestCurrentSeason:
    """The scheduled job derives the season instead of taking it as a parameter.

    A hardcoded "2025-26" works perfectly until October and then silently
    ingests nothing — the exact silent failure the pipeline exists to avoid.
    """

    @pytest.mark.parametrize("today,expected", [
        ("2025-10-21", "2025-26"),   # opening night
        ("2025-12-31", "2025-26"),   # before the calendar roll
        ("2026-01-01", "2025-26"),   # after it — same season
        ("2026-06-13", "2025-26"),   # finals
        ("2026-09-01", "2025-26"),   # offseason: the season that just ended
        ("2026-09-30", "2025-26"),   # last day before the flip
        ("2026-10-01", "2026-27"),   # first day of the new season
    ])
    def test_season_boundaries(self, today, expected):
        assert current_season(dt.date.fromisoformat(today)) == expected

    def test_flips_on_the_first_of_october_not_opening_night(self):
        """October 1st, not the first game. The exact tip-off date moves every
        year, and being a few weeks early costs nothing: the game list for a
        season that has not started yet is simply empty."""
        assert current_season(dt.date(2026, 9, 30)) == "2025-26"
        assert current_season(dt.date(2026, 10, 1)) == "2026-27"

    def test_century_roll_matches_the_sql_convention(self):
        """"2099-00", not "2099-100" — the same convention the NBA uses for
        1999-00, and what 020_load_dimensions.sql derives."""
        assert current_season(dt.date(2099, 11, 1)) == "2099-00"
        assert current_season(dt.date(2100, 1, 1)) == "2099-00"

    def test_defaults_to_today(self):
        assert current_season() == current_season(dt.date.today())
