# Standing DFS strategy preferences

These are Jake's own standing rules for lineup-building, not projections math --
apply them on top of whatever the model says, not instead of it.

## Fade Cleveland's offense (added 2026-09-20)

Jake wants to keep fading the Browns' offense in DFS lineups: whichever DST is
playing Cleveland that week should always be a top-3 DST candidate (guaranteed
visibility in the Strategy tab, not just whatever the projection model ranks
it), independent of its raw projection number.

Reasoning given: team vibe/environment is bad, and Deshaun Watson hasn't shown
he can play (or start) at an all-pro level -- looks checked out.

**Heat-check condition (important -- don't apply this blindly forever):**
Jake explicitly asked to be watched for two things that should end or shift
this rule:
1. The Browns offense starts actually demonstrating competence (better
   protection, Watson playing well, real point production) -- if so, stop
   auto-boosting DSTs against them and say so.
2. A different team's offense turns out to be a more reliable/juicier fade
   (gives up a lot of points to DSTs) -- if research surfaces one, suggest
   swapping the standing fade target to that team instead of (or in addition
   to) Cleveland.

Practically: whenever building a week's dashboard/strategy recommendations
against Cleveland, do a quick current-week sanity check (via nfl_calendar.py
for the right week + a fresh news search, not stale priors) on how the Browns
offense has actually looked recently before leaning on this rule, and flag it
to Jake if the premise looks like it's breaking down.

**Implementation:** `build_projections.py`'s `FADE_OFFENSES` set (currently
just `{"CLE"}`) drives a `FadeTarget` column in the projections CSV, which the
dashboard's Strategy tab uses to always surface that week's Browns-opponent
DST in its own category, regardless of projection rank. To change or add a
fade target, edit `FADE_OFFENSES` in `build_projections.py`.

## Pricing already reflects the fade (added 2026-09-20, Jake's observation)

Jake noticed DK's own salaries may already be pricing in how bad an offense
like Cleveland's is -- meaning the opposing DST is often already the more
expensive "chalk" play, not a hidden inefficiency. If the market's already
priced it, the edge in fading these offenses shows up more in points than in
salary discount, so don't just auto-recommend the fade-target DST outright:
weigh it against its Value (pts/$1k) column same as any other DST, not just
its raw projection or its FadeTarget flag. The FadeTarget badge/category is a
"make sure this one's on your radar" signal, not a "must play regardless of
cost" signal.

The `OppPosAllowedLastWk` / `OppPosAllowedAvg3` columns (added the same day,
originally DST-only, generalized to every position on 2026-09-20 -- see below)
are a useful real check on this: for a DST row they show what DK points the
opponent's offense has actually given up to DSTs recently, so you can compare
that against the DST's salary yourself rather than trusting the fade badge
alone. First real data point: Cleveland's Week 1 opponent's DST scored 14.0
DK points against them -- a strong number worth weighing against whatever the
Buccaneers' DST costs this week.

## "Last Wk" / "Opp Last Wk" columns generalized to all positions (2026-09-20)

Originally these four Build Lineup columns only existed for DST (own points
last week / avg last 3, and what the opponent's offense gave up last week /
avg last 3). Jake corrected this: `PtsLastWk` / `PtsAvg3` (a player's own DK
points) now show for every position, and `OppPosAllowedLastWk` /
`OppPosAllowedAvg3` (what the upcoming opponent's defense gave up to THIS
SPECIFIC position recently) are also computed for every position -- QB/RB/WR/TE
allowed-points data exists via `weekly_stats_2026.csv` (position-tagged) plus
`team_schedule.py`'s team-vs-team matchup tracker, the same mechanism that
already powered the DST-only version. See `load_position_points_by_team_week()`
in `build_projections.py`.

**Weak-defense-vs-position signal (first real example, Week 2):** Jake flagged
that Carson Wentz (his QB) threw for 3 TDs Week 1 and that Chicago's defense
let Bryce Young put up 35.44 DK points on them -- suggesting CHI is soft
against the pass. Checked against real Week 1 grade data: CHI's defense grades
in the bottom handful of the league for both pass defense (17.2 percentile --
3rd-worst, behind only CLE and WAS) and run defense (4.7 percentile -- worst
in the league). Wentz's Week 2 matchup happens to be MIN @ CHI, and the
existing matchup-score system already reflects this independently:
`MatchupScore` = 4/5 ("Great") for Wentz vs. CHI this week, off a 52.7
offensive percentile vs. CHI's 17.2 defensive percentile. This is a good early
validation that the matchup-score + new allowed-columns machinery is picking
up real signal, not just noise -- worth re-checking once Week 2 results are
in to see if CHI's grades hold up or regress with a bigger sample.

