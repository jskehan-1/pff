"""
Pull every NFL player's current roster status (Active/Out/Doubtful/
Questionable/IR/etc.) from ESPN's public site API -- no API key needed.

This is a REAL-TIME cross-check on top of DK's own Status/DraftAlerts field
(which diff_dk_status.py already tracks): ESPN's injury report is the
industry-standard source and sometimes updates before DK's own salary feed
reflects a change, which matters for catching a starter's status flipping to
Out/Doubtful AFTER Tinker Tinker's salaries have already locked for the week
-- see gold_mine_alerts.py, which consumes this file's "current" copy.

Usage:
    python scripts\\pull_espn_status.py --season 2026 --week 3

Writes:
    data\\espn_status\\espn_status_wk{week}_{YYYYMMDD}.csv   (dated snapshot,
        so diff_dk_status.py-style day-over-day diffing is possible later)
    data\\espn_status_current.csv                             (always
        overwritten with the latest pull -- this is what gold_mine_alerts.py
        reads)

Run with --inspect first if anything looks wrong: it prints one raw sample
player record so field names can be fixed here if ESPN's schema differs from
what's assumed below (confirmed live via research on 2026-09-23, but never
run against this project's own network before -- PFF/DK network calls are
made from Jake's machine for the same reason, so this is the same situation).
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
STATUS_DIR = DATA_DIR / "espn_status"

FIELDNAMES = ["Team", "Name", "Position", "Status", "StatusType", "InjuryStatus", "InjuryDetail", "FetchedAtUtc"]


def pull_team_roster(team_code, espn_id, inspect=False):
    resp = espn_get(f"/teams/{espn_id}/roster")
    rows = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    groups = resp.get("athletes", [])
    printed_sample = False
    for group in groups:
        for p in group.get("items", []):
            if inspect and not printed_sample:
                print(f"\n--- sample roster record ({team_code}) ---")
                print(json.dumps(p, indent=2)[:2000])
                printed_sample = True

            # IMPORTANT (confirmed live 2026-09-23 via Jake's own run): the
            # top-level "status" field is a ROSTER category, not an injury
            # designation -- real values seen: "Active", "Day-To-Day",
            # "Practice Squad", "News" (e.g. a suspension). It is NOT
            # "Out"/"Doubtful"/"Questionable". The actual per-game injury
            # designation lives in injuries[0]["status"] instead (e.g.
            # "Questionable", "Out", "Injured Reserve") -- THAT field, not
            # the roster Status, is what gold_mine_alerts.py has to match on.
            status = p.get("status") or {}
            injuries = p.get("injuries") or []
            injury_status = ""
            injury_detail = ""
            if injuries:
                first = injuries[0]
                injury_status = str(first.get("status") or "")
                bits = [str(first.get(k)) for k in ("type", "details") if first.get(k)]
                injury_detail = " / ".join(bits)

            pos = p.get("position") or {}
            rows.append({
                "Team": team_code,
                "Name": p.get("displayName", ""),
                "Position": pos.get("abbreviation", "") if isinstance(pos, dict) else pos,
                "Status": status.get("name", "") if isinstance(status, dict) else status,
                "StatusType": status.get("type", "") if isinstance(status, dict) else "",
                "InjuryStatus": injury_status,
                "InjuryDetail": injury_detail,
                "FetchedAtUtc": now,
            })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--inspect", action="store_true",
                     help="print one raw sample record from the first team and exit (no CSV written)")
    ap.add_argument("--delay", type=float, default=0.5,
                     help="seconds to sleep between per-team calls (default 0.5)")
    args = ap.parse_args()

    all_rows = []
    for i, (team_code, espn_id) in enumerate(iter_teams()):
        print(f"[{i+1}/32] pulling roster status for {team_code} (espn id {espn_id})...")
        try:
            rows = pull_team_roster(team_code, espn_id, inspect=args.inspect)
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

    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    dated_path = STATUS_DIR / f"espn_status_wk{args.week}_{today}.csv"
    current_path = DATA_DIR / "espn_status_current.csv"

    for out_path in (dated_path, current_path):
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(all_rows)

    with_injury = [r for r in all_rows if r["InjuryStatus"]]
    print(f"\nWrote {len(all_rows)} player rows -> {dated_path} and {current_path}")
    print(f"{len(with_injury)} player(s) with a real injury designation (Out/Doubtful/Questionable/IR/etc -- "
          f"excludes plain roster housekeeping like Practice Squad/Active):")
    for r in with_injury[:40]:
        print(f"  {r['Name']} ({r['Team']} {r['Position']}): {r['InjuryStatus']}"
              + (f" -- {r['InjuryDetail']}" if r["InjuryDetail"] else ""))
    if len(with_injury) > 40:
        print(f"  ... and {len(with_injury) - 40} more (see the CSV)")


if __name__ == "__main__":
    main()
