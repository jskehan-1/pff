"""
The "glue" script for Jake's gold-mine scenario (2026-09-23): a primary
position player gets ruled out AFTER DK salaries lock, and the backup sees
far more usage than his (still-cheap) salary reflects.

Cross-references, all for the CURRENT week:
    data\\espn_status_current.csv        (pull_espn_status.py)      -- who's
                                           currently Out/Doubtful/Questionable
    data\\espn_depthchart_current.csv    (pull_espn_depthchart.py)  -- who's
                                           next in line at that position
    data\\dk_salaries\\DKSalaries_wk{W}.csv                          -- is the
                                           backup actually in this week's pool
                                           (i.e. salaries already locked in)
    data\\projections_wk{W}.csv          (build_projections.py)     -- the
                                           backup's current Projection/Value,
                                           to see whether the market has
                                           already repriced him
    data\\backup_usage_summary.csv       (build_backup_usage.py, optional --
                                           may not exist / be too sparse yet)
                                          -- historical snap-jump multiplier
    data\\snap_counts_{season}.csv       (pull_snap_counts.py)       -- the
                                           starter's own snap count in the
                                           most recent COMPLETED week, used
                                           for the freshness check below

FRESHNESS CHECK (added 2026-09-25, per Jake): an Out/Doubtful/IR tag by
itself isn't news if it's been true for a week or more already -- the
backup's own recent snaps and this tool's own Projection already reflect
that reality, so there's nothing left to find. Real example that motivated
this: Johnny Wilson (PHI) had already been on IR for 2+ weeks with Makai
Lemon seeing no snap increase -- that's OLD news, not a gold mine, and the
first version of this script couldn't tell the difference. Now every alert
is tagged NEW (starter was playing normally as recently as the last
completed week -- a real, fresh signal) or ONGOING (starter was already
near-zero-snap last week too -- already known/priced in, deprioritized in
the report). Needs data\\snap_counts_{season}.csv to have at least the most
recent completed week pulled; without it, an alert is tagged "unknown"
freshness rather than guessed at.

Run this AFTER pull_espn_status.py / pull_espn_depthchart.py / pull_snap_counts.py
/ (re)building projections for the week -- daily_status_check.ps1 does all
of that in order (though snap counts only get pulled weekly, on Tuesday, by
auto_weekly.ps1, since they need a completed week of real games).

Usage:
    python scripts\\gold_mine_alerts.py --season 2026 --week 3
"""
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_projections import normalize_name  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Same threshold build_backup_usage.py uses for "this player basically didn't
# play" -- reused here to tell a FRESH absence from an ONGOING one.
NEAR_ZERO_SNAPS = 5

# Tiered on purpose -- Jake's ask is specifically "ruled out AFTER prices
# lock", i.e. a real gold mine needs high confidence the primary guy is NOT
# playing. Mixing tiers together (confirmed live 2026-09-25: an untiered
# first pass produced 65 "alerts" for a single week, mostly Questionable
# players who usually DO play, plus long-standing IR guys whose backup
# situation is already old news/already priced in) makes the whole feature
# noise, not signal.
HIGH_CONFIDENCE_STATUSES = {"out", "doubtful"}
LONG_TERM_STATUSES = {"injured reserve", "ir", "physically unable to perform", "pup"}
WATCH_STATUSES = {"questionable"}
ALL_CONCERN_STATUSES = HIGH_CONFIDENCE_STATUSES | LONG_TERM_STATUSES | WATCH_STATUSES


def tier_of(status):
    if status in HIGH_CONFIDENCE_STATUSES:
        return "high_confidence"
    if status in LONG_TERM_STATUSES:
        return "long_term"
    if status in WATCH_STATUSES:
        return "watch"
    return None


def load_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_snap_history(season, before_week):
    """
    {(team, position, norm_name): {week: snap_counts_total}} for every
    COMPLETED week strictly before `before_week` -- i.e. real, already-played
    games only. Used to tell whether a starter's absence is fresh news for
    the upcoming week (they played normally last week) or old news Jake and
    the market already know about (they were already near-zero last week
    too) -- see the module docstring's freshness section.
    """
    path = DATA_DIR / f"snap_counts_{season}.csv"
    if not path.exists():
        return {}
    out = defaultdict(dict)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            wk = int(row["week"])
            if wk >= before_week:
                continue
            try:
                snaps = float(row.get("snap_counts_total") or 0)
            except ValueError:
                snaps = 0.0
            key = (row["team"], row["position"], normalize_name(row["player_name"]))
            out[key][wk] = snaps
    return out


