"""
League adapters for the daily fantasy report (fantasy_daily.py).

Each adapter loads one league from its platform and returns a normalized
League object, so the report logic doesn't care whether it's ESPN or Sleeper.

Leagues are configured in reference/leagues.json.

ESPN leagues are private -> need espn.com login cookies in .env:
    ESPN_S2 / ESPN_SWID   (one ESPN login covers every ESPN league)
Sleeper's API is public -> no credentials.
"""
import datetime as dt
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
REF_DIR = PROJECT_ROOT / "reference"
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_FILE = REF_DIR / "leagues.json"

UNAVAILABLE = {"OUT", "INJURY_RESERVE", "SUSPENSION"}

ESPN_PRO = {0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
            7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
            14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
            20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
            26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL",
            34: "HOU"}
ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
ESPN_SLOT = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 7: "OP", 16: "DST", 17: "K",
             20: "BE", 21: "IR", 23: "FLEX"}
# ESPN lineupSlotId -> (label, eligible positions) for starting slots
ESPN_SLOT_ELIG = {0: ("QB", {"QB"}), 2: ("RB", {"RB"}), 4: ("WR", {"WR"}),
                  6: ("TE", {"TE"}), 16: ("DST", {"DST"}), 17: ("K", {"K"}),
                  23: ("FLEX", {"RB", "WR", "TE"}),
                  7: ("OP", {"QB", "RB", "WR", "TE"}),
                  3: ("RB/WR", {"RB", "WR"}), 5: ("WR/TE", {"WR", "TE"})}
SLEEPER_SLOT_ELIG = {"QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"},
                     "K": {"K"}, "DEF": {"DST"}, "FLEX": {"RB", "WR", "TE"},
                     "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
                     "WRRB_FLEX": {"RB", "WR"}, "REC_FLEX": {"WR", "TE"}}
SLEEPER_INJ = {"Questionable": "QUESTIONABLE", "Doubtful": "DOUBTFUL",
               "Out": "OUT", "IR": "INJURY_RESERVE", "PUP": "INJURY_RESERVE",
               "Sus": "SUSPENSION", "NA": "OUT", "DNR": "OUT", "COV": "OUT"}


def pick_points(pick_no):
    """Snake-draft 'draft capital' in points: pick 1 = 100, decays ~2.5%/pick
    (pick 12 ~ 76, 24 ~ 55, 48 ~ 30, 96 ~ 9, 150+ ~ 1-2)."""
    return max(1, round(100 * 0.975 ** (int(pick_no) - 1)))


def load_config():
    return json.load(open(CONFIG_FILE, encoding="utf-8"))


def value_of(season_avg, gp, ros_avg, inj):
    """ROS per-game value: projection, nudged by actual production once a
    player has 2+ games, discounted when unavailable."""
    v = (0.6 * ros_avg + 0.4 * season_avg) if gp >= 2 else (ros_avg or season_avg)
    if inj in UNAVAILABLE:
        v *= 0.25 if inj == "INJURY_RESERVE" else 0.6
    return round(v, 2)


def player_dict(pid, name, pos, nfl, inj, proj_wk, last_wk, season_pts, gp,
                ros_avg, pct_owned=0.0):
    season_avg = season_pts / gp if gp else 0.0
    return {"id": str(pid), "name": name, "pos": pos, "nfl": nfl or "FA",
            "inj": inj or "ACTIVE", "proj_wk": round(proj_wk or 0, 1),
            "last_wk": round(last_wk or 0, 1), "season_pts": round(season_pts or 0, 1),
            "gp": gp, "season_avg": round(season_avg, 1),
            "ros_avg": round(ros_avg or 0, 1),
            "value": value_of(season_avg, gp, ros_avg or 0, inj or "ACTIVE"),
            "pct_owned": round(pct_owned or 0, 1),
            "slot": "", "locked": False, "team_id": None}


@dataclass
class League:
    key: str
    name: str
    platform: str
    cfg: dict
    my_team: object = None
    week: int = 1
    active: bool = True
    draft_type: str = "snake"        # "auction" | "snake"
    managers: dict = field(default_factory=dict)   # team_id -> short name
    slots: list = field(default_factory=list)      # [(label, {positions})]
    players: dict = field(default_factory=dict)    # pid -> player dict
    rosters: dict = field(default_factory=dict)    # team_id -> [pid]
    transactions: list = field(default_factory=list)
    draft_picks: list = field(default_factory=list)
    faab_budget: int = 0
    faab_spent: dict = field(default_factory=dict)
    trade_deadline: str = None       # ISO date, or None
    fetch_players: object = None     # fn(pids, week) -> {pid: player dict}

    def mgr(self, team_id):
        if team_id in (None, 0, "0", ""):
            return "FA"
        return self.managers.get(team_id, self.managers.get(str(team_id), f"team{team_id}"))


