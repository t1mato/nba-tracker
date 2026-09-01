-- ============================================================================
-- Data quality checks. Returns one row per check; the runner fails the job if
-- any row with severity 'error' has violations > 0.
--
-- These are NOT the same thing as the constraints in 010_star_schema.sql.
-- A constraint makes a bad row impossible to insert. A check finds rows that
-- are individually legal but collectively wrong — the kind of damage an
-- unattended nightly job does quietly.
--
-- Deliberately absent: orphaned foreign keys, and home_team_id = away_team_id.
-- REFERENCES and a CHECK constraint already make both unrepresentable, so a
-- check for them could never fire. A check that cannot fail is noise.
--
-- No percent signs anywhere in this file, comments included: psycopg2 reads
-- them as parameter placeholders.
-- ============================================================================

SELECT * FROM (

-- The strongest check we have. Points are not stored independently of the
-- shot counts — they are the same event counted twice — so this arithmetic
-- must hold exactly. If a V3 column mapping in ingest_games.PLAYER_BOX_COLUMNS
-- ever drifts (say threePointersMade lands in fg3a), this is what catches it,
-- and almost nothing else would.
SELECT 'player_points_reconcile'   AS check_name,
       'error'                     AS severity,
       count(*)                    AS violations,
       'pts <> 2*(fgm-fg3m) + 3*fg3m + ftm' AS detail
FROM fact_player_game_stats
WHERE pts <> 2 * (fgm - fg3m) + 3 * fg3m + ftm

UNION ALL

-- Makes cannot exceed attempts, and a three is also a field goal.
SELECT 'player_impossible_shooting', 'error', count(*),
       'fgm>fga, fg3m>fgm, fg3m>fg3a or ftm>fta'
FROM fact_player_game_stats
WHERE fgm > fga OR fg3m > fgm OR fg3m > fg3a OR ftm > fta

UNION ALL

-- 48 minutes plus four overtimes is 68. Anything past 75 is a parsing bug,
-- not a marathon.
SELECT 'player_minutes_range', 'error', count(*),
       'min outside 0-75 — likely a MM:SS parsing failure'
FROM fact_player_game_stats
WHERE min IS NOT NULL AND (min < 0 OR min > 75)

UNION ALL

-- The DNP filter in 030_load_facts.sql is a WHERE clause, not a constraint.
-- If it is ever dropped, every rolling average silently drifts toward zero
-- and nothing else complains.
SELECT 'dnp_leaked_into_facts', 'error', count(*),
       'fact rows with NULL minutes — the DNP filter stopped working'
FROM fact_player_game_stats
WHERE min IS NULL

UNION ALL

-- A basketball game has two teams. Anything else means one side's row failed
-- to load and every rating for that game is computed against a missing
-- opponent — or simply absent.
SELECT 'team_rows_per_game', 'error', count(*),
       'games without exactly 2 fact_team_game_stats rows'
FROM (
    SELECT g.game_id
    FROM dim_games g
    LEFT JOIN fact_team_game_stats f ON f.game_id = g.game_id
    GROUP BY g.game_id
    HAVING count(f.team_id) <> 2
) missing_sides

UNION ALL

-- You cannot field fewer than five players. This can only fire on real
-- corruption, never on an unusual game.
SELECT 'team_game_below_five_players', 'error', count(*),
       'team-games with fewer than 5 players — truncated box score'
FROM (
    SELECT game_id, team_id
    FROM fact_player_game_stats
    GROUP BY game_id, team_id
    HAVING count(*) < 5
) too_few

UNION ALL

-- Ingestion succeeded but the transform did not pick the game up. Exactly the
-- silent failure an unattended job is prone to.
SELECT 'staging_not_transformed', 'error', count(*),
       'in-scope staging games with no fact_team_game_stats rows'
FROM (
    SELECT DISTINCT s.game_id
    FROM staging.stg_games s
    WHERE left(s.game_id, 3) IN ('002', '004', '005', '006')
      AND NOT EXISTS (
          SELECT 1 FROM fact_team_game_stats f WHERE f.game_id = s.game_id
      )
) untransformed

UNION ALL

-- Derived-metric guards. These catch the possessions denominator going wrong,
-- which would not violate any single-column constraint but would make every
-- rating meaningless. Ranges are deliberately wide — they flag arithmetic that
-- has broken, not teams having an unusual night.
SELECT 'player_ts_pct_range', 'error', count(*),
       'true_shooting_pct outside 0-1.5'
FROM fact_player_game_stats
WHERE true_shooting_pct IS NOT NULL
  AND (true_shooting_pct < 0 OR true_shooting_pct > 1.5)

UNION ALL

SELECT 'team_pace_range', 'error', count(*),
       'pace outside 80-130 possessions per 48 min'
FROM fact_team_game_stats
WHERE pace IS NOT NULL AND (pace < 80 OR pace > 130)

UNION ALL

SELECT 'team_rating_range', 'error', count(*),
       'off_rating or def_rating outside 70-160 per 100 poss'
FROM fact_team_game_stats
WHERE (off_rating IS NOT NULL AND (off_rating < 70 OR off_rating > 160))
   OR (def_rating IS NOT NULL AND (def_rating < 70 OR def_rating > 160))

UNION ALL

-- Both teams in a game are assigned the SAME possessions estimate (the two
-- sides' estimates are averaged in 030_load_facts.sql, because both teams have
-- essentially equal possessions by construction). That decision has a testable
-- consequence: one team's defensive rating must exactly equal its opponent's
-- offensive rating, since both are "points team B scored per 100 possessions"
-- over the same denominator.
--
-- Nothing else checks this. Every rating could drift together and stay inside
-- the range guards above; this catches the denominators diverging.
SELECT 'team_off_def_mirror', 'error', count(*),
       'team-games where def_rating <> the opponent off_rating'
FROM fact_team_game_stats a
JOIN fact_team_game_stats b
  ON b.game_id = a.game_id AND b.team_id = a.opponent_id
WHERE a.def_rating IS NOT NULL AND b.off_rating IS NOT NULL
  AND abs(a.def_rating - b.off_rating) > 0.01

UNION ALL

-- --- warnings: real signals, but not reasons to fail a run ------------------

-- Partial truncation could leave a team with 6 or 7 players — legal, so not an
-- error, but worth a look.
--
-- Scoped to the last 7 days, and that scoping is the point. The invariant
-- checks above scan the whole warehouse because they must be zero forever.
-- This one is a heuristic, and run against all of history it fires on seven
-- real April 2026 team-games where tanking teams rested everyone. A warning
-- that fires on every run trains you to ignore it, which costs more than the
-- check is worth. Scoped to fresh data it stays silent until tonight's load
-- actually looks truncated.
SELECT 'team_game_thin_roster_recent', 'warning', count(*),
       'recently loaded team-games with 5-7 players — possible truncation'
FROM (
    SELECT game_id, team_id
    FROM fact_player_game_stats
    WHERE game_date >= current_date - 7
    GROUP BY game_id, team_id
    HAVING count(*) BETWEEN 5 AND 7
) thin

UNION ALL

-- Staleness means opposite things in September and January, so the severity
-- depends on the month rather than the check being permanently toothless.
--
-- November through May the league is definitely playing, so a warehouse more
-- than three days stale means the pipeline stopped and nobody noticed — an
-- error. June through October covers the finals tail, the offseason and the
-- October ramp-up, where staleness is expected, so it only warns.
--
-- Three days rather than one: the schedule has genuine gaps (All-Star break),
-- and the ingestion job is self-healing, so a single missed night is not news.
SELECT 'warehouse_freshness',
       CASE WHEN extract(month FROM current_date) BETWEEN 11 AND 12
              OR extract(month FROM current_date) BETWEEN 1 AND 5
            THEN 'error' ELSE 'warning' END,
       CASE WHEN max(game_date) < current_date - 3 THEN 1 ELSE 0 END,
       'newest game is ' || (current_date - max(game_date)) || ' days old ('
           || max(game_date) || '); in-season staleness is an error, '
           || 'offseason staleness only warns'
FROM dim_games

) checks
ORDER BY severity, check_name;
