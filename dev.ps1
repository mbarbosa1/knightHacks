# Windows version of dev.sh: start everything needed to see the 3D map on this computer.
#
#     powershell -ExecutionPolicy Bypass -File .\dev.ps1            # template report wording
#     powershell -ExecutionPolicy Bypass -File .\dev.ps1 -Gemini    # Generate uses Gemini (needs GEMINI_API_KEY set)
#
# Opens three windows: backend API (port 8000), simulator service (port 8100) and website (port 5173), then the
# browser. Close a window (or press Ctrl+C in it) to stop that part. dev.sh is unchanged for Mac and Linux.
# Differences from dev.sh, all because of Windows: Python lives in .venv\Scripts\, not .venv/bin/; the simulator
# uses the `py` launcher instead of a repo-root .venv; and the backend runs with --reload, which makes uvicorn use
# the event loop psycopg needs on Windows.
param([switch]$Gemini)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Fail($message) { Write-Host $message -ForegroundColor Red; exit 1 }

# --- checks: the same ones dev.sh makes, with the Windows commands to fix each ---------------------------------
if (-not (Test-Path "$Root\backend\.env")) {
    Fail "Missing backend\.env. Copy backend\.env.example to backend\.env and fill in DATABASE_URL."
}
$BackendPy = "$Root\backend\.venv\Scripts\python.exe"
if (-not (Test-Path $BackendPy)) {
    Fail "Missing backend\.venv. Run:  cd backend; py -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
}
& py -c "import fastapi, uvicorn, shapely, networkx, scipy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "The simulator's Python packages are missing. Run:  py -m pip install -r simulation\requirements.txt"
}
if ($Gemini -and -not $env:GEMINI_API_KEY) {
    Fail "-Gemini needs your key first:  `$env:GEMINI_API_KEY = `"...`""
}
if (-not (Test-Path "$Root\frontend\node_modules\maplibre-gl")) {
    Write-Host "Installing the website's libraries (first run only)..."
    Push-Location "$Root\frontend"; npm install --no-audit --no-fund; Pop-Location
}

# --- settings every window inherits ----------------------------------------------------------------------------
if (-not $env:BACKEND_URL) { $env:BACKEND_URL = "http://127.0.0.1:8000" }
if (-not $env:VITE_API_BASE) { $env:VITE_API_BASE = $env:BACKEND_URL }
if (-not $env:VITE_SIM_BASE) { $env:VITE_SIM_BASE = "http://127.0.0.1:8100" }
$env:SIM_GEMINI = if ($Gemini) { "1" } else { "" }

function Start-Window($title, $folder, $command) {
    Start-Process powershell -WorkingDirectory $folder -ArgumentList @(
        "-NoExit", "-Command", "`$Host.UI.RawUI.WindowTitle = '$title'; $command")
}

Write-Host "Backend    -> http://127.0.0.1:8000"
Start-Window "KnightHacks backend" "$Root\backend" `
    ".venv\Scripts\python.exe -m uvicorn app.asgi:app --host 127.0.0.1 --port 8000 --reload --log-level warning"

$mode = if ($Gemini) { "Gemini wording" } else { "template wording" }
Write-Host "Simulator  -> http://127.0.0.1:8100   ($mode)"
Start-Window "KnightHacks simulator" "$Root\simulation" `
    "py -m uvicorn sim.server:app --port 8100 --log-level warning"

Write-Host "Website    -> http://localhost:5173   (opens in your browser)"
Start-Window "KnightHacks website" "$Root\frontend" "npx vite --port 5173 --strictPort --open"

Write-Host "Started three windows. Close them (or Ctrl+C in each) to stop."
