"""Database access for the dashboard.

Every query lives here so the page modules stay about layout. Results are cached
by Streamlit: the connection with cache_resource (one per session), query results
with cache_data (keyed on arguments).

Rolling averages are computed in SQL window functions rather than materialised —
the star schema is small enough that this is instant, and it keeps the transform
layer from needing a rebuild every time we change a window size.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import psycopg2
import streamlit as st

from nba_tracker.ingestion.config import get_database_url

ROLLING_WINDOW = 10


@st.cache_resource
def _connection():
    """One long-lived connection per session."""
    return psycopg2.connect(get_database_url())


# psycopg2 raises these two when the socket is gone rather than when the SQL is
# wrong: OperationalError for a connection dropped mid-query, InterfaceError for
# one already known to be closed.
_CONNECTION_LOST = (psycopg2.OperationalError, psycopg2.InterfaceError)


def _execute(conn, sql: str, params: tuple):
    """One attempt. Returns (columns, rows) or raises."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [c.name for c in cur.description]
        rows = cur.fetchall()
    conn.commit()
    return columns, rows


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run SQL and return a DataFrame.

    Built from cursor.description rather than pandas.read_sql, which warns
    about non-SQLAlchemy connections.

    Retries once on a lost connection. This is not defensive padding — it is
    the normal case in deployment: Neon's free tier suspends compute after a
    few minutes idle, and a Community Cloud app is idle almost always, so the
    cached connection is usually dead when the next visitor arrives. Without
    the retry the app serves errors until it is restarted by hand, because
    cache_resource keeps returning the same dead object.
    """
    conn = _connection()
    try:
        columns, rows = _execute(conn, sql, params)
    except _CONNECTION_LOST:
        # Deliberately no rollback: on a dead connection it raises too, which
        # is what masked the real error before. Drop the cached connection so
        # the next call opens a fresh one, then retry exactly once. A second
        # failure is a real outage and should surface.
        _connection.clear()
        conn = _connection()
        columns, rows = _execute(conn, sql, params)
    except psycopg2.Error:
        # A failed query aborts the transaction; reset so the next one works.
        conn.rollback()
        raise

    df = pd.DataFrame(rows, columns=columns)

    # psycopg2 hands back two Python types that survive into object columns and
    # then break the browser: Decimal (from NUMERIC) and date (from DATE).
    # Neither is JSON-serializable, so Altair builds a correct-looking spec whose
    # data silently fails to reach Vega — the charts render empty rather than
    # raising. Normalise both here, once, so no caller has to remember.
    for column in df.columns:
        if df[column].dtype != object:
            continue
        sample = df[column].dropna()
        if sample.empty:
            continue
        first = sample.iloc[0]
        if isinstance(first, Decimal):
            df[column] = df[column].astype(float)
        elif isinstance(first, (dt.date, dt.datetime)):
            df[column] = pd.to_datetime(df[column])

    return df


# --- shared lookups ----------------------------------------------------------

@st.cache_data(ttl=600)
def get_teams() -> pd.DataFrame:
    return _query("""
        SELECT team_id, abbreviation, full_name, conference, division
        FROM dim_teams ORDER BY full_name
    """)


@st.cache_data(ttl=600)
def get_seasons() -> list[str]:
    df = _query("SELECT DISTINCT season FROM dim_games ORDER BY season DESC")
    return df["season"].tolist()


@st.cache_data(ttl=600)
def get_players(min_games: int = 20) -> pd.DataFrame:
    """Players with enough games to be worth charting."""
    return _query("""
        SELECT p.player_id, p.player_name, p.position,
               t.abbreviation AS team, count(*) AS games,
               avg(f.pts) AS ppg
        FROM fact_player_game_stats f
        JOIN dim_players p USING (player_id)
        LEFT JOIN dim_teams t ON t.team_id = p.team_id
        GROUP BY p.player_id, p.player_name, p.position, t.abbreviation
        HAVING count(*) >= %s
        ORDER BY p.player_name
    """, (min_games,))


# --- team trends -------------------------------------------------------------

@st.cache_data(ttl=600)
def get_team_summary(team_id: int, season_type: str = "regular") -> pd.Series:
    df = _query("""
        SELECT count(*)                                   AS games,
               count(*) FILTER (WHERE won)                AS wins,
               count(*) FILTER (WHERE NOT won)            AS losses,
               avg(pts)                                   AS ppg,
               avg(opp_pts)                               AS opp_ppg,
               avg(off_rating)                            AS off_rating,
               avg(def_rating)                            AS def_rating,
               avg(net_rating)                            AS net_rating,
               avg(pace)                                  AS pace,
               avg(true_shooting_pct)                     AS ts_pct
        FROM fact_team_game_stats
        WHERE team_id = %s AND season_type = %s
    """, (team_id, season_type))
    return df.iloc[0]


@st.cache_data(ttl=600)
def get_team_game_log(team_id: int, season_type: str = "regular") -> pd.DataFrame:
    """Game-by-game with rolling averages and a running win percentage."""
    return _query("""
        SELECT
            f.game_date,
            o.abbreviation                              AS opponent,
            f.is_home,
            f.won,
            f.pts, f.opp_pts,
            f.off_rating, f.def_rating, f.net_rating, f.pace,
            f.true_shooting_pct,
            row_number() OVER w                         AS game_no,
            -- Suppress the rolling value until the window is full. Otherwise
            -- the first nine points are 1-, 2-, 3-game averages masquerading as
            -- a 10-game average, and the line opens on a misleading spike.
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.net_rating) OVER roll END   AS roll_net,
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.off_rating) OVER roll END   AS roll_off,
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.def_rating) OVER roll END   AS roll_def,
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.pace)       OVER roll END   AS roll_pace,
            -- Running win rate across the season to date.
            -- Keep percent signs out of this string entirely: psycopg2 treats
            -- them as parameter placeholders, even inside a SQL comment.
            100.0 * sum(CASE WHEN f.won THEN 1 ELSE 0 END) OVER w
                  / row_number() OVER w                 AS win_pct
        FROM fact_team_game_stats f
        JOIN dim_teams o ON o.team_id = f.opponent_id
        WHERE f.team_id = %s AND f.season_type = %s
        WINDOW w    AS (ORDER BY f.game_date, f.game_id),
               roll AS (ORDER BY f.game_date, f.game_id
                        ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
        ORDER BY f.game_date
    """, (team_id, season_type))


@st.cache_data(ttl=600)
def get_team_home_away(team_id: int, season_type: str = "regular") -> pd.DataFrame:
    return _query("""
        SELECT CASE WHEN is_home THEN 'Home' ELSE 'Away' END AS venue,
               count(*) AS games,
               count(*) FILTER (WHERE won) AS wins,
               100.0 * count(*) FILTER (WHERE won) / count(*) AS win_pct,
               avg(pts) AS ppg, avg(off_rating) AS off_rating,
               avg(def_rating) AS def_rating, avg(net_rating) AS net_rating
        FROM fact_team_game_stats
        WHERE team_id = %s AND season_type = %s
        GROUP BY is_home ORDER BY is_home DESC
    """, (team_id, season_type))


# --- player deep dive --------------------------------------------------------

@st.cache_data(ttl=600)
def get_player_summary(player_id: int, season_type: str = "regular") -> pd.Series:
    df = _query("""
        SELECT count(*) AS games, avg(min) AS mpg, avg(pts) AS ppg,
               avg(reb) AS rpg, avg(ast) AS apg, avg(stl) AS spg, avg(blk) AS bpg,
               avg(turnovers) AS topg,
               avg(true_shooting_pct) AS ts_pct, avg(efg_pct) AS efg_pct,
               sum(fgm) AS fgm, sum(fga) AS fga,
               sum(fg3m) AS fg3m, sum(fg3a) AS fg3a
        FROM fact_player_game_stats
        WHERE player_id = %s AND season_type = %s
    """, (player_id, season_type))
    return df.iloc[0]


@st.cache_data(ttl=600)
def get_player_game_log(player_id: int, season_type: str = "regular") -> pd.DataFrame:
    """Game-by-game with rolling averages.

    TS% is NULL for a game with no shot attempts, so the rolling average uses
    only the games where it is defined rather than treating "no attempts" as 0%.
    """
    return _query("""
        SELECT
            f.game_date,
            o.abbreviation AS opponent,
            f.is_home, f.min, f.pts, f.reb, f.ast, f.stl, f.blk,
            f.fgm, f.fga, f.fg3m, f.fg3a, f.ftm, f.fta,
            f.turnovers, f.plus_minus,
            f.true_shooting_pct, f.efg_pct,
            row_number() OVER w AS game_no,
            -- Same guard as the team log: a 10-game average needs 10 games.
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.pts)               OVER roll END AS roll_pts,
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.min)               OVER roll END AS roll_min,
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.reb)               OVER roll END AS roll_reb,
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.ast)               OVER roll END AS roll_ast,
            CASE WHEN row_number() OVER w >= 10
                 THEN avg(f.true_shooting_pct) OVER roll END AS roll_ts
        FROM fact_player_game_stats f
        JOIN dim_games g   ON g.game_id = f.game_id
        JOIN dim_teams  o  ON o.team_id = CASE WHEN f.is_home
                                               THEN g.away_team_id ELSE g.home_team_id END
        WHERE f.player_id = %s AND f.season_type = %s
        WINDOW w    AS (ORDER BY f.game_date, f.game_id),
               roll AS (ORDER BY f.game_date, f.game_id
                        ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
        ORDER BY f.game_date
    """, (player_id, season_type))


@st.cache_data(ttl=600)
def get_player_home_away(player_id: int, season_type: str = "regular") -> pd.DataFrame:
    return _query("""
        SELECT CASE WHEN is_home THEN 'Home' ELSE 'Away' END AS venue,
               count(*) AS games, avg(pts) AS ppg, avg(reb) AS rpg,
               avg(ast) AS apg, avg(true_shooting_pct) AS ts_pct
        FROM fact_player_game_stats
        WHERE player_id = %s AND season_type = %s
        GROUP BY is_home ORDER BY is_home DESC
    """, (player_id, season_type))
