# Restore code files from a backup made by windows_memory_update.ps1.
# API keys, memory/long_term.json, the journal, voice_bridge.py and
# plugins\grok_voice_bridge.py are left as they are now.
# -IncludeData also puts the pre-update snapshot of those files back,
# which discards keys and memories written after the update.
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDir,
    [string]$InstallDir = "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main",
    [switch]$IncludeData
)

$ErrorActionPreference = "Stop"

$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Expected the install's virtualenv at $python."
}

$SourceGuess = Split-Path -Parent $PSScriptRoot
$script = Join-Path $SourceGuess "scripts\apply_memory_update.py"
if (-not (Test-Path -LiteralPath $script)) {
    Write-Error "Cannot find scripts\apply_memory_update.py next to this script."
}

$args = @(
    $script,
    "--install", $InstallDir,
    "--rollback", $BackupDir
)
if ($IncludeData) {
    $args += "--include-data"
}

& $python @args
exit $LASTEXITCODE
