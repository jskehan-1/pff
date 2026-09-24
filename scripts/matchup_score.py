"""
DIY approximation of PFF's Matchup Chart tool, built from data we already
pull (PFF's own 0-100 grade fields, not available through their actual
matchup tool -- that's a paywalled, JS-rendered page with no API backing).

Per PFF's own description of their methodology (relayed by the user):
  - Score is based on the percentile difference between an offensive
    player's quality and the opposing defense's quality in the relevant
    category, on a 1-5 scale (5 = strongest matchup).
  - 4-5 = Great, 3 = Fair, 1-2 = Poor.

This is NOT PFF's actual proprietary model ("next level AI modeling" per
their own description) -- it's a transparent percentile-difference
approximation using PFF's grade data, which is the closest we can get
without access to their tool itself.

Category mapping (our approximation, not confirmed against PFF's own):
  - RB matchups: RB's own rushing grade (grades_run) vs opponent's
    grades_run_defense.
  - QB/WR/TE matchups: player's passing/route grade vs opponent's pass
    defense, blended from grades_pass_rush_defense + grades_coverage_defense
    (we don't have a single unified "pass defense" grade from the API, so
    this blend is our own choice, not PFF's).

Ranking window: season-to-date average (not recent-form) per the project's
current design choice -- more stable early in a season with few games.
"""
import csv
import re
import statistics
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def normalize_name(name):
    """Duplicated minimally from build_projections.py to avoid a circular import."""
    name = str(name or "").strip().lower()
    name = re.sub(r"[.'\"]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

# Duplicated minimally from build_projections.py's TEAM_ALIASES (avoids a
# circular import) -- PFF's weekly_stats exports use a handful of team codes
# that don't match DK's/the rest of this project's canonical codes (notably
# ARZ/BLT/CLV/HST/LA vs ARI/BAL/CLE/HOU/LAR). Without canonicalizing here,
# build_matchup_context()'s defense_percentiles dict ends up keyed by PFF's
# raw codes while score_matchup() is always called with a DK-canonical
# opponent code -- a silent lookup miss (falls back to "No data") for every
# player facing one of those 5 teams. Bug found + fixed 2026-09-23.
TEAM_ALIASES = {
    "ari": "ARI", "arz": "ARI", "cardinals": "ARI",
    "atl": "ATL", "falcons": "ATL",
    "bal": "BAL", "blt": "BAL", "ravens": "BAL",
    "buf": "BUF", "bills": "BUF",
    "car": "CAR", "panthers": "CAR",
    "chi": "CHI", "bears": "CHI",
    "cin": "CIN", "bengals": "CIN",
    "cle": "CLE", "clv": "CLE", "browns": "CLE",
    "dal": "DAL", "cowboys": "DAL",
    "den": "DEN", "broncos": "DEN",
    "det": "DET", "lions": "DET",
    "gb": "GB", "gnb": "GB", "packers": "GB",
    "hou": "HOU", "hst": "HOU", "texans": "HOU",
    "ind": "IND", "colts": "IND",
    "jax": "JAX", "jac": "JAX", "jaguars": "JAX",
    "kc": "KC", "kan": "KC", "chiefs": "KC",
    "lv": "LV", "lvr": "LV", "oak": "LV", "raiders": "LV",
    "lac": "LAC", "sd": "LAC", "chargers": "LAC",
    "lar": "LAR", "la": "LAR", "stl": "LAR", "rams": "LAR",
    "mia": "MIA", "dolphins": "MIA",
    "min": "MIN", "vikings": "MIN",
    "ne": "NE", "nwe": "NE", "patriots": "NE",
    "no": "NO", "nor": "NO", "saints": "NO",
    "nyg": "NYG", "giants": "NYG",
    "nyj": "NYJ", "jets": "NYJ",
    "phi": "PHI", "eagles": "PHI",
    "pit": "PIT", "steelers": "PIT",
    "sea": "SEA", "seahawks": "SEA",
    "sf": "SF", "sfo": "SF", "49ers": "SF", "niners": "SF",
    "tb": "TB", "tam": "TB", "buccaneers": "TB", "bucs": "TB",
    "ten": "TEN", "titans": "TEN",
    "was": "WAS", "wsh": "WAS", "commanders": "WAS",
}


def team_canon(name_or_abbrev):
    key = (name_or_abbrev or "").strip().lower()
    return TEAM_ALIASES.get(key, key.upper())


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def percentile_rank(value, population):
    """% of population strictly below `value`, plus half the ties -- 0-100."""
    if value is None or not population:
        return None
    below = sum(1 for x in population if x < value)
    tied = sum(1 for x in population if x == value)
    return round(100 * (below + 0.5 * tied) / len(population), 1)


def build_matchup_context(season, upcoming_week):
    """
    Reads weekly_stats_{season}.csv (falls back to season-1 if the current
    season has no games yet) and returns:
      offense_percentiles: dict normalized-ish key (player_name, position) -> percentile within position
      defense_percentiles: dict team -> {"pass": pct, "run": pct}
    Position groups considered: QB (grade_pass), RB (grade_run), WR/TE (grade_route).
    """
    path = DATA_DIR / f"weekly_stats_{season}.csv"
    if not path.exists() or upcoming_week <= 1:
        prior = DATA_DIR / f"weekly_stats_{season - 1}.csv"
        path = prior if prior.exists() else path

    if not path.exists():
        return {}, {}

    grade_pass_by_player = {}   # name -> list of grades
    grade_run_by_player = {}
    grade_route_by_player = {}
    team_pass_rush_def = {}     # team -> list of grades
    team_coverage_def = {}
    team_run_def = {}

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            week = int(row["week"])
            if path.name.startswith(f"weekly_stats_{season}") and week >= upcoming_week:
                continue  # never use future/current-week data
            name = normalize_name(row["player_name"])
            gp = _to_float(row.get("grade_pass"))
            gr = _to_float(row.get("grade_run"))
            grt = _to_float(row.get("grade_route"))
            if gp is not None:
                grade_pass_by_player.setdefault(name, []).append(gp)
            if gr is not None:
                grade_run_by_player.setdefault(name, []).append(gr)
            if grt is not None:
                grade_route_by_player.setdefault(name, []).append(grt)

            if row["position"] == "DST":
                team = team_canon(row["team"])
                d1 = _to_float(row.get("team_grade_pass_rush_def"))
                d2 = _to_float(row.get("team_grade_coverage_def"))
                d3 = _to_float(row.get("team_grade_run_def"))
                if d1 is not None:
                    team_pass_rush_def.setdefault(team, []).append(d1)
                if d2 is not None:
                    team_coverage_def.setdefault(team, []).append(d2)
                if d3 is not None:
                    team_run_def.setdefault(team, []).append(d3)

    def avg(vals):
        return statistics.mean(vals) if vals else None

    qb_avgs = {name: avg(vals) for name, vals in grade_pass_by_player.items()}
    rb_avgs = {name: avg(vals) for name, vals in grade_run_by_player.items()}
    wrte_avgs = {name: avg(vals) for name, vals in grade_route_by_player.items()}

    qb_pop = [v for v in qb_avgs.values() if v is not None]
    rb_pop = [v for v in rb_avgs.values() if v is not None]
    wrte_pop = [v for v in wrte_avgs.values() if v is not None]

    offense_percentiles = {}
    for name, v in qb_avgs.items():
        offense_percentiles[(name, "QB")] = percentile_rank(v, qb_pop)
    for name, v in rb_avgs.items():
        offense_percentiles[(name, "RB")] = percentile_rank(v, rb_pop)
    for name, v in wrte_avgs.items():
        offense_percentiles[(name, "WRTE")] = percentile_rank(v, wrte_pop)

    team_pass_rush_avg = {t: avg(v) for t, v in team_pass_rush_def.items()}
    team_coverage_avg = {t: avg(v) for t, v in team_coverage_def.items()}
    team_run_avg = {t: avg(v) for t, v in team_run_def.items()}

    pass_rush_pop = [v for v in team_pass_rush_avg.values() if v is not None]
    coverage_pop = [v for v in team_coverage_avg.values() if v is not None]
    run_pop = [v for v in team_run_avg.values() if v is not None]

    defense_percentiles = {}
    all_teams = set(team_pass_rush_avg) | set(team_coverage_avg) | set(team_run_avg)
    for t in all_teams:
        pr_pct = percentile_rank(team_pass_rush_avg.get(t), pass_rush_pop)
        cov_pct = percentile_rank(team_coverage_avg.get(t), coverage_pop)
        run_pct = percentile_rank(team_run_avg.get(t), run_pop)
        pass_components = [x for x in (pr_pct, cov_pct) if x is not None]
        defense_percentiles[t] = {
            "pass": round(sum(pass_components) / len(pass_components), 1) if pass_components else None,
            "run": run_pct,
        }

    return offense_percentiles, defense_percentiles


def score_matchup(position, offense_percentiles, defense_percentiles, player_name, opponent_team):
    """
    Returns (score_1_to_5_or_None, label, offense_percentile, defense_percentile).
    None score means we don't have enough data (new player, or opponent not found).
    """
    group = "RB" if position == "RB" else ("QB" if position == "QB" else "WRTE")
    category = "run" if group == "RB" else "pass"

    off_pct = offense_percentiles.get((player_name, group))
    def_pct = (defense_percentiles.get(opponent_team) or {}).get(category)

    if off_pct is None or def_pct is None:
        return None, "No data", off_pct, def_pct

    combined = (off_pct + (100 - def_pct)) / 2
    score = min(5, max(1, -(-combined // 20) or 1))  # ceil(combined/20), floor at 1
    score = int(score)
    label = "Great" if score >= 4 else ("Fair" if score == 3 else "Poor")
    return score, label, off_pct, def_pct


def parse_opponent(game_info, own_team_canon, team_canon_fn):
    """Extracts the opposing team's canonical code from a DK-style Game Info
    string like 'BUF@MIA 09/14/2026 01:00PM ET' or 'NO @ DET'."""
    import re
    m = re.search(r"([A-Za-z]{2,4})\s*@\s*([A-Za-z]{2,4})", game_info or "")
    if not m:
        return None
    t1, t2 = team_canon_fn(m.group(1)), team_canon_fn(m.group(2))
    if t1 == own_team_canon:
        return t2
    if t2 == own_team_canon:
        return t1
    return None
