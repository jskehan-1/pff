"""
Thin, unauthenticated client for ESPN's public "site" JSON API. No API key
needed -- these are the same endpoints espn.com's own web/mobile apps call.

Confirmed live (2026-09-23, via research, not yet run against real data from
inside this project -- run --inspect on the pull scripts that use this and
tell Claude what comes back if anything looks off):
  - /teams/{espn_id}/roster        -> per-player Status (Active/Out/Doubtful/
                                       Questionable/IR/...), embedded inline,
                                       no extra $ref calls needed.
  - /teams/{espn_id}/depthcharts   -> depth-chart rank per position, athlete
                                       names embedded inline (also no $ref
                                       chasing needed) via a top-level
                                       "depthchart" list of formations, each
                                       with "positions" -> {posKey: {athletes:[...],
                                       position: {abbreviation: ...}}}.

Rate limiting: none documented/observed, but this is a free public API riding
on espn.com's own infra -- pull_espn_status.py/pull_espn_depthchart.py add a
small delay between the 32 per-team calls to be a considerate citizen, not
because a limit was hit.
"""
import time
import urllib.request
import urllib.error
import json

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

# ESPN's numeric team ids -> our canonical team codes (matches team_canon()
# in build_projections.py / matchup_score.py). Confirmed live via direct
# lookups against /teams/{id} for every id below (2026-09-23).
ESPN_TEAM_IDS = {
    "ATL": 1, "BUF": 2, "CHI": 3, "CIN": 4, "CLE": 5, "DAL": 6, "DEN": 7,
    "DET": 8, "GB": 9, "TEN": 10, "IND": 11, "KC": 12, "LV": 13, "LAR": 14,
    "MIA": 15, "MIN": 16, "NE": 17, "NO": 18, "NYG": 19, "NYJ": 20, "PHI": 21,
    "ARI": 22, "PIT": 23, "LAC": 24, "SF": 25, "SEA": 26, "TB": 27, "WAS": 28,
    "CAR": 29, "JAX": 30, "BAL": 33, "HOU": 34,
}


# ESPN's edge (Akamai) 403s requests that don't look like a real browser --
# confirmed live 2026-09-23 (Jake's first run: every call 403'd with the
# original tool-ish User-Agent). A full, current desktop-Chrome header set
# gets through; a bare urllib default or an obviously-scripted UA does not.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
    "Origin": "https://www.espn.com",
}


def get(path, retries=3, delay_between_retries=3):
    """GET a path under BASE_URL, e.g. '/teams/3/roster'. Returns parsed JSON."""
    url = f"{BASE_URL}{path}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 403:
                # Not a transient issue -- retrying with the same headers
                # won't help. Fail fast with a clear message instead of
                # burning 3 retries x 32 teams on a request that will never
                # succeed as-is.
                raise RuntimeError(
                    f"ESPN returned 403 Forbidden for {url}. This usually means ESPN's edge "
                    f"is blocking the request as non-browser traffic -- tell Claude the exact "
                    f"error so the headers/approach can be adjusted, rather than re-running as-is."
                ) from e
            if attempt < retries - 1:
                time.sleep(delay_between_retries)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay_between_retries)
    raise RuntimeError(f"ESPN API request failed after {retries} attempts: {url} ({last_err})")


def iter_teams():
    """Yields (team_code, espn_id) for all 32 teams, in a stable order."""
    for code, espn_id in ESPN_TEAM_IDS.items():
        yield code, espn_id