def freshness_of(snap_history, team, pos, player_name, before_week):
    """
    Returns ("new", note) if the player was playing normally as of the most
    recent COMPLETED week (so their current Out/Doubtful/IR status for the
    upcoming week is a real, fresh change) -- ("ongoing", note) if they were
    already near-zero-snap last week too (already known, already reflected
    in everyone's projections/pricing) -- or ("unknown", note) if there's no
    snap-count data yet to judge by (early season, bye week, name-match miss).
    """
    key = (team, pos, normalize_name(player_name))
    weeks = snap_history.get(key)
    if not weeks:
        return "unknown", "no snap-count history for this player yet"
    last_week = max(weeks)
    last_snaps = weeks[last_week]
    if last_snaps > NEAR_ZERO_SNAPS:
        return "new", f"played {last_snaps:.0f} snaps as recently as week {last_week} -- this looks like fresh news"
    return "ongoing", f"already near-zero snaps ({last_snaps:.0f}) back in week {last_week} too -- likely already known/priced in"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--out", default=None, help="write a markdown report here too")
    args = ap.parse_args()

    status_rows = load_csv(DATA_DIR / "espn_status_current.csv")
    depth_rows = load_csv(DATA_DIR / "espn_depthchart_current.csv")
    salary_rows = load_csv(DATA_DIR / "dk_salaries" / f"DKSalaries_wk{args.week}.csv")
    proj_rows = load_csv(DATA_DIR / f"projections_wk{args.week}.csv")
    usage_summary = {r["Position"]: r for r in load_csv(DATA_DIR / "backup_usage_summary.csv")}
    snap_history = load_snap_history(args.season, args.week)

    if not status_rows:
        print("[note] data\\espn_status_current.csv missing/empty -- run pull_espn_status.py first.")
        return
    if not depth_rows:
        print("[note] data\\espn_depthchart_current.csv missing/empty -- run pull_espn_depthchart.py first.")
        return
    if not salary_rows:
        print(f"[note] data\\dk_salaries\\DKSalaries_wk{args.week}.csv missing -- pull this week's salaries first.")
        return

    salary_by_name_team = {(r["Name"], r["TeamAbbrev"]): r for r in salary_rows}
    proj_by_name_team = {(r["Name"], r["Team"]): r for r in proj_rows}

    # depth chart: (team, position) -> [(rank, name), ...] sorted
    depth_by_team_pos = defaultdict(list)
    for r in depth_rows:
        try:
            rank = int(r["Rank"])
        except (KeyError, ValueError):
            rank = 99
        depth_by_team_pos[(r["Team"], r["PositionAbbrev"])].append((rank, r["PlayerName"]))
    for key in depth_by_team_pos:
        depth_by_team_pos[key].sort(key=lambda t: t[0])

    # status: name -> row (assumes name is unique enough within a team; good
    # enough for this alerting use case, not a hard data-integrity guarantee)
    status_by_name_team = {(r["Name"], r["Team"]): r for r in status_rows}

    alerts = []
    for (team, pos), ranked in depth_by_team_pos.items():
        for i, (rank, name) in enumerate(ranked):
            status_row = status_by_name_team.get((name, team))
            if not status_row:
                continue
            # InjuryStatus (from injuries[0].status), NOT the roster Status
            # field -- confirmed live 2026-09-23 that ESPN's top-level
            # "status" is a roster category ("Active"/"Day-To-Day"/"Practice
            # Squad"/"News"), not an Out/Doubtful/Questionable designation.
            status = (status_row.get("InjuryStatus") or "").strip().lower()
            tier = tier_of(status)
            if tier is None:
                continue

            # find the next-healthy player below this one in the depth chart
            backup = None
            for j in range(i + 1, len(ranked)):
                _, cand_name = ranked[j]
                cand_status_row = status_by_name_team.get((cand_name, team))
                cand_status = (cand_status_row.get("InjuryStatus") or "").strip().lower() if cand_status_row else ""
                if cand_status not in ALL_CONCERN_STATUSES:
                    backup = cand_name
                    break
            if backup is None:
                continue

            sal_row = salary_by_name_team.get((backup, team))
            if sal_row is None:
                continue  # backup isn't in this week's DK pool at all (practice squad, not elevated, etc.)

            proj_row = proj_by_name_team.get((backup, team))
            usage = usage_summary.get(pos, {})
            multiplier = usage.get("AvgSnapJumpMultiplier") or ""

            freshness, freshness_note = freshness_of(snap_history, team, pos, name, args.week)

            alerts.append({
                "Tier": tier,
                "Freshness": freshness,
                "Team": team, "Position": pos,
                "AtRiskStarter": name, "AtRiskStarterStatus": status_row.get("InjuryStatus", ""),
                "Backup": backup,
                "BackupSalary": sal_row.get("Salary", ""),
                "BackupProjection": proj_row.get("Projection", "") if proj_row else "(not in projections file)",
                "BackupValue": proj_row.get("Value", "") if proj_row else "",
                "HistoricalSnapJumpMultiplier": multiplier,
                "SampleSize": usage.get("SampleSize", ""),
                "FreshnessNote": freshness_note,
                "Note": "" if multiplier else "no historical backup-usage data yet for this position -- judge on depth chart alone",
            })

    def fmt(a):
        mult_note = f" (historical backups at {a['Position']} jump ~{a['HistoricalSnapJumpMultiplier']}x their prior snaps, n={a['SampleSize']})" if a["HistoricalSnapJumpMultiplier"] else ""
        return (f"- **{a['Backup']}** ({a['Team']} {a['Position']}, ${a['BackupSalary']}, "
                f"current Projection {a['BackupProjection']} / Value {a['BackupValue']}) is next in line behind "
                f"**{a['AtRiskStarter']}** ({a['AtRiskStarterStatus']}){mult_note} -- _{a['FreshnessNote']}_")

    # Freshness is the headline split now, not just severity: a NEW
    # high-confidence absence is the actual gold mine; an ONGOING one (the
    # backup's already had the role for a week+, so his snap count and this
    # tool's own Projection already reflect it -- exactly Jake's Makai
    # Lemon/Johnny Wilson example, 2026-09-25) is old news, not a signal.
    fresh_high_conf = [a for a in alerts if a["Tier"] == "high_confidence" and a["Freshness"] == "new"]
    fresh_long_term = [a for a in alerts if a["Tier"] == "long_term" and a["Freshness"] == "new"]
    ongoing = [a for a in alerts if a["Freshness"] == "ongoing" and a["Tier"] != "watch"]
    unknown_freshness = [a for a in alerts if a["Freshness"] == "unknown" and a["Tier"] != "watch"]
    watch = [a for a in alerts if a["Tier"] == "watch"]

    lines = [f"# Gold-mine alerts -- Season {args.season} Week {args.week}", ""]
    lines.append("## NEW this week -- Confirmed Out/Doubtful, starter was playing normally as recently as last week")
    lines.append("")
    if not fresh_high_conf:
        lines.append("None right now.")
    else:
        lines.extend(fmt(a) for a in fresh_high_conf)

    if fresh_long_term:
        lines += ["", "## NEW this week -- freshly placed IR/PUP (still worth a look even though IR sounds long-term)", ""]
        lines.extend(fmt(a) for a in fresh_long_term)

    if ongoing:
        lines += ["", f"## Ongoing, not new -- already known/priced in ({len(ongoing)} total, e.g. a backup who's "
                       f"already had this role for a week+ with no snap jump -- collapsed here, see the CSV for detail)"]

    if unknown_freshness:
        lines += ["", f"## Freshness unknown -- no snap-count history yet to judge by ({len(unknown_freshness)} total, "
                       f"early season / bye week / name-match miss -- see the CSV)"]

    if watch:
        lines += ["", f"## Questionable -- watch only, most of these still play ({len(watch)} total, not itemized here -- see the CSV)"]

    report = "\n".join(lines)
    print(report)

    csv_path = DATA_DIR / f"gold_mine_alerts_wk{args.week}.csv"
    if alerts:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(alerts[0].keys()))
            writer.writeheader()
            writer.writerows(alerts)
        print(f"\n[wrote {csv_path} -- {len(fresh_high_conf)} new high-confidence, {len(fresh_long_term)} new IR, "
              f"{len(ongoing)} ongoing/already-known, {len(unknown_freshness)} freshness-unknown, {len(watch)} questionable/watch]")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"[wrote {args.out}]")


if __name__ == "__main__":
    main()