## Bug fix: team-code mismatch was silently breaking matchup scores (2026-09-23)

Found while building Week 3: `matchup_score.py`'s `build_matchup_context()` was
keying `defense_percentiles` by the raw team codes straight out of
`weekly_stats_2026.csv` (PFF's export), which uses a handful of non-standard
codes -- `ARZ`, `BLT`, `CLV`, `HST`, `LA` -- that don't match the canonical
codes (`ARI`, `BAL`, `CLE`, `HOU`, `LAR`) used everywhere else in this
project (DK's own salary exports, `team_canon()`, `score_matchup()`'s
opponent argument). Every lookup for a player facing one of those 5 teams'
defenses was silently missing and falling back to "No data" instead of a
real Great/Fair/Poor label -- this includes every matchup call made against
**Cleveland's defense specifically**, which matters a lot given the standing
fade-CLE-offense strategy (the fade is about CLE's offense, so this didn't
break the FadeTarget flag itself, but it did mean nobody facing CLE's own
defense, and no Cleveland-based team's real defensive percentile, ever
displayed correctly in Matchup labels prior to this fix).

Fixed by duplicating the `TEAM_ALIASES`/`team_canon()` mapping into
`matchup_score.py` (same pattern as the existing `normalize_name`
duplication, done to avoid a circular import with `build_projections.py`)
and canonicalizing team codes when building `defense_percentiles`. Verified:
`CLE` now correctly resolves (pass defense percentile 32.8 through Week 2,
still bottom-8 in the league), along with `ARI`/`BAL`/`HOU`/`LAR`.

## Week 3 (2026-09-23) notes

- Cleveland's opponent this week is Carolina (CAR @ CLE) -- Panthers DST is
  this week's standing FadeTarget. Per the pricing-reflects-the-fade caveat
  above: Panthers DST projects 7.79 at $3000 (val 2.60), which is *not* the
  best DST value on the board this week (Broncos $2300/8.22 proj/val 3.57 and
  Raiders $2600/8.72 proj/val 3.35 both grade out better) -- FadeTarget stays
  "on your radar," not an auto-play, this week.
- Deshaun Watson himself now grades "Poor" matchup vs. CAR's defense (16.86
  proj) -- consistent with the fade thesis; Browns offense still hasn't shown
  competence, no heat-check trigger yet.
- CHI's defense remains one of the worst vs. the pass through 2 weeks (23.4
  percentile, still bottom-5) -- the Wentz/CHI Week 2 read continues to hold
  up as real signal, not a one-week fluke.

## Fade Cleveland's offense -- RETIRED (2026-09-23)

Jake's call: "remove fade Browns, it didn't work." `FADE_OFFENSES` in
`build_projections.py` is now empty (was `{"CLE"}`). The `FadeTarget`
column/badge/Strategy-tab category all still exist in the code (harmless,
just always False now) in case a new fade target gets added later -- no
scaffolding was ripped out, just the one flag.

## FLEX assignment preference (added 2026-09-23)

