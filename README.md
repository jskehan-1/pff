# PFF / Tinker Tinker DFS Project

Two use cases, one project:

1. **Primary:** weekly DraftKings "Tinker Tinker" (NFL Classic) lineup building
   and tracking, using PFF Elite API data for projections.
2. **Secondary (next season):** Flounder keeper-league draft board. Scoring
   reference saved in `reference/flounder_scoring.md`; nothing built here yet.

## One-time setup

```powershell
cd "C:\Users\skeha\OneDrive\Documents\NFL\pff"
py -m venv venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser   # only if venv activation is blocked
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\pff_client.py   # should print your whoami() response -- confirms the key works
```

Your PFF API key lives in `.env` (already created) as `PFF_API_KEY=...`. Never commit
that file if this ever goes into git.

## IMPORTANT: verify PFF field names before trusting real numbers

`pull_weekly_stats.py` guesses at PFF's JSON field names (see `CANDIDATES` at the
top of that file) because they haven't been confirmed against a real API response
yet. Before relying on any of this for real money decisions:

```powershell
python scripts\pull_weekly_stats.py --season 2026 --week 1 --inspect
```

This prints one raw sample record from each endpoint without writing anything.
Compare the printed keys against `CANDIDATES` in `pull_weekly_stats.py` and fix
any mismatches. The script will also print `[warn]` lines the first time it
can't find an expected stat on a real record, which is another sign something
needs adjusting.

## Weekly workflow

```powershell
cd "C:\Users\skeha\OneDrive\Documents\NFL\pff"
.\venv\Scripts\Activate.ps1

# 1) Pull actual results for weeks that have finished (builds/refreshes history)
python scripts\pull_weekly_stats.py --season 2026 --week 1-4

# 2) Get that week's DK salary/player-pool data, then build projections
#
#    Option A (automated, unofficial -- see "DK salary automation" below):
python scripts\pull_dk_salaries.py --draft-group-id 153054 --out data\dk_salaries\DKSalaries_wk5.csv
#
#    Option B (always works): manually download the CSV from DK
#    (Classic contest -> Export player pool) into data\dk_salaries\
#
python scripts\build_projections.py --season 2026 --week 5 --salaries data\dk_salaries\DKSalaries_wk5.csv

# 3) (Optional, standalone) run the optimizer on its own to print lineups to console
python scripts\optimizer.py --projections data\projections_wk5.csv --lineups 5 --max-overlap 6

# 4) Build the dashboard (also runs the optimizer internally and bakes lineups in)
python scripts\generate_dashboard.py --season 2026 --week 5 --projections data\projections_wk5.csv --lineups 5

# 5) Open Tinker26W05.html in your browser (named Tinker{YY}W{WW}.html by week)
```

The dashboard is written as `Tinker{YY}W{WW}.html` (e.g. `Tinker26W05.html` for
2026 week 5), not a generic `dashboard.html`, so every week's build lives side
by side without overwriting the last one.

