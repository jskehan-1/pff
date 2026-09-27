"""
Shared bits for the Flounder (ESPN keeper auction league) automation.

- League/team constants (manager short names -- team names change often, so
  every report keys off MANAGER, not ESPN team name)
- ESPNLeagueClient: authenticated reads of the private league via the
  lm-api-reads.fantasy.espn.com v3 API. Needs ESPN_S2 and ESPN_SWID in the
  project .env (copy them from your browser's espn.com cookies -- see README).
- Draft capital: reference/flounder_draft_2026.csv, where keepers carry their
  OFFICIAL keeper cost (from the "2026 Keepers" tab, + any trade fee) instead
  of the odd $ amounts ESPN recorded for keepers at the draft.
"""
import csv
import math
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

REF_DIR = PROJECT_ROOT / "reference"
FL_DATA = PROJECT_ROOT / "data" / "flounder"

LEAGUE_ID = 253826
SEASON = 2026
MY_TEAM_ID = 12

# ESPN team id -> (manager short name used in Jake's notes, full name)
MANAGERS = {
    1: ("Marf", "Peter Marafioti"),
    2: ("Demers", "Curt Demers"),
    3: ("Pin", "Jose Pinheiro"),
    4: ("Larsen", "Mark Larsen"),
    5: ("Dave", "David Raphael"),
    6: ("Dre", "Andrei Lombardi"),
    7: ("Davis", "Greg Davis"),
    8: ("Tami", "Scott Tami"),
    9: ("Wilkens", "Daniel Wilkens"),
    10: ("Hagan", "Hagan Blount"),
    11: ("Zips", "Michael Szczepanski"),
    12: ("Jake", "Jake (you)"),
}


def mgr(team_id):
    if not team_id:
        return "FA"
    return MANAGERS.get(int(team_id), (f"team{team_id}", ""))[0]


PRO_TEAMS = {0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
             7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
             14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
             20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
             26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL",
             34: "HOU"}
POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}

# ESPN lineupSlotId -> label (Flounder: QB, RB x2, WR x3, TE, FLEX, OP, DST, 10 BE)
SLOTS = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 7: "OP", 16: "DST", 17: "K",
         20: "BE", 21: "IR", 23: "FLEX"}
STARTER_SLOTS = [("QB", {"QB"}), ("RB", {"RB"}), ("RB", {"RB"}),
                 ("WR", {"WR"}), ("WR", {"WR"}), ("WR", {"WR"}),
                 ("TE", {"TE"}), ("FLEX", {"RB", "WR", "TE"}),
                 ("OP", {"QB", "RB", "WR", "TE"}), ("DST", {"DST"})]

# Official 2026 keeper costs (Flounder 2026 Keepers.xlsx, "2026 Keepers" tab,
# 9/11 snapshot) -> (keeper cost, trade fee paid to acquire the rights).
# Effective draft capital = cost + fee. Next year's keeper cost is based on the
# keeper cost only (fee is a one-off).
KEEPER_COSTS = {
    "Matthew Stafford": (9, 5), "De'Von Achane": (14, 0), "Cam Skattebo": (6, 0),
    "Jaxon Smith-Njigba": (32, 0), "Christian Watson": (5, 5),
    "Rome Odunze": (18, 0), "Kenny Gainwell": (5, 3), "Isaiah Likely": (5, 0),
    "Josh Downs": (5, 0), "Parker Washington": (5, 3),
    "Jahmyr Gibbs": (54, 0), "Colston Loveland": (15, 0), "Trey McBride": (12, 0),
    "Jaxson Dart": (8, 0), "David Montgomery": (12, 0),
    "DJ Moore": (21, 0), "Jake Ferguson": (5, 0), "Wan'Dale Robinson": (5, 0),
    "Michael Wilson": (5, 1),
    "Tyler Shough": (5, 0), "Emeka Egbuka": (15, 0), "Rashee Rice": (12, 0),
    "Chris Godwin Jr.": (6, 0), "Nico Collins": (21, 4),
    "Quentin Johnston": (5, 0), "Bhayshul Tuten": (5, 0),
    "Jameson Williams": (9, 0), "Luther Burden III": (6, 0),
    "Rhamondre Stevenson": (6, 0), "Kyren Williams": (12, 0),
    "Jaylen Waddle": (18, 0), "Chris Olave": (21, 0), "Blake Corum": (5, 0),
    "Javonte Williams": (15, 0), "Chig Okonkwo": (5, 3), "Kyle Pitts Sr.": (6, 0),
    "Zay Flowers": (17, 0),
    "Dallas Goedert": (5, 0), "Bucky Irving": (8, 0), "Chase Brown": (17, 0),
    "Travis Etienne Jr.": (6, 0), "Drake Maye": (17, 0),
    "Puka Nacua": (12, 0), "Quinshon Judkins": (9, 0),
    "Harold Fannin Jr.": (5, 2), "Jayden Reed": (12, 0),
    "Alec Pierce": (5, 0),
    "Rachaad White": (5, 0), "DeVonta Smith": (23, 0), "Sam Darnold": (9, 0),
    "Romeo Doubs": (5, 0),
}
KEEPER_NOTES = {
    "DeVonta Smith": "rights bought from Zips for Pierce + A.Mitchell + $1 draft cash",
}

