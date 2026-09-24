"""
Compares two dated DK salary snapshots (data/dk_salaries/history/DKSalaries_wk{W}_{date}.csv)
and reports any player whose Status or DraftAlerts field changed between them --
e.g. "Player A: Status Questionable -> Doubtful". Used by daily_status_check.ps1
to build a day-over-day injury/news-change report through the week.

Matches players on (Name, TeamAbbrev, Position) -- a player traded mid-week
would show as "new" in the after snapshot and "dropped" in a before-only
sense; that's noted separately from status changes rather than silently
ignored.

Usage:
    python scripts\\diff_dk_status.py --week 3 --season 2026
        (auto-finds the two most recent dated snapshots for that week)
    python scripts\\diff_dk_status.py --before data\\...\\DKSalaries_wk3_20260924.csv --after data\\...\\DKSalaries_wk3_20260925.csv
"""
import argparse
import csv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = PROJECT_ROOT / "data" / "dk_salaries" / "history"


def load_snapshot(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {(r["Name"], r["TeamAbbrev"], r["Position"]): r for r in rows}


def find_two_most_recent(week):
    snaps = sorted(HISTORY_DIR.glob(f"DKSalaries_wk{week}_*.csv"))
    return snaps[-2:] if len(snaps) >= 2 else snaps


def diff(before_path, after_path):
    before = load_snapshot(before_path)
    after = load_snapshot(after_path)

    changes = []
    for key, after_row in after.items():
        before_row = before.get(key)
        if before_row is None:
            continue  # new to the pool since last snapshot -- not a "status change"
        b_status = (before_row.get("Status") or "").strip()
        a_status = (after_row.get("Status") or "").strip()
        b_alerts = (before_row.get("DraftAlerts") or "").strip()
        a_alerts = (after_row.get("DraftAlerts") or "").strip()
        if b_status != a_status or b_alerts != a_alerts:
            name, team, pos = key
            changes.append({
                "name": name, "team": team, "position": pos,
                "status_from": b_status or "(none)", "status_to": a_status or "(none)",
                "alerts_from": b_alerts, "alerts_to": a_alerts,
            })
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--before")
    ap.add_argument("--after")
    ap.add_argument("--out", default=None, help="write a markdown report here instead of just printing")
    args = ap.parse_args()

    if args.before and args.after:
        before_path, after_path = Path(args.before), Path(args.after)
    elif args.week:
        snaps = find_two_most_recent(args.week)
        if len(snaps) < 2:
            print(f"[note] only {len(snaps)} dated snapshot(s) for Week {args.week} so far -- "
                  f"nothing to diff yet (need at least 2 days of pulls).")
            return
        before_path, after_path = snaps[0], snaps[1]
    else:
        ap.error("pass either --week, or both --before and --after")
        return

    changes = diff(before_path, after_path)

    lines = [f"# Status changes: {before_path.name} -> {after_path.name}", ""]
    if not changes:
        lines.append("No Status or DraftAlerts changes since the last pull.")
    else:
        for c in changes:
            lines.append(f"- **{c['name']}** ({c['position']}, {c['team']}): "
                          f"Status `{c['status_from']}` -> `{c['status_to']}`"
                          + (f" -- {c['alerts_to']}" if c['alerts_to'] else ""))

    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"\n[wrote report -> {args.out}]")


if __name__ == "__main__":
    main()
