# Adds the rider symbols (fuel, marina, boat ramp) and the street names to the display's map card pack
# (FORMAT-1.3-SYMBOLS-STREET-NAMES.md). Nothing here touches a card or a board, and no file that is already built is
# changed: the pack of today is only read.
#
# Run it from your OWN PowerShell window (not from inside an app that may restart) and leave the window open:
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\git\pwca_maps\out\_wt-map-symbols-names\tools\add_symbols_and_street_names.ps1
#
# Three steps, each with a marker file when it has finished (<Work>\<step>.done):
#   build     one run over the OpenStreetMap extract: <Work>\pack\<Name>-symbols.pmt and <Name>-streets.pmt
#   check     reads both files back tile by tile and counts the symbols by class and the street names
#   assemble  a NEW, complete card folder <Card>\PANOPTES: today's files copied unchanged (each checked against its
#             sha256), the new files beside them, one MANIFEST.sha256, CURRENT naming the pack <PackId>
# The same command run again carries on at the first step without a marker; a step that was stopped starts again
# from its beginning (its unfinished folder, <Work>\pack.partial or <Card>.partial, is removed first). The script
# never writes into a card folder that exists: it stops and says so.
#
# The window shows a percent line for the step in hand and for all steps, and the same in its title. Every line of a
# step goes to <Work>\<step>.log; start, end and the percent lines also go to <Work>\run.log. A failed step stops the
# run. The steps run at HIGH priority (pass -Normal for normal priority).
#
# The tools run from a snapshot (a detached git worktree of -Commit under <Repo>\out), so the working tree can be
# edited while the build runs. -Commit defaults to the commit this script's own folder is at.
param(
    [string]$Repo = "C:\git\pwca_maps",
    [string]$Commit = "",
    [string]$Extract = "C:\git\pwca_maps\sources\north-america-latest.osm.pbf",
    [string]$BasePack = "C:\temp\panoptes-maps\card-2026-10-06\PANOPTES\MAPS\NA-20260924-F1",
    [string]$Work = "C:\git\pwca_maps\out\na-extras",
    [string]$Card = "C:\temp\panoptes-maps\card-2026-10-06-F2",
    [string]$PackId = "NA-20260924-F2",
    [string]$Name = "north-america",
    [switch]$Normal,
    [switch]$NoRoadFuel
)
# "Continue": git and python write ordinary progress to their error stream, which must not end this script; every
# step's exit code is checked instead.
$ErrorActionPreference = "Continue"
if (-not $Commit) { $Commit = "$(git -C $PSScriptRoot rev-parse HEAD 2>$null)".Trim() }
$short = "$(git -C $Repo rev-parse --short $Commit 2>$null)".Trim()
if (-not $short) {
    Write-Host "No such commit in ${Repo}: $Commit"
    exit 2
}
foreach ($need in @($Extract, (Join-Path $BasePack "MANIFEST.sha256"), (Join-Path $BasePack "SOURCES.json"))) {
    if (-not (Test-Path $need)) {
        Write-Host "Missing: $need"
        exit 2
    }
}
& python -c "import numpy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "python with numpy is needed (the earlier map builds used the same)"
    exit 2
}
$snap = Join-Path $Repo "out\_tools-$short"
if (-not (Test-Path (Join-Path $snap "tools\build_extras.py"))) {
    git -C $Repo worktree add --detach $snap $Commit 2>&1 | Out-Null
}
if (-not (Test-Path (Join-Path $snap "tools\build_extras.py"))) {
    Write-Host "Could not make the snapshot of the tools at $snap (is $Commit the commit with tools\build_extras.py?)"
    exit 2
}
New-Item -ItemType Directory -Force $Work | Out-Null
$runLog = Join-Path $Work "run.log"
$pack = Join-Path $Work "pack"
$packPartial = Join-Path $Work "pack.partial"
$cardPartial = "$Card.partial"
$packDir = Join-Path $cardPartial "PANOPTES\MAPS\$PackId"
$steps = @("build", "check", "assemble")
$share = @{ "build" = 85; "check" = 10; "assemble" = 5 }
$doneShare = 0
function Say([string]$text) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
    Write-Host $line
    Add-Content -Path $runLog -Value $line -Encoding utf8
}
function Run-Step([string]$step, [string[]]$stepArgs) {
    $log = Join-Path $Work "$step.log"
    if (Test-Path $log) { Remove-Item $log }
    & python -u @stepArgs 2>&1 | ForEach-Object {
        $line = "$_"
        Add-Content -Path $log -Value $line -Encoding utf8
        if ($line -match "PROGRESS (\d+)% of this build \((.*)\)") {
            $pct = [int]$Matches[1]
            $overall = [int]($script:doneShare + $share[$step] * $pct / 100)
            Say ("{0}: {1}%   all steps: {2}%   ({3})" -f $step, $pct, $overall, $Matches[2])
            $Host.UI.RawUI.WindowTitle = "Map symbols and street names: all $overall% - $step $pct%"
        } elseif ($line -match "Traceback|Error|error:|PASSED|SYMBOLS|STREET NAMES|^\s+zooms |wrote |copying |assembled |symbols: |street names: ") {
            Say ("{0}: {1}" -f $step, $line)
        }
    }
    return $LASTEXITCODE
}
Say ("START (tools at {0}; extract {1}; today's pack {2}; work folder {3}; new card folder {4}, pack {5})" -f $short, $Extract, $BasePack, $Work, $Card, $PackId)
foreach ($step in $steps) {
    $marker = Join-Path $Work "$step.done"
    if (Test-Path $marker) {
        Say "${step}: finished in an earlier run ($((Get-Content $marker -TotalCount 1))); not repeated"
        $doneShare += $share[$step]
        continue
    }
    Say "${step}: start"
    $code = 0
    if ($step -eq "build") {
        foreach ($old in @($packPartial, $pack)) {
            if (Test-Path $old) { Remove-Item -Recurse -Force $old }
        }
        $a = @((Join-Path $snap "tools\build_extras.py"), $Extract, $packPartial, "--name", $Name)
        if (-not $Normal) { $a += "--high-priority" }
        if (-not $NoRoadFuel) { $a += "--road-fuel" }
        $code = Run-Step $step $a
        if ($code -eq 0) { Rename-Item $packPartial "pack" }
    } elseif ($step -eq "check") {
        $code = Run-Step $step @((Join-Path $snap "tools\check_extras.py"), $pack, "--json", (Join-Path $Work "check.json"))
    } else {
        if (Test-Path $Card) {
            Say "STOPPED: $Card exists. This script never writes into a card folder that exists: pass -Card with a folder that does not exist."
            $Host.UI.RawUI.WindowTitle = "Map symbols and street names: STOPPED (the card folder exists)"
            exit 3
        }
        if (Test-Path $cardPartial) { Remove-Item -Recurse -Force $cardPartial }
        $code = Run-Step $step @((Join-Path $snap "tools\assemble_pack.py"), $packDir, $BasePack, $pack)
        if ($code -eq 0) {
            # CURRENT as the pack of today has it: the pack's name and one line feed.
            [IO.File]::WriteAllText((Join-Path $cardPartial "PANOPTES\MAPS\CURRENT"), "$PackId`n", (New-Object Text.ASCIIEncoding))
            Rename-Item $cardPartial (Split-Path -Leaf $Card)
        }
    }
    Say "${step}: exit $code"
    if ($code -ne 0) {
        Say "STOPPED: the $step step failed. See $(Join-Path $Work "$step.log"). The same command starts this step again."
        $Host.UI.RawUI.WindowTitle = "Map symbols and street names: FAILED at $step"
        exit $code
    }
    Set-Content -Path $marker -Value ("{0} tools {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $short) -Encoding ascii
    $doneShare += $share[$step]
}
$final = Join-Path $Card "PANOPTES\MAPS\$PackId"
$bytes = (Get-ChildItem $final -File | Measure-Object Length -Sum).Sum
Say ("ALL STEPS FINISHED. The new card folder: {0} (copy its folder PANOPTES onto the card's top level). Pack {1}: {2} files, {3:N0} bytes. The counts: {4}" -f $Card, $PackId, (Get-ChildItem $final -File).Count, $bytes, (Join-Path $Work "check.json"))
$Host.UI.RawUI.WindowTitle = "Map symbols and street names: FINISHED"