DRAFT_FILE = REF_DIR / f"flounder_draft_{SEASON}.csv"
DRAFT_FIELDS = ["pick", "team_id", "manager", "player_id", "player", "pos",
                "nfl_team", "keeper", "espn_bid", "keeper_cost", "trade_fee",
                "draft_capital", "keeper_cost_next", "nominated_by", "notes"]


def next_keeper_cost(base):
    """Flounder rule: prior value x 1.5, rounded up, minimum $5."""
    return max(5, math.ceil(float(base or 0) * 1.5))


def draft_row(pick, team_id, player_id, player, pos, pro_team_id, bid, keeper,
              nominating_team_id):
    keeper = bool(int(keeper))
    notes = KEEPER_NOTES.get(player, "")
    if keeper:
        if player not in KEEPER_COSTS:
            raise ValueError(f"keeper {player} missing from KEEPER_COSTS")
        cost, fee = KEEPER_COSTS[player]
        capital = cost + fee
        base = cost
    else:
        cost, fee = "", ""
        capital = int(bid)
        base = int(bid)
    return {
        "pick": int(pick), "team_id": int(team_id), "manager": mgr(team_id),
        "player_id": int(player_id), "player": player, "pos": pos,
        "nfl_team": PRO_TEAMS.get(int(pro_team_id), str(pro_team_id)),
        "keeper": int(keeper), "espn_bid": int(bid), "keeper_cost": cost,
        "trade_fee": fee, "draft_capital": capital,
        "keeper_cost_next": next_keeper_cost(base),
        "nominated_by": "" if keeper else mgr(nominating_team_id),
        "notes": notes,
    }


def write_draft_file(rows, path=DRAFT_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DRAFT_FIELDS)
        w.writeheader()
        for r in sorted(rows, key=lambda r: r["pick"]):
            w.writerow(r)


def load_draft(path=DRAFT_FILE):
    """player_id (int) -> draft row dict (numbers converted)."""
    out = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for k in ("pick", "team_id", "player_id", "keeper", "espn_bid",
                      "draft_capital", "keeper_cost_next"):
                r[k] = int(r[k])
            out[r["player_id"]] = r
    return out


class ESPNLeagueClient:
    BASE = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
            "{season}/segments/0/leagues/{league}")

    def __init__(self, league_id=LEAGUE_ID, season=SEASON):
        s2 = os.getenv("ESPN_S2")
        swid = os.getenv("ESPN_SWID")
        if not s2 or not swid:
            raise SystemExit(
                "Missing ESPN_S2 / ESPN_SWID in .env -- Flounder is a private "
                "league, so the API needs your espn.com login cookies. See "
                "README 'Flounder daily' setup.")
        self.url = self.BASE.format(season=season, league=league_id)
        self.s = requests.Session()
        self.s.cookies.set("espn_s2", s2, domain=".espn.com")
        self.s.cookies.set("SWID", swid, domain=".espn.com")
        self.s.headers["User-Agent"] = "Mozilla/5.0 (flounder_daily)"

    def get(self, views, params=None, headers=None):
        p = [("view", v) for v in views] + list((params or {}).items())
        r = self.s.get(self.url, params=p, headers=headers or {}, timeout=60)
        if r.status_code in (401, 403):
            raise SystemExit(
                f"ESPN returned {r.status_code} -- ESPN_S2/ESPN_SWID are "
                "missing or expired. Re-copy them from your browser cookies.")
        r.raise_for_status()
        return r.json()

    def league(self):
        return self.get(["mTeam", "mRoster", "mSettings"])

    def draft(self):
        return self.get(["mDraftDetail", "mTeam"])

    def transactions(self, scoring_period):
        return self.get(["mTransactions2"],
                        {"scoringPeriodId": scoring_period}).get("transactions", [])

    def players(self, player_ids, scoring_period):
        """Player info (onTeamId, injury, NFL team, stats) for ANY player ids
        -- used for dropped players who are no longer on a roster.
        (ESPN rejects 'limit' without a sort, so no limit here.)"""
        import json
        out = []
        ids = list(player_ids)
        for i in range(0, len(ids), 100):
            flt = {"players": {"filterIds": {"value": ids[i:i + 100]},
                               "filterStatsForTopScoringPeriodIds": {
                                   "value": 2, "additionalValue": [
                                       f"00{SEASON}", f"10{SEASON}",
                                       f"12{SEASON}",
                                       f"11{SEASON}{scoring_period}",
                                       f"01{SEASON}{scoring_period - 1}"]}}}
            out += self.get(["kona_player_info"],
                            {"scoringPeriodId": scoring_period},
                            headers={"X-Fantasy-Filter": json.dumps(flt)}
                            ).get("players", [])
        return out
