"""Tests for the SQL transform layer.

Why these exist, given the data quality checks already run
----------------------------------------------------------
The checks in 050_data_quality.sql catch *corruption* — a row that contradicts
another row. They cannot catch a *formula that is consistently wrong*, because
consistent wrongness satisfies every invariant. If `"26:41"` were parsed as
26.41 minutes, every check would still pass: points would reconcile, makes would
not exceed attempts, ratings would sit in range. Only a test that knows the
right answer catches it.

So these pin the four decisions from CLAUDE.md that fail silently rather than
loudly — the ones where the pipeline keeps running and quietly reports numbers
that are wrong.

How they run
------------
Against a real, disposable Postgres database, applying the *unmodified* SQL
files. Rewriting the SQL to run somewhere else would test a rewrite rather than
the thing that ships. The database is created fresh, loaded with a dozen
hand-built staging rows, and dropped afterwards; your real warehouse is never
touched.

Skipped automatically when no database is reachable, so the suite still runs on
a machine with nothing set up.
"""

from __future__ import annotations

import urllib.parse

import psycopg2
import pytest

from src.ingestion.config import get_database_url
from src.transform.run_transforms import sql_files

TEST_DB = "nba_tracker_test"


# --- fixtures ----------------------------------------------------------------

def _test_db_url(base: str) -> str:
    parts = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{TEST_DB}"))


@pytest.fixture(scope="session")
def warehouse():
    """A fresh, empty warehouse built by the real DDL. Dropped afterwards."""
    try:
        base = get_database_url()
    except RuntimeError as exc:
        pytest.skip(f"no DATABASE_URL: {exc}")

    try:
        admin = psycopg2.connect(base)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"database unreachable: {exc}")

    admin.autocommit = True   # CREATE/DROP DATABASE cannot run in a transaction
    with admin.cursor() as cur:
        try:
            cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            cur.execute(f"CREATE DATABASE {TEST_DB}")
        except psycopg2.Error as exc:
            admin.close()
            pytest.skip(f"cannot create a test database: {exc}")

    conn = psycopg2.connect(_test_db_url(base))
    with conn.cursor() as cur:
        for path in sql_files(("staging", "schema")):
            cur.execute(path.read_text())
    conn.commit()

    yield conn

    conn.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    admin.close()


@pytest.fixture
def load(warehouse):
    """Empty the warehouse, insert staging rows, run the real transforms.

    Returns a callable so each test states only the rows it cares about.
    """
    def _load(games=(), players=(), teams=(), rosters=()):
        with warehouse.cursor() as cur:
            cur.execute("""
                TRUNCATE fact_player_game_stats, fact_team_game_stats,
                         dim_games, dim_players, dim_teams,
                         staging.stg_games, staging.stg_player_box,
                         staging.stg_teams, staging.stg_players
            """)
            for table, rows in (("staging.stg_games", games),
                                ("staging.stg_player_box", players),
                                ("staging.stg_teams", teams),
                                ("staging.stg_players", rosters)):
                for row in rows:
                    cols = ", ".join(row)
                    marks = ", ".join(["%s"] * len(row))
                    cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                                tuple(row.values()))

            for path in sql_files(("transforms",)):
                cur.execute(path.read_text())
        warehouse.commit()
        return warehouse

    return _load


def _query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# --- fixture row builders ----------------------------------------------------
# Full rows with harmless defaults, so a test overrides only what it is about.

def game_row(**overrides) -> dict:
    row = dict(
        game_id="0022500001", team_id=1610612744, season_id="22025",
        game_date="2026-01-15", team_abbreviation="GSW", team_name="Golden State Warriors",
        matchup="GSW vs. LAL", wl="W", min=240, pts=110,
        fgm=40, fga=80, fg3m=15, fg3a=40, ftm=15, fta=20,
        oreb=10, dreb=35, reb=45, ast=25, stl=8, blk=5, tov=15, pf=18,
        plus_minus=5,
    )
    row.update(overrides)
    return row


def player_row(**overrides) -> dict:
    row = dict(
        game_id="0022500001", player_id=201939, team_id=1610612744,
        team_tricode="GSW", first_name="Stephen", family_name="Curry",
        position="G", comment=None, minutes="26:41",
        fgm=0, fga=0, fg3m=0, fg3a=0, ftm=0, fta=0,
        oreb=0, dreb=0, reb=0, ast=0, stl=0, blk=0, turnovers=0, pf=0,
        pts=0, plus_minus=0,
    )
    row.update(overrides)
    return row


