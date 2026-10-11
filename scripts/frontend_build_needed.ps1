# Exit 1 when frontend/src is newer than the production build (or the build is
# missing). start.bat then runs `npm run build` so a CSS/TS drop actually
# reaches `next start`.
#
# The build stamp lives in different places depending on next.config.js:
#   output: "standalone" -> .next\standalone\.next\BUILD_ID (and .next\BUILD_ID)
#   default               -> .next\BUILD_ID
# Checking only one of them made every launch rebuild the frontend, which is
# most of the "why does startup take minutes" experience.

param(
    [Parameter(Mandatory = $true)]
    [string] $Root
)

$ErrorActionPreference = "Continue"
$src = Join-Path $Root "frontend\src"
if (-not (Test-Path -LiteralPath $src)) { exit 0 }

$candidates = @(
    (Join-Path $Root "frontend\.next\BUILD_ID"),
    (Join-Path $Root "frontend\.next\standalone\.next\BUILD_ID")
)
$built = $null
foreach ($c in $candidates) {
    if (Test-Path -LiteralPath $c) {
        $item = Get-Item -LiteralPath $c
        if (-not $built -or $item.LastWriteTimeUtc -gt $built.LastWriteTimeUtc) { $built = $item }
    }
}
if (-not $built) { exit 1 }

$newest = Get-ChildItem -LiteralPath $src -Recurse -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if (-not $newest) { exit 0 }
if ($newest.LastWriteTimeUtc -gt $built.LastWriteTimeUtc) { exit 1 }
exit 0