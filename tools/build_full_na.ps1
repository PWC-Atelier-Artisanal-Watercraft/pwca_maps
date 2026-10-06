# Builds the three long files of the FULL North America pack one after the other (FORMAT.md 7.6): the lines (roads,
# railways, borders), the water (every pond and stream), the minor ways (service roads, tracks, paths). About 15 hours
# of PC time; nothing here touches a card or a board.
#
# Run it from your OWN PowerShell window (not from inside an app that may restart) and leave the window open:
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\git\pwca_maps\tools\build_full_na.ps1 -Commit <commit>
#
# The window shows a percent-complete line for the build in hand and for all three together, and the same in its
# title. Every line of each build goes to <Out>\<step>-build.log; start, end and the progress lines also go to
# <Out>\run.log. A failed step stops the run. The builds run at HIGH priority (pass -Normal for normal priority).
#
# It runs from a snapshot of the tools (a detached git worktree at -Commit), so the working tree can be edited while
# the build runs.
param(
    [string]$Out = "out\na-full",
    [string]$Commit = "HEAD",
    [string[]]$Steps = @("lines", "water", "minor"),
    [string]$Extract = "sources\north-america-latest.osm.pbf",
    [string]$Sea = "sources\water-polygons-split-4326.zip",
    [switch]$Normal
)
# "Continue": git and python write ordinary progress to their error stream, which must not end this script; every
# step's exit code is checked instead.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$short = "$(git -C $repo rev-parse --short $Commit 2>$null)".Trim()
if (-not $short) {
    Write-Host "No such commit in ${repo}: $Commit"
    exit 2
}
$snap = Join-Path $repo "out\_tools-$short"
if (-not (Test-Path (Join-Path $snap "toolsuild_pack.py"))) {
    git -C $repo worktree add --detach $snap $Commit 2>&1 | Out-Null
}
if (-not (Test-Path (Join-Path $snap "toolsuild_pack.py"))) {
    Write-Host "Could not make the snapshot of the tools at $snap"
    exit 2
}
$outDir = Join-Path $repo $Out
New-Item -ItemType Directory -Force $outDir | Out-Null
$extractPath = Join-Path $repo $Extract
$seaPath = Join-Path $repo $Sea
$tool = Join-Path $snap "tools\build_pack.py"
$runLog = Join-Path $outDir "run.log"
$runs = @{
    "lines" = @($extractPath, (Join-Path $outDir "lines"), "--layer", "roads", "--full", "--name", "north-america-roads")
    "water" = @($extractPath, (Join-Path $outDir "water"), "--full", "--sea", $seaPath, "--name", "north-america")
    "minor" = @($extractPath, (Join-Path $outDir "minor"), "--layer", "roads", "--minor", "--name", "north-america-minor")
}
# Each build's rough share of the whole run, for the overall percent.
$share = @{ "lines" = 20; "water" = 60; "minor" = 20 }
$total = 0
foreach ($step in $Steps) { $total += $share[$step] }
$doneShare = 0
$n = 0
function Say([string]$text) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
    Write-Host $line
    Add-Content -Path $runLog -Value $line -Encoding utf8
}
foreach ($step in $Steps) {
    $n++
    $log = Join-Path $outDir "$step-build.log"
    if (Test-Path $log) { Remove-Item $log }
    Say ("{0}: start (build {1} of {2}; tools at {3})" -f $step, $n, $Steps.Count, $short)
    $stepArgs = $runs[$step]
    if (-not $Normal) { $stepArgs += "--high-priority" }
    & python -u $tool @stepArgs 2>&1 | ForEach-Object {
        $line = "$_"
        Add-Content -Path $log -Value $line -Encoding utf8
        if ($line -match "PROGRESS (\d+)% of this build \((.*)$") {
            $pct = [int]$Matches[1]
            $overall = [int](($doneShare + $share[$step] * $pct / 100) * 100 / $total)
            $text = "{0} (build {1} of {2}): {3}%   all builds: {4}%   ({5}" -f $step, $n, $Steps.Count, $pct, $overall, $Matches[2]
            Say $text
            $Host.UI.RawUI.WindowTitle = "Map builds: all $overall% - $step $pct%"
        } elseif ($line -match "Traceback|Error|error:") {
            Say ("{0}: {1}" -f $step, $line)
        }
    }
    $code = $LASTEXITCODE
    $doneShare += $share[$step]
    Say ("{0}: exit {1}" -f $step, $code)
    if ($code -ne 0) {
        Say "STOPPED: the $step build failed. See $log"
        $Host.UI.RawUI.WindowTitle = "Map builds: FAILED at $step"
        exit $code
    }
}
Say "ALL BUILDS FINISHED. Files are in $outDir"
$Host.UI.RawUI.WindowTitle = "Map builds: FINISHED"
