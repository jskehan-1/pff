"""
Daily fantasy report for ALL of Jake's season-long leagues (reference/leagues.json):
Flounder (ESPN auction keeper), Schmidt (ESPN snake), Sleeper league (snake).

Per league:
 1. Roster moves across every team since the last run (first run = season
    backfill), each with a DRAFT-CAPITAL view (auction $ / FAAB, or snake
    round+pick with pick-value points), production and ROS value. Trades
    get a side-by-side capital vs value check.
 2. Dropped players: everyone rostered at some point this season who is now
    unrostered -- status, points, projection, who dropped them.
 3. Sit/start for Jake's team (platform projections in league scoring; ESPN
    locked players stay put) + PFF snap-share trend.
 4. Top-3 trade targets (tracked day to day) with offers that never give up
    more perceived draft capital than the target cost.

Output: FantasyDaily.html (dashboard, one tab per league) in the project root,
plus data/<league>/reports/<league>_YYYYMMDD.md.

Usage:
    python scripts\\fantasy_daily.py                    # all leagues
    python scripts\\fantasy_daily.py --league schmidt   # one league
    python scripts\\fantasy_daily.py --dry-run          # don't save state
    python scripts\\fantasy_daily.py --force            # run outside the season
    python scripts\\fantasy_daily.py --rebuild-draft    # regenerate draft files
"""
import argparse
import csv
import datetime as dt
import html
import itertools
import json
import re
import sys
import traceback
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fantasy_leagues as fl  # noqa: E402

SEASON = 2026
DASHBOARD = fl.PROJECT_ROOT / "FantasyDaily.html"
UNAVAILABLE = fl.UNAVAILABLE
SHAKY = {"QUESTIONABLE", "DOUBTFUL", "DAY_TO_DAY"}
INJ_SHORT = {"ACTIVE": "", "QUESTIONABLE": "Q", "DOUBTFUL": "D", "OUT": "OUT",
             "INJURY_RESERVE": "IR", "SUSPENSION": "SUSP", "DAY_TO_DAY": "DTD",
             "PROBABLE": "P"}

MIN_GAIN = 0.75        # pts/wk lineup improvement to count as a trade target
THEIR_MAX_LOSS = 1.0   # offer may not cost the other team > 1 pt/wk of lineup
VALUE_PARITY = 0.85    # package value (best + half the rest) >= 85% of target


# --------------------------------------------------------------------------
# draft capital
# --------------------------------------------------------------------------
def draft_file(key):
    return fl.REF_DIR / f"{key}_draft_{SEASON}.csv"


