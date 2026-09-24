"""
Pull weekly player/team stats from PFF and convert them to DK Classic
fantasy points, appending to a running data/weekly_stats_{season}.csv.

Confirmed against the real openapi.json spec (2026-09-07):
  - /v1/facet/passing/summary   -> {"passing_summary": [...]}
  - /v1/facet/rushing/summary   -> {"rushing_summary": [...]}
  - /v1/facet/receiving/summary -> {"receiving_summary": [...]}
  - /v1/facet/defense/summary   -> {"defense_summary": [...]}
  - /v1/teams/summary           -> {"team_summary": [...]}, confirmed real
    fields include points_allowed and points_scored directly (no need to
    derive from /v1/games).

Each report row has a handful of pinned "identity" columns (player,
player_id, position, team, team_name, franchise_id, ...) plus a dynamic
set of stat columns that depend on "the report, the position, and the
caller's entitlement" (PFF's own words) -- there is NO fixed schema for
the stat columns, so this script cannot know their names in advance.

If a response includes a "restricted" key, that lists columns PFF withheld
from your account's entitlement -- this script prints it prominently so
you know immediately if a needed stat (e.g. passing yards) simply isn't
available on your tier, rather than silently defaulting to 0.

IMPORTANT: run --inspect first, every time you're not sure:
    python scripts\\pull_weekly_stats.py --inspect --season 2025 --week 1

Usage:
    python scripts\\pull_weekly_stats.py --season 2025 --week 1
    python scripts\\pull_weekly_stats.py --season 2025 --week 1-4
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pff_client import PFFClient  # noqa: E402
from dk_scoring import score_offense, score_dst  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Candidate field names to try, in order, for each stat. First match wins.
# These are still best-guesses for the DYNAMIC stat columns -- run --inspect
# and fix these once you see real column names for your entitlement tier.
CANDIDATES = {
    "pass_yds": ["yards", "passing_yards", "pass_yards", "total_yards"],
    "pass_td": ["touchdowns", "passing_touchdowns", "pass_touchdowns", "td"],
    "ints": ["interceptions", "ints", "interceptions_thrown"],
    "rush_yds": ["yards", "rushing_yards", "rush_yards", "total_yards"],
    "rush_td": ["touchdowns", "rushing_touchdowns", "rush_touchdowns"],
    "rec": ["receptions", "catches", "total_receptions"],
    "rec_yds": ["yards", "receiving_yards", "rec_yards", "total_yards"],
    "rec_td": ["touchdowns", "receiving_touchdowns", "rec_touchdowns"],
    "fumbles_lost": ["fumbles_lost", "fumbles"],
    "sacks": ["sacks", "total_sacks"],
    "def_ints": ["interceptions", "ints"],
    "def_fumble_rec": ["fumble_recoveries", "fumbles_recovered"],
    "points_allowed": ["points_allowed"],
    # PFF grade fields (0-100 scale) -- confirmed live field names. Used for
    # the matchup-score feature, not for DK point scoring.
    "grade_pass": ["grades_pass"],
    "grade_run": ["grades_run", "grades_offense"],
    "grade_route": ["grades_pass_route", "grades_offense"],
    "team_grade_pass_rush_def": ["grades_pass_rush_defense"],
    "team_grade_coverage_def": ["grades_coverage_defense"],
    "team_grade_run_def": ["grades_run_defense"],
}

_warned = set()


def field(record, stat_key, default=0):
    for key in CANDIDATES.get(stat_key, [stat_key]):
        if key in record and record[key] is not None:
            return record[key]
    if stat_key not in _warned:
        _warned.add(stat_key)
        print(
            f"  [warn] couldn't find '{stat_key}' on a record "
            f"(tried {CANDIDATES.get(stat_key)}). Available keys: "
            f"{sorted(record.keys())}. Using {default}. "
            f"Fix CANDIDATES in this script once you know the real field name.",
            file=sys.stderr,
        )
    return default


def parse_weeks(week_arg):
    if "-" in week_arg:
        lo, hi = week_arg.split("-")
        return list(range(int(lo), int(hi) + 1))
    return [int(week_arg)]


def get_report(client, path, envelope_key, league, season, week):
    resp = client.get(path, params={"league": league, "season": season, "week": week})
    restricted = resp.get("restricted")
    if restricted:
        print(f"  [!] {path} withheld columns for your entitlement: {restricted}",
              file=sys.stderr)
    return resp.get(envelope_key, [])


def pull_week(client, league, season, week, inspect=False):
    rows = []

    passing = get_report(client, "/v1/facet/passing/summary", "passing_summary", league, season, week)
    rushing = get_report(client, "/v1/facet/rushing/summary", "rushing_summary", league, season, week)
    receiving = get_report(client, "/v1/facet/receiving/summary", "receiving_summary", league, season, week)
    defense = get_report(client, "/v1/facet/defense/summary", "defense_summary", league, season, week)
    teams = client.get("/v1/teams", params={"league": league, "season": season}).get("teams", [])

    if inspect:
        for label, data in [
            ("passing/summary", passing), ("rushing/summary", rushing),
            ("receiving/summary", receiving), ("defense/summary", defense),
        ]:
            print(f"\n--- sample {label} record ---")
            print(json.dumps(data[0], indent=2) if data else "(empty -- either no games that week, or entitlement issue)")
        if teams:
            fid = teams[0].get("franchise_id")
            team_summary = client.get(
                "/v1/teams/summary",
                params={"league": league, "season": season, "week": week, "franchise_id": fid},
            ).get("team_summary", [])
            print(f"\n--- sample /v1/teams/summary record (franchise_id={fid}) ---")
            print(json.dumps(team_summary[0] if team_summary else team_summary, indent=2))
        return []

    # Merge offense by player_id (falls back to name+team if missing).
    by_player = {}

    def bucket(r):
        pid = r.get("player_id")
        k = pid if pid is not None else (r.get("player"), r.get("team"))
        if k not in by_player:
            by_player[k] = {
                "player_id": r.get("player_id", ""),
                "player_name": r.get("player", ""), "team": r.get("team", ""),
                "position": r.get("position", ""),
                "pass_yds": 0, "pass_td": 0, "ints": 0,
                "rush_yds": 0, "rush_td": 0,
                "rec": 0, "rec_yds": 0, "rec_td": 0,
                "fumbles_lost": 0, "two_pt": 0,
                "grade_pass": "", "grade_run": "", "grade_route": "",
            }
        return by_player[k]

    # NOTE on fumbles: PFF's "fumbles" field (only present on rushing/receiving
    # rows, confirmed live) counts ALL fumbles by that player, not just ones lost
    # to the opponent -- DK only docks points for fumbles LOST. This slightly
    # overstates the penalty on the rare play where a player recovers his own
    # fumble. QB fumbles aren't in the passing report at all and aren't captured
    # here -- a known, minor gap (sack-fumbles are the main case missed).
    for r in passing:
        p = bucket(r)
        p["pass_yds"] += field(r, "pass_yds")
        p["pass_td"] += field(r, "pass_td")
        p["ints"] += field(r, "ints")
        p["grade_pass"] = field(r, "grade_pass", "")

    for r in rushing:
        p = bucket(r)
        p["rush_yds"] += field(r, "rush_yds")
        p["rush_td"] += field(r, "rush_td")
        p["fumbles_lost"] += field(r, "fumbles_lost")
        p["grade_run"] = field(r, "grade_run", "")

    for r in receiving:
        p = bucket(r)
        p["rec"] += field(r, "rec")
        p["rec_yds"] += field(r, "rec_yds")
        p["rec_td"] += field(r, "rec_td")
        p["fumbles_lost"] += field(r, "fumbles_lost")
        p["grade_route"] = field(r, "grade_route", "")

    for p in by_player.values():
        pts = score_offense(
            pass_yds=p["pass_yds"], pass_td=p["pass_td"], ints=p["ints"],
            rush_yds=p["rush_yds"], rush_td=p["rush_td"],
            rec=p["rec"], rec_yds=p["rec_yds"], rec_td=p["rec_td"],
            fumbles_lost=p["fumbles_lost"], two_pt=p["two_pt"],
        )
        rows.append({
            "season": season, "week": week, **p, "dk_points": pts,
            "team_grade_pass_rush_def": "", "team_grade_coverage_def": "",
            "team_grade_run_def": "", "notes": "",
        })

    # DST: aggregate sacks/INTs/fumble recoveries per team from defense/summary,
    # points allowed from /v1/teams/summary (confirmed real field).
    team_def = {}
    for r in defense:
        team = r.get("team", "")
        d = team_def.setdefault(team, {"sacks": 0, "ints": 0, "fumble_rec": 0})
        d["sacks"] += field(r, "sacks")
        d["ints"] += field(r, "def_ints")
        d["fumble_rec"] += field(r, "def_fumble_rec")

    for t in teams:
        abbrev = t.get("abbreviation")
        fid = t.get("franchise_id")
        if not abbrev or fid is None:
            continue
        summary = client.get(
            "/v1/teams/summary",
            params={"league": league, "season": season, "week": week, "franchise_id": fid},
        ).get("team_summary", [])
        if not summary:
            # No game this week for this team -- bye week (or the game hasn't
            # been played/processed yet). Skip entirely rather than writing a
            # fake 0-point row that would drag down averages later.
            continue
        pa = field(summary[0], "points_allowed", None)
        if pa is None:
            # /v1/teams/summary returns a schedule-shaped placeholder (with
            # keys like lock_status/start but points_allowed/points_scored
            # both null) for a game that hasn't been played yet -- confirmed
            # live 2026-09-24 pulling a future week. Skip entirely rather
            # than writing a fake 0-point DST row that would then look like
            # a real "this defense scored 0 fantasy points" result to every
            # rolling-stat/average column downstream. Only pull a week's
            # actual results once its games have actually been played.
            continue
        d = team_def.get(abbrev, {"sacks": 0, "ints": 0, "fumble_rec": 0})
        pts = score_dst(sacks=d["sacks"], ints=d["ints"], fumble_rec=d["fumble_rec"], points_allowed=pa)
        rows.append({
            "season": season, "week": week, "player_id": "", "player_name": abbrev, "team": abbrev,
            "position": "DST",
            "pass_yds": 0, "pass_td": 0, "ints": 0, "rush_yds": 0, "rush_td": 0,
            "rec": 0, "rec_yds": 0, "rec_td": 0, "fumbles_lost": 0, "two_pt": 0,
            "grade_pass": "", "grade_run": "", "grade_route": "",
            "dk_points": pts,
            "team_grade_pass_rush_def": field(summary[0], "team_grade_pass_rush_def", ""),
            "team_grade_coverage_def": field(summary[0], "team_grade_coverage_def", ""),
            "team_grade_run_def": field(summary[0], "team_grade_run_def", ""),
            "notes": "points_allowed from /v1/teams/summary -- confirm it excludes points "
                     "off this team's own offensive turnovers (DK's rule) before fully trusting it",
        })

    return rows


def upsert_csv(rows, season):
    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"weekly_stats_{season}.csv"
    fieldnames = ["season", "week", "player_id", "player_name", "team", "position",
                  "pass_yds", "pass_td", "ints", "rush_yds", "rush_td",
                  "rec", "rec_yds", "rec_td", "fumbles_lost", "two_pt",
                  "grade_pass", "grade_run", "grade_route",
                  "team_grade_pass_rush_def", "team_grade_coverage_def", "team_grade_run_def",
                  "dk_points", "notes"]

    existing = []
    weeks_touched = {r["week"] for r in rows}
    if out_path.exists():
        with open(out_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if int(r["week"]) not in weeks_touched:
                    existing.append(r)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows (weeks {sorted(weeks_touched)}) -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="nfl")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", required=True, help="single week, e.g. 5, or a range, e.g. 1-4")
    ap.add_argument("--inspect", action="store_true",
                     help="print one raw sample record per endpoint and exit (no CSV written)")
    args = ap.parse_args()

    client = PFFClient()
    weeks = parse_weeks(args.week)

    # Save after each week rather than only at the end -- a rate limit, a
    # network blip, or a bad week partway through a multi-week pull
    # shouldn't cost you the weeks that already succeeded.
    for wk in weeks:
        print(f"Pulling {args.league} season {args.season} week {wk}...")
        rows = pull_week(client, args.league, args.season, wk, inspect=args.inspect)
        if args.inspect:
            return
        upsert_csv(rows, args.season)


if __name__ == "__main__":
    main()
