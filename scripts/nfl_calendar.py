"""
Authoritative 2026 NFL week-by-week calendar (reference/nfl_2026_calendar.csv),
plus helpers for figuring out "what NFL week is it right now" and for sanity
-checking news/search results against that -- so a stale article (wrong year,
wrong week) doesn't get treated as current.

Built after a real mistake: an earlier session assumed "week 3" from a
malformed search-result snippet and cited an Isaiah Likely injury article
that was actually from the 2025 season. This module exists so that never
happens silently again -- every date-sensitive decision should go through
current_week()/week_for_date() rather than guessing from a headline.

Usage:
    python scripts\\nfl_calendar.py                  # prints today's week + self-tests
    python scripts\\nfl_calendar.py --date 2026-11-27 # prints the week for any date
"""
import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CALENDAR_PATH = PROJECT_ROOT / "reference" / "nfl_2026_calendar.csv"


@dataclass
class WeekInfo:
    week: int
    window_start: date
    window_end: date
    game_dates: list  # list of date objects, may be empty (e.g. week 18 TBD)
    notes: str

    @property
    def thursday_game_played(self):
        """True if this week's Thursday/early game date has already passed
        as of today. Falls back to False if no early-week game date is
        recorded (e.g. week 18)."""
        if not self.game_dates:
            return False
        return self.game_dates[0] <= date.today()

    def is_current(self, as_of=None):
        as_of = as_of or date.today()
        return self.window_start <= as_of <= self.window_end


def load_calendar(path=CALENDAR_PATH):
    if not path.exists():
        raise SystemExit(
            f"Couldn't find {path}. This is the saved NFL {path.stem.split('_')[1]} "
            f"calendar -- if it's missing, re-derive it from nfl.com's by-week "
            f"schedule pages before trusting any week-number logic."
        )
    weeks = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            game_dates = [
                datetime.strptime(d, "%Y-%m-%d").date()
                for d in row["game_dates"].split(";") if d.strip()
            ]
            weeks.append(WeekInfo(
                week=int(row["week"]),
                window_start=datetime.strptime(row["window_start"], "%Y-%m-%d").date(),
                window_end=datetime.strptime(row["window_end"], "%Y-%m-%d").date(),
                game_dates=game_dates,
                notes=row["notes"],
            ))
    weeks.sort(key=lambda w: w.week)
    return weeks


def week_for_date(d, calendar=None):
    """Returns the WeekInfo whose window contains date `d`, or None if `d`
    falls outside the loaded calendar entirely (e.g. offseason, or a season
    not yet added to reference/)."""
    calendar = calendar or load_calendar()
    for w in calendar:
        if w.is_current(d):
            return w
    return None


def current_week(calendar=None):
    """Convenience wrapper: week_for_date(date.today())."""
    return week_for_date(date.today(), calendar)


def check_relevance(item_date, as_of=None, max_age_days=10):
    """
    Sanity check for a news/search result's own date against "now". Returns
    (is_relevant: bool, reason: str). Use this before treating any fetched
    article's injury/status/depth-chart claim as current -- a hit here means
    the article should be discarded or re-searched with a tighter date/year
    filter, not cited as this week's news.

    item_date: a date (or None if unknown -- treated as unverifiable).
    """
    as_of = as_of or date.today()
    if item_date is None:
        return False, "article has no verifiable date -- treat as unconfirmed, don't cite as current"
    if item_date.year != as_of.year:
        return False, f"article is dated {item_date.year}, but it's currently {as_of.year} -- almost certainly stale (wrong season)"
    age = (as_of - item_date).days
    if age < 0:
        return False, f"article is dated {item_date.isoformat()}, which is in the future relative to {as_of.isoformat()} -- suspicious, re-verify"
    if age > max_age_days:
        return False, f"article is {age} days old (dated {item_date.isoformat()}) -- likely stale for a weekly injury/lineup decision"
    return True, f"article dated {item_date.isoformat()}, {age} day(s) old -- within the {max_age_days}-day relevance window"


def _self_test():
    calendar = load_calendar()
    assert len(calendar) == 18, f"expected 18 weeks, got {len(calendar)}"

    # Known-good anchor points, cross-checked against nfl.com's by-week pages.
    assert week_for_date(date(2026, 9, 20), calendar).week == 2, "Sep 20 2026 should be week 2"
    assert week_for_date(date(2026, 9, 10), calendar).week == 1, "Sep 10 2026 (week 1 TNF-ish opener) should be week 1"
    assert week_for_date(date(2026, 11, 26), calendar).week == 12, "Thanksgiving 2026 should be week 12"
    assert week_for_date(date(2026, 12, 25), calendar).week == 16, "Christmas 2026 should be week 16"
    assert week_for_date(date(2027, 1, 4), calendar).week == 17, "Jan 4 2027 (week 17 MNF) should be week 17"
    assert week_for_date(date(2026, 7, 4)) is None, "July 4 2026 is offseason -- should not match any week"

    wk2 = week_for_date(date(2026, 9, 20), calendar)
    assert wk2.thursday_game_played is True, "week 2's Thursday game (9/17) already happened by 9/20"

    wk3 = week_for_date(date(2026, 9, 22), calendar)
    assert wk3.thursday_game_played is False, "week 3's Thursday game (9/24) hasn't happened yet as of 9/22"

    ok, reason = check_relevance(date(2025, 9, 15), as_of=date(2026, 9, 20))
    assert ok is False, "a 2025-dated article should be flagged stale in 2026"

    ok, reason = check_relevance(date(2026, 9, 18), as_of=date(2026, 9, 20))
    assert ok is True, "a 2-day-old same-season article should be relevant"

    print("All nfl_calendar.py self-tests passed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD to look up (defaults to today)")
    ap.add_argument("--self-test", action="store_true", help="run the built-in assertions and exit")
    ap.add_argument("--json", action="store_true",
                     help="print {\"week\": N, \"thursday_game_played\": bool} and nothing else "
                          "(or {\"week\": null} if outside any known week) -- for scripts/schtasks "
                          "to consume, e.g. weekly_update.ps1's automated wrapper.")
    args = ap.parse_args()

    if args.self_test:
        _self_test()
        return

    calendar = load_calendar()
    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    wk = week_for_date(target, calendar)

    if args.json:
        import json
        if wk is None:
            print(json.dumps({"week": None}))
        else:
            print(json.dumps({"week": wk.week, "thursday_game_played": wk.thursday_game_played}))
        return

    if wk is None:
        print(f"{target.isoformat()} doesn't fall inside any known 2026 week window (offseason, or check reference/nfl_2026_calendar.csv).")
        return
    print(f"{target.isoformat()} -> NFL 2026 Week {wk.week} (window {wk.window_start} to {wk.window_end})")
    print(f"  Game dates: {', '.join(d.isoformat() for d in wk.game_dates) or '(TBD)'}")
    print(f"  Notes: {wk.notes}")
    print(f"  Thursday/early game already played as of today: {wk.thursday_game_played}")

    _self_test()


if __name__ == "__main__":
    main()
