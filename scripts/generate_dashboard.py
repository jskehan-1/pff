"""
Bake a projections CSV (+ optional optimizer output + history) into a
self-contained dashboard.html, based on templates/dashboard_template.html.

Usage:
    python scripts\\generate_dashboard.py --season 2026 --week 5 --projections data\\projections_wk5.csv
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "dashboard_template.html"
HISTORY_PATH = DATA_DIR / "dfs_history.csv"


def load_projections(path):
    players = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            floor_raw = row.get("Floor", "")
            matchup_raw = row.get("MatchupScore", "")
            games2026_raw = row.get("Games2026", "")
            ewma_proj_raw = row.get("EwmaProjection", "")
            ewma_val_raw = row.get("EwmaValue", "")
            fade_target_raw = row.get("FadeTarget", "")
            pts_last_raw = row.get("PtsLastWk", "")
            pts_avg3_raw = row.get("PtsAvg3", "")
            opp_last_raw = row.get("OppPosAllowedLastWk", "")
            opp_avg3_raw = row.get("OppPosAllowedAvg3", "")
            players.append({
                "name": row["Name"], "team": row["Team"], "position": row["Position"],
                "salary": int(row["Salary"]), "gameInfo": row.get("GameInfo", ""),
                "gameStart": row.get("GameStart", ""),
                "projection": float(row["Projection"]), "value": float(row["Value"]),
                "floor": float(floor_raw) if floor_raw not in (None, "") else None,
                "status": row.get("Status", ""),
                "draftAlerts": row.get("DraftAlerts", ""),
                "matchupScore": int(matchup_raw) if matchup_raw not in (None, "") else None,
                "matchupLabel": row.get("MatchupLabel", ""),
                "games2026": int(games2026_raw) if games2026_raw not in (None, "") else 0,
                "ewmaProjection": float(ewma_proj_raw) if ewma_proj_raw not in (None, "") else None,
                "ewmaValue": float(ewma_val_raw) if ewma_val_raw not in (None, "") else None,
                "ewmaNotes": row.get("EwmaNotes", ""),
                "fadeTarget": str(fade_target_raw).strip().lower() == "true",
                "ptsLastWk": float(pts_last_raw) if pts_last_raw not in (None, "") else None,
                "ptsAvg3": float(pts_avg3_raw) if pts_avg3_raw not in (None, "") else None,
                "oppPosAllowedLastWk": float(opp_last_raw) if opp_last_raw not in (None, "") else None,
                "oppPosAllowedAvg3": float(opp_avg3_raw) if opp_avg3_raw not in (None, "") else None,
                "notes": row.get("Notes", ""),
            })
    return players


def run_optimizer(projections_path, n_lineups, metric="Projection"):
    """Run optimizer.py as a subprocess and parse its printed lineups back
    into structured data (keeps optimizer.py itself simple/CLI-first)."""
    script = Path(__file__).resolve().parent / "optimizer.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--projections", str(projections_path),
             "--lineups", str(n_lineups), "--metric", metric],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        print("[warn] couldn't run optimizer.py (pulp likely not installed yet) -- "
              "dashboard will have no optimizer suggestions.")
        return []
    except subprocess.CalledProcessError as e:
        print(f"[warn] optimizer.py failed, skipping optimizer tab content:\n{e.stderr}")
        return []

    lineups = []
    current = None
    for line in result.stdout.splitlines():
        m = re.match(r"=== Lineup (\d+) -- projected ([\d.]+) pts, \$([\d,]+) / \$([\d,]+) ===", line)
        if m:
            if current:
                lineups.append(current)
            current = {"players": [], "totalProjection": float(m.group(2)),
                       "totalSalary": int(m.group(3).replace(",", ""))}
            continue
        if current is not None and re.match(r"\s+(QB|RB|WR|TE|DST)\s", line):
            # Player line looks like (fields separated by 2+ spaces):
            #   "  QB   Josh Allen                BUF  $ 8,500  proj  21.81"
            # cols[0] is the position, cols[1] is the full name (a name has
            # only single internal spaces, so it survives the split intact).
            cols = [c for c in re.split(r"\s{2,}", line.strip()) if c]
            if len(cols) >= 2:
                current["players"].append(cols[1])
    if current:
        lineups.append(current)
    return lineups


def load_history():
    if not HISTORY_PATH.exists():
        return []
    rows = []
    with open(HISTORY_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({
                "week": int(row["week"]),
                "salaryUsed": int(row["salary_used"]) if row.get("salary_used") else 0,
                "projected": float(row["projected"]) if row.get("projected") else None,
                "actual": float(row["actual"]) if row.get("actual") else None,
                "notes": row.get("notes", ""),
            })
    rows.sort(key=lambda r: r["week"])
    return rows


def dashboard_name(season, week):
    return f"Tinker{season % 100:02d}W{week:02d}.html"


def roster_path(season, week):
    # Saved by the dashboard's "Save roster" button into the same folder as the
    # generated HTML file itself (PROJECT_ROOT), so Jake doesn't have to hunt
    # for a separate data\lineups folder -- just save next to the dashboard.
    return PROJECT_ROOT / f"Tinker{season % 100:02d}W{week:02d}_roster.json"


def load_saved_roster(season, week):
    """
    Looks for data\\lineups\\Tinker{YY}W{WW}_roster.json (written by the
    dashboard's "Save roster" button) and returns a 9-element list of player
    names in ROSTER_SLOTS order (QB,RB,RB,WR,WR,WR,TE,FLEX,DST), or None if no
    roster has been saved for this week yet -- in which case the dashboard
    just starts with an empty lineup, same as before.
    """
    path = roster_path(season, week)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] couldn't read saved roster {path.name}: {e} -- starting with an empty lineup.")
        return None
    roster = payload.get("roster", [])
    names = [(r or {}).get("name") for r in roster]
    while len(names) < 9:
        names.append(None)
    filled = sum(1 for n in names if n)
    print(f"[note] loaded saved roster from {path.name} ({filled}/9 slots filled).")
    return names[:9]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--projections", required=True)
    ap.add_argument("--lineups", type=int, default=3, help="how many optimizer lineups to bake in")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    players = load_projections(args.projections)
    optimizer_lineups = run_optimizer(args.projections, args.lineups, metric="Projection")
    optimizer_lineups_ewma = run_optimizer(args.projections, args.lineups, metric="EwmaProjection")
    history = load_history()
    saved_roster_names = load_saved_roster(args.season, args.week)

    data = {
        "season": args.season, "week": args.week,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "salaryCap": 50000,
        "players": players,
        "optimizerLineups": optimizer_lineups,
        "optimizerLineupsEwma": optimizer_lineups_ewma,
        "history": history,
        "savedRosterNames": saved_roster_names,
        "rosterFileName": roster_path(args.season, args.week).name,
    }

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    out_html = template.replace("__DASHBOARD_DATA_JSON__", json.dumps(data))

    out_path = Path(args.out) if args.out else PROJECT_ROOT / dashboard_name(args.season, args.week)
    out_path.write_text(out_html, encoding="utf-8")
    print(f"Wrote dashboard -> {out_path}")
    print(f"({len(players)} players, {len(optimizer_lineups)} blend-model optimizer lineups, "
          f"{len(optimizer_lineups_ewma)} EWMA-model optimizer lineups, "
          f"{len(history)} history rows baked in)")


if __name__ == "__main__":
    main()