def both_sides(**overrides) -> tuple[dict, dict]:
    """The two stg_games rows for one game. Ratings need the opponent's numbers,
    so a test that touches team facts must insert both."""
    home = game_row(**overrides)
    away = game_row(
        team_id=1610612747, team_abbreviation="LAL", team_name="Los Angeles Lakers",
        matchup="LAL @ GSW", wl="L", pts=105,
        fgm=38, fga=85, fg3m=12, fg3a=35, ftm=17, fta=15,
        oreb=12, dreb=33, reb=45, ast=22, stl=7, blk=4, tov=13, pf=20,
        plus_minus=-5,
        **{k: v for k, v in overrides.items()
           if k in {"game_id", "season_id", "game_date", "min"}},
    )
    return home, away


# --- the tests ---------------------------------------------------------------

class TestMinutesParsing:
    """`minutes` is "MM:SS". Read as a decimal, every rate metric is wrong.

    This is the highest-value test in the file: 26.41 is a perfectly plausible
    number, so nothing downstream would ever complain.
    """

    def test_mmss_is_parsed_as_minutes_and_seconds(self, load):
        conn = load(games=both_sides(), players=[player_row(minutes="26:41")])
        [(minutes,)] = _query(conn, "SELECT min FROM fact_player_game_stats")

        assert round(float(minutes), 3) == 26.683
        assert float(minutes) != 26.41, "read MM:SS as a decimal"

    @pytest.mark.parametrize("raw,expected", [
        ("00:00", 0.0),
        ("12:30", 12.5),
        ("26:41", 26.683),
        ("36:00", 36.0),
        ("47:59", 47.983),
    ])
    def test_across_the_range(self, load, raw, expected):
        conn = load(games=both_sides(), players=[player_row(minutes=raw)])
        [(minutes,)] = _query(conn, "SELECT min FROM fact_player_game_stats")
        assert round(float(minutes), 3) == expected


class TestDnpFiltering:
    """DNPs land in staging and must not reach the facts.

    Including them would drag every rolling average toward zero and make
    "games played" meaningless — while looking entirely healthy.
    """

    def test_dnp_row_is_excluded(self, load):
        conn = load(
            games=both_sides(),
            players=[
                player_row(player_id=201939, minutes="30:00", pts=20, fgm=8, fga=15),
                player_row(player_id=999999, minutes=None,
                           comment="DNP - Coach's Decision"),
            ],
        )
        ids = [r[0] for r in _query(conn, "SELECT player_id FROM fact_player_game_stats")]

        assert ids == [201939]

    def test_dnp_still_lands_in_staging(self, load):
        """Staging keeps the raw truth; only the fact table filters."""
        conn = load(
            games=both_sides(),
            players=[player_row(player_id=999999, minutes=None, comment="DNP")],
        )
        [(staged,)] = _query(conn, "SELECT count(*) FROM staging.stg_player_box")
        [(facts,)] = _query(conn, "SELECT count(*) FROM fact_player_game_stats")

        assert (staged, facts) == (1, 0)


class TestTrueShootingPct:
    """TS% must be NULL, never 0, when a player attempted nothing.

    "No attempts" is not "0% efficiency". A zero here would be averaged into
    every rolling TS% and silently drag it down.
    """

    def test_null_when_no_attempts(self, load):
        conn = load(games=both_sides(),
                    players=[player_row(minutes="5:00", fga=0, fta=0, pts=0)])
        [(ts,)] = _query(conn, "SELECT true_shooting_pct FROM fact_player_game_stats")

        assert ts is None

    def test_formula(self, load):
        # TS% = PTS / (2 * (FGA + 0.44 * FTA))
        #     = 25 / (2 * (18 + 2.64)) = 25 / 41.28 = 0.6056
        conn = load(games=both_sides(),
                    players=[player_row(minutes="35:00", pts=25, fga=18, fta=6,
                                        fgm=9, ftm=5, fg3m=2, fg3a=6)])
        [(ts,)] = _query(conn, "SELECT true_shooting_pct FROM fact_player_game_stats")

        assert round(float(ts), 4) == 0.6056

    def test_efg_credits_a_three_as_one_and_a_half(self, load):
        # eFG% = (FGM + 0.5 * FG3M) / FGA = (9 + 1) / 18 = 0.5556
        conn = load(games=both_sides(),
                    players=[player_row(minutes="35:00", fgm=9, fga=18, fg3m=2)])
        [(efg,)] = _query(conn, "SELECT efg_pct FROM fact_player_game_stats")

        assert round(float(efg), 4) == 0.5556


