"""
Build optimal (or near-optimal, diversified) DraftKings Classic NFL
lineups from a projections CSV, using integer linear programming (pulp).

Lineup rules encoded: 1 QB, 2 RB, 3 WR, 1 TE, 1 FLEX (RB/WR/TE), 1 DST,
salary cap $50,000. The "players from at least 2 different games" rule is
checked after solving and flagged if violated (rare with a full slate).

Usage:
    python scripts\\optimizer.py --projections data\\projections_wk5.csv
    python scripts\\optimizer.py --projections data\\projections_wk5.csv --lineups 5 --max-overlap 6
"""
import argparse
import csv
from pathlib import Path

try:
    import pulp
except ImportError:
    raise SystemExit(
        "This script needs the 'pulp' package. Install it with:\n"
        "    pip install pulp"
    )

SALARY_CAP = 50000
ROSTER = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
FLEX_ELIGIBLE = {"RB", "WR", "TE"}
FLEX_COUNT = 1


def load_players(path, metric="Projection"):
    players = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pos = row["Position"].strip().upper()
            # DK sometimes lists multi-position eligibility like "RB/FLEX" -- take the first token.
            pos = pos.split("/")[0]
            players.append({
                "name": row["Name"], "team": row["Team"], "position": pos,
                "salary": int(row["Salary"]), "game_info": row.get("GameInfo", ""),
                "projection": float(row[metric]),
            })
    return players


def solve_lineup(players, exclude_lineups=None, max_overlap=None):
    exclude_lineups = exclude_lineups or []
    prob = pulp.LpProblem("dk_classic_lineup", pulp.LpMaximize)
    x = {i: pulp.LpVariable(f"x_{i}", cat="Binary") for i in range(len(players))}

    prob += pulp.lpSum(x[i] * players[i]["projection"] for i in x)
    prob += pulp.lpSum(x[i] * players[i]["salary"] for i in x) <= SALARY_CAP
    prob += pulp.lpSum(x[i] for i in x) == 9

    for pos, minimum in ROSTER.items():
        prob += pulp.lpSum(x[i] for i in x if players[i]["position"] == pos) >= minimum

    flex_positions = pulp.lpSum(
        x[i] for i in x if players[i]["position"] in FLEX_ELIGIBLE
    )
    prob += flex_positions == sum(ROSTER[p] for p in FLEX_ELIGIBLE) + FLEX_COUNT

    # Diversification: cap overlap with any previously generated lineup.
    if max_overlap is not None:
        for prev in exclude_lineups:
            prev_idx = {i for i in x if players[i]["name"] in prev}
            prob += pulp.lpSum(x[i] for i in prev_idx) <= max_overlap

    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if pulp.LpStatus[prob.status] != "Optimal":
        return None

    chosen = [players[i] for i in x if x[i].value() == 1]
    return chosen


def check_min_games(lineup):
    games = {p["game_info"] for p in lineup if p["game_info"]}
    return len(games) >= 2 if games else True  # can't check if game_info missing


def print_lineup(n, lineup):
    total_salary = sum(p["salary"] for p in lineup)
    total_proj = sum(p["projection"] for p in lineup)
    print(f"\n=== Lineup {n} -- projected {total_proj:.2f} pts, ${total_salary:,} / ${SALARY_CAP:,} ===")
    order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "DST": 4}
    for p in sorted(lineup, key=lambda p: order.get(p["position"], 9)):
        print(f"  {p['position']:4s} {p['name']:25s} {p['team']:4s} "
              f"${p['salary']:>6,}  proj {p['projection']:>6.2f}")
    if not check_min_games(lineup):
        print("  [warn] lineup may not satisfy the 'at least 2 different games' rule -- "
              "GameInfo column was missing or all one game.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projections", required=True)
    ap.add_argument("--lineups", type=int, default=1, help="how many diversified lineups to generate")
    ap.add_argument("--max-overlap", type=int, default=6,
                     help="max shared players allowed between generated lineups (of 9)")
    ap.add_argument("--metric", default="Projection",
                     help="which projections CSV column to optimize against, "
                          "e.g. 'Projection' (season blend, default) or 'EwmaProjection'")
    args = ap.parse_args()

    players = load_players(args.projections, metric=args.metric)

    generated = []
    for n in range(1, args.lineups + 1):
        prev_name_sets = [{p["name"] for p in lu} for lu in generated]
        lineup = solve_lineup(players, exclude_lineups=prev_name_sets,
                               max_overlap=args.max_overlap if n > 1 else None)
        if lineup is None:
            print(f"\nCould only generate {n - 1} lineup(s) meeting the constraints "
                  f"(cap of {args.max_overlap} overlap made it infeasible after that).")
            break
        generated.append(lineup)
        print_lineup(n, lineup)


if __name__ == "__main__":
    main()
