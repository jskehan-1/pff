"""
Merge a DraftKings salary export with your accumulated PFF-derived weekly
stats history to produce a projections file for the upcoming slate.

Expects the standard DraftKings "Export to CSV" columns from the
lineup/contest player pool page: Position, Name + ID, Name, ID,
Roster Position, Salary, Game Info, TeamAbbrev, AvgPointsPerGame
(DK's own column names/casing have changed before -- this script matches
loosely, see SALARY_COLS below).

Usage:
    python scripts\\build_projections.py --season 2026 --week 5 --salaries data\\dk_salaries\\DKSalaries_wk5.csv
"""
import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preseason_projections import load_preseason_projections  # noqa: E402
from matchup_score import build_matchup_context, score_matchup, parse_opponent  # noqa: E402
from nfl_calendar import current_week  # noqa: E402
import team_schedule  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Loose candidate names for each DK salary CSV column (case-insensitive).
SALARY_COLS = {
    "name": ["Name", "Player Name", "Name + ID"],
    "position": ["Position", "Roster Position"],
    "team": ["TeamAbbrev", "Team"],
    "salary": ["Salary"],
    "game_info": ["Game Info"],
    "dk_avg": ["AvgPointsPerGame", "Avg Points Per Game"],
    # Only present when salaries came from pull_dk_salaries.py (not a manual
    # DK CSV export) -- optional, missing entirely is fine.
    "status": ["Status"],
    "draft_alerts": ["DraftAlerts"],
    "game_start": ["GameStart"],
}

# DK status values that mean "not going to suit up" -- excluded from the
# player pool entirely (not just hidden), so neither the tables nor the
# optimizer ever consider them. Deliberately does NOT include Questionable
# ("Q") or Doubtful ("D") -- those players often do play, so hiding them
# would cost you real information. Not yet confirmed against a real flagged
# player (only "None" for a healthy player has been observed live) -- this
# list is DK's commonly-known status vocabulary; widen it here if a real
# excluded-looking player still shows up in the tables.
EXCLUDED_STATUSES = {"O", "OUT", "IR", "IR-R", "NA", "SUSP", "SUSPENDED"}

# Front-loaded weights for the last-N-games weighted average, most recent first.
RECENCY_WEIGHTS = [0.4, 0.3, 0.2, 0.1]

# EWMA (alternate projection model): 5-game lookback, current season (2026+)
# only -- no snap-count layer, per explicit simplification. Below this many
# games of real current-season data, EWMA falls back to the preseason
# per-game projection (or DK's average) as a placeholder.
EWMA_SPAN = 5

# Standing strategy preference (see reference/dfs_strategy_notes.md): keep
# fading these offenses -- whichever DST plays one of these teams gets
# flagged as a guaranteed top strategy candidate regardless of its raw
# projection. Re-evaluate periodically (per the notes file's "heat check"
# condition) rather than treating this as permanent.
FADE_OFFENSES = set()  # was {"CLE"} -- retired 2026-09-23 per Jake: "it didn't work"

# weekly_stats_*.csv tags running backs as "HB" (and occasionally "FB"),
# not DK's "RB" -- this normalizes so the position-based lookups below
# (which key off DK's own position codes) line up correctly.
POSITION_ALIASES = {"HB": "RB", "FB": "RB"}


# Maps every team abbreviation/nickname spelling we've seen from DK or PFF
# (lowercased) to one canonical code, so a DST matches across sources
# regardless of which style either side uses (PFF uses some non-standard
# abbreviations like ARZ/BLT/HST/LA that don't match DK's more standard ones).
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


def dst_key(team_or_abbrev):
    return "dst:" + team_canon(team_or_abbrev)


