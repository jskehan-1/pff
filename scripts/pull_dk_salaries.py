"""
Pull DraftKings Classic NFL salary/player-pool data directly from DK's own
(unofficial, undocumented) draftables endpoint, instead of manually
exporting a CSV from the site.

This is NOT a supported/documented DraftKings API. It's the same JSON
their own web client loads before rendering the lineup builder page, found
via Chrome DevTools. It could change or break without notice, and this
script hasn't been verified against a real response yet -- run with
--inspect first.

Finding your draftGroupId each week:
    1. Open the DK Classic NFL contest / lineup builder for the slate you want.
    2. Chrome DevTools (F12) -> Network tab -> filter Fetch/XHR.
    3. Look for a request to .../draftgroups/v1/draftgroups/<ID>/draftables
    4. That <ID> is your --draft-group-id.

Usage:
    python scripts\\pull_dk_salaries.py --draft-group-id 153054 --inspect
    python scripts\\pull_dk_salaries.py --draft-group-id 153054 --out data\\dk_salaries\\DKSalaries_wk5.csv
"""
import argparse
import csv
from datetime import datetime, timezone
import json
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Full browser-like header set. DK's bot protection appears to check for
# standard fetch-metadata headers (sec-fetch-*, sec-ch-ua) in addition to the
# basics -- a request missing them can get a bare 403 even with a valid
# draftGroupId. Deliberately NOT including the one-off Datadog trace/span
# headers (traceparent, x-datadog-*) a real browser sends -- those are
# randomly generated per request and aren't part of what a WAF checks;
# hardcoding stale ones would be pointless.
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "dnt": "1",
    "origin": "https://www.draftkings.com",
    "referer": "https://www.draftkings.com/",
    "sec-ch-ua": '"Google Chrome";v="129", "Not_A Brand";v="8", "Chromium";v="129"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
}

# Candidate JSON key paths to try for each field, tried in order. A "." means
# nested lookup (e.g. "competition.name" -> record["competition"]["name"]).
CANDIDATES = {
    "name": ["displayName", "shortName", "name"],
    "position": ["position", "rosterSlotId", "positionName"],
    "team": ["teamAbbreviation", "team.abbreviation"],
    "salary": ["salary"],
    "game_info": ["competition.name", "draftAlerts", "competition"],
    # Best-effort -- DK's schema for this has moved around historically and
    # this hasn't been verified against a live response from this sandbox
    # (no network access here to run --inspect). Candidates below match the
    # most commonly documented shapes; if all come back blank, run
    # `python scripts\pull_dk_salaries.py --draft-group-id <id> --inspect`
    # yourself and check the raw record for the actual kickoff-time field,
    # then add it to this list. Used for the "FLEX = latest kickoff, highest
    # salary as tiebreak" lineup-construction preference.
    "game_start": ["competitionStartTime", "competition.startTime", "startTime", "competition.startDate"],
    "dk_avg": ["draftStatAttributes"],  # usually a list; handled specially below
    # confirmed present on a real record: status="None" for a healthy player,
    # and confirmed there is NO separate "newsStatus" field (checked against a
    # real response) -- status is the only DK-provided injury/availability signal.
    "status": ["status"],
}

_warned = set()


def get_nested(record, path):
    parts = path.split(".")
    cur = record
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur


def field(record, key, default=None):
    for path in CANDIDATES.get(key, [key]):
        val = get_nested(record, path)
        if val is not None:
            return val
    if key not in _warned:
        _warned.add(key)
        print(f"  [warn] couldn't find '{key}' on a draftable record "
              f"(tried {CANDIDATES.get(key)}). Available top-level keys: "
              f"{sorted(record.keys())}", file=sys.stderr)
    return default


def extract_dk_avg(record):
    """draftStatAttributes is a list of {id, value, sortValue, [quality]}
    with no descriptive name -- confirmed against a real response. id==90
    is DraftKings' average-fantasy-points-per-game attribute; rank-type
    attributes (e.g. "13th") carry a "quality" field and id==90 doesn't."""
    attrs = record.get("draftStatAttributes")
    if not isinstance(attrs, list):
        return None
    for a in attrs:
        if a.get("id") == 90:
            try:
                return float(a.get("value"))
            except (TypeError, ValueError):
                pass
    for a in attrs:
        if "quality" not in a:
            try:
                return float(a.get("value"))
            except (TypeError, ValueError):
                continue
    return None


