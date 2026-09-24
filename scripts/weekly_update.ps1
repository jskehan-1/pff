# weekly_update.ps1 -- one-command weekly Tinker Tinker pipeline.
#
# Always calls venv\Scripts\python.exe directly, so it works whether or not
# you remembered to activate the venv first (that's what broke the Week 3
# optimizer run -- global python has no `pulp`, only the venv's does).
#
# Usage examples:
#   New week, first time (pulls last week's results + this week's salaries):
#     .\scripts\weekly_update.ps1 -Week 3 -DraftGroupId 153768 -PrevWeek 2
#
#   Re-running the same week later (salaries already pulled, just rebuild):
#     .\scripts\weekly_update.ps1 -Week 3
#
#   First week of a season (no prior week to pull):
#     .\scripts\weekly_update.ps1 -Week 1 -DraftGroupId 123456

param(
    [Parameter(Mandatory = $true)][int]$Week,
    [int]$Season = 2026,
    [string]$DraftGroupId,
    [int]$PrevWeek
)

$ErrorActionPreference = "Stop"

# Project root = parent of the folder this script lives in (scripts\..)
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root "venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Error "Can't find the venv's python.exe at $py -- has the venv moved? Fix the path in weekly_update.ps1 if so."
    exit 1
}

Write-Host "Using venv python: $py" -ForegroundColor DarkGray

if ($PrevWeek) {
    Write-Host "`n== Pulling Week $PrevWeek actual results into weekly_stats_$Season.csv ==" -ForegroundColor Cyan
    & $py "$root\scripts\pull_weekly_stats.py" --season $Season --week $PrevWeek
    if ($LASTEXITCODE -ne 0) { Write-Error "pull_weekly_stats.py failed -- stopping."; exit 1 }
}

$salariesPath = Join-Path $root "data\dk_salaries\DKSalaries_wk$Week.csv"

if ($DraftGroupId) {
    Write-Host "`n== Pulling Week $Week DK salaries (draft group $DraftGroupId) ==" -ForegroundColor Cyan
    & $py "$root\scripts\pull_dk_salaries.py" --draft-group-id $DraftGroupId --out $salariesPath --season $Season --week $Week
    if ($LASTEXITCODE -ne 0) { Write-Error "pull_dk_salaries.py failed -- stopping."; exit 1 }
}
elseif (-not (Test-Path $salariesPath)) {
    Write-Error "No -DraftGroupId given and $salariesPath doesn't exist yet -- pass -DraftGroupId the first time you run this for a new week."
    exit 1
}
else {
    Write-Host "`n== Reusing existing $salariesPath (no -DraftGroupId passed) ==" -ForegroundColor Cyan
}

Write-Host "`n== Building Week $Week projections ==" -ForegroundColor Cyan
& $py "$root\scripts\build_projections.py" --season $Season --week $Week --salaries $salariesPath
if ($LASTEXITCODE -ne 0) { Write-Error "build_projections.py failed -- stopping."; exit 1 }

$yy = $Season % 100
$outHtml = Join-Path $root ("Tinker{0:D2}W{1:D2}.html" -f $yy, $Week)
$projPath = Join-Path $root "data\projections_wk$Week.csv"

Write-Host "`n== Generating dashboard -> $outHtml ==" -ForegroundColor Cyan
& $py "$root\scripts\generate_dashboard.py" --season $Season --week $Week --projections $projPath --out $outHtml
if ($LASTEXITCODE -ne 0) { Write-Error "generate_dashboard.py failed -- stopping."; exit 1 }

Write-Host "`n== Checking gold-mine backup-value alerts (best-effort) ==" -ForegroundColor Cyan
& $py "$root\scripts\pull_espn_status.py" --season $Season --week $Week
& $py "$root\scripts\pull_espn_depthchart.py" --season $Season --week $Week
& $py "$root\scripts\gold_mine_alerts.py" --season $Season --week $Week --out (Join-Path $root "data\gold_mine_alerts_wk$Week.md")

Write-Host "`nDone. Dashboard: $outHtml" -ForegroundColor Green
