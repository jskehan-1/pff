"""
Pull per-player-per-week snap counts from PFF (a much cleaner opportunity
signal than raw fantasy points for detecting "backup got way more usage than
expected" situations -- Jake's request, 2026-09-23: "PFF should have snap
counts which are better proxies than depth chart... QB only cares about snap
counts, RB/WR/TE would count targets+carries/snap count").

This is a PER-PLAYER PFF endpoint (/v1/player/snaps/summary requires a single
player_id -- confirmed via the openapi spec, there is no bulk/facet-style
snap-count leaderboard like passing/rushing/receiving summary have). So this
script:
  1. Reads data/weekly_stats_{season}.csv for the requested week(s) to get the
     player_id/name/team/position of everyone who actually recorded offensive
     stats that week (player_id was added to that file in this same update --
     re-run pull_weekly_stats.py for any older week first if it predates that
     change, or this will just skip rows with a blank player_id).
  2. Restricts to QB/RB/WR/TE (DST/OL don't matter for the "backup value
     spike" use case).
  3. Calls /v1/player/snaps/summary once per unique player_id for the
     requested week (PFFClient already backs off on the account's 100
     calls/minute limit, so this is safe to run even for ~150-250 players).
  4. Appends to a running data/snap_counts_{season}.csv.

IMPORTANT: run --inspect first, exactly like pull_weekly_stats.py -- this
endpoint's exact response shape (whether snap_counts_total lives at
snaps[0].snap_counts.snap_counts_total or something else) was confirmed only
via API-doc research, never against a real live call from inside this
project. If --inspect's printed sample doesn't match what CANDIDATES expects
below, tell Claude the real shape and it'll fix this script.

Usage:
    python scripts\\pull_snap_counts.py --inspect --season 2026 --week 3
    python scripts\\pull_snap_counts.py --season 2026 --week 3
    python scripts\\pull_snap_counts.py --season 2026 --week 1-3
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pff_client import PFFClient  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "HB", "FB"}

FIELDNAMES = ["season", "week", "player_id", "player_name", "team", "position",
              "snap_counts_offense", "snap_counts_total",
              "snap_counts_pass", "snap_counts_run", "notes"]


def parse_weeks(week_arg):
    if "-" in week_arg:
        lo, hi = week_arg.split("-")
        return list(range(int(lo), int(hi) + 1))
    return [int(week_arg)]


def load_skill_players_for_week(season, week):
    path = DATA_DIR / f"weekly_stats_{season}.csv"
    if not path.exists():
        return []
    out = []
    seen = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if int(row["week"]) != week:
                continue
            pos = row.get("position", "")
            if pos not in SKILL_POSITIONS:
                continue
            pid = row.get("player_id", "")
            if not pid:
                continue  # older pull, predates player_id -- re-pull that week first
            key = (pid, week)
            if key in seen:
                continue
            seen.add(key)
            out.append({"player_id": pid, "player_name": row.get("player_name", ""),
                        "team": row.get("team", ""), "position": pos})
    return out


def extract_snap_counts(resp, season, week, inspect_label=None):
    """
    Confirmed live 2026-09-25 against a real response (Jake's machine,
    C.J. Stroud, week=2): the envelope is a single object, not a list --
    {"snaps": {"season": 2026, "snap_counts": {"offense": 80, "pass": 65,
    "run": 0, "run_block": 15, "defense": 0, ...}}} -- and there is NO "week"
    key anywhere in the payload. The response is already scoped to whatever
    `week` was passed as a request param, so there's nothing to filter by
    here; a missing/empty "snaps" object just means no data for that
    player/week (bye, DNP, injury) rather than a parsing failure.

    "offense" is the total offensive snaps played -- there's no separate
    "total" field, and it already equals pass + run_block + ... for the
    positions that matter here (QB/RB/WR/TE). That's the field
    build_backup_usage.py keys off of.
    """
    if inspect_label:
        print(f"\n--- sample /v1/player/snaps/summary response ({inspect_label}) ---")
        print(json.dumps(resp, indent=2)[:3000])

    snaps_obj = resp.get("snaps")
    if not isinstance(snaps_obj, dict):
        return None
    sc = snaps_obj.get("snap_counts")
    if not isinstance(sc, dict):
        return None

    offense = sc.get("offense")
    if offense is None:
        return None
    return {
        "snap_counts_offense": offense,
        "snap_counts_total": offense,
        "snap_counts_pass": sc.get("pass", ""),
        "snap_counts_run": sc.get("run", ""),
    }


def pull_week(client, league, season, week, inspect=False):
    players = load_skill_players_for_week(season, week)
    if not players:
        print(f"  [note] no skill-position players with a player_id found in "
              f"weekly_stats_{season}.csv for week {week} -- nothing to pull "
              f"(re-run pull_weekly_stats.py for this week first if it predates "
              f"the player_id column).")
        return []

    rows = []
    for i, p in enumerate(players):
        label = f"{p['player_name']} ({p['team']} {p['position']}), player_id={p['player_id']}" if inspect else None
        try:
            resp = client.get("/v1/player/snaps/summary",
                               params={"league": league, "season": season,
                                       "player_id": p["player_id"], "week": week})
        except RuntimeError as e:
            print(f"  [warn] snap pull failed for {p['player_name']}: {e}", file=sys.stderr)
            continue

        if inspect:
            extract_snap_counts(resp, season, week, inspect_label=label)
            return []

        sc = extract_snap_counts(resp, season, week)
        if sc is None:
            print(f"  [warn] no matching season/week entry in response for "
                  f"{p['player_name']} (player_id={p['player_id']}) -- skipping.", file=sys.stderr)
            continue

        rows.append({
            "season": season, "week": week, "player_id": p["player_id"],
            "player_name": p["player_name"], "team": p["team"], "position": p["position"],
            **sc, "notes": "",
        })

        if (i + 1) % 25 == 0:
            print(f"  ...{i + 1}/{len(players)} players pulled")

    return rows


def upsert_csv(rows, season):
    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"snap_counts_{season}.csv"

    existing = []
    weeks_touched = {r["week"] for r in rows}
    if out_path.exists():
        with open(out_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if int(r["week"]) not in weeks_touched:
                    existing.append(r)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows (weeks {sorted(weeks_touched)}) -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="nfl")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", required=True, help="single week, e.g. 3, or a range, e.g. 1-3")
    ap.add_argument("--inspect", action="store_true",
                     help="print one raw sample record and exit (no CSV written)")
    args = ap.parse_args()

    client = PFFClient()
    weeks = parse_weeks(args.week)

    for wk in weeks:
        print(f"Pulling {args.league} season {args.season} week {wk} snap counts...")
        rows = pull_week(client, args.league, args.season, wk, inspect=args.inspect)
        if args.inspect:
            return
        if rows:
            upsert_csv(rows, args.season)


if __name__ == "__main__":
    main()
