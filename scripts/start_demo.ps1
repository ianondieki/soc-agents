# Start Kenya NOC Mission Control (API + optional note)
# Usage: .\scripts\start_demo.ps1
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "Starting API on http://127.0.0.1:8000 ..." -ForegroundColor Cyan
Write-Host "If you use Vite UI: cd frontend; npm run dev  ->  http://127.0.0.1:5173" -ForegroundColor Yellow
Write-Host "Or open built UI at http://127.0.0.1:8000 after npm run build" -ForegroundColor Yellow
Write-Host ""
Write-Host "On Mission Control click: Launch heavy-rain MW storm (LIVE)" -ForegroundColor Green

python -m uvicorn noc_agents.main:app --app-dir src --host 127.0.0.1 --port 8000 --reload --no-proxy-headers