def build_draft_file(L):
    """Regenerate reference/<key>_draft_<season>.csv from the platform."""
    picks = L.load_draft_picks()
    path = draft_file(L.key)
    if L.key == "flounder":
        import flounder_common as fc  # official keeper costs + trade fees
        rows = [fc.draft_row(p["pick"], p["team_id"], p["player_id"], p["player"],
                             p["pos"], 0, p["bid"], p["keeper"], p["nominated_by"])
                for p in picks]
        for r, p in zip(rows, picks):
            r["nfl_team"] = p["nfl"]
        fc.write_draft_file(rows, path)
        return
    fields = ["pick", "round", "round_pick", "team_id", "manager", "player_id",
              "player", "pos", "nfl_team", "keeper", "bid", "draft_capital",
              "keeper_cost_next", "notes"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in sorted(picks, key=lambda p: p["pick"]):
            cap = p["bid"] if L.draft_type == "auction" else fl.pick_points(p["pick"])
            w.writerow({"pick": p["pick"], "round": p["round"],
                        "round_pick": p["round_pick"], "team_id": p["team_id"],
                        "manager": L.mgr(p["team_id"]), "player_id": p["player_id"],
                        "player": p["player"], "pos": p["pos"], "nfl_team": p["nfl"],
                        "keeper": p["keeper"], "bid": p["bid"], "draft_capital": cap,
                        "keeper_cost_next": "", "notes": ""})
    print(f"[{L.key}] wrote {path} ({len(picks)} picks)")


def load_draft(key):
    path = draft_file(key)
    out = {}
    if not path.exists():
        return out
    for r in csv.DictReader(open(path, encoding="utf-8")):
        r["pick"] = int(r["pick"])
        r["keeper"] = int(r.get("keeper") or 0)
        r["draft_capital"] = int(float(r["draft_capital"] or 0))
        out[str(r["player_id"])] = r
    return out


class Capital:
    """Perceived capital: auction $ (keepers at official cost) / FAAB, or
    snake pick-value points (pick 1 = 100)."""

    def __init__(self, L, draft, acq):
        self.L, self.draft, self.acq = L, draft, acq
        self.auction = L.draft_type == "auction"
        self.keeper = L.cfg.get("keeper")          # {"multiplier", "min"} or None
        self.slack = 2 if self.auction else 5

    def unit(self, n):
        return f"${n}" if self.auction else f"{n} pts"

    def of(self, pid, original=False):
        d = self.draft.get(pid)
        a = self.acq.get(pid)
        if a and not (original and d):
            if self.auction:
                return a.get("faab", 0), f"FAAB ${a.get('faab', 0)} ({a.get('mgr')} {a.get('date')})"
            return 0, f"waiver/FA ({a.get('mgr')} {a.get('date')}" + \
                (f", FAAB ${a['faab']})" if a.get("faab") else ")")
        if d:
            if self.auction:
                kind = "keeper" if d["keeper"] else f"pick {d['pick']}"
                return d["draft_capital"], f"${d['draft_capital']} ({d['manager']} {kind})"
            rp = f"R{d.get('round')}.{int(d.get('round_pick') or 0):02d}" if d.get("round") else f"#{d['pick']}"
            return d["draft_capital"], f"{rp} ({d['manager']}, {d['draft_capital']} pts)"
        return 0, "undrafted"

    def next_keeper(self, pid):
        if not self.keeper:
            return None
        a = self.acq.get(pid)
        d = self.draft.get(pid)
        if a:
            base = a.get("faab", 0)
        elif d and d.get("keeper_cost_next"):
            return int(d["keeper_cost_next"])
        else:
            base = d["draft_capital"] if d else 0
        return max(self.keeper["min"], int(-(-base * self.keeper["multiplier"] // 1)))

    def keeper_txt(self, pid):
        k = self.next_keeper(pid)
        return f", next-yr keeper ${k}" if k is not None else ""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def best_lineup(slots, plist, key="value", fixed=None):
    """Greedy is optimal: slots are pre-ordered most-restrictive first and
    each flex slot's eligibility is a superset of the earlier ones."""
    fixed = fixed or {}
    used = {p["id"] for p in fixed.values()}
    pool = sorted((p for p in plist if p["id"] not in used), key=lambda p: p[key], reverse=True)
    chosen = dict(fixed)
    for i, (lbl, elig) in enumerate(slots):
        if i in chosen:
            continue
        for p in pool:
            if p["pos"] in elig and p["id"] not in used:
                chosen[i] = p
                used.add(p["id"])
                break
    out = [(lbl, chosen.get(i)) for i, (lbl, _) in enumerate(slots)]
    return round(sum(p[key] for _, p in out if p), 2), out


def pline(p):
    inj = INJ_SHORT.get(p.get("inj", ""), p.get("inj", ""))
    return f"{p['name']} ({p['pos']}, {p['nfl']})" + (f" [{inj}]" if inj else "")


def prod(p):
    return (f"{p['season_pts']} pts/{p['gp']}gp, ROS {p['ros_avg']}/wk, "
            f"wk proj {p['proj_wk']}")


def tsdate(ms, fmt="%Y-%m-%d"):
    return dt.datetime.fromtimestamp((ms or 0) / 1000).strftime(fmt)


def norm(n):
    n = n.lower().replace(".", "").replace("'", "")
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"[^a-z]", "", n)


def snap_trends():
    f = fl.DATA_DIR / f"snap_counts_{SEASON}.csv"
    if not f.exists():
        return {}
    rows = list(csv.DictReader(open(f, encoding="utf-8")))
    team = defaultdict(int)
    for r in rows:
        if r["position"] == "QB":
            team[(r["team"], r["week"])] += int(r["snap_counts_offense"] or 0)
    by = defaultdict(dict)
    for r in rows:
        t = team[(r["team"], r["week"])]
        if t:
            by[norm(r["player_name"])][int(r["week"])] = round(
                100 * int(r["snap_counts_offense"] or 0) / t)
    out = {}
    for k, wks in by.items():
        ws = sorted(wks)[-2:]
        out[k] = "→".join(f"{wks[w]}%" for w in ws) + f" (wk {'-'.join(map(str, ws))})"
    return out


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------
def apply_tx(L, t, acq, ever):
    date = tsdate(t["ts"])
    for i in t["items"]:
        pid = i.get("pid")
        if not pid:
            continue
        if i["type"] == "ADD":
            acq[pid] = {"faab": t["bid"], "mgr": L.mgr(i["to"]), "date": date}
            ever.setdefault(pid, {"first_team": L.mgr(i["to"])})
        elif i["type"] == "TRADE" and pid in acq:
            acq[pid]["mgr"] = L.mgr(i["to"])
        elif i["type"] == "DROP":
            ever.setdefault(pid, {}).update(dropped_by=L.mgr(i["from"]), dropped_on=date)
            acq.pop(pid, None)


def section_moves(L, new_tx, info, cap, acq, ever):
    out = ["## Roster moves", ""]
    if not new_tx:
        return out + ["No adds, drops or trades since the last run.", ""]
    out += ["_Capital = what the team paid: " + (
        "draft $ (keepers at official keeper cost + trade fee) or FAAB bid"
        + (". Next-yr keeper = 1.5x, min $5" if cap.keeper else "")
        if cap.auction else "snake pick value (pick 1 = 100 pts, ~-2.5%/pick); "
        "waiver/FA pickups = 0 pts") + "._", ""]
    for t in new_tx:
        date = tsdate(t["ts"], "%a %m/%d")
        if t["type"] == "TRADE":
            sides = defaultdict(list)
            for i in t["items"]:
                sides[i["from"]].append(i)
            teams = sorted(sides, key=str)
            out.append(f"### TRADE {date}: " + " ↔ ".join(L.mgr(x) for x in teams))
            summ = {}
            for tm in teams:
                c_tot = v_tot = 0
                out.append(f"- **{L.mgr(tm)} gives:**")
                for i in sides[tm]:
                    if i["type"] == "PICK":
                        out.append(f"  - {i['desc']}")
                        c_tot += fl.pick_points((i.get("round", 8) - 1) * 12 + 6) if not cap.auction else 0
                        continue
                    p = info(i["pid"])
                    c, cdesc = cap.of(i["pid"])
                    c_tot += c
                    v_tot += p["value"]
                    out.append(f"  - {pline(p)} — capital {cdesc}{cap.keeper_txt(i['pid'])}; {prod(p)}")
                summ[tm] = (c_tot, v_tot)
            if len(teams) == 2:
                a, b = teams
                (ca, va), (cb, vb) = summ[a], summ[b]
                out.append(f"- **Capital:** {L.mgr(a)} gave {cap.unit(ca)} vs {L.mgr(b)} "
                           f"gave {cap.unit(cb)}. **ROS value:** {va:.1f} vs {vb:.1f} pts/wk.")
                if max(ca, cb) >= 10 and abs(ca - cb) >= 0.25 * max(ca, cb):
                    over = a if ca > cb else b
                    out.append(f"- ⚠️ {L.mgr(over)} gave up {cap.unit(abs(ca - cb))} more "
                               "draft capital — note this price point for trade talks.")
            out.append("")
            apply_tx(L, t, acq, ever)
            continue
        head = f"### {L.mgr(t['team'])} — {t['type']} {date}"
        if t["type"] == "WAIVER" and L.faab_budget:
            spent = L.faab_spent.get(t["team"])
            head += f" (FAAB ${t['bid']}" + (f"; ${L.faab_budget - spent} left now)" if spent is not None else ")")
        out.append(head)
        for i in t["items"]:
            p = info(i["pid"])
            if i["type"] == "ADD":
                k = ""
                if cap.keeper:
                    k = f"; next-yr keeper ${max(cap.keeper['min'], int(-(-t['bid'] * cap.keeper['multiplier'] // 1)))}"
                cost = f"${t['bid']} FAAB" if L.faab_budget else "waiver/FA"
                out.append(f"- ➕ {pline(p)} — cost {cost}{k}; {prod(p)}")
        for i in t["items"]:
            if i["type"] == "DROP":
                p = info(i["pid"])
                c, cdesc = cap.of(i["pid"])
                big = 5 if cap.auction else 30
                sunk = f" — **{cap.unit(c)} of draft capital cut loose**" if c >= big else ""
                out.append(f"- ➖ {pline(p)} — capital {cdesc}{sunk}; {prod(p)}")
        out.append("")
        apply_tx(L, t, acq, ever)
    return out


def section_dropped(L, ever, dropped, cap, my_players):
    out = ["## Dropped players (ever rostered, now unrostered)", ""]
    rostered = {p for ids in L.rosters.values() for p in ids}
    rows = []
    for pid, e in ever.items():
        if pid in rostered or pid not in dropped:
            continue
        rows.append((dropped[pid], e, cap.of(pid, original=True)[0]))
    if not rows:
        return out + ["None.", ""], []
    rows.sort(key=lambda r: (r[0]["value"], r[0]["season_pts"]), reverse=True)
    _, lu = best_lineup(L.slots, my_players, "proj_wk")
    weakest = {}
    for _, p in lu:
        if p:
            weakest[p["pos"]] = min(weakest.get(p["pos"], 99), p["proj_wk"])
    out += ["| Player | Status | Dropped by | Draft capital | Season | Last wk | Wk proj | ROS/wk | |",
            "|---|---|---|---|---|---|---|---|---|"]
    for p, e, c in rows[:30]:
        st = INJ_SHORT.get(p["inj"], p["inj"]) or "Active"
        if p["nfl"] == "FA":
            st += " (no NFL team)"
        flag = "📈 beats your starter" if p["proj_wk"] > weakest.get(p["pos"], 99) else ""
        out.append(f"| {p['name']} ({p['pos']}, {p['nfl']}) | {st} | "
                   f"{e.get('dropped_by', '?')} {e.get('dropped_on', '')} | {cap.unit(c)} | "
                   f"{p['season_pts']} | {p['last_wk']} | {p['proj_wk']} | {p['ros_avg']} | {flag} |")
    if len(rows) > 30:
        out.append(f"\n({len(rows) - 30} more in data/{L.key}/dropped_players.csv)")
    return out + [""], rows


def section_sit_start(L, my_players, snaps):
    out = [f"## Sit / start — Week {L.week}", ""]
    fixed, locked_bench = {}, set()
    idx = defaultdict(list)
    for i, (lbl, _) in enumerate(L.slots):
        idx[lbl].append(i)
    for p in my_players:
        if p["locked"]:
            if idx.get(p["slot"]):
                fixed[idx[p["slot"]].pop(0)] = p
            else:
                locked_bench.add(p["id"])
    avail = [p for p in my_players if p["id"] not in locked_bench and p["inj"] not in UNAVAILABLE]
    total, lu = best_lineup(L.slots, avail, "proj_wk", fixed)
    cur = sum(p["proj_wk"] for p in my_players if p["slot"] not in ("BE", "IR", "?", ""))
    starters = {p["id"] for _, p in lu if p}
    order = ["QB", "RB", "WR", "TE", "RB/WR", "WR/TE", "FLEX", "OP", "K", "DST"]
    lu = sorted(lu, key=lambda x: order.index(x[0]) if x[0] in order else 99)
    out += ["| Slot | Start | Proj | Snap % trend | Note |", "|---|---|---|---|---|"]
    for lbl, p in lu:
        if not p:
            out.append(f"| {lbl} | — (empty!) | | | |")
            continue
        note = []
        if p["locked"]:
            note.append("locked")
        if p["inj"] in SHAKY:
            note.append(f"⚠️ {p['inj'].title()}")
        if p["slot"] in ("BE", "IR"):
            note.append("**move in from bench**")
        out.append(f"| {lbl} | {pline(p)} | {p['proj_wk']} | "
                   f"{snaps.get(norm(p['name']), '')} | {', '.join(note)} |")
    out += ["", f"Optimal projected: **{total}** vs current lineup **{round(cur, 1)}**."]
    benched = [p for p in my_players if p["slot"] not in ("BE", "IR", "?", "") and p["id"] not in starters]
    if benched:
        out.append("**Bench these:** " + ", ".join(f"{p['name']} ({p['proj_wk']})" for p in benched))
    hurt = [p for p in my_players if p["inj"] in UNAVAILABLE]
    if hurt:
        out.append("**Unavailable:** " + ", ".join(
            f"{p['name']} ({INJ_SHORT.get(p['inj'], p['inj'])})" for p in hurt))
    if L.platform == "sleeper":
        out.append("_Sleeper doesn't expose lineup locks — skip anyone whose game has started._")
    return out + [""]


def deadline_passed(L):
    dlw = L.cfg.get("deadline_week")
    if L.platform == "sleeper" and dlw:
        return L.week > int(dlw)
    return bool(L.trade_deadline and dt.date.today().isoformat() > L.trade_deadline)


def section_trade_targets(L, cap, prev_targets):
    out = ["## Top 3 trade targets", ""]
    if deadline_passed(L):
        return out + [f"Trade deadline ({L.trade_deadline}) has passed.", ""], []
    P = L.players
    mine = [P[i] for i in L.rosters[L.my_team]]
    base = best_lineup(L.slots, mine)[0]
    cands, their_base = [], {}
    for tid, ids in L.rosters.items():
        if tid == L.my_team:
            continue
        their_base[tid] = best_lineup(L.slots, [P[i] for i in ids])[0]
        for pid in ids:
            t = P[pid]
            if t["inj"] in UNAVAILABLE or t["pos"] in ("DST", "K"):
                continue
            gain = best_lineup(L.slots, mine + [t])[0] - base
            if gain >= MIN_GAIN:
                cands.append((gain, t, tid))
    cands.sort(key=lambda c: c[0], reverse=True)
    results = []
    for gain, t, tid in cands[:80]:
        tcap = cap.of(t["id"])[0]
        theirs = [P[i] for i in L.rosters[tid] if i != t["id"]]
        best = None
        for n in (1, 2):
            for pkg in itertools.combinations(mine, n):
                gcap = sum(cap.of(p["id"])[0] for p in pkg)
                if gcap > tcap + cap.slack:
                    continue  # never give up more perceived capital than we get
                vals = sorted((p["value"] for p in pkg), reverse=True)
                if vals[0] + 0.5 * sum(vals[1:]) < VALUE_PARITY * t["value"]:
                    continue  # lopsided on paper
                ids = {p["id"] for p in pkg}
                my_net = best_lineup(L.slots, [p for p in mine if p["id"] not in ids] + [t])[0] - base
                if my_net < MIN_GAIN:
                    continue
                their_net = best_lineup(L.slots, theirs + list(pkg))[0] - their_base[tid]
                if their_net < -THEIR_MAX_LOSS:
                    continue  # they'd be giving away lineup value
                score = my_net + 0.5 * min(their_net, 2) - 0.05 * abs(tcap - gcap)
                if best is None or score > best[0]:
                    best = (score, pkg, gcap, my_net, their_net)
        if best:
            results.append((best[0], gain, t, tid, tcap, best))
    results.sort(key=lambda r: r[0], reverse=True)
    top, seen = [], set()
    for r in results:
        if r[2]["id"] not in seen:
            seen.add(r[2]["id"])
            top.append(r)
        if len(top) == 3:
            break
    prev = {x["id"]: x for x in prev_targets}
    today = dt.date.today().isoformat()
    new_list = []
    for rank, (_, gain, t, tid, tcap, (_, pkg, gcap, my_net, their_net)) in enumerate(top, 1):
        since = prev[t["id"]]["since"] if t["id"] in prev else today
        tag = "NEW" if t["id"] not in prev else f"on list since {since}"
        out += [f"**{rank}. {pline(t)}** — {L.mgr(tid)}'s team ({tag})",
                f"- Adds **+{gain:.1f} pts/wk** to your best ROS lineup; {prod(t)}",
                f"- Their perceived capital: **{cap.of(t['id'])[1]}**{cap.keeper_txt(t['id'])}",
                "- Suggested offer: " + " + ".join(
                    f"{p['name']} ({cap.of(p['id'])[1]}, ROS {p['ros_avg']}/wk"
                    f"{cap.keeper_txt(p['id'])})" for p in pkg)
                + f" = **{cap.unit(gcap)}** → your net **+{my_net:.1f}/wk**, "
                  f"their lineup {their_net:+.1f}/wk", ""]
        new_list.append({"id": t["id"], "name": t["name"], "team": L.mgr(tid), "since": since})
    if not top:
        out += ["No trade found that improves your lineup without overspending draft capital.", ""]
    gone = [x for k, x in prev.items() if k not in {n["id"] for n in new_list}]
    if gone:
        out += ["Dropped off the list: " + ", ".join(f"{x['name']} ({x['team']})" for x in gone), ""]
    out += [f"_ROS value = platform rest-of-season projection blended 60/40 with actual "
            f"avg (after 2 games). Offers give up at most the target's capital + "
            f"{cap.unit(cap.slack)}, cost the other team ≤{THEIR_MAX_LOSS} pt/wk of "
            f"lineup, and are ≥{int(VALUE_PARITY * 100)}% of the target's value on paper._", ""]
    return out, new_list


# --------------------------------------------------------------------------
def run_league(cfg, a, snaps, raw=None):
    L = fl.load_league(cfg, SEASON, raw)
    if a.rebuild_draft or not draft_file(L.key).exists():
        build_draft_file(L)
        if a.rebuild_draft:
            return None
    if not L.active and not a.force:
        return f"# {L.name}\n\nSeason not active — nothing to report.\n"

    ddir = fl.DATA_DIR / L.key
    state_f = ddir / "state.json"
    state = json.load(open(state_f, encoding="utf-8")) if state_f.exists() else {}
    first = not state
    seen = set(map(str, state.get("seen_tx", [])))
    acq = {str(k): v for k, v in state.get("acq", {}).items()}
    ever = {str(k): v for k, v in state.get("ever", {}).items()}
    draft = load_draft(L.key)
    cap = Capital(L, draft, acq)

    new_tx = sorted((t for t in L.transactions if t["id"] not in seen), key=lambda t: t["ts"])
    for pid, d in draft.items():
        ever.setdefault(pid, {"name": d["player"], "first_team": d["manager"]})
    for pid, p in L.players.items():
        ever.setdefault(pid, {"name": p["name"], "first_team": L.mgr(p["team_id"])})
    for t in new_tx:
        for i in t["items"]:
            if i.get("pid"):
                ever.setdefault(i["pid"], {})

    rostered = {p for ids in L.rosters.values() for p in ids}
    need = sorted(p for p in ever if p not in rostered)
    extra = L.fetch_players(need, L.week) if need else {}

    def info(pid):
        return L.players.get(pid) or extra.get(pid) or fl.player_dict(
            pid, ever.get(pid, {}).get("name", pid), "?", "?", "ACTIVE", 0, 0, 0, 0, 0)

    my_players = [L.players[i] for i in L.rosters[L.my_team]]
    R = [f"# {L.name} — Week {L.week}", ""]
    if first:
        R += ["_First run: roster moves are a season-to-date backfill since the draft._", ""]
    # moves first: it replays new transactions into acq/ever (capital as of
    # each move), which the trade-target and dropped sections then rely on
    moves = section_moves(L, new_tx, info, cap, acq, ever)
    R += section_sit_start(L, my_players, snaps)
    tt, new_targets = section_trade_targets(L, cap, state.get("targets", []))
    R += tt + moves
    dsec, drows = section_dropped(L, ever, extra, cap, my_players)
    R += dsec
    text = "\n".join(R)

    if not a.dry_run:
        (ddir / "reports").mkdir(parents=True, exist_ok=True)
        (ddir / "reports" / f"{L.key}_{dt.date.today():%Y%m%d}.md").write_text(text, encoding="utf-8")
        with open(ddir / "dropped_players.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["player", "pos", "nfl", "status", "dropped_by", "dropped_on",
                        "draft_capital", "season_pts", "last_wk", "proj_wk", "ros_avg"])
            for p, e, c in drows:
                w.writerow([p["name"], p["pos"], p["nfl"], p["inj"], e.get("dropped_by"),
                            e.get("dropped_on"), c, p["season_pts"], p["last_wk"],
                            p["proj_wk"], p["ros_avg"]])
        log = ddir / "transactions_log.csv"
        new_file = not log.exists()
        with open(log, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["date", "type", "team", "item", "player_id", "player", "from", "to", "faab"])
            for t in new_tx:
                for i in t["items"]:
                    w.writerow([tsdate(t["ts"]), t["type"], L.mgr(t["team"]), i["type"],
                                i.get("pid") or "", info(i["pid"])["name"] if i.get("pid") else i.get("desc"),
                                L.mgr(i["from"]), L.mgr(i["to"]), t["bid"]])
        state.update(last_run=dt.datetime.now().isoformat(timespec="seconds"),
                     seen_tx=sorted(seen | {t["id"] for t in new_tx}), acq=acq, ever=ever,
                     targets=new_targets)
        json.dump(state, open(state_f, "w", encoding="utf-8"), indent=1)
    return text


# --------------------------------------------------------------------------
# HTML dashboard (one tab per league)
# --------------------------------------------------------------------------
def md_to_html(md):
    def inline(s):
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])_(.+?)_(?![\w*])", r"<em>\1</em>", s)
        return s
    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[2:]]
            out.append("<div class='tw'><table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join(
                           "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table></div>")
            continue
        if ln.startswith("- ") or ln.startswith("  - "):
            items = []
            while i < len(lines) and (lines[i].startswith("- ") or lines[i].startswith("  - ")):
                sub = lines[i].startswith("  - ")
                items.append(f"<li class='{'sub' if sub else ''}'>{inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        m = re.match(r"^(#{1,3}) (.*)", ln)
        if m:
            lvl = len(m.group(1))
            if lvl == 2:
                out.append("</section><section class='card'>")
            out.append(f"<h{lvl + 1}>{inline(m.group(2))}</h{lvl + 1}>")
        elif ln.strip():
            out.append(f"<p>{inline(ln)}</p>")
        i += 1
    return "<section class='card'>" + "\n".join(out) + "</section>"


def write_dashboard(reports):
    tabs = "".join(f"<button class='tab{' on' if n == 0 else ''}' data-t='t{n}'>{html.escape(name)}</button>"
                   for n, (name, _) in enumerate(reports))
    panes = "".join(f"<div class='pane{' on' if n == 0 else ''}' id='t{n}'>{md_to_html(txt)}</div>"
                    for n, (_, txt) in enumerate(reports))
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fantasy Daily</title><style>
:root{{--bg:#f5f6f8;--card:#fff;--ink:#1c2230;--mute:#667085;--line:#e4e7ec;--acc:#2f6fed}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151c;--card:#1b2029;--ink:#e6e9ef;--mute:#98a2b3;--line:#2c3340;--acc:#6b9bff}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,sans-serif}}
header{{padding:14px 16px 0;max-width:1100px;margin:auto}}h1{{font-size:20px;margin:0 0 4px}}.ts{{color:var(--mute);font-size:12px}}
nav{{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 0;border-bottom:1px solid var(--line)}}
.tab{{border:0;background:none;color:var(--mute);padding:8px 14px;font:inherit;font-weight:600;cursor:pointer;border-bottom:2px solid transparent}}
.tab.on{{color:var(--acc);border-color:var(--acc)}}main{{max-width:1100px;margin:auto;padding:12px 16px 40px}}
.pane{{display:none}}.pane.on{{display:block}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 16px 12px;margin:12px 0}}
.card:empty{{display:none}}h2{{font-size:18px}}h3{{font-size:16px;margin:14px 0 6px}}h4{{font-size:14px;margin:12px 0 4px}}
.tw{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--mute);font-weight:600}}ul{{padding-left:18px;margin:4px 0}}li.sub{{margin-left:18px;list-style:circle}}em{{color:var(--mute)}}
</style></head><body><header><h1>Fantasy Daily</h1><div class="ts">Updated {dt.datetime.now():%a %b %d %Y %I:%M %p}</div>
<nav>{tabs}</nav></header><main>{panes}</main><script>
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.tab,.pane').forEach(e=>e.classList.remove('on'));
b.classList.add('on');document.getElementById(b.dataset.t).classList.add('on');try{{localStorage.setItem('fdTab',b.dataset.t)}}catch(e){{}}}});
try{{const t=localStorage.getItem('fdTab');const b=document.querySelector(`.tab[data-t="${{t}}"]`);if(b)b.click()}}catch(e){{}}
</script></body></html>"""
    DASHBOARD.write_text(page, encoding="utf-8")
    return DASHBOARD


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", help="league key from reference/leagues.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rebuild-draft", action="store_true")
    ap.add_argument("--fixture", action="append", default=[],
                    help="offline test: KEY=path.json (saved API responses)")
    a = ap.parse_args()
    fixtures = dict(f.split("=", 1) for f in a.fixture)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    snaps = snap_trends()
    reports, failed = [], 0
    for cfg in fl.load_config():
        if a.league and cfg["key"] != a.league:
            continue
        try:
            raw = json.load(open(fixtures[cfg["key"]], encoding="utf-8")) \
                if cfg["key"] in fixtures else None
            txt = run_league(cfg, a, snaps, raw)
        except Exception as e:  # one broken league shouldn't sink the others
            failed += 1
            traceback.print_exc()
            txt = f"# {cfg.get('name', cfg['key'])}\n\n**Failed to load:** {e}\n"
        if txt is None:
            continue
        print(txt)
        reports.append((cfg.get("name", cfg["key"]), txt))
    if reports and not a.dry_run:
        print(f"\n[wrote {write_dashboard(reports)}]")
    sys.exit(1 if failed and failed == len(reports) else 0)


if __name__ == "__main__":
    main()
