# Smart launcher for the Claude Code registry dashboard.
# Behaviour:
#   1. If the local server is already responding on 127.0.0.1:8765 → just open the browser.
#   2. Otherwise → start server.py silently via pythonw, wait until it's ready, then open the browser.
# Designed to be invoked from a desktop shortcut with: powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File <this>

$ErrorActionPreference = 'SilentlyContinue'

$port    = 8765
$root    = Join-Path $env:USERPROFILE '.claude\registry'
$server  = Join-Path $root 'server.py'
$pyw     = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'
$url     = "http://127.0.0.1:$port/"

if (-not (Test-Path $pyw)) {
    $cmd = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($cmd) { $pyw = $cmd.Source }
}
if (-not (Test-Path $pyw)) {
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-Command',
        "Write-Host 'pythonw.exe not found. Install Python or set the path inside launch.ps1.' -ForegroundColor Red"
    )
    exit 1
}

function Test-Server {
    try {
        $r = Invoke-WebRequest -Uri "$url`api/health" -TimeoutSec 1 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (-not (Test-Server)) {
    Start-Process -FilePath $pyw -ArgumentList @($server) -WindowStyle Hidden -WorkingDirectory $root | Out-Null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
        if (Test-Server) { break }
    }
}

Start-Process $url
