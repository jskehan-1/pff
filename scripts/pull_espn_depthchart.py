"""
Pull each team's current depth chart (starter/backup rank per position) from
ESPN's public site API -- no API key needed.

Used by gold_mine_alerts.py to answer "who's the #2 at this position for this
team" once a starter's status flips to Out/Doubtful (per pull_espn_status.py)
after Tinker Tinker's DK salaries have already locked -- Jake's "gold mine"
scenario: the backup often gets much more usage than his DK salary/history
would suggest, because the market hasn't repriced him yet.

Depth charts change far less often than injury status day-to-day, so this
doesn't need to run as often as pull_espn_status.py -- once a week (e.g.
alongside the Tuesday auto_weekly.ps1 pull) is normally enough, though
daily_status_check.ps1 also refreshes it since it's a cheap, fast call.

Usage:
    python scripts\\pull_espn_depthchart.py --season 2026 --week 3

Writes:
    data\\espn_depthchart\\espn_depthchart_wk{week}_{YYYYMMDD}.csv (dated)
    data\\espn_depthchart_current.csv                               (latest)

Run with --inspect first if the parsed rows look wrong -- prints one raw
formation record so the key names below can be corrected against the real
response (confirmed live via research on 2026-09-23, not yet run against
this project's own network).
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from espn_client import get as espn_get, iter_teams  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEPTH_DIR = DATA_DIR / "espn_depthchart"

FIELDNAMES = ["Team", "PositionAbbrev", "Rank", "PlayerName", "FormationName", "FetchedAtUtc"]

# We only care about the "skill" positions for DFS purposes -- offensive line
# / defensive depth chart noise is filtered out. ESPN's position abbreviations
# for these are stable (QB/RB/WR/TE); adjust here if --inspect shows different
# codes (e.g. "HB" instead of "RB" on some formations).
KEEP_POSITIONS = {"QB", "RB", "WR", "TE"}


def pull_team_depthchart(team_code, espn_id, inspect=False):
    resp = espn_get(f"/teams/{espn_id}/depthcharts")
    rows = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    formations = resp.get("depthchart", resp.get("items", []))
    printed_sample = False
    for formation in formations:
        formation_name = formation.get("name", "")
        positions = formation.get("positions", {})
        pos_iter = positions.values() if isinstance(positions, dict) else positions
        for pos_group in pos_iter:
            if inspect and not printed_sample:
                print(f"\n--- sample position-group record ({team_code}, formation={formation_name}) ---")
                print(json.dumps(pos_group, indent=2)[:2000])
                printed_sample = True

            pos_info = pos_group.get("position") or {}
            abbrev = (pos_info.get("abbreviation") or "").upper()
            if abbrev not in KEEP_POSITIONS:
                continue
            for local_idx, athlete_entry in enumerate(pos_group.get("athletes", [])):
                # Two shapes seen in the wild: a plain athlete dict, or a
                # wrapper like {"athlete": {...}, "rank": N}. Handle both.
                rank = athlete_entry.get("rank")
                athlete = athlete_entry.get("athlete", athlete_entry)
                name = athlete.get("displayName", "") if isinstance(athlete, dict) else ""
                if not name:
                    continue
                if rank is None:
                    # BUG FIXED 2026-09-25 (Jake caught it via Justin Fields
                    # showing rank 9 behind Mahomes): this used to be
                    # `len(rows) + 1`, the running total across the WHOLE
                    # team's output so far -- so a QB group processed after
                    # several other position groups inherited a rank in the
                    # high single digits instead of starting at 1. It has to
                    # reset per position group -- this athlete's own index
                    # within pos_group's athletes list, which IS the
                    # depth-chart order for this position specifically.
                    rank = local_idx + 1
                rows.append({
                    "Team": team_code,
                    "PositionAbbrev": abbrev,
                    "Rank": rank,
                    "PlayerName": name,
                    "FormationName": formation_name,
                    "FetchedAtUtc": now,
                })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    all_rows = []
    for i, (team_code, espn_id) in enumerate(iter_teams()):
        print(f"[{i+1}/32] pulling depth chart for {team_code} (espn id {espn_id})...")
        try:
            rows = pull_team_depthchart(team_code, espn_id, inspect=args.inspect)
        except Exception as e:
            print(f"  [warn] failed for {team_code}: {e}", file=sys.stderr)
            rows = []
        all_rows.extend(rows)
        if args.inspect and all_rows:
            return
        time.sleep(args.delay)

    if not all_rows:
        print("[warn] no rows pulled at all -- something's wrong upstream (network, schema change). Nothing written.")
        return

    DEPTH_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    dated_path = DEPTH_DIR / f"espn_depthchart_wk{args.week}_{today}.csv"
    current_path = DATA_DIR / "espn_depthchart_current.csv"

    for out_path in (dated_path, current_path):
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(all_rows)

    print(f"\nWrote {len(all_rows)} depth-chart rows (QB/RB/WR/TE only) -> {dated_path} and {current_path}")


if __name__ == "__main__":
    main()