def extract_alerts(record):
    """draftAlerts shape isn't confirmed against a real flagged player yet --
    best-effort join of whatever descriptive text fields are present."""
    alerts = record.get("draftAlerts")
    if not isinstance(alerts, list) or not alerts:
        return ""
    texts = []
    for a in alerts:
        if isinstance(a, str):
            texts.append(a)
        elif isinstance(a, dict):
            for k in ("description", "message", "text", "type", "category"):
                if a.get(k):
                    texts.append(str(a[k]))
                    break
    return "; ".join(texts)


def fetch_draftables(draft_group_id):
    url = f"https://api.draftkings.com/draftgroups/v1/draftgroups/{draft_group_id}/draftables"
    resp = requests.get(url, params={"format": "json"}, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft-group-id", required=True)
    ap.add_argument("--inspect", action="store_true",
                     help="print one raw sample draftable record and exit")
    ap.add_argument("--out", default=None)
    ap.add_argument("--season", type=int, default=None,
                     help="if given (with --week), records this draft group ID as "
                          "the 'current' one for daily_status_check.ps1 to keep "
                          "reusing through the rest of the week, and writes a "
                          "dated snapshot copy alongside --out for day-over-day "
                          "status-change diffing.")
    ap.add_argument("--week", type=int, default=None)
    args = ap.parse_args()

    data = fetch_draftables(args.draft_group_id)
    draftables = data.get("draftables", data if isinstance(data, list) else [])

    if not draftables:
        print("No draftables returned -- check the draft group ID, or DK may have "
              "changed the response shape. Top-level keys were: "
              f"{list(data.keys()) if isinstance(data, dict) else type(data)}")
        return

    if args.inspect:
        print(json.dumps(draftables[0], indent=2))
        return

    # DK lists a player once per eligible roster slot (e.g. a flex-eligible
    # RB appears once tagged "RB" and again tagged "FLEX"). Group by name and
    # prefer the record with a real position over a generic slot label.
    CANONICAL_POS = {"QB", "RB", "WR", "TE", "DST"}
    by_name = {}
    for r in draftables:
        name = field(r, "name")
        if not name:
            continue
        by_name.setdefault(name, []).append(r)

    rows = []
    for name, records in by_name.items():
        best = next((r for r in records if field(r, "position", "") in CANONICAL_POS), records[0])
        status = field(best, "status", "") or ""
        rows.append({
            "Name": name,
            "Position": field(best, "position", ""),
            "TeamAbbrev": field(best, "team", ""),
            "Salary": field(best, "salary", 0),
            "Game Info": field(best, "game_info", ""),
            "GameStart": field(best, "game_start", ""),
            "AvgPointsPerGame": extract_dk_avg(best) or "",
            "Status": "" if status.lower() == "none" else status,
            "DraftAlerts": extract_alerts(best),
        })

    out_path = Path(args.out) if args.out else PROJECT_ROOT / "data" / "dk_salaries" / f"DKSalaries_{args.draft_group_id}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "Position", "TeamAbbrev", "Salary", "Game Info", "GameStart", "AvgPointsPerGame", "Status", "DraftAlerts"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} players -> {out_path}")
    print("This file is compatible with build_projections.py's --salaries argument.")

    # Log every draft group ID we've ever pulled, with a timestamp, so we can
    # look for a usable pattern later (e.g. whether it's roughly sequential
    # week over week) instead of guessing off memory. Doesn't affect the
    # salaries CSV at all -- purely a side log.
    log_path = PROJECT_ROOT / "data" / "draft_group_id_log.csv"
    log_is_new = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if log_is_new:
            writer.writerow(["pulled_at_utc", "draft_group_id", "out_path", "player_count", "note"])
        writer.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          args.draft_group_id, str(out_path), len(rows), ""])

    if args.season is not None and args.week is not None:
        # Record this as the "current" draft group for the week, and stash a
        # dated snapshot -- daily_status_check.ps1 reads the former to know
        # what ID to keep re-pulling with (no re-pasting the curl daily), and
        # diff_dk_status.py compares consecutive snapshots to report Status/
        # DraftAlerts changes day over day.
        state_path = PROJECT_ROOT / "data" / "current_draft_group.json"
        state = {
            "season": args.season,
            "week": args.week,
            "draft_group_id": args.draft_group_id,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        snap_dir = PROJECT_ROOT / "data" / "dk_salaries" / "history"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_date = datetime.now(timezone.utc).strftime("%Y%m%d")
        snap_path = snap_dir / f"DKSalaries_wk{args.week}_{snap_date}.csv"
        with open(snap_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Name", "Position", "TeamAbbrev", "Salary", "Game Info", "GameStart", "AvgPointsPerGame", "Status", "DraftAlerts"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"Also wrote dated snapshot -> {snap_path} and updated {state_path}")


if __name__ == "__main__":
    main()
