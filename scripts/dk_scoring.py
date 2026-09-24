"""
DraftKings NFL Classic scoring math.

Pure functions -- no PFF-specific field names here. Callers pass in plain
keyword args (yards, TDs, etc.) however they've pulled them from a data
source. See reference/dk_classic_scoring.md for the rules this implements.
"""


def score_offense(
    pass_yds=0, pass_td=0, ints=0,
    rush_yds=0, rush_td=0,
    rec=0, rec_yds=0, rec_td=0,
    fumbles_lost=0, two_pt=0,
    return_td=0, off_fumble_rec_td=0,
):
    """DK points for a single offensive player's game line."""
    pts = 0.0
    pts += pass_yds * 0.04
    pts += pass_td * 4
    if pass_yds >= 300:
        pts += 3
    pts += ints * -1
    pts += rush_yds * 0.1
    pts += rush_td * 6
    if rush_yds >= 100:
        pts += 3
    pts += rec_yds * 0.1
    pts += rec_td * 6
    if rec_yds >= 100:
        pts += 3
    pts += rec * 1
    pts += fumbles_lost * -1
    pts += two_pt * 2
    pts += return_td * 6
    pts += off_fumble_rec_td * 6
    return round(pts, 2)


def score_dst(
    sacks=0, ints=0, fumble_rec=0,
    return_td=0, int_return_td=0, fumble_rec_td=0, blocked_return_td=0,
    safety=0, blocked_kick=0, two_pt_return=0,
    points_allowed=None,
):
    """
    DK points for a DST's game line.

    NOTE: points_allowed should be points surrendered *while the defense/
    special teams unit was on the field* (DK excludes points off the
    offense's own turnovers). PFF's team/game endpoints don't expose that
    split directly as of this writing -- pull_weekly_stats.py uses the
    final score against as an approximation and flags it as such. Treat
    the points-allowed tier bonus as approximate until you've verified it
    against a couple of real box scores.
    """
    pts = 0.0
    pts += sacks * 1
    pts += ints * 2
    pts += fumble_rec * 2
    pts += return_td * 6
    pts += int_return_td * 6
    pts += fumble_rec_td * 6
    pts += blocked_return_td * 6
    pts += safety * 2
    pts += blocked_kick * 2
    pts += two_pt_return * 2
    if points_allowed is not None:
        pa = points_allowed
        if pa == 0:
            pts += 10
        elif pa <= 6:
            pts += 7
        elif pa <= 13:
            pts += 4
        elif pa <= 20:
            pts += 1
        elif pa <= 27:
            pts += 0
        elif pa <= 34:
            pts += -1
        else:
            pts += -4
    return round(pts, 2)


if __name__ == "__main__":
    # Quick self-test with known scenarios -- run:  python scripts\dk_scoring.py
    tests = []

    # QB: 310 yds, 3 TD, 1 INT -> 310*.04=12.4 +12 (3 TD) +3 (300+ bonus) -1 = 26.4
    qb = score_offense(pass_yds=310, pass_td=3, ints=1)
    tests.append(("QB 310/3TD/1INT", qb, 26.4))

    # RB: 105 rush yds, 1 rush TD, 3 catches for 20 yds -> 10.5 +6 +3(100+ bonus) +2(rec yds) +3(catches) = 24.5
    rb = score_offense(rush_yds=105, rush_td=1, rec=3, rec_yds=20)
    tests.append(("RB 105rush/1TD/3rec-20yds", rb, 24.5))

    # WR: 8 catches, 130 rec yds, 2 TD -> 8 + 13 + 12 + 3(100+ bonus) = 36
    wr = score_offense(rec=8, rec_yds=130, rec_td=2)
    tests.append(("WR 8rec/130yds/2TD", wr, 36.0))

    # DST: 3 sacks, 1 INT, 1 fumble rec, 17 points allowed -> 3+2+2+1(14-20 tier) = 8
    dst = score_dst(sacks=3, ints=1, fumble_rec=1, points_allowed=17)
    tests.append(("DST 3sk/1int/1fum/17PA", dst, 8.0))

    print(f"{'scenario':35s} {'got':>8s} {'expected':>10s}  ok?")
    all_ok = True
    for name, got, expected in tests:
        ok = abs(got - expected) < 0.001
        all_ok = all_ok and ok
        print(f"{name:35s} {got:8.2f} {expected:10.2f}  {'PASS' if ok else 'FAIL'}")
    if not all_ok:
        raise SystemExit(1)
    print("\nAll scoring self-tests passed.")
