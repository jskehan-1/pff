"""
Lightweight team-vs-team schedule tracker for 2026, built incrementally from
each week's DK salary export's "Game Info" column every time
build_projections.py runs. This exists because the weekly stats pull
(pull_weekly_stats.py) doesn't record who played whom -- just each team's own
box score -- but the new DST "what has this opponent's offense given up to
DSTs recently" columns need exactly that (team-by-week opponent lookup).

Usage (called internally by build_projections.py every run; can also be run
standalone to backfill history from salary files you already have):
    python scripts\\team_schedule.py --backfill
"""
import argparse
import csv
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SCHEDULE_PATH = DATA_DIR / "team_schedule_2026.csv"


def matchups_from_salaries(salaries, team_canon_fn):
    """salaries: the list of player dicts load_salaries() in build_projections.py
    already produces (each has "team" and "game_info"). Returns {team: opponent}."""
    matchups = {}
    for p in salaries:
        m = re.search(r"([A-Za-z]{2,4})\s*@\s*([A-Za-z]{2,4})", p.get("game_info") or "")
        if not m:
            continue
        t1, t2 = team_canon_fn(m.group(1)), team_canon_fn(m.group(2))
        matchups[t1] = t2
        matchups[t2] = t1
    return matchups


def upsert_schedule(season, week, matchups):
    """matchups: {team: opponent}. Upserts into team_schedule_2026.csv,
    replacing any existing rows for this (season, week) -- safe to call every
    time build_projections.py runs, even for a week already recorded."""
    if not matchups:
        return
    DATA_DIR.mkdir(exist_ok=True)
    existing = []
    if SCHEDULE_PATH.exists():
        with open(SCHEDULE_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not (int(row["season"]) == season and int(row["week"]) == week):
                    existing.append(row)
    new_rows = [{"season": season, "week": week, "team": t, "opponent": o}
                for t, o in sorted(matchups.items())]
    with open(SCHEDULE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["season", "week", "team", "opponent"])
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(new_rows)


def load_schedule(season, path=SCHEDULE_PATH):
    """Returns {(team, week): opponent} for the given season."""
    result = {}
    if not path.exists():
        return result
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if int(row["season"]) == season:
                result[(row["team"], int(row["week"]))] = row["opponent"]
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                     help="rebuild team_schedule_2026.csv from every DKSalaries_wk*.csv "
                          "already sitting in data/dk_salaries/")
    ap.add_argument("--season", type=int, default=2026)
    args = ap.parse_args()

    if not args.backfill:
        print("Nothing to do without --backfill (this module is normally called "
              "automatically by build_projections.py).")
        return

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_projections import team_canon, load_salaries  # noqa: E402

    salary_dir = DATA_DIR / "dk_salaries"
    for f in sorted(salary_dir.glob("DKSalaries_wk*.csv")):
        m = re.search(r"wk(\d+)", f.name)
        if not m:
            continue
        wk = int(m.group(1))
        salaries = load_salaries(f)
        matchups = matchups_from_salaries(salaries, team_canon)
        if matchups:
            upsert_schedule(args.season, wk, matchups)
            print(f"Week {wk}: recorded {len(matchups)//2} matchups from {f.name}")
    print(f"Backfill complete -> {SCHEDULE_PATH}")


if __name__ == "__main__":
    main()
