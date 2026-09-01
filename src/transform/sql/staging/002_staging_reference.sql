-- ============================================================================
-- Reference staging tables — slow-changing attributes the box scores don't have.
--
-- Box scores describe *events*. These two describe *entities*, and are refreshed
-- occasionally rather than nightly.
-- ============================================================================

-- Conference and division for dim_teams. One leaguestandingsv3 call covers all 30.
CREATE TABLE IF NOT EXISTS staging.stg_teams (
    team_id     BIGINT      NOT NULL,
    season      TEXT        NOT NULL,     -- "2025-26"; divisions can be realigned
    team_city   TEXT,
    team_name   TEXT,
    conference  TEXT,
    division    TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, season)
);

-- Real player positions for dim_players. The box score's `position` is the
-- starting-lineup slot for that night, not a player attribute — it is blank for
-- anyone who came off the bench.
--
-- Keyed by team as well as player: a traded player legitimately appears on two
-- rosters, and dropping one would lose information. dim_players resolves a
-- player's current team from their most recent game instead.
CREATE TABLE IF NOT EXISTS staging.stg_players (
    player_id   BIGINT      NOT NULL,
    team_id     BIGINT      NOT NULL,
    season      TEXT        NOT NULL,
    player_name TEXT,
    position    TEXT,                     -- 'G', 'F', 'C', or hybrids: 'G-F', 'F-C'
    jersey_num  TEXT,
    height      TEXT,
    weight      TEXT,
    age         NUMERIC,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, team_id, season)
);
