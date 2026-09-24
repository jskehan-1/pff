"""
Build a historical "when the starter sits, how much does the backup's usage
jump" table from data/snap_counts_{season}.csv -- Jake's "gold mine" scenario
(2026-09-23): the primary position player is ruled out AFTER DK salaries
lock, and the backup gets far more usage (and fantasy value) than the market
priced in.

Method, per (team, position) each week:
  1. Rank that team's players at that position by snap_counts_total.
  2. The player ranked #1 in week W-1 is treated as "the starter" heading
     into week W.
  3. If that same player's snaps in week W are near zero (missed the game --
     hurt, benched, bye doesn't apply since this is same-team-same-week
     comparison) AND a DIFFERENT player is #1 in week W, that's a promotion
     event: record the promoted player's week-W snaps vs. their own average
     snaps in the weeks *before* the promotion (their "before" role).

This is intentionally simple (season-to-date only, no play-by-play), and the
sample size will be small/zero early in a season -- that's expected and
printed plainly rather than papered over, same philosophy as the EWMA
placeholder columns in build_projections.py.

Writes:
    data\\backup_usage_history.csv  -- one row per detected promotion event
    data\\backup_usage_summary.csv  -- aggregated by position (QB/RB/WR/TE),
                                        for gold_mine_alerts.py to apply as a
                                        rough multiplier

Usage:
    python scripts\\build_backup_usage.py --season 2026
"""
import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

NEAR_ZERO_SNAPS = 5  # a "starter" recording <= this many snaps counts as missing the game


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_snap_counts(season):
    path = DATA_DIR / f"snap_counts_{season}.csv"
    if not path.exists():
        return None
    # {(team, position, week): {player_name: snaps_total}}
    by_team_pos_week = defaultdict(dict)
    for row in csv.DictReader(open(path, newline="", encoding="utf-8")):
        team, pos, week = row["team"], row["position"], int(row["week"])
        by_team_pos_week[(team, pos, week)][row["player_name"]] = to_float(row.get("snap_counts_total"))
    return by_team_pos_week


def build(season):
    data = load_snap_counts(season)
    if data is None:
        print(f"[note] data\\snap_counts_{season}.csv doesn't exist yet -- run "
              f"pull_snap_counts.py for at least two weeks first.")
        return [], []

    # Group weeks present per (team, position)
    keys_by_team_pos = defaultdict(set)
    for (team, pos, week) in data:
        keys_by_team_pos[(team, pos)].add(week)

    events = []
    for (team, pos), weeks in keys_by_team_pos.items():
        weeks = sorted(weeks)
        # season-to-date snaps per player, for computing "prior average" as of
        # just before the promotion week
        running = defaultdict(list)  # player_name -> [snaps per week so far]

        for idx, week in enumerate(weeks):
            week_snaps = data[(team, pos, week)]
            if idx > 0:
                prev_week = weeks[idx - 1]
                prev_snaps = data[(team, pos, prev_week)]
                if prev_snaps:
                    prev_starter = max(prev_snaps, key=prev_snaps.get)
                    prev_starter_snaps_now = week_snaps.get(prev_starter, 0.0)
                    if week_snaps and prev_starter_snaps_now <= NEAR_ZERO_SNAPS:
                        new_starter = max(week_snaps, key=week_snaps.get)
                        if new_starter != prev_starter:
                            prior_games = running.get(new_starter, [])
                            prior_avg = round(statistics.mean(prior_games), 2) if prior_games else 0.0
                            events.append({
                                "Team": team, "Position": pos, "Week": week,
                                "DisplacedStarter": prev_starter,
                                "DisplacedStarterSnapsThisWeek": prev_starter_snaps_now,
                                "PromotedPlayer": new_starter,
                                "PromotedPlayerSnapsThisWeek": week_snaps[new_starter],
                                "PromotedPlayerPriorAvgSnaps": prior_avg,
                                "PromotedPlayerPriorGames": len(prior_games),
                            })
            for player, snaps in week_snaps.items():
                running[player].append(snaps)

    # Aggregate by position -- only using events where we actually had a
    # "before" role to compare against (prior_games > 0); zero-to-hero
    # promotions (rookie call-ups, practice-squad elevations) are counted
    # separately since a jump multiplier off a 0 base is meaningless.
    summary = []
    for pos in ("QB", "RB", "WR", "TE"):
        pos_events = [e for e in events if e["Position"] == pos]
        with_baseline = [e for e in pos_events if e["PromotedPlayerPriorGames"] > 0 and e["PromotedPlayerPriorAvgSnaps"] > 0]
        zero_to_hero = [e for e in pos_events if e not in with_baseline]
        multipliers = [e["PromotedPlayerSnapsThisWeek"] / e["PromotedPlayerPriorAvgSnaps"] for e in with_baseline]
        summary.append({
            "Position": pos,
            "SampleSize": len(with_baseline),
            "AvgSnapJumpMultiplier": round(statistics.mean(multipliers), 2) if multipliers else "",
            "AvgPromotedSnapsThisWeek": round(statistics.mean([e["PromotedPlayerSnapsThisWeek"] for e in pos_events]), 1) if pos_events else "",
            "ZeroToHeroEvents": len(zero_to_hero),
            "Note": "not enough events yet -- accumulates as more weeks are pulled" if len(with_baseline) < 3 else "",
        })

    return events, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    args = ap.parse_args()

    events, summary = build(args.season)

    hist_path = DATA_DIR / "backup_usage_history.csv"
    if events:
        with open(hist_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(events[0].keys()))
            writer.writeheader()
            writer.writerows(events)
        print(f"Wrote {len(events)} promotion event(s) -> {hist_path}")
    else:
        print("No promotion events detected yet (need at least 2 pulled weeks of snap_counts, "
              "and at least one where a team's snap-leader at a position went near-zero the "
              "next week while someone else took over).")

    summary_path = DATA_DIR / "backup_usage_summary.csv"
    if summary:
        with open(summary_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            writer.writeheader()
            writer.writerows(summary)
        print(f"Wrote position summary -> {summary_path}")
        for s in summary:
            print(f"  {s['Position']}: n={s['SampleSize']}, avg jump multiplier="
                  f"{s['AvgSnapJumpMultiplier'] or 'n/a'}, zero-to-hero events={s['ZeroToHeroEvents']}"
                  + (f" -- {s['Note']}" if s["Note"] else ""))


if __name__ == "__main__":
    main()
