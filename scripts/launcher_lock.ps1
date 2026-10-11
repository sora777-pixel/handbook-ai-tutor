# Single-instance guard for start.bat.
#
# Symptom this prevents: two launcher runs at once (double-click twice, a
# "hung" window that is actually still running, a leftover cmd from a previous
# session) each start a backend, and then Windows refuses the second
# Start-Process redirection with
#   "The process cannot access the file because it is being used by another
#    process"
# because both processes try to open tmp\backend8000.log for writing. The
# losing backend then also fights over :8000 (EADDRINUSE) and the SQLite file
# (database is locked), which is what a stalled progress bar looks like.
#
# The lock is a file opened with FileShare.None -- the OS itself guarantees
# only one holder, so it cannot be bypassed. It stores the owning cmd PID, so
# a lock left behind by a crash is detected as stale and taken over instead of
# blocking the app forever.
#
# Usage: launcher_lock.ps1 -Lock <path> -OwnerPid <owner pid> [-Release]
#   Exit 0 + stdout "acquired"           lock taken (fresh or stolen from a dead owner)
#   Exit 1 + stdout "busy:<pid>"         another launcher is alive and holds it
#   With -Release: removes the lock if this process owns it.
#
# The parameter is deliberately NOT called -Pid: $PID is a read-only automatic
# variable, so a parameter of that name makes the script fail to bind at all.

param(
    [Parameter(Mandatory = $true)]
    [string] $Lock,
    [Parameter(Mandatory = $false)]
    [int] $OwnerPid = 0,
    [switch] $Release
)

$ErrorActionPreference = "Continue"

function Get-OwnerAlive([string] $path) {
    try {
        $raw = Get-Content -LiteralPath $path -Raw -ErrorAction Stop
    } catch {
        return 0
    }
    if (-not $raw) { return 0 }
    $raw = $raw.Trim()
    $num = 0
    if (-not [int]::TryParse($raw, [ref]$num)) { return -1 }   # unreadable -> treat as alive
    if ($num -le 0) { return 0 }
    $proc = Get-Process -Id $num -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return 0 }
    return $num
}

if ($Release) {
    try {
        $owner = Get-OwnerAlive $Lock
        if ($owner -eq -1 -or $owner -eq 0 -or $owner -eq $OwnerPid) {
            Remove-Item -LiteralPath $Lock -Force -ErrorAction SilentlyContinue
        }
    } catch { }
    Write-Output "released"
    exit 0
}

function New-Lock([string] $path, [int] $owner) {
    try {
        $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $sw = New-Object System.IO.StreamWriter($fs)
        $sw.WriteLine($owner)
        $sw.Flush()
        $sw.Close()
        return $true
    } catch {
        return $false
    }
}

if (New-Lock $Lock $OwnerPid) {
    Write-Output "acquired"
    exit 0
}

$owner = Get-OwnerAlive $Lock
if ($owner -eq 0 -or $owner -eq -1) {
    # Stale lock from a crash: take it over.
    Remove-Item -LiteralPath $Lock -Force -ErrorAction SilentlyContinue
    if (New-Lock $Lock $OwnerPid) {
        Write-Output "acquired-stale"
        exit 0
    }
}

Write-Output ("busy:" + $(if ($owner -gt 0) { $owner } else { "unknown" }))
exit 1