def order_slots(slots):
    """Fill order for the greedy optimizer: most-restrictive slots first."""
    return sorted(slots, key=lambda s: len(s[1]))


# ==========================================================================
# ESPN
# ==========================================================================
class ESPNClient:
    BASE = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
            "{season}/segments/0/leagues/{league}")

    def __init__(self, key, league_id, season):
        s2 = os.getenv("ESPN_S2")
        swid = os.getenv("ESPN_SWID")
        if not s2 or not swid:
            raise RuntimeError(
                "Missing ESPN_S2/ESPN_SWID in .env -- ESPN leagues are private and need "
                "your espn.com login cookies.")
        self.url = self.BASE.format(season=season, league=league_id)
        self.s = requests.Session()
        self.s.cookies.set("espn_s2", s2, domain=".espn.com")
        self.s.cookies.set("SWID", swid, domain=".espn.com")
        self.s.headers["User-Agent"] = "Mozilla/5.0 (fantasy_daily)"
        self.season = season

    def get(self, views, params=None, headers=None):
        p = [("view", v) for v in views] + list((params or {}).items())
        r = self.s.get(self.url, params=p, headers=headers or {}, timeout=60)
        if r.status_code in (401, 403):
            raise RuntimeError(f"ESPN returned {r.status_code} -- ESPN cookies "
                               "missing/expired; re-copy espn_s2 and SWID.")
        r.raise_for_status()
        return r.json()

    def players(self, ids, week):
        out = []
        ids = list(ids)
        for i in range(0, len(ids), 100):
            flt = {"players": {"filterIds": {"value": ids[i:i + 100]},
                               "filterStatsForTopScoringPeriodIds": {
                                   "value": 2, "additionalValue": [
                                       f"00{self.season}", f"10{self.season}",
                                       f"12{self.season}",
                                       f"11{self.season}{week}",
                                       f"01{self.season}{week - 1}"]}}}
            out += self.get(["kona_player_info"], {"scoringPeriodId": week},
                            {"X-Fantasy-Filter": json.dumps(flt)}).get("players", [])
        return out


def espn_stat(stats, season, source, split, period):
    for s in stats or []:
        if (s.get("seasonId") == season and s.get("statSourceId") == source
                and s.get("statSplitTypeId") == split
                and s.get("scoringPeriodId") == period):
            return s
    return None


def espn_player(p, season, week):
    st = p.get("stats", [])
    wk = espn_stat(st, season, 1, 1, week)
    last = espn_stat(st, season, 0, 1, week - 1) if week > 1 else None
    sea = espn_stat(st, season, 0, 0, 0) or {}
    ros = espn_stat(st, season, 1, 2, 0) or espn_stat(st, season, 1, 0, 0) or {}
    tot = sea.get("appliedTotal") or 0.0
    avg = sea.get("appliedAverage") or 0.0
    gp = round(tot / avg) if avg else 0   # ESPN only returns recent weekly rows
    return player_dict(p["id"], p.get("fullName", str(p["id"])),
                       ESPN_POS.get(p.get("defaultPositionId"), "?"),
                       ESPN_PRO.get(p.get("proTeamId", 0), "?"),
                       p.get("injuryStatus") or "ACTIVE",
                       (wk or {}).get("appliedTotal"), (last or {}).get("appliedTotal"),
                       tot, gp, ros.get("appliedAverage") or 0.0,
                       (p.get("ownership") or {}).get("percentOwned"))


