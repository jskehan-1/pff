# auto_weekly.ps1 -- fully unattended half of the weekly Tinker Tinker pipeline.
#
# Meant to be run on a schedule (see the one-time `schtasks` registration in
# README.md / the setup instructions) via Windows Task Scheduler, NOT through
# the Claude/Cowork device bridge -- that bridge's own sandboxed VM has no
# outbound network access at all (confirmed 2026-09-23: even google.com 403s
# from it), so a Claude-scheduled task could never reach api.pff.com or
# api.draftkings.com. Native Task Scheduler runs this under your own Windows
# account with your machine's real network, which is why this has to be a
# schtasks entry rather than something Claude can register in this session.
#
# What it does automatically, no input needed:
#   - Figures out the current NFL week from nfl_calendar.py
#   - Pulls last week's real results (pull_weekly_stats.py)
#   - If this week's DK salary file already exists, rebuilds projections +
#     the dashboard
#
# What it can NOT do automatically (this is why Claude also set up a Tuesday
# 6am reminder message separately): pull THIS week's DK salaries, because
# that needs a fresh --draft-group-id you have to find on draftkings.com
# each week. If that file isn't there yet, this script logs that and stops
# cleanly -- rerun weekly_update.ps1 (or just pull_dk_salaries.py) once you
# have the ID, and the dashboard will build then.
#
# Logs to logs\auto_weekly.log (created if missing) so you can check what
# happened on a run you weren't watching.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root "venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "auto_weekly.log"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $logPath -Value $line
}

Log "=== auto_weekly.ps1 starting ==="

if (-not (Test-Path $py)) {
    Log "ERROR: venv python not found at $py -- aborting."
    exit 1
}

$Season = 2026

$calJson = & $py "$root\scripts\nfl_calendar.py" --json 2>&1 | Out-String
try {
    $cal = $calJson | ConvertFrom-Json
}
catch {
    Log "ERROR: couldn't parse nfl_calendar.py --json output: $calJson"
    exit 1
}

if ($null -eq $cal.week) {
    Log "Today doesn't fall inside a known NFL week (offseason?) -- nothing to do."
    exit 0
}

$Week = [int]$cal.week
$PrevWeek = $Week - 1
Log "Current week per nfl_calendar.py: Week $Week (prev = Week $PrevWeek)"

if ($PrevWeek -ge 1) {
    Log "Pulling Week $PrevWeek actual results..."
    & $py "$root\scripts\pull_weekly_stats.py" --season $Season --week $PrevWeek 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Log "WARNING: pull_weekly_stats.py exited non-zero for Week $PrevWeek -- continuing anyway (may just mean it was already pulled, or PFF hasn't posted yet)."
    }

    Log "Pulling Week $PrevWeek snap counts (feeds the backup-usage 'gold mine' model)..."
    & $py "$root\scripts\pull_snap_counts.py" --season $Season --week $PrevWeek 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Log "WARNING: pull_snap_counts.py exited non-zero for Week $PrevWeek -- continuing anyway (this is a supplementary model, not required for the core dashboard)."
    }

    Log "Rebuilding backup-usage history/summary..."
    & $py "$root\scripts\build_backup_usage.py" --season $Season 2>&1 | ForEach-Object { Log "  $_" }
}

$salariesPath = Join-Path $root "data\dk_salaries\DKSalaries_wk$Week.csv"
if (-not (Test-Path $salariesPath)) {
    Log "No salary file yet for Week $Week ($salariesPath) -- can't build projections/dashboard until this week's DraftKings draft group ID is pulled. Stopping cleanly for this run."
    exit 0
}

Log "Building Week $Week projections..."
& $py "$root\scripts\build_projections.py" --season $Season --week $Week --salaries $salariesPath 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: build_projections.py failed."
    exit 1
}

$yy = $Season % 100
$outHtml = Join-Path $root ("Tinker{0:D2}W{1:D2}.html" -f $yy, $Week)
$projPath = Join-Path $root "data\projections_wk$Week.csv"

Log "Generating dashboard -> $outHtml"
& $py "$root\scripts\generate_dashboard.py" --season $Season --week $Week --projections $projPath --out $outHtml 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: generate_dashboard.py failed."
    exit 1
}

Log "Done. Dashboard: $outHtml"