Standing rule for how the optimizer's suggested lineups (and the "load
into builder" / saved-lineup comparison) assign which specific player
fills FLEX: whichever RB/WR/TE has the LATEST kickoff that week, with
highest salary as the tiebreak (or as the only sort key, until real
kickoff times are confirmed -- see caveat below). Rationale (Jake's own):
keeping the latest-kicking, priciest flex-eligible player "in reserve" as
FLEX maximizes the window to react to last-minute injury news before that
player's own lock, since DK locks each player individually at their game's
kickoff, not the whole slate at once.

Implemented in `slotPlayersToRoster()` in `dashboard_template.html`: finds
whichever position (RB/WR/TE) has more than its dedicated-slot minimum
among the 9 chosen players (exactly one will, by construction of a legal
roster), then within that surplus group picks latest-`gameStart`-then-
highest-salary as FLEX, and fills the dedicated slots from the rest.

**Caveat -- GameStart is unverified.** `pull_dk_salaries.py` now attempts to
extract a kickoff timestamp per player (`GameStart` column, candidate JSON
paths `competitionStartTime` / `competition.startTime` / `startTime` /
`competition.startDate`), but this couldn't be tested against a live DK
response from this session (no network access from any environment
available here). If a real pull comes back with `GameStart` blank for
everyone, the FLEX logic silently falls back to salary-only ordering
(still directionally reasonable, just not the full rule) -- run
`python scripts\pull_dk_salaries.py --draft-group-id <id> --inspect` and
check the raw JSON for the actual kickoff-time field name, then update the
candidate list in `pull_dk_salaries.py`'s `CANDIDATES["game_start"]`.

## Gold-mine backup-value alerts (added 2026-09-23)

Jake's request: "the gold mines are situations where the primary position
player is ruled out AFTER prices are locked and the #2 or true bench player
get much higher than expected usage." Built a full pipeline for this, all
free/no-auth except the snap-count piece which reuses the existing PFF key:

- **`scripts/espn_client.py`** -- unauthenticated client for ESPN's public
  site API (the same JSON endpoints espn.com's own site/app call).
- **`scripts/pull_espn_status.py`** -- pulls all 32 teams' rosters, gets each
  player's live Status (Active/Out/Doubtful/Questionable/IR) inline, no extra
  calls needed. Writes `data/espn_status_current.csv` (+ dated snapshots).
  This is a real-time cross-check ON TOP OF DK's own Status/DraftAlerts field
  (already tracked by diff_dk_status.py) -- ESPN's report sometimes updates
  before DK's own feed does.
- **`scripts/pull_espn_depthchart.py`** -- pulls all 32 teams' depth charts
  (QB/RB/WR/TE only), so we know who's "next in line" at a position. Writes
  `data/espn_depthchart_current.csv` (+ dated snapshots).
- **`scripts/pull_snap_counts.py`** -- pulls real per-player-per-week snap
  counts from PFF's `/v1/player/snaps/summary` (per Jake: snap share is a
  cleaner opportunity signal than raw fantasy points, which garbage-time TDs
  can distort). This is a PER-PLAYER PFF endpoint (no bulk facet-style report
  exists for snaps), so it's scoped to QB/RB/WR/TE players who actually
  recorded stats that week (via `weekly_stats_{season}.csv`, which now also
  carries `player_id` for this reason). Appends to `data/snap_counts_{season}.csv`.
- **`scripts/build_backup_usage.py`** -- mines `snap_counts_{season}.csv` for
  "starter's snaps went near-zero the next week AND someone else took over"
  events per team/position, and aggregates an average snap-jump multiplier
  per position into `data/backup_usage_summary.csv`. Will be sparse/empty
  early in a season -- prints that plainly rather than faking a number.
- **`scripts/gold_mine_alerts.py`** -- the glue: cross-references current
  ESPN status + depth chart + this week's DK salary pool + projections (+
  the historical multiplier, if any) into a plain-English alert list and
  `data/gold_mine_alerts_wk{W}.csv`/`.md`.

Wired into the existing automation: `auto_weekly.ps1` (Tuesday) now also
pulls the prior week's snap counts and rebuilds the backup-usage summary;
`daily_status_check.ps1` (daily Tue-Sun) now also pulls ESPN status/depth
charts and runs the gold-mine check after each day's salary re-pull;
`weekly_update.ps1` (manual one-command run) does the same at the end,
best-effort (won't fail the whole run if ESPN is unreachable).

**Caveat, same as the GameStart caveat above**: the ESPN JSON shapes here
(roster/depthchart/snap-count field names) were confirmed via API-doc
research and direct fetches, but never run against this project's own
network -- PFF/DK calls have always had to run on Jake's machine for the
same reason (the Claude bridge's VM has no outbound network access at all).
Run `--inspect` on `pull_espn_status.py` / `pull_espn_depthchart.py` /
`pull_snap_counts.py` the first time and paste back anything that looks
off so the field-name guesses can be corrected against the real response.
