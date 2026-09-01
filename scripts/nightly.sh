#!/bin/bash
#
# The nightly pipeline's local half.
#
#   1. Ingest every game of the current season the warehouse does not have
#   2. On success, trigger the GitHub Actions workflow that rebuilds the star
#      schema and runs the data quality checks
#
# Ingestion runs here rather than on a runner because stats.nba.com black-holes
# datacenter IPs — verified on GitHub Actions (Azure) and Oracle Cloud, both
# ReadTimeout, while this laptop gets HTTP 200 in 0.3s. See scripts/probe_api.py.
#
# Installed as a launchd job; see scripts/com.nba-tracker.nightly.plist.
# Safe to run by hand at any time: every write is an upsert.

set -uo pipefail   # deliberately NOT -e: failures are handled, not fatal-by-default

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The console script from the editable install (see pyproject.toml), not
# `python -m ...`: it resolves the package itself, so this no longer depends on
# the job happening to start in the project root.
NBA_INGEST="$PROJECT_ROOT/venv/bin/nba-ingest"

# launchd gives a job /usr/bin:/bin:/usr/sbin:/sbin and nothing else, so gh
# (Homebrew) is invisible unless we say where it is. This is the single most
# common reason a job that works in a terminal does nothing at 8am.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

log "=== nightly run starting ==="
cd "$PROJECT_ROOT" || { log "FATAL: cannot cd to $PROJECT_ROOT"; exit 1; }

if [ ! -x "$NBA_INGEST" ]; then
    log "FATAL: no nba-ingest at $NBA_INGEST"
    log "       run: venv/bin/pip install -e '.[dev]'"
    exit 1
fi

# --- 1. ingest ---------------------------------------------------------------
# --catch-up derives the current season and fetches only games we lack, so a
# missed day is collected by the next run instead of becoming a permanent hole.
log "ingesting (catch-up)..."
"$NBA_INGEST" --catch-up
INGEST_STATUS=$?

if [ $INGEST_STATUS -ne 0 ]; then
    log "ingestion FAILED (exit $INGEST_STATUS) — not triggering the rebuild"
    log "the warehouse keeps yesterday's data rather than being rebuilt from a partial load"
    exit $INGEST_STATUS
fi
log "ingestion OK"

# --- 2. trigger the rebuild --------------------------------------------------
# Event-driven rather than a second cron guessing when this finished. A fixed
# offset would transform stale data on any night this ran late or not at all.
if ! command -v gh >/dev/null 2>&1; then
    log "WARN: gh not found — skipping trigger; the 10am backstop schedule will cover it"
    exit 0
fi

if ! gh auth status >/dev/null 2>&1; then
    log "WARN: gh not authenticated — skipping trigger; the backstop schedule will cover it"
    exit 0
fi

log "triggering the warehouse rebuild..."
if gh workflow run "Rebuild warehouse" --repo t1mato/nba-tracker; then
    log "rebuild triggered"
else
    # Not fatal. The ingestion succeeded and is durable in staging; the daily
    # backstop schedule will rebuild from it.
    log "WARN: could not trigger the rebuild — the backstop schedule will cover it"
fi

log "=== nightly run complete ==="
