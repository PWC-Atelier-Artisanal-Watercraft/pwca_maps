# Builds the three long files of the FULL North America pack one after the other (FORMAT.md 7.6): the lines (roads,
# railways, borders), the water (every pond and stream), the minor ways (service roads, tracks, paths). About 15 hours
# of PC time on 4 processes; nothing here touches a card or a board.
#
# It runs from a snapshot of the tools (a detached git worktree at -Commit), so the working tree can be edited while
# the build runs. Each step writes <Out>\<step>-build.log; a failed step stops the run.
#
# Usage: powershell -File tools\build_full_na.ps1 [-Out out\na-full] [-Commit HEAD] [-Steps lines,water,minor]
param(
    [string]$Out = "out\na-full",
    [string]$Commit = "HEAD",
    [string[]]$Steps = @("lines", "water", "minor"),
    [string]$Extract = "sources\north-america-latest.osm.pbf",
    [string]$Sea = "sources\water-polygons-split-4326.zip"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$short = (git -C $repo rev-parse --short $Commit).Trim()
$snap = Join-Path $repo "out\_tools-$short"
if (-not (Test-Path $snap)) {
    git -C $repo worktree add --detach $snap $Commit | Out-Null
}
$outDir = Join-Path $repo $Out
New-Item -ItemType Directory -Force $outDir | Out-Null
$extractPath = Join-Path $repo $Extract
$seaPath = Join-Path $repo $Sea
$tool = Join-Path $snap "tools\build_pack.py"
$runs = @{
    "lines" = @($extractPath, (Join-Path $outDir "lines"), "--layer", "roads", "--full", "--name", "north-america-roads")
    "water" = @($extractPath, (Join-Path $outDir "water"), "--full", "--sea", $seaPath, "--name", "north-america")
    "minor" = @($extractPath, (Join-Path $outDir "minor"), "--layer", "roads", "--minor", "--name", "north-america-minor")
}
foreach ($step in $Steps) {
    $log = Join-Path $outDir "$step-build.log"
    "[{0}] {1}: start (tools at {2})" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $step, $short | Tee-Object -FilePath (Join-Path $outDir "run.log") -Append
    $stepArgs = $runs[$step]
    & python $tool @stepArgs --low-priority *> $log
    $code = $LASTEXITCODE
    "[{0}] {1}: exit {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $step, $code | Tee-Object -FilePath (Join-Path $outDir "run.log") -Append
    if ($code -ne 0) {
        exit $code
    }
}
