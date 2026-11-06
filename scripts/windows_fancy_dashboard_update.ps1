# Add the Fancy Dashboard tool to a Mark LVI install that already has the
# memory journal. Does not start Jarvis, does not call an API, and does not
# overwrite API keys, memory, .venv, voice_bridge.py, grok_voice_bridge.py,
# or the dashboard launcher under JarvisProjects.
#
# Dry run (writes nothing):
#   powershell -ExecutionPolicy Bypass -File scripts\windows_fancy_dashboard_update.ps1 `
#       -InstallDir "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main" `
#       -SourceDir  "C:\path\to\this\update"
#
# Apply:
#   powershell -ExecutionPolicy Bypass -File scripts\windows_fancy_dashboard_update.ps1 `
#       -InstallDir "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main" `
#       -SourceDir  "C:\path\to\this\update" -Yes
param(
    [string]$InstallDir = "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main",
    [string]$SourceDir = "",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

if (-not $SourceDir) {
    $SourceDir = Split-Path -Parent $PSScriptRoot
}

$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Expected the install's virtualenv at $python. Refusing to use any other Python."
}

$store = Join-Path $InstallDir "memory\store.py"
if (-not (Test-Path -LiteralPath $store)) {
    Write-Error "memory\store.py is missing. Install the memory update first (MEMORY_VERSION 1)."
}
$version = Select-String -LiteralPath $store -Pattern 'MEMORY_VERSION\s*=\s*"1"' -Quiet
if (-not $version) {
    Write-Error "memory\store.py is not MEMORY_VERSION 1. This dashboard tool expects that memory update."
}

$script = Join-Path $SourceDir "scripts\apply_memory_update.py"
$manifest = Join-Path $SourceDir "scripts\fancy_dashboard_update_files.txt"
if (-not (Test-Path -LiteralPath $script)) {
    Write-Error "Updater not found at $script. Point -SourceDir at the unpacked update, not at the installation."
}
if (-not (Test-Path -LiteralPath $manifest)) {
    Write-Error "File list not found at $manifest."
}

Write-Host "Leaving these alone if they exist:"
foreach ($rel in @(
    "config\api_keys.json",
    "memory\long_term.json",
    "memory\journal.sqlite",
    "voice_bridge.py",
    "plugins\grok_voice_bridge.py"
)) {
    $path = Join-Path $InstallDir $rel
    if (Test-Path -LiteralPath $path) { Write-Host "  $path" }
}
Write-Host "Not touching C:\Users\Chris\Desktop\JarvisProjects\Fancy_Dashboard\Launch-FancyDashboard.ps1"

& $python -c "import sys; print('Python', sys.version)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$args = @(
    $script,
    "--install", $InstallDir,
    "--source", $SourceDir,
    "--manifest", $manifest
)
if ($Yes) {
    $args += "--yes"
} else {
    $args += "--dry-run"
}

& $python @args
exit $LASTEXITCODE
