"""Run the SQL transform layer in order.

    python -m nba_tracker.transform.run_transforms            # schema + transforms
    python -m nba_tracker.transform.run_transforms --list     # show what would run

Files run in filename order, so the numeric prefixes control sequencing:
  staging/    tables the ingestion writes into
  schema/     star-schema DDL
  transforms/ the SQL that populates the star schema from staging

Every script is idempotent (CREATE TABLE IF NOT EXISTS / INSERT ... ON CONFLICT),
so a full re-run is safe and is the normal way to apply a modelling change.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import psycopg2

from nba_tracker.ingestion.config import get_database_url

log = logging.getLogger("transform")

# Resolved against this package rather than the repo root, so the SQL is
# found the same way whether the project is installed or run from a checkout.
SQL_ROOT = Path(__file__).resolve().parent / "sql"
STAGES = ("staging", "schema", "transforms")


def sql_files(stages: tuple[str, ...] = STAGES) -> list[Path]:
    """Every .sql file, ordered by stage then filename.

    `stages` is narrowable so a caller can apply the DDL and the transforms as
    separate steps — the tests build an empty warehouse first, load fixtures,
    and only then run the transforms over them.
    """
    files: list[Path] = []
    for stage in stages:
        files.extend(sorted((SQL_ROOT / stage).glob("*.sql")))
    return files


def run(conn, path: Path) -> None:
    started = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(path.read_text())
        # rowcount reflects the last statement only, which for our transform
        # files is the INSERT we care about.
        affected = cur.rowcount
    conn.commit()
    log.info("%-42s %6s rows  %5.1fs",
             f"{path.parent.name}/{path.name}",
             affected if affected >= 0 else "-",
             time.monotonic() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="show files, run nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    files = sql_files()
    if not files:
        log.error("no .sql files found under %s", SQL_ROOT)
        return 1

    if args.list:
        for path in files:
            print(f"  {path.relative_to(SQL_ROOT)}")
        return 0

    with psycopg2.connect(get_database_url()) as conn:
        for path in files:
            try:
                run(conn, path)
            except Exception as exc:
                conn.rollback()
                log.error("%s FAILED: %s: %s", path.name, type(exc).__name__, exc)
                return 1

    log.info("transform layer complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
