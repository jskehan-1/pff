# fantasy_daily.ps1 -- daily report for ALL season-long leagues in
# reference\leagues.json (Flounder, Schmidt, Sleeper). 6am daily.
#
# fantasy_daily.py skips any league whose season isn't active, so it's safe
# to leave scheduled year-round. ESPN leagues need espn.com cookies in .env.
# Must run under Windows Task Scheduler (real network), not the Claude bridge.
#
# Output: FantasyDaily.html (dashboard, one tab per league) in the project
# root, data\<league>\reports\*.md, and logs\fantasy_daily.log.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root "venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "fantasy_daily.log"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $logPath -Value $line -Encoding utf8
}

Log "=== fantasy_daily.ps1 starting ==="
if (-not (Test-Path $py)) {
    Log "ERROR: venv python not found at $py -- aborting."
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"
& $py "$root\scripts\fantasy_daily.py" 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: fantasy_daily.py failed (exit $LASTEXITCODE) -- a 401/403 means re-copy the ESPN cookies into .env."
    exit 1
}
Log "Done. Dashboard: $root\FantasyDaily.html"
