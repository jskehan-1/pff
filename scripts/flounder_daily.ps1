# flounder_daily.ps1 -- kept so the existing "Flounder Daily" scheduled task
# keeps working. It now runs the all-leagues report (fantasy_daily.ps1).
& (Join-Path $PSScriptRoot "fantasy_daily.ps1")
exit $LASTEXITCODE