class TestPace:
    """Pace is per 48 minutes, so an overtime game must be scaled by its real
    length. Team `min` is 240 in regulation (5 players x 48)."""

    def test_regulation(self, load):
        # poss = ((80 + 0.44*20 - 10 + 15) + (85 + 0.44*15 - 12 + 13)) / 2
        #      = (93.8 + 92.6) / 2 = 93.2
        # pace = 48 * 93.2 / (240/5) = 93.2
        conn = load(games=both_sides(min=240))
        [(pace,)] = _query(conn,
            "SELECT pace FROM fact_team_game_stats WHERE team_id = 1610612744")

        assert round(float(pace), 2) == 93.20

    def test_overtime_is_not_treated_as_regulation(self, load):
        # One OT: team min 265 -> 53 minutes played.
        # pace = 48 * 93.2 / (265/5) = 4473.6 / 53 = 84.41
        conn = load(games=both_sides(min=265))
        [(pace,)] = _query(conn,
            "SELECT pace FROM fact_team_game_stats WHERE team_id = 1610612744")

        assert round(float(pace), 2) == 84.41
        assert round(float(pace), 2) != 93.20, "OT game scaled as regulation"

    def test_ratings_are_per_100_possessions(self, load):
        # off = 100 * 110 / 93.2 = 118.03 ; def = 100 * 105 / 93.2 = 112.66
        conn = load(games=both_sides())
        [(off, dfn, net)] = _query(conn,
            "SELECT off_rating, def_rating, net_rating FROM fact_team_game_stats "
            "WHERE team_id = 1610612744")

        assert round(float(off), 2) == 118.03
        assert round(float(dfn), 2) == 112.66
        assert round(float(net), 2) == 5.36


class TestMatchupParsing:
    """Home/away comes from MATCHUP parsed structurally, not row-relative.

    Some games return the SAME matchup string on both team rows. "My row says
    'vs.' so I am home" misclassifies about one game in nine — and produces a
    warehouse that looks completely normal.
    """

    def test_away_at_home_form(self, load):
        home, away = both_sides()
        home["matchup"], away["matchup"] = "GSW vs. LAL", "LAL @ GSW"
        conn = load(games=[home, away])
        [(h, a)] = _query(conn, "SELECT home_team_id, away_team_id FROM dim_games")

        assert (h, a) == (1610612744, 1610612747)

    def test_identical_matchup_string_on_both_rows(self, load):
        """The bug CLAUDE.md warns about: both rows say "GSW vs. LAL".

        Row-relative parsing would call BOTH teams home. Structural parsing
        reads the string itself, so both rows agree GSW is home.
        """
        home, away = both_sides()
        home["matchup"] = away["matchup"] = "GSW vs. LAL"
        conn = load(games=[home, away])
        [(h, a)] = _query(conn, "SELECT home_team_id, away_team_id FROM dim_games")

        assert (h, a) == (1610612744, 1610612747)

    def test_is_home_flag_follows_dim_games(self, load):
        home, away = both_sides()
        conn = load(games=[home, away],
                    players=[player_row(team_id=1610612744, minutes="30:00"),
                             player_row(player_id=2544, team_id=1610612747,
                                        team_tricode="LAL", minutes="30:00")])
        rows = dict(_query(conn,
            "SELECT player_id, is_home FROM fact_player_game_stats"))

        assert rows == {201939: True, 2544: False}


class TestIdempotency:
    """Re-running the transforms is the normal way to apply a modelling change,
    so it must refresh rows in place rather than duplicate them."""

    def test_second_run_changes_nothing(self, load, warehouse):
        conn = load(games=both_sides(), players=[player_row(minutes="30:00")])
        before = _query(conn, """
            SELECT (SELECT count(*) FROM fact_player_game_stats),
                   (SELECT count(*) FROM fact_team_game_stats),
                   (SELECT count(*) FROM dim_games),
                   (SELECT count(*) FROM dim_teams)
        """)

        with warehouse.cursor() as cur:
            for path in sql_files(("transforms",)):
                cur.execute(path.read_text())
        warehouse.commit()

        assert _query(conn, """
            SELECT (SELECT count(*) FROM fact_player_game_stats),
                   (SELECT count(*) FROM fact_team_game_stats),
                   (SELECT count(*) FROM dim_games),
                   (SELECT count(*) FROM dim_teams)
        """) == before


class TestSeasonDerivation:
    """season_id "22025" becomes "2025-26"."""

    @pytest.mark.parametrize("season_id,expected", [
        ("22025", "2025-26"),
        ("22024", "2024-25"),
        # The century roll. "2099-00", not "2099-100" — this is the NBA's own
        # convention, the same one that writes the 1999-2000 season as
        # "1999-00", and the format nba_api uses for its season parameter.
        ("22099", "2099-00"),
    ])
    def test_season_label(self, load, season_id, expected):
        conn = load(games=both_sides(season_id=season_id))
        [(season,)] = _query(conn, "SELECT season FROM dim_games")
        assert season == expected


class TestSeasonType:
    """The game_id prefix decides season_type, which every dashboard filter uses."""

    @pytest.mark.parametrize("game_id,expected", [
        ("0022500001", "regular"),
        ("0042500101", "playoffs"),
        ("0052500201", "playin"),
        ("0062500001", "nba_cup_final"),
    ])
    def test_prefix_maps_to_season_type(self, load, game_id, expected):
        conn = load(games=both_sides(game_id=game_id))
        [(season_type,)] = _query(conn, "SELECT season_type FROM dim_games")
        assert season_type == expected
