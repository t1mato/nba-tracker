-- ============================================================================
-- Populate the dimensions from staging. Idempotent: re-running refreshes rows
-- in place rather than duplicating them.
-- ============================================================================

-- --- dim_teams --------------------------------------------------------------
-- Names come from the game data (present for every team that played);
-- conference/division from the standings snapshot.
INSERT INTO dim_teams (team_id, team_name, team_city, full_name, abbreviation,
                       conference, division)
SELECT DISTINCT ON (g.team_id)
       g.team_id,
       COALESCE(t.team_name, g.team_name),
       t.team_city,
       g.team_name,                      -- stg_games carries the full name
       g.team_abbreviation,
       t.conference,
       t.division
FROM staging.stg_games g
LEFT JOIN staging.stg_teams t ON t.team_id = g.team_id
ORDER BY g.team_id, g.game_date DESC
ON CONFLICT (team_id) DO UPDATE SET
    team_name    = EXCLUDED.team_name,
    team_city    = EXCLUDED.team_city,
    full_name    = EXCLUDED.full_name,
    abbreviation = EXCLUDED.abbreviation,
    conference   = EXCLUDED.conference,
    division     = EXCLUDED.division;


-- --- dim_games --------------------------------------------------------------
-- Home/away is derived from MATCHUP, parsed STRUCTURALLY rather than relative
-- to the row's own team: some games return the same matchup string on both
-- rows, so "my row says vs. therefore I'm home" is wrong about 1 game in 9.
--
--   "AWAY @ HOME"    -> home is the second token
--   "HOME vs. AWAY"  -> home is the first token
--
-- Both rows of a game yield the same answer, so we group and pick the team_id
-- whose abbreviation matches.
INSERT INTO dim_games (game_id, game_date, season, season_type,
                       home_team_id, away_team_id)
WITH parsed AS (
    SELECT
        game_id,
        team_id,
        team_abbreviation,
        game_date,
        season_id,
        CASE
            WHEN matchup LIKE '% vs. %' THEN split_part(matchup, ' vs. ', 1)
            WHEN matchup LIKE '% @ %'   THEN split_part(matchup, ' @ ', 2)
        END AS home_abbreviation
    FROM staging.stg_games
)
SELECT
    game_id,
    max(game_date),
    -- "22025" -> "2025-26"
    max(substring(season_id FROM 2)::int)::text || '-'
        || right((max(substring(season_id FROM 2)::int) + 1)::text, 2),
    max(CASE left(game_id, 3)
            WHEN '002' THEN 'regular'
            WHEN '004' THEN 'playoffs'
            WHEN '005' THEN 'playin'
            WHEN '006' THEN 'nba_cup_final'
            ELSE 'unknown'
        END),
    max(team_id) FILTER (WHERE team_abbreviation =  home_abbreviation),
    max(team_id) FILTER (WHERE team_abbreviation <> home_abbreviation)
FROM parsed
GROUP BY game_id
-- Guard: drop any game where the parse failed to identify both sides rather
-- than inserting a NULL and failing the NOT NULL constraint opaquely.
HAVING max(team_id) FILTER (WHERE team_abbreviation =  home_abbreviation) IS NOT NULL
   AND max(team_id) FILTER (WHERE team_abbreviation <> home_abbreviation) IS NOT NULL
ON CONFLICT (game_id) DO UPDATE SET
    game_date    = EXCLUDED.game_date,
    season       = EXCLUDED.season,
    season_type  = EXCLUDED.season_type,
    home_team_id = EXCLUDED.home_team_id,
    away_team_id = EXCLUDED.away_team_id;


-- --- dim_players ------------------------------------------------------------
-- Name and current team from the most recent game they appeared in (this is
-- what makes trades resolve correctly); position from the roster snapshot.
INSERT INTO dim_players (player_id, player_name, position, team_id)
WITH latest AS (
    SELECT DISTINCT ON (b.player_id)
           b.player_id,
           trim(coalesce(b.first_name, '') || ' ' || coalesce(b.family_name, '')) AS player_name,
           b.team_id
    FROM staging.stg_player_box b
    JOIN staging.stg_games g ON g.game_id = b.game_id AND g.team_id = b.team_id
    ORDER BY b.player_id, g.game_date DESC, b.game_id DESC
),
positions AS (
    -- A traded player has more than one roster row; any of them carries the
    -- same position, so collapse to one.
    SELECT DISTINCT ON (player_id) player_id, position
    FROM staging.stg_players
    ORDER BY player_id, season DESC
)
SELECT l.player_id, l.player_name, p.position, l.team_id
FROM latest l
LEFT JOIN positions p ON p.player_id = l.player_id
ON CONFLICT (player_id) DO UPDATE SET
    player_name = EXCLUDED.player_name,
    position    = EXCLUDED.position,
    team_id     = EXCLUDED.team_id;
