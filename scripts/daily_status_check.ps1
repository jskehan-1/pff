# daily_status_check.ps1 -- runs daily Tue-Sun. Does NOT touch pull_weekly_stats.py
# (that's auto_weekly.ps1's job, once, on Tuesday) -- this script only re-pulls
# THIS week's DK salaries (once the draft group ID is known) and reports what
# changed since yesterday's pull: Status (O/Q/D/etc.) and DraftAlerts changes.
#
# Skips cleanly, doing nothing, until data\current_draft_group.json exists for
# the current week -- that file is written automatically the first time
# pull_dk_salaries.py or weekly_update.ps1 is run with --season/--week for the
# week (i.e. as soon as Jake pastes this week's curl and Claude runs the pull).
# No re-pasting needed on later days -- this script reuses the same ID.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root "venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "daily_status_check.log"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $logPath -Value $line
}

Log "=== daily_status_check.ps1 starting ==="

if (-not (Test-Path $py)) {
    Log "ERROR: venv python not found at $py -- aborting."
    exit 1
}

$Season = 2026

$calJson = & $py "$root\scripts\nfl_calendar.py" --json 2>&1 | Out-String
try { $cal = $calJson | ConvertFrom-Json } catch {
    Log "ERROR: couldn't parse nfl_calendar.py --json output: $calJson"
    exit 1
}
if ($null -eq $cal.week) {
    Log "Today doesn't fall inside a known NFL week -- nothing to do."
    exit 0
}
$Week = [int]$cal.week

$statePath = Join-Path $root "data\current_draft_group.json"
if (-not (Test-Path $statePath)) {
    Log "No draft group ID on record yet for any week (data\current_draft_group.json missing) -- skipping. Nothing to re-pull until Jake pastes this week's curl."
    exit 0
}

$state = Get-Content $statePath -Raw | ConvertFrom-Json
if ([int]$state.week -ne $Week) {
    Log "Draft group ID on record is for Week $($state.week), but current week is $Week -- skipping (stale ID, not yet updated for this week). Waiting on Jake's fresh curl."
    exit 0
}

$draftGroupId = $state.draft_group_id
Log "Using known draft group ID $draftGroupId for Week $Week (recorded $($state.updated_at))"

$salariesPath = Join-Path $root "data\dk_salaries\DKSalaries_wk$Week.csv"
Log "Re-pulling Week $Week salaries..."
& $py "$root\scripts\pull_dk_salaries.py" --draft-group-id $draftGroupId --out $salariesPath --season $Season --week $Week 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: pull_dk_salaries.py failed -- stopping (leaving yesterday's data in place)."
    exit 1
}

$today = Get-Date -Format "yyyyMMdd"
$reportPath = Join-Path $root "data\status_reports\status_report_wk${Week}_$today.md"
Log "Diffing against the prior snapshot..."
& $py "$root\scripts\diff_dk_status.py" --week $Week --out $reportPath 2>&1 | ForEach-Object { Log "  $_" }

Log "Pulling ESPN status (real-time Out/Doubtful/Questionable cross-check)..."
& $py "$root\scripts\pull_espn_status.py" --season $Season --week $Week 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Log "WARNING: pull_espn_status.py failed -- continuing without it (gold-mine alerts will be skipped this run)."
}

Log "Pulling ESPN depth charts..."
& $py "$root\scripts\pull_espn_depthchart.py" --season $Season --week $Week 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Log "WARNING: pull_espn_depthchart.py failed -- continuing without it."
}

Log "Rebuilding projections + dashboard with today's data..."
& $py "$root\scripts\build_projections.py" --season $Season --week $Week --salaries $salariesPath 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: build_projections.py failed."
    exit 1
}

$yy = $Season % 100
$outHtml = Join-Path $root ("Tinker{0:D2}W{1:D2}.html" -f $yy, $Week)
$projPath = Join-Path $root "data\projections_wk$Week.csv"
& $py "$root\scripts\generate_dashboard.py" --season $Season --week $Week --projections $projPath --out $outHtml 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: generate_dashboard.py failed."
    exit 1
}

$goldMinePath = Join-Path $root "data\gold_mine_alerts_wk$Week.md"
Log "Checking for gold-mine backup-value alerts..."
& $py "$root\scripts\gold_mine_alerts.py" --season $Season --week $Week --out $goldMinePath 2>&1 | ForEach-Object { Log "  $_" }

Log "Done. Dashboard refreshed: $outHtml -- status report (if any changes): $reportPath -- gold-mine alerts: $goldMinePath"
