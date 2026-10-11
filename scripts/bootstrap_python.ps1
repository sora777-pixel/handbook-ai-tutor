# Validate -- or rebuild -- the project virtual environment.
#
# Why this exists: start.bat used to check only that .venv\Scripts\python.exe
# *exists*. A venv carries a hard pointer to the interpreter that created it
# (pyvenv.cfg -> home / executable). Uninstall that interpreter and the
# launcher still starts "successfully", while every python.exe call dies
# instantly with "No Python at '<old-path>\python.exe'".
# The backend then never binds :8000, the launcher waits the full health
# timeout, and the browser finally opens onto "Backend unreachable".
#
# So: run the interpreter, not stat it. Exit 0 = .venv is usable.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap_python.ps1 -Root <project>

param(
    [Parameter(Mandatory = $true)]
    [string] $Root,
    [string] $Index = "https://pypi.tuna.tsinghua.edu.cn/simple"
)

# Native tools (python.exe, pip) write progress to stderr. With
# ErrorActionPreference="Stop" PowerShell 5.1 turns that into a *terminating*
# NativeCommandError -- which is exactly what a broken venv prints on every
# call. So the default here is Continue and correctness comes from exit codes
# plus explicit -ErrorAction Stop on the cmdlets we actually want to catch.
$ErrorActionPreference = "Continue"
$venv = Join-Path $Root ".venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
$backend = Join-Path $Root "backend"
$stampFile = Join-Path $venv ".bootstrap-ok"

function Say([string] $m) { Write-Host "[python] $m" }

# ---------------------------------------------------------------- usable venv

function Test-Venv {
    if (-not (Test-Path -LiteralPath $venvPy)) { return $false }
    # A broken venv prints "No Python at ..." and exits non-zero within ~50ms.
    # stderr goes to $null so it cannot be mistaken for a terminating error.
    $null = & $venvPy -c "import sys" 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    # Imports, not just the interpreter: a venv missing uvicorn/fastapi would
    # otherwise pass here and blow up 30 seconds later inside the health wait.
    $null = & $venvPy -c "import uvicorn, fastapi, sqlalchemy" 2>$null
    return ($LASTEXITCODE -eq 0)
}

if (Test-Venv) {
    $v = (& $venvPy -c "import sys; print(sys.version.split()[0])" 2>$null | Select-Object -Last 1)
    Say "venv ready (python $v)"
    exit 0
}

if (Test-Path -LiteralPath $venvPy) {
    # Keep the evidence instead of deleting it: the old folder holds the
    # (unusable) site-packages, and "where did 400 MB go?" is a bad surprise.
    $broken = Join-Path $Root (".venv.broken-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    Say "existing .venv cannot run -- it points at an interpreter that no longer exists"
    try {
        Rename-Item -LiteralPath $venv -NewName (Split-Path -Leaf $broken) -ErrorAction Stop
        Say "moved it aside to $(Split-Path -Leaf $broken) (safe to delete)"
    } catch {
        Say "could not move it aside ($($_.Exception.Message)); recreating in place"
        Remove-Item -LiteralPath $venv -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------------- find a base python

function Get-Version([string] $exe) {
    try {
        $out = & $exe --version 2>$null | Select-Object -First 1
    } catch { return $null }
    if ("$out" -match "Python\s+(\d+)\.(\d+)\.(\d+)") {
        return [pscustomobject]@{
            Exe  = $exe
            Text = "$($Matches[1]).$($Matches[2]).$($Matches[3])"
            Major = [int]$Matches[1]
            Minor = [int]$Matches[2]
        }
    }
    return $null
}

function Get-BasePython {
    $candidates = New-Object System.Collections.Generic.List[string]

    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates.Add($cmd.Source) }
    }
    # py launcher: "  -3.13-64   C:\...\python.exe"
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($line in (& py -0p 2>$null)) {
            if ("$line" -match "([A-Za-z]:\\[^\s]+\.exe)") { $candidates.Add($Matches[1]) }
        }
    }
    foreach ($pattern in @(
        "$env:ProgramFiles\Python*\python.exe",
        "${env:ProgramFiles(x86)}\Python*\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
        "$env:USERPROFILE\anaconda3\python.exe",
        "$env:USERPROFILE\miniconda3\python.exe",
        "$env:ProgramData\Anaconda3\python.exe",
        # tool-managed interpreters (WorkBuddy / Codex style layouts)
        "$env:USERPROFILE\.workbuddy\binaries\python\versions\*\python.exe",
        "$env:USERPROFILE\.codex\binaries\python\versions\*\python.exe"
    )) {
        foreach ($hit in (Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue)) {
            if ($hit.PSIsContainer) { continue }
            $candidates.Add($hit.FullName)
        }
    }

    $found = @()
    foreach ($exe in $candidates) {
        if (-not $exe) { continue }
        $info = Get-Version $exe
        if (-not $info) { continue }
        if ($info.Major -lt 3 -or ($info.Major -eq 3 -and $info.Minor -lt 11)) { continue }
        $found += $info
    }
    if (-not $found) { return $null }
    # newest first
    return ($found | Sort-Object -Property Major, Minor -Descending | Select-Object -First 1)
}

