# nx.ps1 — one-stop supervisor for nexus_bot.py
#
# Subcommands:
#   nx start               — launch bot detached, redirect stdout -> nexus_bot.log
#   nx stop                — kill every python running nexus_bot.py
#   nx restart             — HTTP /restart if alive, else stop+start
#   nx kill                — HTTP /kill (graceful, no respawn)
#   nx ping                — GET /ping (fastest "is it alive")
#   nx state               — GET /state (full JSON snapshot)
#   nx logs [N]            — GET /logs?tail=N (default 120)
#   nx tail [N]            — GET /tail?lines=N (same thing, different name)
#   nx reload <module>     — POST /reload {"module": "..."}
#   nx status              — ping + pid + log freshness in one view
#   nx help                — this message
#
# PID is tracked via `Get-CimInstance Win32_Process` cmdline-match (no pidfile
# to go stale). Log file: nexus_bot.log. HTTP base: http://127.0.0.1:18789.

param(
    [Parameter(Position = 0)][string]$Cmd = "help",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][object[]]$Args
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$LogFile  = Join-Path $here "nexus_bot.log"
$BaseUrl  = "http://127.0.0.1:18789"
$BotArg   = "nexus_bot.py"

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Get-BotProcs {
    try {
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
            Where-Object {
                $_.CommandLine -and $_.CommandLine -match 'nexus_bot\.py' -and
                $_.CommandLine -notmatch 'dev_watcher\.py'
            }
    } catch { @() }
}

