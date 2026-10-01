# Full demo stack: rebuild UI, start API + UI on one port with the live agent delay on.
# Usage:  powershell -ExecutionPolicy Bypass -File .\scripts\run_all.ps1            # http://127.0.0.1:8000
#         powershell -ExecutionPolicy Bypass -File .\scripts\run_all.ps1 -Port 8010 # another port
#         ... -NoBuild                                                               # skip the UI build
param(
  [int]$Port = 8000,
  [switch]$NoBuild
)

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# Another program on the port answers in its own words and never shows the NOC UI. The
# second-brain Bridge API (docker compose) publishes 127.0.0.1:8000, and its 404 reads
# {"detail":{"code":"not_found","message":"Not found."}} -- not this app's.
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
  $owner = ($busy | Select-Object -First 1).OwningProcess
  $name = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
  Write-Host "Port $Port is already in use (process $owner $name)." -ForegroundColor Red
  Write-Host "Stop that program, or run this script with -Port 8010 and open http://127.0.0.1:8010" -ForegroundColor Yellow
  exit 1
}

if (-not $NoBuild) {
  Write-Host "== Building UI ==" -ForegroundColor Cyan
  Set-Location frontend
  npm run build
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  Set-Location $Root
}

# Demo posture: slow the agents down just enough to watch; every external channel stays
# mocked unless the caller's environment says otherwise.
if (-not $env:LIVE_AGENT_DELAY_MS) { $env:LIVE_AGENT_DELAY_MS = "70" }
if (-not $env:OPERATOR_PROFILE) { $env:OPERATOR_PROFILE = "safaricom" }

Write-Host ""
Write-Host "API + UI:  http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "Mission Control auto-runs the rain storm on an empty board; or click 'Launch heavy-rain storm (live)'." -ForegroundColor Yellow
Write-Host "Managers: open http://127.0.0.1:$Port/showcase  -  Presenters: press 'Guided demo' in the top bar." -ForegroundColor Yellow
Write-Host ""

python -m uvicorn noc_agents.main:app --app-dir src --host 127.0.0.1 --port $Port
