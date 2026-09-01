"""Diagnostic: can we reach stats.nba.com from here?

    python scripts/probe_api.py

Answers one question — does `nba_api` work from this machine, or is this IP
blocked? Run it locally and it should pass; the point is running it on a GitHub
Actions runner, whose datacenter IP stats.nba.com may reject.

Why this bypasses src/ingestion/nba_client.py
---------------------------------------------
nba_api never calls `raise_for_status()`. On a 403 it takes the HTML error page
as the response body, fails to parse it as JSON, and the endpoint surfaces a
`JSONDecodeError` — which `nba_client._RETRYABLE` treats as transient, so it
retries four times with backoff and finally raises a generic
"failed after 4 attempts". The HTTP status never appears anywhere.

So this script builds the request nba_api *would* send (`get_request=False`
returns the endpoint object without firing it), sends it, and reads the status
code off the response before anything has a chance to discard it.

Exit code is 0 only if every endpoint returned parseable JSON.
"""

from __future__ import annotations

import json
import sys
import time

import requests
from nba_api.stats.endpoints import boxscoretraditionalv3, leaguegamefinder
from nba_api.stats.library.http import NBAStatsHTTP

TIMEOUT = 60

# A real 2025-26 regular season game (opening night), so a zero-row result means
# something is wrong rather than "nothing was scheduled".
SAMPLE_GAME_ID = "0022500001"
SAMPLE_SEASON = "2025-26"


def egress_ip() -> str:
    """The IP stats.nba.com will see. Worth logging: if we are blocked, this is
    the evidence, and it distinguishes a runner IP from your home connection."""
    try:
        return requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception as exc:
        return f"unknown ({type(exc).__name__})"


def _row_count(payload: dict) -> str:
    """Rows in the payload, for the two response shapes we consume.

    leaguegamefinder returns the legacy resultSets format; the V3 box score
    returns a nested object. Unknown shapes report their keys instead of
    pretending to a count.
    """
    if "resultSets" in payload:
        return f"{len(payload['resultSets'][0]['rowSet'])} rows"
    if "boxScoreTraditional" in payload:
        box = payload["boxScoreTraditional"]
        players = len(box["homeTeam"]["players"]) + len(box["awayTeam"]["players"])
        return f"{players} player rows"
    return f"unrecognised shape, keys={sorted(payload)}"


def probe(label: str, endpoint_class, **kwargs) -> bool:
    """Send one endpoint's request raw and report what came back."""
    print(f"\n--- {label} ---")

    # get_request=False builds the parameters without firing the request, so we
    # send exactly what nba_api would send — same endpoint, same params, same
    # headers — but keep control of the response.
    endpoint = endpoint_class(get_request=False, **kwargs)

    started = time.monotonic()
    try:
        response = NBAStatsHTTP().send_api_request(
            endpoint=endpoint.endpoint,
            parameters=endpoint.parameters,
            timeout=TIMEOUT,
        )
    except Exception as exc:
        print(f"  FAILED before a response: {type(exc).__name__}: {exc}")
        return False

    elapsed = time.monotonic() - started
    body = response.get_response()

    # No public accessor for the status code — NBAResponse keeps it private and
    # nothing above ever reads it. That omission is the reason this script exists.
    status = response._status_code

    print(f"  HTTP {status}  ({elapsed:.1f}s, {len(body)} bytes)")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # This is what a block looks like from inside the pipeline: not an
        # HTTPError, just a body that isn't JSON.
        print("  body is NOT valid JSON — this is what the pipeline would retry 4x")
        print(f"  first 300 bytes: {body[:300]!r}")
        return False

    print(f"  OK — {_row_count(payload)}")
    return True


def main() -> int:
    print(f"egress IP: {egress_ip()}")

    results = [
        probe(
            "leaguegamefinder (game discovery)",
            leaguegamefinder.LeagueGameFinder,
            season_nullable=SAMPLE_SEASON,
            league_id_nullable="00",
        ),
        probe(
            "boxscoretraditionalv3 (per-game stats)",
            boxscoretraditionalv3.BoxScoreTraditionalV3,
            game_id=SAMPLE_GAME_ID,
        ),
    ]

    print()
    if all(results):
        print("RESULT: stats.nba.com is reachable from this IP.")
        return 0

    print("RESULT: at least one endpoint failed — see the status codes above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
