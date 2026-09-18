# Full demo stack: rebuild UI, start API with live agent delay
# Usage:  powershell -ExecutionPolicy Bypass -File .\scripts\run_all.ps1

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "== Building UI ==" -ForegroundColor Cyan
Set-Location frontend
npm run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Set-Location $Root

# Optional Gmail from environment already set by user
$env:LIVE_AGENT_DELAY_MS = "70"
$env:OPERATOR_PROFILE = "safaricom"

Write-Host ""
Write-Host "API + UI:  http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "Mission Control auto-runs rain storm if board empty." -ForegroundColor Yellow
Write-Host "Or click: Launch heavy-rain MW storm (LIVE)" -ForegroundColor Yellow
Write-Host ""

python -m uvicorn noc_agents.main:app --app-dir src --host 127.0.0.1 --port 8000
