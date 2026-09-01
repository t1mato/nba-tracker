"""Run the data quality checks and fail loudly if the warehouse is wrong.

    python -m src.transform.run_checks           # exit 1 if any error check fails
    python -m src.transform.run_checks --strict  # warnings fail the run too

Separate from run_transforms.py on purpose. That script's job is to *build* the
warehouse; this one's job is to *judge* it. Keeping them apart means a failing
check reports a verdict instead of aborting a rebuild half-way, and it means the
checks can be run on their own against a warehouse nobody just rebuilt.

This is what makes the nightly job trustworthy. `ingest_games` exits 0 when it
finds no games, which is correct in the offseason and indistinguishable from
"the API returned an empty payload and we loaded nothing". The checks are what
tell those two apart.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import psycopg2

from src.ingestion.config import PROJECT_ROOT, get_database_url

log = logging.getLogger("checks")

CHECKS_SQL = PROJECT_ROOT / "src" / "transform" / "sql" / "checks" / "050_data_quality.sql"


def run_checks(conn) -> list[tuple[str, str, int, str]]:
    """Execute the checks file. Returns (name, severity, violations, detail) rows."""
    with conn.cursor() as cur:
        cur.execute(CHECKS_SQL.read_text())
        return cur.fetchall()


def report(rows: list[tuple[str, str, int, str]]) -> tuple[int, int]:
    """Print one line per check. Returns (errors, warnings) that fired."""
    errors = warnings = 0
    width = max((len(name) for name, *_ in rows), default = 20)

    for name, severity, violations, detail in rows:
        if not violations:
            log.info("  PASS  %-*s", width, name)
            continue

        if severity == "error":
            errors += 1
            log.error(" FAIL  %-*s  %s violation(s): %s", width, name, violations, detail)
        else:
            warnings += 1
            log.warning(" WARN  %-*s  %s: %s", width, name, violations, detail)

    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    if not CHECKS_SQL.exists():
        log.error("checks file not found: %s", CHECKS_SQL)
        return 1

    with psycopg2.connect(get_database_url()) as conn:
        rows = run_checks(conn)

    errors, warnings = report(rows)

    log.info("%d checks run: %d failed, %d warned", len(rows), errors, warnings)

    if errors:
        log.error("data quality FAILED — the warehouse is wrong, do not trust it")
        return 1

    if warnings and args.strict:
        log.error("data quality failed under --strict (%d warning(s))", warnings)
        return 1

    log.info("data quality OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
