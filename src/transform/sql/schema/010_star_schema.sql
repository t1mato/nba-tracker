-- ============================================================================
-- Star schema. Dimensions describe entities, facts record measurements.
--
-- Lives in `public` so the dashboard needs no schema prefix; `staging` keeps
-- the raw payloads it is built from.
--
-- These tables are rebuilt from staging, never written to directly. If a
-- modelling decision changes, re-run the transforms — no re-fetching.
-- ============================================================================

-- --- dimensions -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dim_teams (
    team_id     BIGINT PRIMARY KEY,
    team_name   TEXT NOT NULL,          -- "Magic"
    team_city   TEXT,                   -- "Orlando"
    full_name   TEXT,                   -- "Orlando Magic"
    abbreviation TEXT,                  -- "ORL"
    conference  TEXT,
    division    TEXT
);

CREATE TABLE IF NOT EXISTS dim_games (
    game_id      TEXT PRIMARY KEY,
    game_date    DATE   NOT NULL,
    season       TEXT   NOT NULL,       -- "2025-26"
    season_type  TEXT   NOT NULL,       -- regular / playoffs / playin / nba_cup_final
    home_team_id BIGINT NOT NULL REFERENCES dim_teams(team_id),
    away_team_id BIGINT NOT NULL REFERENCES dim_teams(team_id),
    CONSTRAINT dim_games_teams_differ CHECK (home_team_id <> away_team_id)
);

CREATE INDEX IF NOT EXISTS idx_dim_games_date ON dim_games (game_date);

CREATE TABLE IF NOT EXISTS dim_players (
    player_id   BIGINT PRIMARY KEY,
    player_name TEXT NOT NULL,
    -- From the team roster, not the box score: the box score's `position` is
    -- the starting-lineup slot for one night. NULL for players who left the
    -- league mid-season and appear on no end-of-season roster.
    position    TEXT,
    -- Most recent team, resolved from their last game — this handles trades,
    -- which a roster snapshot alone cannot.
    team_id     BIGINT REFERENCES dim_teams(team_id)
);


-- --- facts ------------------------------------------------------------------

-- One row per player per game they actually PLAYED. DNPs are excluded here
-- (they stay in staging) so rolling averages aren't dragged toward zero.
CREATE TABLE IF NOT EXISTS fact_player_game_stats (
    game_id   TEXT   NOT NULL REFERENCES dim_games(game_id),
    player_id BIGINT NOT NULL REFERENCES dim_players(player_id),
    team_id   BIGINT NOT NULL REFERENCES dim_teams(team_id),

    -- Parsed from the raw "MM:SS": "26:41" becomes 26.683, not 26.41.
    min       NUMERIC(5,3),

    pts       INTEGER NOT NULL,
    reb       INTEGER NOT NULL,
    oreb      INTEGER NOT NULL,
    dreb      INTEGER NOT NULL,
    ast       INTEGER NOT NULL,
    stl       INTEGER NOT NULL,
    blk       INTEGER NOT NULL,
    fgm       INTEGER NOT NULL,
    fga       INTEGER NOT NULL,
    fg3m      INTEGER NOT NULL,
    fg3a      INTEGER NOT NULL,
    ftm       INTEGER NOT NULL,
    fta       INTEGER NOT NULL,
    turnovers INTEGER NOT NULL,
    pf        INTEGER NOT NULL,
    plus_minus NUMERIC,

    -- Derived. NULL rather than 0 when a player took no shots and no free
    -- throws — "no attempts" is not "0% efficiency".
    true_shooting_pct NUMERIC(5,4),
    efg_pct           NUMERIC(5,4),

    -- Denormalised from dim_games so the dashboard can filter without a join.
    game_date   DATE NOT NULL,
    is_home     BOOLEAN NOT NULL,
    season_type TEXT NOT NULL,

    PRIMARY KEY (game_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_fpgs_player_date ON fact_player_game_stats (player_id, game_date);
CREATE INDEX IF NOT EXISTS idx_fpgs_team_date   ON fact_player_game_stats (team_id, game_date);


-- One row per team per game.
CREATE TABLE IF NOT EXISTS fact_team_game_stats (
    game_id      TEXT   NOT NULL REFERENCES dim_games(game_id),
    team_id      BIGINT NOT NULL REFERENCES dim_teams(team_id),
    opponent_id  BIGINT NOT NULL REFERENCES dim_teams(team_id),

    pts          INTEGER NOT NULL,
    opp_pts      INTEGER NOT NULL,
    won          BOOLEAN NOT NULL,
    is_home      BOOLEAN NOT NULL,

    reb INTEGER, oreb INTEGER, dreb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER,
    fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
    turnovers INTEGER, pf INTEGER,

    -- Possessions, the denominator everything below is built on:
    --   poss = FGA + 0.44*FTA - OREB + TOV,  averaged with the opponent's
    -- (both teams have nearly equal possessions in a game by construction).
    possessions  NUMERIC(6,2),
    pace         NUMERIC(6,2),   -- possessions per 48 minutes
    off_rating   NUMERIC(6,2),   -- points scored per 100 possessions
    def_rating   NUMERIC(6,2),   -- points allowed per 100 possessions
    net_rating   NUMERIC(6,2),

    true_shooting_pct NUMERIC(5,4),
    game_date    DATE NOT NULL,
    season_type  TEXT NOT NULL,

    PRIMARY KEY (game_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_ftgs_team_date ON fact_team_game_stats (team_id, game_date);