def normalize_name(name):
    name = name.strip().lower()
    name = re.sub(r"[.'\"]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def find_col(header, candidates):
    lower_map = {h.lower(): h for h in header}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def load_salaries(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        cols = {k: find_col(header, v) for k, v in SALARY_COLS.items()}
        OPTIONAL_COLS = {"dk_avg", "status", "draft_alerts", "game_start"}
        missing = [k for k, v in cols.items() if v is None and k not in OPTIONAL_COLS]
        if missing:
            raise SystemExit(
                f"Couldn't find expected columns {missing} in {path}. "
                f"Actual header was: {header}. Adjust SALARY_COLS in this "
                f"script to match."
            )
        players = []
        excluded_count = 0
        for row in reader:
            position = row[cols["position"]].strip()
            team = row[cols["team"]].strip()
            name = row[cols["name"]].strip()
            # DST match key is the team, not the display name (DK shows a
            # nickname like "Chargers" that won't match anything else).
            key = dst_key(team) if position == "DST" else normalize_name(name)
            status_val = (row.get(cols["status"], "") if cols["status"] else "") or ""
            if status_val.strip().upper() in EXCLUDED_STATUSES:
                excluded_count += 1
                continue
            players.append({
                "name": name,
                "norm_name": key,
                "position": position,
                "team": team,
                "salary": int(row[cols["salary"]]),
                "game_info": row[cols["game_info"]] if cols["game_info"] else "",
                "game_start": row.get(cols["game_start"], "") if cols["game_start"] else "",
                "dk_avg": float(row[cols["dk_avg"]]) if cols["dk_avg"] and row.get(cols["dk_avg"]) else None,
                "status": status_val,
                "draft_alerts": row.get(cols["draft_alerts"], "") if cols["draft_alerts"] else "",
            })
        if excluded_count:
            print(f"[note] excluded {excluded_count} player(s) marked OUT/IR by DK -- "
                  f"not in projections, tables, or the optimizer pool.")
        return players


def _zero_fill_current_season(history, player_team, schedule, upcoming_week):
    """
    Per Jake, 2026-09-26 (confirmed via Josh Cameron: DK's own
    AvgPointsPerGame was exactly half our SeasonAvgDKPts, matching a real
    zero-point Week 2 -- 10 offensive snaps, 0 targets, per DK's own News
    feed -- that our data had NO ROW for at all): PFF's per-week facet
    reports (passing/rushing/receiving summary) only include a player if
    they recorded a real stat that week -- a catch, a carry, a target. A
    player who played but was never involved gets no row, not a zero-point
    row, and the old behavior silently skipped that week entirely rather
    than counting it as a real zero -- inflating the average for exactly
    the sporadic-usage, boom-or-bust players this matters most for.

    Fix, matching DK's own logic: once a player has at least one real
    current-season game on record, any LATER completed week where their
    team played (per team_schedule) but they have no row counts as a real
    zero. Never backfill a week before their first appearance (they may not
    have been active/rostered yet -- "0s only starting from the first game
    with any data", not before) or a bye week (no game happened to score a
    zero in).
    """
    for key, weeks in history.items():
        real_weeks = {w for w, _ in weeks if w >= 1}
        if not real_weeks:
            continue
        first_week = min(real_weeks)
        team = player_team.get(key)
        if not team:
            continue
        for wk in range(first_week, upcoming_week):
            if wk in real_weeks:
                continue
            if (team, wk) not in schedule:
                continue  # bye week, or that week's schedule was never recorded -- don't guess
            weeks.append((wk, 0.0))
    for key in history:
        history[key].sort(key=lambda t: t[0])


def load_history(season, upcoming_week, schedule=None):
    """
    Builds history from the current season's completed weeks, and -- when
    there aren't many of those yet (e.g. week 1, or even weeks 1-3) --
    blends in last season's full year as a baseline so early-season
    projections aren't just guessing. Prior-season games are tagged with a
    week number far in the past so they always sort as "oldest" relative to
    any current-season game; as real current-season data accumulates, the
    recency-weighted average naturally shifts weight onto it (it only ever
    looks at the last 4 games regardless of season), while the season
    average still blends across both until enough current-season games
    exist. This is a simplification, not a true multi-season model -- a
    player who's a totally different role/situation this year will look
    off until a few real 2026 games are in.
    """
    history = {}
    player_team = {}

    prior_path = DATA_DIR / f"weekly_stats_{season - 1}.csv"
    if prior_path.exists():
        with open(prior_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = dst_key(row["team"]) if row.get("position") == "DST" else normalize_name(row["player_name"])
                # offset well below any real week number so these always sort first
                fake_week = int(row["week"]) - 1000
                history.setdefault(key, []).append((fake_week, float(row["dk_points"])))

    cur_path = DATA_DIR / f"weekly_stats_{season}.csv"
    if cur_path.exists():
        with open(cur_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                week = int(row["week"])
                if week >= upcoming_week:
                    continue  # never use future/current-week data as history
                key = dst_key(row["team"]) if row.get("position") == "DST" else normalize_name(row["player_name"])
                history.setdefault(key, []).append((week, float(row["dk_points"])))
                player_team[key] = team_canon(row["team"])

    if not history:
        print(f"[warn] no history found in {cur_path} or {prior_path} -- "
              f"projections will fall back to DK's own AvgPointsPerGame for everyone.")

    if schedule:
        _zero_fill_current_season(history, player_team, schedule, upcoming_week)

    for key in history:
        history[key].sort(key=lambda t: t[0])  # oldest -> newest
    return history


def load_current_season_only(season, upcoming_week, schedule=None):
    """
    Same shape as load_history()'s return value, but deliberately does NOT
    blend in the prior season -- the EWMA model is "start fresh 2026 only"
    per explicit instruction, so a player's EWMA should only ever reflect
    real current-season games, never last year's.
    """
    history = {}
    player_team = {}
    cur_path = DATA_DIR / f"weekly_stats_{season}.csv"
    if cur_path.exists():
        with open(cur_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                week = int(row["week"])
                if week >= upcoming_week:
                    continue
                key = dst_key(row["team"]) if row.get("position") == "DST" else normalize_name(row["player_name"])
                history.setdefault(key, []).append((week, float(row["dk_points"])))
                player_team[key] = team_canon(row["team"])
    if schedule:
        _zero_fill_current_season(history, player_team, schedule, upcoming_week)
    for key in history:
        history[key].sort(key=lambda t: t[0])
    return history


def load_espn_depth_chart():
    """
    {(team, position): [(rank, norm_name), ...]} from data/espn_depthchart_current.csv
    (pull_espn_depthchart.py). Returns {} if that file doesn't exist yet --
    this is an optional enhancement, not a hard requirement.
    """
    path = DATA_DIR / "espn_depthchart_current.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rank = int(row["Rank"])
            except (KeyError, ValueError):
                continue
            key = (team_canon(row["Team"]), row["PositionAbbrev"])
            out.setdefault(key, []).append((rank, normalize_name(row["PlayerName"])))
    for key in out:
        out[key].sort(key=lambda t: t[0])
    return out


def load_espn_injury_status():
    """{(team, norm_name): lowercased InjuryStatus} from data/espn_status_current.csv
    (pull_espn_status.py). Returns {} if that file doesn't exist yet."""
    path = DATA_DIR / "espn_status_current.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (team_canon(row["Team"]), normalize_name(row["Name"]))
            out[key] = (row.get("InjuryStatus") or "").strip().lower()
    return out


# A backup with 0 real 2026 games gets whatever raw number the preseason
# projection or DK average hands over -- but that number assumed he'd be
# playing meaningful snaps somewhere. If he's actually a clipboard-holder
# behind a healthy, currently-active starter (confirmed via the ESPN depth
# chart + live injury status, both now pulled by pull_espn_depthchart.py /
# pull_espn_status.py), that assumption is false and the projection is a
# trap. Real example that surfaced this, 2026-09-25: Justin Fields (KC's
# real $4,000 backup QB behind a fully healthy Patrick Mahomes, 0 games in
# 2026, blank DK average) inherited a 17.06 preseason-fallback projection
# and got picked by the salary-cap optimizer over Mahomes' real 22.43 --
# a value trap, not a real find. Heavily discounted rather than zeroed
# entirely, since backups do occasionally see mop-up/garbage-time series.
BACKUP_BEHIND_HEALTHY_STARTER_DISCOUNT = 0.15


def is_backup_behind_healthy_starter(depth_chart, injury_status, team, position, norm_name):
    # Uses "lowest rank in this group" as the starter, not the literal
    # number 1 -- ESPN's site API doesn't expose an explicit per-athlete
    # rank field (confirmed live 2026-09-23), so pull_espn_depthchart.py
    # falls back to list order within each position group; comparing by
    # relative order (entries are pre-sorted ascending) is robust to any
    # numbering-offset bug the way a hardcoded `== 1` check isn't (this bit
    # us for real, 2026-09-25: a bug in that fallback numbering meant no QB
    # entry had rank exactly 1 for any team, so this check silently never
    # fired at all until both bugs were found and fixed together).
    entries = depth_chart.get((team, position))
    if not entries:
        return False
    starter_rank, starter_name = entries[0]
    if starter_name == norm_name:
        return False  # this player IS the starter
    if not any(n == norm_name for _, n in entries):
        return False  # not found in this team's depth chart at all
    starter_status = injury_status.get((team, starter_name), "")
    return starter_status == ""  # blank InjuryStatus = no designation = presumed healthy


def load_position_points_by_team_week(season, upcoming_week):
    """
    Reads weekly_stats_{season}.csv and sums DK points by (team, week,
    position) for every completed week (< upcoming_week).

    This generalizes what used to be a DST-only "dst_pts_by_team_week"
    dict: for a DST row the total is just that team's DST score for the
    week (a team only fields one DST, so summing is a no-op), and for an
    offensive position it's the combined DK points every player at that
    position put up against whichever defense they played that week --
    i.e. "how much did the opposing DST allow to this position, in the
    week they played." Both are exactly what the "Opp Last Wk" / "Opp
    Last 3 Wks" columns need, for every position, not just DST.
    """
    totals = {}
    cur_path = DATA_DIR / f"weekly_stats_{season}.csv"
    if not cur_path.exists():
        return totals
    with open(cur_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            week = int(row["week"])
            if week >= upcoming_week:
                continue  # never use future/current-week data
            pos = POSITION_ALIASES.get(row["position"], row["position"])
            if pos not in ("QB", "RB", "WR", "TE", "DST"):
                continue  # not a DK-relevant position (C, CB, P, etc.)
            team = team_canon(row["team"])
            pts_raw = row.get("dk_points")
            pts = float(pts_raw) if pts_raw not in (None, "") else 0.0
            key = (team, week, pos)
            totals[key] = totals.get(key, 0.0) + pts
    return totals


def compute_ewma(points, span=EWMA_SPAN):
    """
    points: list of DK points, oldest -> newest, for ONE player, current
    season only. Returns None if fewer than `span` games exist yet (caller
    should use the placeholder fallback in that case).

    5-game lookback: only the most recent `span` games are used at all (not
    a running EWMA over the whole season) -- weights decay exponentially
    within that window, most recent game weighted heaviest. alpha = 2/(span+1)
    matches pandas' .ewm(span=span, adjust=False) convention.
    """
    if len(points) < span:
        return None
    window = points[-span:]  # oldest -> newest, length == span
    alpha = 2 / (span + 1)
    ewma = window[0]
    for pt in window[1:]:
        ewma = alpha * pt + (1 - alpha) * ewma
    return round(ewma, 2)


def project(games):
    """games: list of (week, dk_points) sorted oldest->newest.
    Returns (season_avg, l4_weighted, n_games, floor, stdev)."""
    if not games:
        return None, None, 0, None, None
    pts = [p for _, p in games]
    season_avg = sum(pts) / len(pts)

    last_n = pts[-4:]
    weights = RECENCY_WEIGHTS[: len(last_n)]
    # weights list is front-loaded most-recent-first; last_n is oldest->newest,
    # so reverse last_n to line up with weights.
    weighted_pairs = list(zip(reversed(last_n), weights))
    weight_sum = sum(w for _, w in weighted_pairs)
    l4_weighted = sum(p * w for p, w in weighted_pairs) / weight_sum

    stdev = None
    floor = None
    if len(pts) >= 2:
        mean = season_avg
        variance = sum((x - mean) ** 2 for x in pts) / (len(pts) - 1)
        stdev = variance ** 0.5
        floor = max(0.0, season_avg - stdev)

    return round(season_avg, 2), round(l4_weighted, 2), len(games), \
        (round(floor, 2) if floor is not None else None), \
        (round(stdev, 2) if stdev is not None else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True, help="the upcoming slate's week number")
    ap.add_argument("--salaries", required=True, help="path to the DK salary export CSV")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # Sanity-check --week against today's actual date, using the saved NFL
    # calendar (reference/nfl_2026_calendar.csv) rather than assuming the
    # number you passed is right -- catches the "thought it was week 3, it
    # was actually week 2" class of mistake before it produces a stale slate.
    actual_wk = current_week()
    if actual_wk and actual_wk.week != args.week:
        print(f"[warn] today looks like NFL Week {actual_wk.week} (window {actual_wk.window_start} "
              f"to {actual_wk.window_end}), but you're building --week {args.week}. "
              f"Continuing anyway -- this is fine if that's intentional (e.g. building ahead), "
              f"but double-check if it's not.")

    salaries = load_salaries(args.salaries)
    team_schedule.upsert_schedule(
        args.season, args.week, team_schedule.matchups_from_salaries(salaries, team_canon)
    )
    schedule = team_schedule.load_schedule(args.season)

    history = load_history(args.season, args.week, schedule=schedule)
    current_season_history = load_current_season_only(args.season, args.week, schedule=schedule)

    # {(team, week, position): summed dk_points}, 2026-only -- powers the
    # generalized "Last Wk" / "Last 3 Wks" / "Opp Last Wk" / "Opp Last 3
    # Wks" columns below, for every position (originally DST-only).
    pos_pts_by_team_week = load_position_points_by_team_week(args.season, args.week)
    preseason = load_preseason_projections()
    if preseason:
        print(f"[note] loaded {len(preseason)} preseason per-game projections "
              f"(used as the fallback for anyone with 0 games of real history).")
    offense_pcts, defense_pcts = build_matchup_context(args.season, args.week)
    depth_chart = load_espn_depth_chart()
    injury_status = load_espn_injury_status()
    if not depth_chart or not injury_status:
        print("[note] espn_depthchart_current.csv / espn_status_current.csv not found (or empty) -- "
              "skipping the healthy-backup discount check (run pull_espn_depthchart.py / "
              "pull_espn_status.py first to enable it). A 0-2026-games backup behind a healthy "
              "starter will get the full undiscounted preseason/DK-average fallback value until then.")

    out_rows = []
    for p in salaries:
        games = history.get(p["norm_name"], [])
        season_avg, l4_weighted, n_games, floor, stdev = project(games)

        preseason_pts = preseason.get(p["norm_name"])

        # Alternate model: EWMA of DK points, 5-game lookback, 2026-only.
        games_2026 = current_season_history.get(p["norm_name"], [])
        n_2026 = len(games_2026)
        ewma_val = compute_ewma([pt for _, pt in games_2026])
        if ewma_val is not None:
            ewma_projection = ewma_val
            ewma_notes = f"EWMA of last {EWMA_SPAN} 2026 games"
        elif preseason_pts is not None:
            ewma_projection = preseason_pts
            ewma_notes = f"placeholder (only {n_2026} 2026 game(s)) -- used preseason projection"
        elif p["dk_avg"] is not None:
            ewma_projection = p["dk_avg"]
            ewma_notes = f"placeholder (only {n_2026} 2026 game(s)) -- used DK's AvgPointsPerGame"
        else:
            ewma_projection = 0.0
            ewma_notes = "no EWMA data, no preseason projection, no DK average -- needs manual look"
        team_c = team_canon(p["team"])
        is_clipboard_backup = (
            n_2026 == 0
            and p["position"] in ("QB", "RB", "WR", "TE")
            and is_backup_behind_healthy_starter(depth_chart, injury_status, team_c, p["position"], p["norm_name"])
        )
        if is_clipboard_backup:
            ewma_projection = round(ewma_projection * BACKUP_BEHIND_HEALTHY_STARTER_DISCOUNT, 2)
            ewma_notes += " -- discounted, backup behind a healthy starter (not expected to play meaningful snaps)"
        ewma_projection = round(ewma_projection, 2)
        ewma_value = round(ewma_projection / (p["salary"] / 1000), 3) if p["salary"] else 0

        # Require at least 1 game of CURRENT-SEASON (2026) data before trusting
        # season_avg/l4_weighted at face value -- those are computed from
        # load_history()'s blended prior+current season data (n_games), which
        # can be entirely stale prior-season games (possibly on a different
        # team) for a player with zero 2026 games. Per Jake, 2026-09-23:
        # "since we are in the season. require 1 game of data in 2026 season."
        if n_2026 >= 1 and n_games >= 3:
            projection = round(0.5 * season_avg + 0.5 * l4_weighted, 2)
            notes = f"{n_games} games of PFF history"
        elif n_2026 >= 1 and n_games > 0:
            projection = season_avg
            notes = f"only {n_games} game(s) of history -- low confidence"
        elif preseason_pts is not None:
            projection = preseason_pts
            notes = "no 2026 game history -- used PFF preseason season-long projection / games"
        elif p["dk_avg"] is not None:
            projection = p["dk_avg"]
            notes = "no 2026 game history or preseason projection match -- used DK's AvgPointsPerGame"
        else:
            projection = 0.0
            notes = "no history, no preseason projection, and no DK average -- needs manual look"

        if is_clipboard_backup:
            projection = round(projection * BACKUP_BEHIND_HEALTHY_STARTER_DISCOUNT, 2)
            notes = "[clipboard backup behind a healthy starter -- discounted] " + notes

        value = round(projection / (p["salary"] / 1000), 3) if p["salary"] else 0

        # Floor: only meaningful with >= 3 games of history; below that the
        # sample stdev is too noisy to call it a "floor" with any confidence.
        floor_val = floor if (floor is not None and n_games >= 3 and n_2026 >= 1) else ""

        # Surface injury/news signal in Notes too so it's visible even when
        # a column-blind view (like a quick CSV skim) is all someone checks.
        alert_bits = [b for b in [p.get("status"), p.get("draft_alerts")] if b]
        if alert_bits:
            notes = f"[{' / '.join(alert_bits)}] " + notes

        # Matchup score: DIY approximation of PFF's matchup chart (see
        # matchup_score.py for methodology and caveats). Skipped for DST
        # entirely -- the concept is offense-vs-defense, doesn't apply to DST.
        own_canon = team_canon(p["team"])
        opponent = parse_opponent(p["game_info"], own_canon, team_canon)

        # Matchup score: DIY approximation of PFF's matchup chart (see
        # matchup_score.py for methodology and caveats). Skipped for DST
        # entirely -- the concept is offense-vs-defense, doesn't apply to DST.
        matchup_score = matchup_label = matchup_off_pct = matchup_def_pct = None
        fade_target = False
        if p["position"] != "DST":
            if opponent:
                matchup_score, matchup_label, matchup_off_pct, matchup_def_pct = score_matchup(
                    p["position"], offense_pcts, defense_pcts, p["norm_name"], opponent
                )
        else:
            if opponent in FADE_OFFENSES:
                fade_target = True
                notes = f"[standing fade target -- opponent {opponent}] " + notes

        # Rolling DK-points stats, 2026 data only -- generalized to every
        # position (originally DST-only, corrected per instruction): own
        # last-game / rolling-3-game points for everyone, plus what the
        # upcoming opponent's defense gave up to THIS SPECIFIC position in
        # their last (up to) 3 games. Sparse early in the season and fills
        # in as more weeks are pulled, same philosophy as the EWMA columns.
        pts_last_wk = pts_avg3 = ""
        opp_pos_allowed_last_wk = opp_pos_allowed_avg3 = ""

        own_games = current_season_history.get(p["norm_name"], [])  # [(week, pts), ...] oldest->newest
        if own_games:
            pts_last_wk = own_games[-1][1]
            last3 = [pts for _, pts in own_games[-3:]]
            pts_avg3 = round(sum(last3) / len(last3), 2)

        if opponent:
            # Opponent's own last-3 games (from the schedule tracker), most
            # recent first -- for each, look up what THAT week's opposing
            # team put up at p's own position, i.e. what the opponent's
            # defense "gave up" to this position recently.
            opp_weeks_played = sorted(
                (wk for (team, wk) in schedule if team == opponent and wk < args.week),
                reverse=True,
            )[:3]
            allowed_values = []
            for wk in opp_weeks_played:
                opp_of_opponent = schedule.get((opponent, wk))
                if opp_of_opponent is None:
                    continue
                val = pos_pts_by_team_week.get((opp_of_opponent, wk, p["position"]))
                if val is not None:
                    allowed_values.append((wk, val))
            if allowed_values:
                opp_pos_allowed_last_wk = allowed_values[0][1]  # already sorted most-recent-first
                opp_pos_allowed_avg3 = round(sum(v for _, v in allowed_values) / len(allowed_values), 2)

        out_rows.append({
            "Name": p["name"], "Team": p["team"], "Position": p["position"],
            "Salary": p["salary"], "GameInfo": p["game_info"], "GameStart": p.get("game_start", ""),
            "GamesOfHistory": n_games,
            "SeasonAvgDKPts": season_avg if season_avg is not None else "",
            "L4WeightedProj": l4_weighted if l4_weighted is not None else "",
            "Projection": projection, "Value": value,
            "Floor": floor_val,
            "StdDev": stdev if stdev is not None else "",
            "Status": p.get("status", ""),
            "DraftAlerts": p.get("draft_alerts", ""),
            "MatchupScore": matchup_score if matchup_score is not None else "",
            "MatchupLabel": matchup_label if matchup_label is not None else "",
            "Games2026": n_2026,
            "EwmaProjection": ewma_projection,
            "EwmaValue": ewma_value,
            "EwmaNotes": ewma_notes,
            "FadeTarget": fade_target,
            "PtsLastWk": pts_last_wk,
            "PtsAvg3": pts_avg3,
            "OppPosAllowedLastWk": opp_pos_allowed_last_wk,
            "OppPosAllowedAvg3": opp_pos_allowed_avg3,
            "Notes": notes,
        })

    out_rows.sort(key=lambda r: r["Projection"], reverse=True)

    out_path = Path(args.out) if args.out else DATA_DIR / f"projections_wk{args.week}.csv"
    DATA_DIR.mkdir(exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"Wrote {len(out_rows)} player projections -> {out_path}")
    no_history = sum(1 for r in out_rows if r["GamesOfHistory"] == 0)
    if no_history:
        print(f"[note] {no_history} players had no PFF history match "
              f"(new to league, name-matching miss, or bye-week team). "
              f"Check the Notes column.")

    real_ewma = sum(1 for r in out_rows if r["Games2026"] >= EWMA_SPAN)
    print(f"[note] EWMA (2026, {EWMA_SPAN}-game lookback): {real_ewma} player(s) have "
          f"{EWMA_SPAN}+ real 2026 games so far; the rest are using a placeholder "
          f"(preseason projection or DK average) in EwmaProjection until they do.")


if __name__ == "__main__":
    main()