def load_espn(cfg, season, raw=None):
    """raw: optional pre-fetched {"league":..., "transactions": {week: [...]},
    "players": [...]} for offline testing."""
    key = cfg["key"]
    client = None if raw else ESPNClient(key, cfg["league_id"], season)
    lg = raw["league"] if raw else client.get(["mTeam", "mRoster", "mSettings"])
    st = lg["status"]
    week = lg.get("scoringPeriodId") or st.get("latestScoringPeriod", 1)
    L = League(key=key, name=cfg.get("name") or lg.get("settings", {}).get("name", key),
               platform="espn", cfg=cfg, my_team=cfg["my_team"], week=week)
    L.active = bool(st.get("isActive")) and not st.get("isExpired") and \
        st.get("latestScoringPeriod", 0) <= st.get("finalScoringPeriod", 18)
    settings = lg.get("settings") or {}
    L.draft_type = "auction" if (settings.get("draftSettings") or {}).get("type") == "AUCTION" else "snake"
    acq = settings.get("acquisitionSettings") or {}
    L.faab_budget = acq.get("acquisitionBudget", 0) if acq.get("isUsingAcquisitionBudget") else 0
    dl = (settings.get("tradeSettings") or {}).get("deadlineDate")
    L.trade_deadline = dt.datetime.fromtimestamp(dl / 1000).date().isoformat() if dl else None

    # managers: config overrides, else member first names
    mem = {m["id"]: m for m in lg.get("members", [])}
    first = {}
    for t in lg["teams"]:
        o = mem.get(t.get("primaryOwner") or (t.get("owners") or [None])[0], {})
        first[t["id"]] = ((o.get("firstName") or "").strip().title(),
                          (o.get("lastName") or "").strip().title())
    counts = {}
    for f, _ in first.values():
        counts[f] = counts.get(f, 0) + 1
    for tid, (f, l) in first.items():
        L.managers[tid] = f if counts.get(f, 0) == 1 and f else f"{f} {l[:1]}".strip() or f"team{tid}"
    for k, v in (cfg.get("managers") or {}).items():
        L.managers[int(k)] = v
    L.managers[cfg["my_team"]] = "Jake"

    counts_ = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    for sid, n in counts_.items():
        if int(sid) in ESPN_SLOT_ELIG:
            L.slots += [ESPN_SLOT_ELIG[int(sid)]] * int(n)
    L.slots = order_slots(L.slots)

    for t in lg["teams"]:
        tid = t["id"]
        L.rosters[tid] = []
        L.faab_spent[tid] = (t.get("transactionCounter") or {}).get("acquisitionBudgetSpent")
        for e in (t.get("roster") or {}).get("entries", []):
            ppe = e["playerPoolEntry"]
            d = espn_player(ppe["player"], season, week)
            d.update(team_id=tid, slot=ESPN_SLOT.get(e.get("lineupSlotId"), "?"),
                     locked=bool(ppe.get("lineupLocked")))
            if e.get("injuryStatus"):
                d["inj"] = e["injuryStatus"]
            L.players[d["id"]] = d
            L.rosters[tid].append(d["id"])

    # transactions (all weeks; the engine skips ones it has already seen)
    for w in range(1, week + 1):
        txs = raw["transactions"].get(str(w), []) if raw else \
            client.get(["mTransactions2"], {"scoringPeriodId": w}).get("transactions", [])
        for t in txs:
            if t.get("status") != "EXECUTED":
                continue
            items = [i for i in t.get("items", []) if i["type"] in ("ADD", "DROP", "TRADE")]
            if t["type"] in ("WAIVER", "FREEAGENT"):
                typ = "WAIVER" if t["type"] == "WAIVER" else "FA"
            elif t["type"].startswith("TRADE") and any(i["type"] == "TRADE" for i in items):
                typ = "TRADE"
            else:
                continue
            L.transactions.append({
                "id": str(t["id"]), "type": typ, "team": t.get("teamId"),
                "bid": t.get("bidAmount") or 0,
                "ts": t.get("processDate") or t.get("proposedDate", 0),
                "items": [{"type": i["type"], "pid": str(i["playerId"]),
                           "from": i.get("fromTeamId"), "to": i.get("toTeamId")}
                          for i in items]})

    def fetch(pids, wk):
        rows = raw.get("players", []) if raw else \
            client.players([int(p) for p in pids], wk)
        out = {}
        for r in rows:
            d = espn_player(r["player"], season, wk)
            out[d["id"]] = d
        return out
    L.fetch_players = fetch

    def draft():
        j = client.get(["mDraftDetail"])
        picks = j["draftDetail"]["picks"]
        info = fetch([p["playerId"] for p in picks], 1)
        out = []
        for p in picks:
            pl = info.get(str(p["playerId"]), {"name": str(p["playerId"]), "pos": "?", "nfl": "?"})
            out.append({"pick": p["overallPickNumber"], "round": p.get("roundId"),
                        "round_pick": p.get("roundPickNumber"), "team_id": p["teamId"],
                        "player_id": str(p["playerId"]), "player": pl["name"],
                        "pos": pl["pos"], "nfl": pl["nfl"], "bid": p.get("bidAmount") or 0,
                        "keeper": 1 if p.get("keeper") else 0,
                        "nominated_by": p.get("nominatingTeamId", 0)})
        return out
    L.load_draft_picks = draft
    return L


