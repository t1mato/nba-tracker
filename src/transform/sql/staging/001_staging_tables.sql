-- ============================================================================
-- Staging layer — raw landing zone for nba_api payloads.
--
-- Staging stays faithful to what the API returned: no derived metrics, no
-- cleaning, no joins. The star schema is built from these tables by the
-- transform layer, so a modelling mistake is fixed by re-running SQL rather
-- than re-fetching 1300+ games from a rate-limited endpoint.
--
-- Both tables use a natural-key primary key so ingestion can upsert
-- (ON CONFLICT ... DO UPDATE). Re-running a date is a no-op, never a duplicate.
--
-- Apply with:
--   psql "$DATABASE_URL" -f src/transform/sql/staging/001_staging_tables.sql
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS staging;


-- ----------------------------------------------------------------------------
-- stg_games — from leaguegamefinder. Two rows per game, one per team.
--
-- The "what happened, and when" table. It is the only source of game_date,
-- home/away and win/loss, and it is how we discover which games to fetch.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.stg_games (
    -- TEXT, never an integer: the leading zeros in "0022500578" encode the
    -- game type and an integer cast destroys them.
    game_id           TEXT        NOT NULL,
    team_id           BIGINT      NOT NULL,

    season_id         TEXT        NOT NULL,   -- "22025" = 2025-26 regular season
    game_date         DATE        NOT NULL,

    team_abbreviation TEXT        NOT NULL,
    team_name         TEXT        NOT NULL,

    -- "AWAY @ HOME" or "HOME vs. AWAY". Parse structurally, NOT relative to
    -- this row's team — some games return the same string on both rows.
    matchup           TEXT        NOT NULL,
    wl                TEXT,

    min               INTEGER,                -- team minutes here is an int (240)
    pts               INTEGER,
    fgm               INTEGER,
    fga               INTEGER,
    fg3m              INTEGER,
    fg3a              INTEGER,
    ftm               INTEGER,
    fta               INTEGER,
    oreb              INTEGER,
    dreb              INTEGER,
    reb               INTEGER,
    ast               INTEGER,
    stl               INTEGER,
    blk               INTEGER,
    tov               INTEGER,
    pf                INTEGER,
    plus_minus        NUMERIC,

    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (game_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_stg_games_date ON staging.stg_games (game_date);
CREATE INDEX IF NOT EXISTS idx_stg_games_team ON staging.stg_games (team_id, game_date);


-- ----------------------------------------------------------------------------
-- stg_player_box — from boxscoretraditionalv3, one call per game.
--
-- The "who did what" table, ~26 rows per game. Joins to stg_games on game_id;
-- that join is what makes rolling averages and home/away splits possible, since
-- this payload carries no date, opponent or result of its own.
--
-- NB: boxscoretraditionalv2 is deprecated and returns ZERO ROWS with its columns
-- intact — it fails silently rather than raising. V3 is the only working source.
--
-- DNP rows are included: blank minutes, all counting stats hard 0, and a
-- populated comment. They are a true fact about the game; the transform filters
-- them out of the fact table so they don't drag rolling averages toward zero.
--
-- The API's *_PERCENTAGE columns are deliberately not stored: they return 0.0
-- when attempts are 0, so an 0-for-0 night reads as "0% shooting". They are
-- derivable from made/attempted, so downstream recomputes them with a zero guard.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.stg_player_box (
    game_id       TEXT        NOT NULL,
    player_id     BIGINT      NOT NULL,
    team_id       BIGINT      NOT NULL,

    team_tricode  TEXT        NOT NULL,
    first_name    TEXT,
    family_name   TEXT,

    -- Only populated for STARTERS — blank for the other ~16 of 26 rows, so
    -- dim_players.position cannot be sourced from here alone.
    position      TEXT,
    -- Empty for players who appeared; a reason string for DNP/DND.
    comment       TEXT,

    -- Raw "MM:SS" as delivered; NULL for a DNP. "26:41" is 26.68 minutes, not
    -- 26.41 — the transform layer parses it.
    minutes       TEXT,

    fgm           INTEGER     NOT NULL,
    fga           INTEGER     NOT NULL,
    fg3m          INTEGER     NOT NULL,
    fg3a          INTEGER     NOT NULL,
    ftm           INTEGER     NOT NULL,
    fta           INTEGER     NOT NULL,
    oreb          INTEGER     NOT NULL,
    dreb          INTEGER     NOT NULL,
    reb           INTEGER     NOT NULL,
    ast           INTEGER     NOT NULL,
    stl           INTEGER     NOT NULL,
    blk           INTEGER     NOT NULL,
    turnovers     INTEGER     NOT NULL,
    pf            INTEGER     NOT NULL,
    pts           INTEGER     NOT NULL,
    plus_minus    NUMERIC,

    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The idempotency key called for in CLAUDE.md.
    PRIMARY KEY (game_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_stg_player_box_player ON staging.stg_player_box (player_id);
CREATE INDEX IF NOT EXISTS idx_stg_player_box_team   ON staging.stg_player_box (team_id);