function Wait-ForPing {
    param([int]$TimeoutS = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutS)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri "$BaseUrl/ping" -TimeoutSec 1 -ErrorAction Stop
            if ($r.ok -and $r.ready) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Write-Json {
    param($Obj)
    if ($null -eq $Obj) { Write-Host "(null)"; return }
    $Obj | ConvertTo-Json -Depth 8
}

function Http-Get  { param([string]$Path) Invoke-RestMethod -Uri "$BaseUrl$Path" -Method Get  -TimeoutSec 5 }
function Http-Post { param([string]$Path, $Body)
    if ($null -eq $Body) {
        Invoke-RestMethod -Uri "$BaseUrl$Path" -Method Post -TimeoutSec 5
    } else {
        Invoke-RestMethod -Uri "$BaseUrl$Path" -Method Post -TimeoutSec 5 `
            -ContentType "application/json" -Body ($Body | ConvertTo-Json -Compress)
    }
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
function Cmd-Start {
    $existing = @(Get-BotProcs)
    if ($existing.Count -gt 0) {
        $pids = ($existing | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Host "[nx] already running: pid(s) $pids"
        return
    }
    # Use cmd /c with shell redirect — Start-Process can't merge stdout+stderr
    # to the same file, but `>> log 2>&1` does it cleanly. The cmd wrapper
    # exits as soon as python does, so process tracking still works fine via
    # cmdline-match (Get-BotProcs walks for nexus_bot.py).
    $p = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", "python $BotArg >> `"$LogFile`" 2>&1" `
        -WorkingDirectory $here `
        -WindowStyle Hidden -PassThru
    Write-Host "[nx] launched (cmd wrapper pid $($p.Id), python child spawning) — waiting for /ping …"
    if (Wait-ForPing -TimeoutS 30) {
        Write-Host "[nx] ready"
    } else {
        Write-Host "[nx] didn't respond to /ping in 30s — check: nx logs 80"
    }
}

function Cmd-Stop {
    $procs = @(Get-BotProcs)
    if ($procs.Count -eq 0) { Write-Host "[nx] not running"; return }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "[nx] killed pid $($p.ProcessId)"
        } catch {
            Write-Host "[nx] could not kill pid $($p.ProcessId): $_"
        }
    }
}

function Cmd-Restart {
    # Prefer HTTP /restart — lets the bot respawn itself detached. If it's
    # not responsive, fall back to stop+start.
    try {
        $r = Http-Post "/restart" $null
        Write-Host "[nx] HTTP restart fired (old pid $($r.old_pid) -> child $($r.child_pid))"
        Start-Sleep -Seconds 1
        if (Wait-ForPing -TimeoutS 30) { Write-Host "[nx] ready"; return }
        Write-Host "[nx] child didn't come up — falling through to hard restart"
    } catch {
        Write-Host "[nx] /restart unreachable — hard restart"
    }
    Cmd-Stop
    Start-Sleep -Milliseconds 800
    Cmd-Start
}

function Cmd-Kill {
    try {
        $r = Http-Post "/kill" $null
        Write-Host "[nx] graceful kill of pid $($r.pid)"
    } catch {
        Write-Host "[nx] /kill unreachable — hard stop"
        Cmd-Stop
    }
}

function Cmd-Ping   { try { Write-Json (Http-Get "/ping") }  catch { Write-Host "[nx] DOWN: $_" } }
function Cmd-State  { try { Write-Json (Http-Get "/state") } catch { Write-Host "[nx] /state DOWN: $_" } }

function Cmd-Logs {
    $n = 120
    if ($Args.Count -ge 1) { $n = [int]$Args[0] }
    try {
        $r = Invoke-WebRequest -Uri "$BaseUrl/logs?lines=$n" -TimeoutSec 5
        Write-Host $r.Content
    } catch {
        # Fall back to direct file tail if HTTP is dead
        if (Test-Path $LogFile) {
            Write-Host "[nx] /logs unreachable — reading file directly"
            Get-Content -Path $LogFile -Tail $n
        } else {
            Write-Host "[nx] no log file and /logs down"
        }
    }
}

function Cmd-Tail { Cmd-Logs }

function Cmd-Reload {
    if ($Args.Count -lt 1) {
        Write-Host "usage: nx reload <module_name>"
        try {
            $i = Http-Get "/"
            Write-Host "reloadable: $($i.reloadable_modules -join ', ')"
        } catch { }
        return
    }
    $mod = [string]$Args[0]
    try {
        $r = Http-Post "/reload" @{ module = $mod }
        Write-Json $r
    } catch {
        Write-Host "[nx] reload failed: $_"
    }
}

function Cmd-Status {
    $procs = @(Get-BotProcs)
    if ($procs.Count -eq 0) {
        Write-Host "[nx] process: DOWN (no python running nexus_bot.py)"
    } else {
        foreach ($p in $procs) {
            $ws = "{0:N1}" -f ($p.WorkingSetSize / 1MB)
            Write-Host "[nx] process: UP  pid=$($p.ProcessId)  mem=${ws}MB  started=$($p.CreationDate)"
        }
    }
    try {
        $pong = Http-Get "/ping"
        Write-Host "[nx] http:    UP  ready=$($pong.ready)  pid=$($pong.pid)"
    } catch {
        Write-Host "[nx] http:    DOWN ($BaseUrl/ping unreachable)"
    }
    if (Test-Path $LogFile) {
        $info = Get-Item $LogFile
        $age = [int]((Get-Date) - $info.LastWriteTime).TotalSeconds
        $sz  = "{0:N0}" -f $info.Length
        Write-Host "[nx] log:     $LogFile ($sz bytes, last write ${age}s ago)"
    } else {
        Write-Host "[nx] log:     (missing)"
    }
}

function Cmd-Help {
    Get-Content -Path $MyInvocation.MyCommand.Path -TotalCount 20 |
        Where-Object { $_ -match '^# ' } |
        ForEach-Object { $_ -replace '^# ?', '' }
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
switch ($Cmd.ToLower()) {
    "start"   { Cmd-Start }
    "stop"    { Cmd-Stop }
    "restart" { Cmd-Restart }
    "kill"    { Cmd-Kill }
    "ping"    { Cmd-Ping }
    "state"   { Cmd-State }
    "logs"    { Cmd-Logs }
    "tail"    { Cmd-Tail }
    "reload"  { Cmd-Reload }
    "status"  { Cmd-Status }
    "help"    { Cmd-Help }
    default   {
        Write-Host "unknown subcommand: $Cmd"
        Write-Host "try: nx help"
    }
}