# ==========================================================================
# Sleeper (public API: api.sleeper.app, projections/stats: api.sleeper.com)
# ==========================================================================
SLEEPER = "https://api.sleeper.app/v1"
SLEEPER_STATS = "https://api.sleeper.com"
POS_Q = "&".join(f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF"))


def _sget(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2)


def sleeper_players_db():
    """Full Sleeper player DB (~5MB), cached for a day in data/."""
    f = DATA_DIR / "sleeper_players_nfl.json"
    if not f.exists() or time.time() - f.stat().st_mtime > 86400:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db = _sget(f"{SLEEPER}/players/nfl")
        slim = {pid: {k: p.get(k) for k in ("full_name", "first_name", "last_name",
                                              "position", "team", "injury_status")}
                for pid, p in db.items()}
        json.dump(slim, open(f, "w", encoding="utf-8"))
    return json.load(open(f, encoding="utf-8"))


def sleeper_points(stats, scoring):
    if not stats:
        return 0.0
    pts = sum(float(stats.get(k, 0) or 0) * float(v) for k, v in scoring.items())
    if pts == 0 and stats.get("pts_ppr"):
        pts = float(stats["pts_ppr"])
    return pts


def load_sleeper(cfg, season, raw=None):
    key, lid = cfg["key"], cfg["league_id"]
    g = (lambda u: raw[u]) if raw else _sget
    lg = g(f"{SLEEPER}/league/{lid}")
    users = g(f"{SLEEPER}/league/{lid}/users")
    rosters = g(f"{SLEEPER}/league/{lid}/rosters")
    state = g(f"{SLEEPER}/state/nfl")
    week = max(1, int(state.get("display_week") or state.get("week") or 1))
    L = League(key=key, name=cfg.get("name") or lg["name"], platform="sleeper", cfg=cfg,
               week=week)
    L.active = lg.get("status") == "in_season" and state.get("season_type") == "regular"
    ls = lg.get("settings") or {}
    L.draft_type = "snake"
    L.faab_budget = ls.get("waiver_budget", 0) if ls.get("waiver_type") == 2 else 0
    dlw = ls.get("trade_deadline")
    L.trade_deadline = f"week {dlw}" if dlw and dlw < 99 else None
    L.cfg["deadline_week"] = dlw
    scoring = lg.get("scoring_settings") or {}

    uname = {u["user_id"]: u.get("display_name") or u["user_id"] for u in users}
    for r in rosters:
        rid = r["roster_id"]
        L.managers[rid] = (cfg.get("managers") or {}).get(str(rid)) or uname.get(r.get("owner_id"), f"team{rid}")
        if uname.get(r.get("owner_id")) == cfg["my_user"]:
            L.my_team = rid
            L.managers[rid] = "Jake"
    if L.my_team is None:
        raise RuntimeError(f"Sleeper user {cfg['my_user']} not found in league {lid}")

    starters_pos = [p for p in lg["roster_positions"] if p not in ("BN", "IR", "TAXI")]
    L.slots = order_slots([(p if p != "DEF" else "DST", SLEEPER_SLOT_ELIG[p])
                           for p in starters_pos if p in SLEEPER_SLOT_ELIG])

    db = raw["players_db"] if raw else sleeper_players_db()

    def proj_week(w):
        rows = g(f"{SLEEPER_STATS}/projections/nfl/{season}/{w}?season_type=regular&{POS_Q}")
        return {r["player_id"]: sleeper_points(r.get("stats"), scoring) for r in rows}

    def stats_week(w):
        rows = g(f"{SLEEPER_STATS}/stats/nfl/{season}/{w}?season_type=regular&{POS_Q}")
        return {r["player_id"]: r.get("stats") or {} for r in rows}

    proj = {w: proj_week(w) for w in range(week, min(week + 4, 19))}
    season_rows = g(f"{SLEEPER_STATS}/stats/nfl/{season}?season_type=regular&{POS_Q}")
    sea = {r["player_id"]: r.get("stats") or {} for r in season_rows}
    last = stats_week(week - 1) if week > 1 else {}
    L.cfg["_snaps"] = {}   # pid -> {week: snap%}, filled lazily by snap trend
    L.cfg["_stats_week"] = stats_week

    def mk(pid):
        p = db.get(pid, {})
        pos = p.get("position") or "?"
        pos = "DST" if pos == "DEF" else pos
        name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid
        if pos == "DST":
            name = f"{pid} D/ST"
        s = sea.get(pid, {})
        gp = int(s.get("gp") or 0)
        ros_vals = [proj[w].get(pid) for w in proj if proj[w].get(pid)]
        ros = sum(ros_vals) / len(ros_vals) if ros_vals else 0.0
        return player_dict(pid, name, pos, p.get("team") or (pid if pos == "DST" else "FA"),
                           SLEEPER_INJ.get(p.get("injury_status") or "", "ACTIVE"),
                           proj.get(week, {}).get(pid, 0.0),
                           sleeper_points(last.get(pid), scoring),
                           sleeper_points(s, scoring), gp, ros)

    for r in rosters:
        rid = r["roster_id"]
        L.rosters[rid] = []
        L.faab_spent[rid] = (r.get("settings") or {}).get("waiver_budget_used", 0)
        starters = r.get("starters") or []
        reserve = set(r.get("reserve") or [])
        slot_of = {}
        for i, pid in enumerate(starters):
            if pid and pid != "0" and i < len(starters_pos):
                lbl = starters_pos[i]
                slot_of[pid] = {"DEF": "DST", "SUPER_FLEX": "OP"}.get(lbl, lbl)
        for pid in r.get("players") or []:
            d = mk(pid)
            d.update(team_id=rid, slot=slot_of.get(pid, "IR" if pid in reserve else "BE"))
            L.players[pid] = d
            L.rosters[rid].append(pid)
    # SUPER_FLEX shows as OP in the lineup table
    L.slots = [("OP" if lbl == "SUPER_FLEX" else lbl, e) for lbl, e in L.slots]

    for w in range(1, week + 1):
        for t in g(f"{SLEEPER}/league/{lid}/transactions/{w}"):
            if t.get("status") != "complete" or t["type"] not in ("waiver", "free_agent", "trade"):
                continue
            adds, drops = t.get("adds") or {}, t.get("drops") or {}
            items = []
            if t["type"] == "trade":
                for pid, to in adds.items():
                    items.append({"type": "TRADE", "pid": pid, "from": drops.get(pid), "to": to})
                for dp in t.get("draft_picks") or []:
                    items.append({"type": "PICK", "pid": None, "from": dp.get("previous_owner_id"),
                                  "to": dp.get("owner_id"),
                                  "desc": f"{dp.get('season')} R{dp.get('round')} pick "
                                          f"(orig. {L.mgr(dp.get('roster_id'))})",
                                  "round": dp.get("round")})
                typ = "TRADE"
            else:
                for pid, to in adds.items():
                    items.append({"type": "ADD", "pid": pid, "from": 0, "to": to})
                for pid, fr in drops.items():
                    items.append({"type": "DROP", "pid": pid, "from": fr, "to": 0})
                typ = "WAIVER" if t["type"] == "waiver" else "FA"
            L.transactions.append({
                "id": str(t["transaction_id"]), "type": typ,
                "team": (t.get("roster_ids") or [None])[0],
                "bid": (t.get("settings") or {}).get("waiver_bid") or 0,
                "ts": t.get("status_updated") or t.get("created", 0), "items": items})

    L.fetch_players = lambda pids, wk: {p: mk(p) for p in pids}

    def draft():
        out = []
        for d in g(f"{SLEEPER}/league/{lid}/drafts"):
            for p in g(f"{SLEEPER}/draft/{d['draft_id']}/picks"):
                pl = mk(p["player_id"])
                out.append({"pick": p["pick_no"], "round": p.get("round"),
                            "round_pick": p.get("draft_slot"), "team_id": p.get("roster_id"),
                            "player_id": p["player_id"], "player": pl["name"],
                            "pos": pl["pos"], "nfl": pl["nfl"],
                            "bid": (p.get("metadata") or {}).get("amount") or 0,
                            "keeper": 1 if p.get("is_keeper") else 0, "nominated_by": ""})
        return out
    L.load_draft_picks = draft
    return L


LOADERS = {"espn": load_espn, "sleeper": load_sleeper}


def load_league(cfg, season, raw=None):
    return LOADERS[cfg["platform"]](cfg, season, raw)
