-- ============================================================================
-- Populate the fact tables. This is where the transform layer earns its keep:
-- parsing minutes, filtering DNPs, and computing efficiency metrics that do not
-- exist anywhere in the source data.
-- ============================================================================

-- --- fact_player_game_stats -------------------------------------------------
INSERT INTO fact_player_game_stats (
    game_id, player_id, team_id, min,
    pts, reb, oreb, dreb, ast, stl, blk,
    fgm, fga, fg3m, fg3a, ftm, fta, turnovers, pf, plus_minus,
    true_shooting_pct, efg_pct, game_date, is_home, season_type
)
SELECT
    b.game_id,
    b.player_id,
    b.team_id,

    -- "26:41" is 26 minutes and 41 seconds = 26.683, NOT 26.41. Reading this
    -- as a decimal would quietly corrupt every per-minute metric.
    split_part(b.minutes, ':', 1)::numeric
        + split_part(b.minutes, ':', 2)::numeric / 60 AS min,

    b.pts, b.reb, b.oreb, b.dreb, b.ast, b.stl, b.blk,
    b.fgm, b.fga, b.fg3m, b.fg3a, b.ftm, b.fta, b.turnovers, b.pf, b.plus_minus,

    -- True shooting %: points per shooting possession, where a shooting
    -- possession is a field goal attempt plus 0.44 of a free throw attempt
    -- (the empirical rate at which FTs end a possession).
    --   TS% = PTS / (2 * (FGA + 0.44 * FTA))
    -- NULLIF guards the zero denominator: a player who attempted nothing has
    -- UNKNOWN efficiency, not 0% — averaging a 0 there would be a real bug.
    CASE WHEN (b.fga + 0.44 * b.fta) > 0
         THEN b.pts / (2 * (b.fga + 0.44 * b.fta))
    END AS true_shooting_pct,

    -- Effective FG%: like FG%, but credits a three as 1.5 makes.
    CASE WHEN b.fga > 0
         THEN (b.fgm + 0.5 * b.fg3m)::numeric / b.fga
    END AS efg_pct,

    g.game_date,
    (g.home_team_id = b.team_id) AS is_home,
    g.season_type

FROM staging.stg_player_box b
JOIN dim_games g ON g.game_id = b.game_id
-- DNPs stay in staging. Including them here would drag every rolling average
-- toward zero and make "games played" meaningless.
WHERE b.minutes IS NOT NULL
ON CONFLICT (game_id, player_id) DO UPDATE SET
    team_id           = EXCLUDED.team_id,
    min               = EXCLUDED.min,
    pts               = EXCLUDED.pts,
    reb               = EXCLUDED.reb,
    oreb              = EXCLUDED.oreb,
    dreb              = EXCLUDED.dreb,
    ast               = EXCLUDED.ast,
    stl               = EXCLUDED.stl,
    blk               = EXCLUDED.blk,
    fgm               = EXCLUDED.fgm,
    fga               = EXCLUDED.fga,
    fg3m              = EXCLUDED.fg3m,
    fg3a              = EXCLUDED.fg3a,
    ftm               = EXCLUDED.ftm,
    fta               = EXCLUDED.fta,
    turnovers         = EXCLUDED.turnovers,
    pf                = EXCLUDED.pf,
    plus_minus        = EXCLUDED.plus_minus,
    true_shooting_pct = EXCLUDED.true_shooting_pct,
    efg_pct           = EXCLUDED.efg_pct,
    game_date         = EXCLUDED.game_date,
    is_home           = EXCLUDED.is_home,
    season_type       = EXCLUDED.season_type;


