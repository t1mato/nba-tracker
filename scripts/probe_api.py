"""Diagnostic: can we reach stats.nba.com from here?

    python scripts/probe_api.py

Answers one question — is this machine's IP blocked by stats.nba.com? Run it
locally and it should pass; the point is running it somewhere you are
*evaluating* as an ingestion host.

Confirmed results (2026-09-01):
  * laptop (residential IP)      HTTP 200, 0.3s
  * GitHub-hosted runner (Azure) ReadTimeout after 60s, twice, two IPs
  * Oracle Cloud always-free VM  ReadTimeout after 60s

Two independent clouds, three datacenter IPs, one verdict: stats.nba.com
blocks datacenter ranges broadly. Ingestion has to run from a residential
connection. Note the probe's own control — the "egress IP" line is a
successful HTTPS call to a different host, so a timeout below it means
stats.nba.com specifically, not a broken network.

Note the failure shape: not a 403. The TLS handshake succeeds and the server
then never replies. Blocked infrastructure often will not tell you it blocked
you.

To run on a bare host with no project checkout:

    pip install requests
    curl -sO https://raw.githubusercontent.com/t1mato/nba-tracker/main/scripts/probe_api.py
    python probe_api.py

nba_api is optional — without it the script issues the same URLs with the same
headers via plain requests, and returns byte-identical payloads.

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

# nba_api is optional here on purpose. The most useful place to run this probe
# is a bare VM you are evaluating as an ingestion host, where `pip install
# nba_api` drags in pandas for a two-call diagnostic. Without it we fall back to
# hitting the same URLs with the same headers via plain requests — which is all
# nba_api does anyway.
try:
    from nba_api.stats.endpoints import boxscoretraditionalv3, leaguegamefinder
    from nba_api.stats.library.http import NBAStatsHTTP
    HAVE_NBA_API = True
except ImportError:
    HAVE_NBA_API = False

TIMEOUT = 60

BASE_URL = "https://stats.nba.com/stats/{endpoint}"

# Copied verbatim from nba_api.stats.library.http.STATS_HEADERS. stats.nba.com
# rejects requests without a browser-shaped header set, so a bare requests.get
# would fail for reasons unrelated to the IP and give a false positive.
STATS_HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.nba.com/",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "Sec-Ch-Ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Fetch-Dest": "empty",
}

# The parameters each endpoint requires, for the no-nba_api path. stats.nba.com
# is strict: every documented parameter must be present, even when empty.
FALLBACK_REQUESTS = {
    "leaguegamefinder": {
        "Conference": "", "DateFrom": "", "DateTo": "", "Division": "",
        "DraftNumber": "", "DraftRound": "", "DraftTeamID": "", "DraftYear": "",
        "EqAST": "", "EqBLK": "", "EqDD": "", "EqDREB": "", "EqFG3A": "",
        "EqFG3M": "", "EqFG3_PCT": "", "EqFGA": "", "EqFGM": "", "EqFG_PCT": "",
        "EqFTA": "", "EqFTM": "", "EqFT_PCT": "", "EqMINUTES": "", "EqOREB": "",
        "EqPF": "", "EqPTS": "", "EqREB": "", "EqSTL": "", "EqTD": "", "EqTOV": "",
        "GtAST": "", "GtBLK": "", "GtDD": "", "GtDREB": "", "GtFG3A": "",
        "GtFG3M": "", "GtFG3_PCT": "", "GtFGA": "", "GtFGM": "", "GtFG_PCT": "",
        "GtFTA": "", "GtFTM": "", "GtFT_PCT": "", "GtMINUTES": "", "GtOREB": "",
        "GtPF": "", "GtPTS": "", "GtREB": "", "GtSTL": "", "GtTD": "", "GtTOV": "",
        "LeagueID": "00", "Location": "", "LtAST": "", "LtBLK": "", "LtDD": "",
        "LtDREB": "", "LtFG3A": "", "LtFG3M": "", "LtFG3_PCT": "", "LtFGA": "",
        "LtFGM": "", "LtFG_PCT": "", "LtFTA": "", "LtFTM": "", "LtFT_PCT": "",
        "LtMINUTES": "", "LtOREB": "", "LtPF": "", "LtPTS": "", "LtREB": "",
        "LtSTL": "", "LtTD": "", "LtTOV": "", "Outcome": "", "PORound": "",
        "PlayerID": "", "PlayerOrTeam": "T", "RookieYear": "", "Season": "2025-26",
        "SeasonSegment": "", "SeasonType": "", "StarterBench": "", "TeamID": "",
        "VsConference": "", "VsDivision": "", "VsTeamID": "", "YearsExperience": "",
    },
    "boxscoretraditionalv3": {
        "GameID": "0022500001", "LeagueID": "00", "endPeriod": 0, "endRange": 28800,
        "rangeType": 0, "startPeriod": 0, "startRange": 0,
    },
}

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


def probe_without_nba_api(label: str, endpoint: str) -> bool:
    """Same request, issued with plain requests. Used when nba_api is absent."""
    print(f"\n--- {label} ---")

    started = time.monotonic()
    try:
        response = requests.get(
            BASE_URL.format(endpoint=endpoint),
            params=FALLBACK_REQUESTS[endpoint],
            headers=STATS_HEADERS,
            timeout=TIMEOUT,
        )
    except Exception as exc:
        print(f"  FAILED before a response: {type(exc).__name__}: {exc}")
        return False

    elapsed = time.monotonic() - started
    print(f"  HTTP {response.status_code}  ({elapsed:.1f}s, {len(response.text)} bytes)")

    try:
        payload = response.json()
    except ValueError:
        print("  body is NOT valid JSON — this is what the pipeline would retry 4x")
        print(f"  first 300 bytes: {response.text[:300]!r}")
        return False

    print(f"  OK — {_row_count(payload)}")
    return True


def main() -> int:
    print(f"egress IP: {egress_ip()}")
    if not HAVE_NBA_API:
        print("nba_api not installed — using the plain-requests fallback "
              "(same URLs, same headers)")

    if HAVE_NBA_API:
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
    else:
        results = [
            probe_without_nba_api("leaguegamefinder (game discovery)", "leaguegamefinder"),
            probe_without_nba_api("boxscoretraditionalv3 (per-game stats)", "boxscoretraditionalv3"),
        ]

    print()
    if all(results):
        print("RESULT: stats.nba.com is reachable from this IP.")
        return 0

    print("RESULT: at least one endpoint failed — see the status codes above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
