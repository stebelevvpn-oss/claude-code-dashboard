# Starts the registry launcher server and opens the dashboard in your browser.
# Usage: powershell -ExecutionPolicy Bypass -File ~/.claude/registry/start.ps1

$ErrorActionPreference = 'Stop'
$registry = Join-Path $env:USERPROFILE '.claude\registry'
$server = Join-Path $registry 'server.py'

if (-not (Test-Path $server)) {
    Write-Host "Server script not found at $server" -ForegroundColor Red
    exit 1
}

Start-Process "http://127.0.0.1:8765/"
Write-Host "Browser opened. Server starting (Ctrl+C to stop)..." -ForegroundColor Cyan
& python $server