$base = Get-BasePython
if (-not $base) {
    Say "no Python 3.11+ interpreter found on this machine."
    Say "install one from https://www.python.org/downloads/ (tick 'Add Python to PATH'),"
    Say "or point PYTHON_HOME at an existing python.exe, then double-click start.bat again."
    exit 1
}
Say "using base interpreter $($base.Exe) (python $($base.Text))"
if ($base.Major -ge 3 -and $base.Minor -ge 13) {
    Say "note: python 3.13 has no offline-OCR wheel (rapidocr-onnxruntime),"
    Say "      so images / scanned PDFs will use your vision model instead."
}

# ------------------------------------------------------------- create + fill

Say "creating virtual environment .venv (first run takes a few minutes) ..."
& $base.Exe -m venv $venv
if ($LASTEXITCODE -ne 0) { Say "python -m venv failed (exit $LASTEXITCODE)"; exit 1 }

# Try the fast mirror first, then fall back. A blocked or unreachable mirror is
# a common way for an otherwise fine install to fail ("Could not find a version
# that satisfies the requirement setuptools"), and the official index usually
# works when a regional mirror does not.
$indexes = New-Object System.Collections.Generic.List[string]
if ($Index) { $indexes.Add($Index) }
if ($env:PYPI_FALLBACK_INDEX) { $indexes.Add($env:PYPI_FALLBACK_INDEX) }
$indexes.Add("https://pypi.org/simple")

$installed = $false
foreach ($ix in ($indexes | Select-Object -Unique)) {
    Say "installing backend dependencies from $ix ..."
    $null = & $venvPy -m pip install --upgrade pip --disable-pip-version-check -i $ix --quiet 2>&1
    # [dev,ocr]: the offline OCR engine is an extra and carries a python_version
    # marker, so this same command works on 3.11/3.12 (engine installed) and on
    # 3.13+ (engine skipped) instead of failing the whole install.
    $null = & $venvPy -m pip install -e "$backend[dev,ocr]" -i $ix --disable-pip-version-check --quiet 2>&1
    if ($LASTEXITCODE -eq 0) { $installed = $true; break }
    Say "  that index did not work; trying the next one ..."
}

if (-not $installed) {
    Say "pip install failed on every index (" + ($indexes -join ", ") + ")."
    Say "Install manually once the network/proxy is right, then start again:"
    Say "  $venvPy -m pip install -e `"$backend[dev,ocr]`" -i <index-url>"
    exit 1
}

if (-not (Test-Venv)) {
    Say "venv was created but still cannot import uvicorn/fastapi/sqlalchemy."
    Say "run: $venvPy -m pip install -e `"$backend[dev]`" -i $Index   and read the error above"
    exit 1
}

Set-Content -LiteralPath $stampFile -Value (Get-Date -Format "o") -Encoding ASCII
$v = (& $venvPy -c "import sys; print(sys.version.split()[0])" 2>$null | Select-Object -Last 1)
Say "venv ready (python $v). This will not be rebuilt on later starts."
exit 0