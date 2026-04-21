# run-dev.ps1 — foreground dev watcher for nexus_bot.py.
# Ctrl-C cleanly kills the watcher AND any nexus_bot.py child it launched.
# Leaves run.ps1 (production launcher) untouched.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Write-Host "nexus dev watcher - Ctrl-C to stop"

# Start the watcher as a child so we can reliably clean it (and its child bot)
# up on Ctrl-C without orphan python procs.
$watcher = Start-Process -FilePath "python" `
    -ArgumentList "dev_watcher.py" `
    -NoNewWindow -PassThru

function Stop-DevTree {
    # Kill the watcher first.
    if ($watcher -and -not $watcher.HasExited) {
        try { Stop-Process -Id $watcher.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    # Sweep any stray python procs running nexus_bot.py or dev_watcher.py.
    try {
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
            Where-Object {
                $_.CommandLine -and (
                    $_.CommandLine -match 'nexus_bot\.py' -or
                    $_.CommandLine -match 'dev_watcher\.py'
                )
            } |
            ForEach-Object {
                try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
            }
    } catch {}
}

# Fire cleanup on normal exit AND on Ctrl-C.
Register-EngineEvent PowerShell.Exiting -Action { Stop-DevTree } | Out-Null

try {
    while (-not $watcher.HasExited) {
        Start-Sleep -Milliseconds 500
    }
    $exit = $watcher.ExitCode
    Write-Host "[run-dev] watcher exited with code $exit"
    exit $exit
}
finally {
    Stop-DevTree
}
