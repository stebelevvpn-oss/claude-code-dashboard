# PostToolUse hook: rebuild the registry dashboard when an agent/skill/settings file changes.
# Reads the hook payload (JSON) from stdin, extracts the modified file path, and only rebuilds if it matches.

$ErrorActionPreference = 'SilentlyContinue'

try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
} catch {
    exit 0
}

$path = $payload.tool_input.file_path
if (-not $path) { exit 0 }

# Trigger only on changes to agents, skills, or top-level settings files.
$normalized = $path -replace '\\', '/'
if ($normalized -notmatch '\.claude/(agents|skills|plugins)/' -and
    $normalized -notmatch '\.claude\.json$' -and
    $normalized -notmatch '\.claude/settings\.json$' -and
    $normalized -notmatch '\.claude/registry/translations\.json$') {
    exit 0
}

$script = Join-Path $env:USERPROFILE '.claude\registry\build_registry.py'
& python $script *>&1 | Out-Null
exit 0
