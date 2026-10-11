# Kill leftovers of THIS app before a new launch.
#
# Why: a crashed or half-killed run leaves an uvicorn (python) or `next start`
# (node) behind. The old launcher only looked at ports 8000/3000, so a stray
# process that had already lost its port -- or a `next` child whose parent died
# -- stayed resident, held the log files open, and made the next start fail with
# "The process cannot access the file because it is being used by another
# process" or bind errors. This script removes them first, so double-clicking
# start.bat always begins from a clean slate.
#
# Safety: a process is killed only when ALL of these hold
#   * image name is python.exe / pythonw.exe / node.exe
#   * its command line mentions this project directory
#   * it is not the process running this script, and not its parent
# WorkBuddy's own node processes, other projects, and unrelated Python tools
# never match the path, so they are untouched.
#
# Usage: clean_stale.ps1 -Root <project> [-WhatIf]

param(
    [Parameter(Mandatory = $true)]
    [string] $Root,
    [switch] $WhatIf,
    # Evaluate one candidate command line against the same rule and print
    # "would kill" / "skipped", without touching any process. Lets the matching
    # rule be checked (and demonstrated) on its own.
    [string] $TestCommandLine = ""
)

$ErrorActionPreference = "Continue"

function Say([string] $m) { Write-Host "[clean] $m" }

$rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')

function Test-IsOurProcess([string] $commandLine) {
    <#
    True only for a python/node process that belongs to this project.

    The rule is deliberately narrow: the image must be python/pythonw/node AND
    the command line must mention this project directory. Anything else -- the
    editor's own node, another project's uvicorn, a system Python -- is left
    alone, because killing a stranger's process is far worse than leaving a
    leftover behind for one more run.
    #>
    if (-not $commandLine) { return $false }
    $norm = $commandLine -replace '/', '\'
    return $norm -like "*$rootFull*"
}

if ($TestCommandLine) {
    if (Test-IsOurProcess $TestCommandLine) {
        Say "would kill: $TestCommandLine"
        exit 0
    }
    Say "skipped (not this project): $TestCommandLine"
    exit 1
}

$self = $PID
$parent = 0
try { $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$self" -ErrorAction Stop).ParentProcessId } catch { }

$killed = 0
$seen = 0
try {
    $procs = Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object { $_.Name -match '^(python|pythonw|node)\.exe$' }
} catch {
    Say "could not enumerate processes ($($_.Exception.Message)); skipping"
    exit 0
}

foreach ($p in $procs) {
    if ($p.ProcessId -eq $self -or $p.ProcessId -eq $parent) { continue }
    $cmd = [string]$p.CommandLine
    if (-not (Test-IsOurProcess $cmd)) { continue }
    $seen++
    if ($WhatIf) {
        Say "would stop pid $($p.ProcessId) $($p.Name)"
        continue
    }
    Say "stopping leftover pid $($p.ProcessId) $($p.Name)"
    # /T so a `next start` child goes with its parent.
    & taskkill.exe /PID $p.ProcessId /T /F 2>&1 | Out-Null
    $killed++
}

if ($seen -eq 0) {
    Say "no leftover processes for this project"
} elseif (-not $WhatIf) {
    Say "stopped $killed process(es); waiting for the OS to release ports and files"
    Start-Sleep -Milliseconds 800
}

exit 0