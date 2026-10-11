# Session watchdog: stop backend + frontend once the browser UI is gone.
#
# The request behind this script: closing the browser should shut the app
# down, so nobody has to stop it by hand. A port probe cannot see that (next start
# answers long after the last tab is closed), so the page itself reports in:
#
#   frontend/src/components/session-heartbeat.tsx
#     -> POST http://127.0.0.1:8000/api/v1/system/heartbeat  every 15s
#   backend/app/api/system.py
#     -> writes the epoch seconds of that beat to tmp\last_beat
#   this script
#     -> reads tmp\last_beat only. No network calls at all, so a proxy, an
#        antivirus or a corporate policy that blocks PowerShell web requests
#        cannot make a healthy app look dead (that bug killed a running app in
#        testing).
#
# Rules, in order:
#   * never fired a beat  -> keep waiting (the browser may still be loading, or
#                            JS may be blocked). Nothing is killed.
#   * idle >= IdleSeconds -> the browser is gone: kill :8000 and :3000, exit.
#   * backend port closed  -> it died on its own: clean up whatever is left.
#
# IdleSeconds defaults to AUTO_STOP_IDLE_SECONDS or 180. Set it to 0 to disable
# auto-stop entirely (then the app runs until Windows reboots).
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File session_watchdog.ps1 -Root <project>

param(
    [Parameter(Mandatory = $true)]
    [string] $Root,
    [int] $IdleSeconds = 0,
    [int] $PollSeconds = 5
)

# Native tools write progress to stderr; Stop would turn that into a
# terminating error, so correctness comes from exit codes here.
$ErrorActionPreference = "Continue"

$logDir = Join-Path $Root "tmp"
$logFile = Join-Path $logDir "watchdog.log"
$beatFile = Join-Path $logDir "last_beat"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

function Say([string] $m) {
    $line = "[watchdog] $m"
    Write-Host $line
    try {
        Add-Content -LiteralPath $logFile -Value ((Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " " + $line) -Encoding UTF8
    } catch { }
}

if ($IdleSeconds -le 0) {
    $fromEnv = 0
    try { $fromEnv = [int]($env:AUTO_STOP_IDLE_SECONDS) } catch { $fromEnv = 0 }
    $IdleSeconds = if ($fromEnv -gt 0) { $fromEnv } else { 180 }
}
if ($IdleSeconds -le 0) {
    Say "AUTO_STOP_IDLE_SECONDS=0 - auto-stop disabled, not starting"
    exit 0
}

function Stop-Listener([int] $port) {
    # netstat + taskkill rather than Get-NetTCPConnection: the CIM query needs
    # a working WMI stack, and when it stalls the shutdown stalls with it.
    try {
        foreach ($line in (netstat -ano -p tcp | Select-String "LISTENING")) {
            $parts = ($line.ToString().Trim() -split '\s+')
            if ($parts.Count -lt 5) { continue }
            if ($parts[1] -notmatch ":(\d+)$") { continue }
            if ([int]$Matches[1] -ne $port) { continue }
            Say "stopping pid $($parts[4]) on port $port"
            & taskkill.exe /PID $parts[4] /F 2>&1 | Out-Null
        }
    } catch {
        Say "could not inspect port $port ($($_.Exception.Message))"
    }
}

function Test-PortOpen([int] $port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect("127.0.0.1", $port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(1500, $false)) { $client.Close(); return $false }
        $client.EndConnect($async)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Get-LastBeat {
    # Returns $null when the page has not reported (yet), else epoch seconds.
    try {
        if (-not (Test-Path -LiteralPath $beatFile)) { return $null }
        $raw = (Get-Content -LiteralPath $beatFile -Raw -ErrorAction Stop)
        if (-not $raw) { return $null }
        $value = 0.0
        if (-not [double]::TryParse($raw.Trim(), [ref]$value)) { return $null }
        if ($value -le 0) { return $null }
        return $value
    } catch {
        return $null
    }
}

Say "watching session; auto-stop after $IdleSeconds s without a browser heartbeat"

$misses = 0

while ($true) {
    Start-Sleep -Seconds $PollSeconds

    $beat = Get-LastBeat
    if ($null -eq $beat) {
        # Never heard from the page. start.bat deletes tmp\last_beat before it
        # starts anything, so an old file cannot be mistaken for this session.
        if (-not (Test-PortOpen 8000)) {
            $misses++
            if ($misses -ge 3) {
                Say "nothing is listening on :8000 and the browser never reported"
                Say "reaching out. Cleaning up and exiting."
                Stop-Listener 8000
                Stop-Listener 3000
                break
            }
        }
        continue
    }
    $misses = 0

    $idle = [double]([DateTimeOffset]::Now.ToUnixTimeMilliseconds()) / 1000.0 - $beat
    if ($idle -ge $IdleSeconds) {
        Say "no browser heartbeat for $([int]$idle) s (limit $IdleSeconds) - shutting the app down"
        Stop-Listener 8000
        Stop-Listener 3000
        Say "done. Double-click start.bat to bring it back."
        break
    }
}

exit 0