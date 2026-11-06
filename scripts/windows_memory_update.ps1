# Apply the memory update onto an existing Mark LVI install.
# Does not start the app, does not call an API, and does not overwrite
# API keys, memory, .venv, voice_bridge.py or plugins\grok_voice_bridge.py.
#
# Dry run (default — prints the plan, writes nothing):
#   powershell -ExecutionPolicy Bypass -File scripts\windows_memory_update.ps1 `
#       -InstallDir "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main" `
#       -SourceDir  "C:\path\to\this\update"
#
# Apply:
#   powershell -ExecutionPolicy Bypass -File scripts\windows_memory_update.ps1 `
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

$script = Join-Path $SourceDir "scripts\apply_memory_update.py"
if (-not (Test-Path -LiteralPath $script)) {
    Write-Error "Update script not found at $script. Point -SourceDir at the unpacked update, not at the installation."
}

# Show the interpreter version before anything is copied. The Python script
# aborts unless it is 3.12.x and warns when it is not exactly 3.12.10.
& $python -c "import sys; print('Python', sys.version)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$args = @(
    $script,
    "--install", $InstallDir,
    "--source", $SourceDir
)
if ($Yes) {
    $args += "--yes"
} else {
    $args += "--dry-run"
}

& $python @args
exit $LASTEXITCODE
