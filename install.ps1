# Claude Code Registry — installer for Windows.
# Run from the bundle directory:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = 'Stop'

function Info($msg)  { Write-Host "[*] $msg" -ForegroundColor Cyan }
function OK($msg)    { Write-Host "[+] $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Fail($msg)  { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }

# --- Locate the bundle (this script's directory) ---
$bundle = Split-Path -Parent $MyInvocation.MyCommand.Path
$registrySrc = Join-Path $bundle 'registry'
$agentsSrc   = Join-Path $bundle 'agents'
$scriptsSrc  = Join-Path $bundle 'scripts'

foreach ($d in @($registrySrc, $agentsSrc, $scriptsSrc)) {
    if (-not (Test-Path $d)) { Fail "Bundle is incomplete — missing folder: $d" }
}

# --- Prerequisite checks ---
Info 'Checking prerequisites...'

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { Fail 'python not on PATH. Install Python 3.10+ first (https://python.org/downloads), tick "Add to PATH" during install.' }
$pyVer = & python --version 2>&1
OK "Python: $pyVer"

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Warn 'claude CLI not on PATH. Install with: npm install -g @anthropic-ai/claude-code'
    Warn 'Then run: claude  (and authenticate). The dashboard will install but the Run buttons will fail until you do.'
} else {
    $claudeVer = & claude --version 2>&1
    OK "Claude CLI: $claudeVer"
}

# Find Edge/Chrome for PDF
$browsers = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
$browserFound = $browsers | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($browserFound) { OK "Browser for PDF: $browserFound" } else { Warn 'Edge/Chrome not found — PDF export will not work until you install one.' }

# --- Destination paths ---
$claudeDir   = Join-Path $env:USERPROFILE '.claude'
$registryDst = Join-Path $claudeDir 'registry'
$agentsDst   = Join-Path $claudeDir 'agents'
$home_       = $env:USERPROFILE
$settings    = Join-Path $claudeDir 'settings.json'

Info "Target: $claudeDir"
New-Item -ItemType Directory -Force -Path $registryDst, $agentsDst | Out-Null

# --- Copy files ---
Info 'Copying registry files...'
Copy-Item (Join-Path $registrySrc '*') $registryDst -Force -Recurse
OK "registry → $registryDst"

Info 'Copying agents...'
Copy-Item (Join-Path $agentsSrc '*.md') $agentsDst -Force
OK "agents → $agentsDst"

Info 'Copying WB scripts...'
Copy-Item (Join-Path $scriptsSrc 'wb_download.py')       $home_ -Force
Copy-Item (Join-Path $scriptsSrc 'wb_reviews.py')        $home_ -Force
Copy-Item (Join-Path $scriptsSrc 'wb_supply_planner.py') $home_ -Force
OK "wb_download.py + wb_reviews.py + wb_supply_planner.py → $home_"

# --- Install Pillow (best-effort) ---
$pip = Get-Command pip -ErrorAction SilentlyContinue
if ($pip) {
    Info 'Installing Pillow (used by wb_download.py for webp→jpg)...'
    try {
        & pip install --quiet --user --upgrade Pillow 2>&1 | Out-Null
        OK 'Pillow installed.'
    } catch {
        Warn "Pillow install failed: $($_.Exception.Message). wb-photo-downloader will save .webp instead of .jpg."
    }
} else {
    Warn 'pip not on PATH — skipping Pillow install. wb-photo-downloader will save .webp instead of .jpg.'
}

# --- Merge hooks into settings.json ---
Info 'Merging hooks into settings.json...'

$hookScript    = Join-Path $registryDst 'rebuild_hook.ps1'
$builderScript = Join-Path $registryDst 'build_registry.py'

# Use %USERPROFILE% so the JSON is portable across users on the same install.
$hookCmd       = 'powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\.claude\registry\rebuild_hook.ps1"'
$sessionStartCmd = 'powershell -NoProfile -ExecutionPolicy Bypass -Command "python ""%USERPROFILE%\.claude\registry\build_registry.py"" | Out-Null"'

if (Test-Path $settings) {
    $existing = Get-Content $settings -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    $existing = New-Object PSObject
}

# Ensure 'hooks' object exists
if (-not $existing.PSObject.Properties.Match('hooks').Count) {
    $existing | Add-Member -MemberType NoteProperty -Name 'hooks' -Value (New-Object PSObject)
}

$postToolUseEntry = @(
    @{
        matcher = 'Edit|Write|MultiEdit'
        hooks = @(@{ type = 'command'; command = $hookCmd })
    }
)
$sessionStartEntry = @(
    @{
        hooks = @(@{ type = 'command'; command = $sessionStartCmd })
    }
)

# Replace just our entries (idempotent)
$existing.hooks | Add-Member -MemberType NoteProperty -Name 'PostToolUse' -Value $postToolUseEntry -Force
$existing.hooks | Add-Member -MemberType NoteProperty -Name 'SessionStart' -Value $sessionStartEntry -Force

$existing | ConvertTo-Json -Depth 12 | Set-Content -Path $settings -Encoding UTF8
OK "Hooks merged into $settings"

# --- Generate initial dashboard ---
Info 'Generating dashboard...'
& python (Join-Path $registryDst 'build_registry.py') | Out-Null
if (Test-Path (Join-Path $registryDst 'dashboard.html')) {
    OK 'dashboard.html generated.'
} else {
    Warn 'dashboard.html was not created — check the build_registry.py output above.'
}

# --- Desktop shortcut ---
Info 'Creating desktop shortcut...'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Мои агенты.lnk'
$launcher = Join-Path $registryDst 'launch.ps1'
$icon = Join-Path $registryDst 'dashboard.ico'

$wsh = New-Object -ComObject WScript.Shell
$sc = $wsh.CreateShortcut($lnk)
$sc.TargetPath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$sc.Arguments = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`""
$sc.WorkingDirectory = $registryDst
$sc.WindowStyle = 7
$sc.Description = 'Открыть дашборд агентов Claude Code'
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
$sc.Save()
OK "Shortcut: $lnk"

# --- Done ---
Write-Host ''
Write-Host '╔════════════════════════════════════════╗' -ForegroundColor Green
Write-Host '║  Установка завершена.                  ║' -ForegroundColor Green
Write-Host '╚════════════════════════════════════════╝' -ForegroundColor Green
Write-Host ''
Write-Host 'Откройте ярлык «Мои агенты» на рабочем столе.' -ForegroundColor White
Write-Host 'Или вручную:' -ForegroundColor Gray
Write-Host "  python `"$registryDst\server.py`"" -ForegroundColor Gray
Write-Host '  → http://127.0.0.1:8765/' -ForegroundColor Gray

if (-not $claude) {
    Write-Host ''
    Warn 'Не забудьте установить и авторизовать Claude CLI:'
    Warn '  npm install -g @anthropic-ai/claude-code'
    Warn '  claude   # пройти авторизацию в браузере'
}