It has four tabs: Build Lineup (browse/filter/sort all players, click Add to
slot them into your 9-man roster with live salary-cap tracking), Optimizer
Suggestions (the ILP-generated lineups, one click to load into the builder),
Strategy (Boom/Bust), and Performance History (a simple chart once you start
logging `data\dfs_history.csv`). A "Projection model" dropdown under the tabs
toggles every number on the page between the season-blend model and the
2026-only EWMA model (see `scripts\build_projections.py`'s `EWMA_SPAN`).

### Saving your roster (so it carries over to the next regeneration)

Click "Save roster to data\lineups" (top-right, or "Save this lineup" in the
Build Lineup panel). In Chrome or Edge this opens the browser's native Save As
dialog -- navigate to `pff\data\lineups\` once and save; it writes
`Tinker{YY}W{WW}_roster.json` there directly. (Firefox, or any browser without
the File System Access API, falls back to a normal download -- move that file
into `data\lineups\` yourself.)

The next time you run `generate_dashboard.py --season 2026 --week 5 ...`, it
looks for `data\lineups\Tinker26W05_roster.json` and, if found, loads that
roster automatically (matching by player name against the fresh player pool,
so projections/status/matchup are always current -- a player who got excluded
or renamed since you saved just shows up as an empty slot). If no roster file
exists yet for that week, the dashboard just starts empty, same as before.

### Logging actual results (for the History tab)

Create/append to `data\dfs_history.csv` with columns:
`week,salary_used,projected,actual,notes` -- then re-run `generate_dashboard.py`
to refresh the chart.

## DK salary automation (unofficial, unverified)

`scripts\pull_dk_salaries.py` hits DraftKings' own undocumented draftables
endpoint (`api.draftkings.com/draftgroups/v1/draftgroups/{id}/draftables`) --
the same JSON their site loads before rendering the lineup builder. No login
or auth needed since it's public contest data, but:

- It's not a supported API and could change/break with no notice.
- Field-name guesses in `CANDIDATES` at the top of the script haven't been
  confirmed against a real response -- run with `--inspect` first:
  ```powershell
  python scripts\pull_dk_salaries.py --draft-group-id 153054 --inspect
  ```
  and check the printed JSON against `CANDIDATES`, fixing any mismatches.
- The `draftGroupId` changes per slate/contest and isn't predictable --
  find it each week via Chrome DevTools (Network tab -> Fetch/XHR -> look
  for a `draftgroups/.../draftables` request) on the contest you want.
- If this ever stops working cleanly, Option B (manual CSV download) in the
  weekly workflow above is the fallback -- always current, always simple.

## Trusting dates (why nfl_calendar.py exists)

A live search for "current" NFL news can and does return stale or wrong-year
results -- a headline snippet can be truncated or mis-cached, and an old
article about, say, an injury from last season can rank highly for a generic
query. That caused a real mistake once: assuming "week 3" from a bad search
snippet and citing an injury article that turned out to be from the 2025
season, not 2026.

The fix isn't "be more careful reading snippets" -- it's not trusting them at
all for date/week logic. `reference/nfl_2026_calendar.csv` was built by
directly fetching nfl.com's per-week schedule pages (and the Thursday Night
Football page, which conveniently prints final scores for games already
played) and is the single source of truth for "what NFL week is this date."
`scripts/nfl_calendar.py` reads it and answers two things that should never
be guessed from a search result:

- **What week is it right now** (`current_week()` / `python
  scripts\nfl_calendar.py`) -- and whether that week's Thursday game has
  already been played, which is exactly the kind of thing that's easy to get
  off-by-one on.
- **Is a given news item's date actually current** (`check_relevance()`) --
  flags anything from the wrong year, from before the current week started,
  or dated in the future, so it gets discarded instead of cited.

`build_projections.py` calls `current_week()` automatically and prints a
`[warn]` if the `--week` you passed doesn't match what today's date says it
should be -- it still runs (you might legitimately be building ahead), but
you'll see the mismatch instead of silently building the wrong week.

Run `python scripts\nfl_calendar.py --self-test` any time to re-verify the
calendar's logic against known anchor dates (season kickoff, Thanksgiving,
Christmas, the week 2/3 boundary, etc.).

## Known limitations / things to sanity-check

- **DST points-allowed**: DK's rule excludes points scored off the *offense's*
  own turnovers. `pull_weekly_stats.py` currently uses the final score against
  from `/v1/games` as an approximation -- it will occasionally overstate a
  defense's points-allowed tier. Spot-check against a couple of real box
  scores before trusting DST projections heavily.
- **Projection model is simple by design**: season average blended 50/50 with
  a recency-weighted last-4-games average, falling back to DK's own
  `AvgPointsPerGame` for players with no PFF history yet (rookies, new
  team-week matchups, or a name-matching miss). It does not account for
  injuries, weather, Vegas lines, or opponent matchup difficulty -- treat it
  as a starting point, not a final answer.
- **DK salary CSV**: no reliable, sanctioned way to automate pulling this
  found yet (see chat) -- current workflow assumes you download it manually
  each week from DraftKings and drop it in `data\dk_salaries\`.
- **Name matching**: `build_projections.py` normalizes names (strips
  punctuation/suffixes) to match PFF's player names against DK's, but a
  mismatch is still possible for tricky names -- check the `Notes` column
  in `projections_wk{N}.csv` for anyone flagged "no PFF history match" who
  you know should have history, and fix names in `CANDIDATES`/normalize
  logic if it's a systematic issue.

## Fantasy daily report (Flounder, Schmidt, Sleeper)

`scripts\fantasy_daily.py` runs every league in `reference\leagues.json`
(scheduled 6:00am daily via `scripts\fantasy_daily.ps1`; the old "Flounder
Daily" task still works -- `flounder_daily.ps1` now just calls it). Leagues
whose season isn't active are skipped.

Output: **`FantasyDaily.html`** in the project root -- a dashboard with one tab
per league -- plus `data\<league>\reports\<league>_YYYYMMDD.md`. Each tab:

1. **Sit/start** for your team (platform projections in league scoring; ESPN
   locked players stay put) with PFF snap-share trend.
2. **Top-3 trade targets**, tracked day to day, each with an offer that never
   gives up more perceived draft capital than the target cost.
3. **Roster moves** across every team since the last run (first run backfills
   the season), with a draft-capital view: auction $ / FAAB (Flounder, with
   next-yr keeper cost), or snake round.pick + pick-value points (pick 1 = 100,
   ~-2.5%/pick) for Schmidt and Sleeper. Trades get capital vs value side by side.
4. **Dropped players** -- everyone rostered this season who's now unrostered.

Managers are shown by name (Flounder short names from `leagues.json`, ESPN
first names, Sleeper usernames), not team names, since those change.

Draft results: `reference\<league>_draft_2026.csv`, built automatically on the
first run (`--rebuild-draft` regenerates). Flounder keepers carry their
OFFICIAL keeper cost + trade fee (`KEEPER_COSTS` in `flounder_common.py`).

Credentials: ESPN leagues are private -- `.env` needs `ESPN_S2` /
`ESPN_SWID` (one ESPN login covers every ESPN league). Copy from
espn.com cookies (DevTools -> Application -> Cookies). Sleeper needs nothing.
A 401/403 in `logs\fantasy_daily.log` means re-copy the cookies.

    python scripts\fantasy_daily.py --dry-run            # all leagues, print only
    python scripts\fantasy_daily.py --league sleeper     # one league

## Project layout

```
pff/
  .env                     - your PFF API key (not shared/committed)
  requirements.txt
  reference/
    dk_classic_scoring.md  - DK NFL Classic scoring rules
    flounder_scoring.md    - Flounder league scoring rules (secondary use case)
    nfl_2026_calendar.csv  - week-by-week game dates/windows for the 2026 season,
                              cross-checked against nfl.com's by-week + TNF schedule
                              pages (not derived from search-result snippets, which
                              can be stale or mis-dated -- see "Trusting dates" below)
  scripts/
    dk_scoring.py          - DK point-value math (self-testable)
    nfl_calendar.py         - authoritative week-by-week 2026 calendar lookup
                              (self-testable: python scripts\nfl_calendar.py --self-test)
    pff_client.py          - authenticated PFF API client
    pull_weekly_stats.py   - pulls actuals, computes DK pts, builds history CSV
    build_projections.py   - merges history + DK salaries -> weekly projections
    optimizer.py           - ILP lineup optimizer (pulp)
    generate_dashboard.py  - bakes projections/optimizer/history into Tinker{YY}W{WW}.html
  templates/
    dashboard_template.html
  data/
    weekly_stats_{season}.csv   - accumulated actual results, DK-scored
    dk_salaries/                 - drop weekly DK salary CSV exports here
    projections_wk{N}.csv
    lineups/
      Tinker{YY}W{WW}_roster.json - saved rosters (written by the dashboard's
                                     "Save roster" button; auto-loaded by the
                                     next generate_dashboard.py run for that week)
    dfs_history.csv              - your logged actual lineup results
  output/                  - (reserved, currently unused)
  Tinker{YY}W{WW}.html     - generated per week -- open this in your browser
```
