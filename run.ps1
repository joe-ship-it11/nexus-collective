# run.ps1 — runs a python admin script, waits for it, prints stdout+stderr.
# Usage: .\run.ps1 <script.py> [timeoutSeconds]
param(
    [Parameter(Mandatory=$true)][string]$Script,
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$outFile = Join-Path $here "_run_out.txt"
$errFile = Join-Path $here "_run_err.txt"
if (Test-Path $outFile) { Remove-Item $outFile }
if (Test-Path $errFile) { Remove-Item $errFile }

$env:PYTHONIOENCODING = "utf-8"

$p = Start-Process -FilePath "python" -ArgumentList $Script `
    -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
    -PassThru -NoNewWindow

if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
    try { $p.Kill() } catch {}
    Write-Output "TIMEOUT after $TimeoutSeconds s"
    exit 124
}

Write-Output "--- STDOUT ---"
if (Test-Path $outFile) { Get-Content $outFile }
Write-Output "--- STDERR ---"
if (Test-Path $errFile) { Get-Content $errFile }
Write-Output "--- EXIT $($p.ExitCode) ---"
