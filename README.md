# 🏀 NBA Analytics Data Warehouse

[![CI](https://img.shields.io/github/actions/workflow/status/t1mato/nba-tracker/ci.yml?branch=main&label=CI&logo=githubactions&logoColor=white)](https://github.com/t1mato/nba-tracker/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-72_passing-brightgreen?logo=pytest&logoColor=white)](tests/)
[![Python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)](requirements.txt)
[![Postgres](https://img.shields.io/badge/postgres-18_(Neon)-4169E1?logo=postgresql&logoColor=white)](https://neon.tech)
[![Streamlit](https://img.shields.io/badge/streamlit-1.62-FF4B4B?logo=streamlit&logoColor=white)](src/dashboard/)

[![Games](https://img.shields.io/badge/games-1%2C322-orange)](#data-sources)
[![Player rows](https://img.shields.io/badge/player--game_rows-28%2C716-orange)](#data-sources)
[![Season](https://img.shields.io/badge/season-2025--26-orange)](#data-sources)
[![Quality checks](https://img.shields.io/badge/quality_checks-13-blueviolet)](src/transform/sql/checks/050_data_quality.sql)

An end-to-end pipeline that ingests NBA box scores, models them into a dimensional
warehouse, and serves the result through a Streamlit dashboard.

---

## ⚠️ Learning Project Disclosure

This is a **personal learning project**, built to practice data engineering — not a
production service or a maintained library.

- It uses `nba_api`, an **unofficial** wrapper around undocumented `stats.nba.com`
  endpoints. There is no API contract; endpoints can change or disappear without notice.
- It is **not affiliated with, endorsed by, or associated with the NBA** or any of its
  teams. All data belongs to its respective owners.
- Ingestion runs on a personal laptop on a daily schedule, so the hosted data may lag.
- Please don't depend on it for anything that matters. Read it, fork it, learn from it.

---

## Introduction

A scoreboard tells you Boston scored 118 last night. It won't tell you whether their
offense is trending up over ten games, how their true shooting compares home versus away,
or what their net rating looks like against playoff teams.

That gap is the point of this project. Moving rows from an API into a database is the
easy half; the interesting work is the transform layer, which turns raw box scores into
metrics that don't exist in the source data — true shooting %, pace, offensive and
defensive ratings, and rolling averages.

The warehouse currently holds the complete **2025-26 season**: 1,322 games from
2025-10-21 through 2026-06-13, covering the regular season, play-in, playoffs, and the
NBA Cup final.

---

## Features

**Ingestion**
- Idempotent by design — every write upserts on `(game_id, player_id)`, so re-running any
  date is always safe.
- Self-healing catch-up: `--catch-up` derives the current season and fetches only missing
  games, so a missed night is collected by the next run instead of becoming a permanent hole.
- Runs for a single date, a full season backfill, or catch-up — all through the same code
  path, not a separate backfill script.
- Rate-limit aware: request delays plus retry-with-backoff against an unofficial API.

**Transform**
- Proper star schema — 3 dimensions, 2 fact tables.
- Derived metrics computed in SQL: true shooting %, effective FG%, pace, offensive /
  defensive / net rating, and rolling 10-game averages.
- Season-type tagging keeps preseason, All-Star, and the NBA Cup final out of
  regular-season aggregates.

**Data quality**
- **13 checks** that fail the job loudly rather than letting a bad load pass quietly.
- These are distinct from schema constraints: a constraint makes a bad row impossible to
  insert, while a check finds rows that are individually legal but collectively wrong.
- The strongest is `player_points_reconcile` — points and shot counts are the same events
  counted twice, so the arithmetic must hold exactly. If a column mapping ever drifts
  (say `threePointersMade` lands in `fg3a`), almost nothing else would catch it.

**Dashboard**
- Team trends and player deep-dive views, with query results cached at `ttl=600`.

---

## Architecture

The pipeline is split across two machines, and **the split is forced, not chosen.**

`stats.nba.com` black-holes datacenter IPs. This was verified as a `ReadTimeout` from both
GitHub Actions (Azure) and Oracle Cloud, while the identical call from a laptop returns
HTTP 200 in 0.3s. So ingestion cannot run on a hosted runner. Everything downstream only
needs Postgres, so it can.

```
  LAPTOP · launchd 08:00 daily          GITHUB ACTIONS · triggered on success
  ┌──────────────────────────────┐      ┌──────────────────────────────────┐
  │ nba_api → ingestion script   │      │  SQL transforms                  │
  └──────────────┬───────────────┘      │        ↓                         │
                 │   gh workflow run ──▶│  star schema (dims + facts)      │
                 ▼                      │        ↓                         │
        ╔════════════════════╗          │  data quality checks             │
        ║  NEON POSTGRES     ║◀────────▶└──────────────────────────────────┘
        ║  staging → star    ║
        ╚════════╤═══════════╝
                 ▼
        Streamlit dashboard
```

**Pipeline behaviour**

| Stage | Where | Trigger |
|---|---|---|
| Ingest box scores → staging | Laptop | `launchd`, 08:00 daily |
| Rebuild star schema | GitHub Actions | `gh workflow run` on successful ingest |
| Data quality checks | GitHub Actions | Immediately after the rebuild |

Two decisions worth calling out:

- **The rebuild is event-driven, not a second cron.** A fixed clock offset would transform
  stale data on any night ingestion ran late or not at all — and the checks would happily
  pass on it. A 17:00 UTC schedule exists only as a backstop for days the laptop was off;
  transforms are idempotent, so a no-op run is harmless.
- **A failed ingest does not trigger the rebuild.** The warehouse keeps yesterday's good
  data rather than being rebuilt from a partial load.

**Star schema**

| Table | Grain | Rows |
|---|---|---|
| `dim_teams` | one per team | 30 |
| `dim_players` | one per player | 591 |
| `dim_games` | one per game | 1,322 |
| `fact_player_game_stats` | player × game | 28,716 |
| `fact_team_game_stats` | team × game | 2,644 |

---

## Data Sources

Everything comes from [`nba_api`](https://github.com/swar/nba_api), which wraps
`stats.nba.com`. No API key is required.

| Endpoint | Used for |
|---|---|
| `leaguegamefinder` | Season game list — one call returns the whole season |
| `BoxScoreTraditionalV3` | Player box scores, one call per game |
| `commonteamroster` | Player positions and team assignments |
| `leaguestandingsv3` | Conference and division for all 30 teams |

**Games in scope** are filtered by `GAME_ID` prefix:

| Prefix | Type | Count | In scope |
|---|---|---|---|
| `002` | Regular season | 1,230 | ✅ |
| `004` | Playoffs | 85 | ✅ |
| `005` | Play-in | 6 | ✅ |
| `006` | NBA Cup final | 1 | ⚠️ Ingested, tagged, excluded from regular-season aggregates |
| `001` | Preseason | — | ❌ Exhibitions vs. non-NBA clubs, duplicate partial rows |
| `003` | All-Star | — | ❌ Fake rosters |

Three source quirks shaped the ingestion code, and each one fails silently rather than
raising:

- **`BoxScoreTraditionalV2` returns zero rows** with its columns intact as of 2025-26. A
  pipeline built on it would "succeed" nightly while loading nothing. V3 is used instead.
- **`MATCHUP` must be parsed structurally**, not row-relative. Some games return the same
  matchup string on *both* team rows, so "my row says `vs.` so I'm home" misclassifies
  roughly 1 game in 9.
- **`minutes` is a `"MM:SS"` string.** `"26:41"` is 26.68 minutes, not 26.41. Parsed at
  load time, or every rate-based metric is wrong.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.14 |
| Ingestion | `nba_api`, `psycopg2` |
| Warehouse | Postgres 18 on Neon (free tier) |
| Test database | Postgres 16 via Docker Compose |
| Transform | Hand-written SQL |
| Orchestration | `launchd` (ingest) + GitHub Actions (transform, checks) |
| Dashboard | Streamlit |
| Testing | pytest — 72 tests |

Two databases, for one specific reason: the transform tests `CREATE` and `DROP` a
throwaway database per run, and Neon's free tier doesn't permit `CREATE DATABASE`. So
local Docker Postgres stayed on as the test database. `DATABASE_URL` and
`TEST_DATABASE_URL` select between them.

*No dbt, no Airflow.* Hand-written SQL and GitHub Actions are enough at this scale; both
are noted stretch goals.

---

## Project Structure

```
├── src/
│   ├── ingestion/
│   │   ├── ingest_games.py        # box scores → staging (date | --season | --catch-up)
│   │   ├── ingest_reference.py    # teams and player positions
│   │   ├── nba_client.py          # rate limiting, retry-with-backoff
│   │   └── config.py              # DATABASE_URL resolution
│   ├── transform/
│   │   ├── run_transforms.py      # executes the SQL below, in order
│   │   ├── run_checks.py          # runs the checks, fails loud
│   │   └── sql/
│   │       ├── staging/           # 001-002  raw landing tables
│   │       ├── schema/            # 010      star schema DDL
│   │       ├── transforms/        # 020-030  load dimensions, then facts
│   │       └── checks/            # 050      13 data quality checks
│   └── dashboard/
│       ├── app.py                 # entry point + navigation
│       ├── queries.py             # all SQL, cached
│       ├── charts.py              # shared chart helpers
│       └── pages/                 # team trends, player deep dive
├── scripts/
│   ├── nightly.sh                 # ingest, then trigger the rebuild
│   ├── install_launchd.sh         # generates + loads the launchd plist
│   └── probe_api.py               # is stats.nba.com reachable from here?
├── tests/                         # 72 tests
├── .github/workflows/             # ci · warehouse · probe-nba-api
├── notebooks/                     # API exploration findings
└── docker-compose.yml             # local Postgres 16 (test database)
```

---

## Additional Documentation

| Where | What |
|---|---|
| [`notebooks/01_api_exploration.ipynb`](notebooks/01_api_exploration.ipynb) | Endpoint exploration — where the V2/V3 and `MATCHUP` findings came from |
| [`scripts/probe_api.py`](scripts/probe_api.py) | Reachability probe; run it via the **Probe nba_api** workflow to re-test the blocked-IP finding |
| [`src/transform/sql/checks/050_data_quality.sql`](src/transform/sql/checks/050_data_quality.sql) | All 13 checks, each with a comment on what it catches and why |
| [`src/transform/sql/schema/010_star_schema.sql`](src/transform/sql/schema/010_star_schema.sql) | Full DDL with constraints |
| [`.github/workflows/`](.github/workflows/) | CI, warehouse rebuild, and probe workflows |
| [`.env.example`](.env.example) | Every environment variable, annotated |

The SQL files are the best documentation of the modelling decisions — each carries
comments explaining not just what it does but what would break without it.