-- --- fact_team_game_stats ---------------------------------------------------
-- Ratings need the OPPONENT's numbers, so each team row is joined to the other
-- side of the same game.
INSERT INTO fact_team_game_stats (
    game_id, team_id, opponent_id, pts, opp_pts, won, is_home,
    reb, oreb, dreb, ast, stl, blk, fgm, fga, fg3m, fg3a, ftm, fta, turnovers, pf,
    possessions, pace, off_rating, def_rating, net_rating,
    true_shooting_pct, game_date, season_type
)
WITH sides AS (
    -- Pair each team row with its opponent's row for the same game.
    SELECT
        s.game_id, s.team_id, s.wl, s.min,
        s.pts, s.reb, s.oreb, s.dreb, s.ast, s.stl, s.blk,
        s.fgm, s.fga, s.fg3m, s.fg3a, s.ftm, s.fta, s.tov, s.pf,
        o.team_id AS opponent_id,
        o.pts  AS opp_pts,
        o.fga  AS opp_fga,
        o.fta  AS opp_fta,
        o.oreb AS opp_oreb,
        o.tov  AS opp_tov,
        g.game_date, g.season_type, g.home_team_id
    FROM staging.stg_games s
    JOIN staging.stg_games o
      ON o.game_id = s.game_id AND o.team_id <> s.team_id
    JOIN dim_games g ON g.game_id = s.game_id
),
possessions AS (
    SELECT
        sides.*,
        -- Possession estimate, the denominator for every rating below:
        --   poss = FGA + 0.44*FTA - OREB + TOV
        -- Averaged with the opponent's estimate, because both teams have
        -- essentially equal possessions in a game and averaging cancels noise.
        (
          (fga     + 0.44 * fta     - oreb     + tov) +
          (opp_fga + 0.44 * opp_fta - opp_oreb + opp_tov)
        ) / 2.0 AS poss
    FROM sides
)
SELECT
    game_id, team_id, opponent_id, pts, opp_pts,
    (wl = 'W')                AS won,
    (home_team_id = team_id)  AS is_home,
    reb, oreb, dreb, ast, stl, blk, fgm, fga, fg3m, fg3a, ftm, fta, tov, pf,

    round(poss, 2) AS possessions,

    -- Pace: possessions per 48 minutes. Team `min` is 240 in regulation
    -- (5 players x 48), so minutes/5 recovers the game length and overtime
    -- games are scaled correctly rather than looking artificially fast.
    CASE WHEN min > 0 THEN round(48 * poss / (min / 5.0), 2) END AS pace,

    -- Points scored / allowed per 100 possessions.
    CASE WHEN poss > 0 THEN round(100 * pts     / poss, 2) END AS off_rating,
    CASE WHEN poss > 0 THEN round(100 * opp_pts / poss, 2) END AS def_rating,
    CASE WHEN poss > 0 THEN round(100 * (pts - opp_pts) / poss, 2) END AS net_rating,

    CASE WHEN (fga + 0.44 * fta) > 0
         THEN round(pts / (2 * (fga + 0.44 * fta)), 4) END AS true_shooting_pct,

    game_date, season_type
FROM possessions
ON CONFLICT (game_id, team_id) DO UPDATE SET
    opponent_id       = EXCLUDED.opponent_id,
    pts               = EXCLUDED.pts,
    opp_pts           = EXCLUDED.opp_pts,
    won               = EXCLUDED.won,
    is_home           = EXCLUDED.is_home,
    reb = EXCLUDED.reb, oreb = EXCLUDED.oreb, dreb = EXCLUDED.dreb,
    ast = EXCLUDED.ast, stl = EXCLUDED.stl, blk = EXCLUDED.blk,
    fgm = EXCLUDED.fgm, fga = EXCLUDED.fga, fg3m = EXCLUDED.fg3m,
    fg3a = EXCLUDED.fg3a, ftm = EXCLUDED.ftm, fta = EXCLUDED.fta,
    turnovers = EXCLUDED.turnovers, pf = EXCLUDED.pf,
    possessions       = EXCLUDED.possessions,
    pace              = EXCLUDED.pace,
    off_rating        = EXCLUDED.off_rating,
    def_rating        = EXCLUDED.def_rating,
    net_rating        = EXCLUDED.net_rating,
    true_shooting_pct = EXCLUDED.true_shooting_pct,
    game_date         = EXCLUDED.game_date,
    season_type       = EXCLUDED.season_type;
