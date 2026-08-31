# Reclaim local demo (Windows PowerShell) — run the FastAPI backend and the
# Vite dev server together.
#
# Usage:   .\scripts\dev.ps1      (or: powershell -File scripts/dev.ps1)
# Stops:   Ctrl+C
#
# If you prefer two separate terminals (handy for seeing backend logs while
# editing), just run these in two shells instead:
#   Terminal 1:  uvicorn reclaim.api:app --reload
#   Terminal 2:  cd frontend; npm run dev

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host ">> Starting Reclaim backend on :8000 ..."
$backend = Start-Process uvicorn -ArgumentList "reclaim.api:app","--host","127.0.0.1","--port","8000","--reload" -PassThru -NoNewWindow

Write-Host ">> Starting Reclaim frontend on :5173 ..."
Push-Location "$root/frontend"
$frontend = Start-Process npm -ArgumentList "run","dev" -PassThru -NoNewWindow
Pop-Location

Write-Host ">> Open http://localhost:5173"
Write-Host ">> Press Ctrl+C to stop."

try {
    Wait-Process -Id $frontend.Id -ErrorAction SilentlyContinue
}
finally {
    Write-Host ">> Shutting down ..."
    Stop-Process -Id $frontend.Id -ErrorAction SilentlyContinue
    Stop-Process -Id $backend.Id -ErrorAction SilentlyContinue
}
