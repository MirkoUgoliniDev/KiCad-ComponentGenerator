<#
.SYNOPSIS
Publish KiCadGen version X.Y.Z: set the version, commit, tag vX.Y.Z and push.

.DESCRIPTION
The push of the tag starts .github/workflows/release.yml, which runs the
tests, builds the MSI on Windows and attaches it to the GitHub Release.
Modelled on employee-scheduling/scripts/publish-release.ps1. By the user's
choice (2026-09-25) the release is made from the CURRENT branch, not only
from main; the branch is printed and must be up to date with GitHub.

A tag v* is permanent (repository ruleset): the script refuses to create it
until the test suite is proven green for the commit being released (review
2026-10-27, I4). By default the proof is the latest GitHub «Tests» workflow
run of the current HEAD, read with the gh CLI; -RunTestsLocally runs the
whole suite here instead (tools/run_test_halves.py both, several minutes);
-SkipTests publishes without any proof and prints a red warning, for the
one case where the user has decided so knowingly.

.PARAMETER Version
Three numbers, e.g. 1.0.0 (a leading v is accepted).

.PARAMETER RunTestsLocally
Run the full suite in this checkout before tagging, instead of asking GitHub
for the result of the «Tests» workflow.

.PARAMETER SkipTests
Tag without any test proof. Must be typed explicitly; prints a red warning.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [switch]$RunTestsLocally,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$number = $Version.Trim()
if ($number.StartsWith('v', [System.StringComparison]::OrdinalIgnoreCase)) { $number = $number.Substring(1) }
if ($number -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid version '$Version'. Use three numbers, e.g. 1.2.3." }
$tag = "v$number"

function Assert-LastCommand([string]$description) {
    if ($LASTEXITCODE -ne 0) { throw "$description failed with exit code $LASTEXITCODE." }
}

function Assert-TestsGreenOnGitHub([string]$sha) {
    # The «Tests» workflow runs on every branch push (not on tag pushes:
    # review 2026-10-29, K2, the release workflow tests the tag itself), so
    # the commit must have been pushed on a branch and a run for it must have
    # completed successfully. No run at all means the commit was never pushed
    # (or the run has not started yet): refuse, never guess.
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'The gh CLI is not installed: install it, or run with -RunTestsLocally.'
    }
    # The same commit may have several runs: one per branch that carries it
    # (main and the work branch), and a duplicate gets cancelled by the
    # concurrency group when the branch moves on (2026-10-28, v1.0.2: the
    # main run was green, the ADVANCED copy cancelled, and the newest run was
    # the cancelled one). A green run of this exact commit is the evidence,
    # whichever branch ran it; a cancelled duplicate is not a verdict.
    $json = & gh run list --commit $sha --workflow Tests --json status,conclusion,url --limit 10
    Assert-LastCommand 'Reading the Tests workflow runs from GitHub'
    $runs = @($json | ConvertFrom-Json)
    if ($runs.Count -eq 0) {
        throw "No «Tests» run exists on GitHub for commit $sha. Push the branch, wait for the run, then publish."
    }
    $green = @($runs | Where-Object { $_.status -eq 'completed' -and $_.conclusion -eq 'success' })
    if ($green.Count -gt 0) {
        # Printed before the return: after the final throw it was unreachable
        # and named an undefined variable (review 2026-10-29, K6).
        Write-Host "Tests green on GitHub for $sha ($($green[0].url))." -ForegroundColor Green
        return
    }
    $failed = @($runs | Where-Object { $_.status -eq 'completed' -and $_.conclusion -eq 'failure' })
    if ($failed.Count -gt 0) {
        throw "The «Tests» run for commit $sha failed: fix the suite before tagging ($($failed[0].url))."
    }
    $pending = @($runs | Where-Object { $_.status -ne 'completed' })
    if ($pending.Count -gt 0) {
        throw "The «Tests» run for commit $sha is still $($pending[0].status): wait for it ($($pending[0].url))."
    }
    throw "No successful «Tests» run for commit $sha (latest: '$($runs[0].conclusion)'): re-run it on GitHub, then publish ($($runs[0].url))."
}

function Invoke-TestsLocally() {
    # The same two-process run as CI (tools/run_test_halves.py both), with
    # the project's own interpreter; a failing half stops the publication.
    $python = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { throw "Project interpreter not found: $python" }
    Write-Host 'Running the full test suite locally (two halves)...' -ForegroundColor Cyan
    & $python (Join-Path $Root 'tools\run_test_halves.py') both
    Assert-LastCommand 'The local test suite'
    Write-Host 'Tests green locally.' -ForegroundColor Green
}

Push-Location $Root
try {
    $branch = (& git branch --show-current).Trim()
    Assert-LastCommand 'Reading the current branch'
    if (-not $branch) { throw 'Detached HEAD: check out a branch first.' }

    $changes = @(& git status --porcelain --untracked-files=no)
    Assert-LastCommand 'Checking the working tree'
    if ($changes.Count -gt 0) { throw 'Tracked files have changes. Commit or discard them first.' }

    & git fetch origin $branch --tags
    Assert-LastCommand 'Fetching from GitHub'
    $behind = [int]((& git rev-list --count "HEAD..origin/$branch").Trim())
    if ($behind -gt 0) { throw "Branch $branch is behind GitHub: pull first." }

    if ((& git tag --list $tag).Count -gt 0) { throw "Tag $tag already exists locally." }
    & git ls-remote --exit-code --tags origin "refs/tags/$tag" | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "Tag $tag already exists on GitHub." }
    if ($LASTEXITCODE -ne 2) { throw 'Unable to check the tag on GitHub.' }

    # Test gate, on the code as it is NOW: the version commit below changes
    # only the version string, so the proof for the current HEAD covers it.
    $head = (& git rev-parse HEAD).Trim()
    Assert-LastCommand 'Reading HEAD'
    if ($SkipTests) {
        Write-Host "WARNING: -SkipTests given. Tagging $tag WITHOUT any proof that the test suite passes; the tag is permanent." -ForegroundColor Red
    }
    elseif ($RunTestsLocally) {
        Invoke-TestsLocally
    }
    else {
        Assert-TestsGreenOnGitHub $head
    }

    $init = Join-Path $Root 'kicadgen\__init__.py'
    $text = [System.IO.File]::ReadAllText($init)
    $pattern = '__version__\s*=\s*"[^"]*"'
    if ([regex]::Matches($text, $pattern).Count -ne 1) { throw 'Version line not found in kicadgen\__init__.py.' }
    $updated = [regex]::Replace($text, $pattern, "__version__ = `"$number`"")
    if ($updated -ne $text) {
        [System.IO.File]::WriteAllText($init, $updated, (New-Object System.Text.UTF8Encoding($false)))
        & git add $init
        Assert-LastCommand 'Staging the version'
        & git commit -m "release: $tag"
        Assert-LastCommand 'Committing the version'
    }

    Write-Host "Publishing KiCadGen $tag from branch $branch..." -ForegroundColor Cyan
    # The branch goes first: when its push is refused (branch moved on
    # GitHub, no rights) no local tag is left behind to block the next
    # attempt (review 2026-10-29, K7). The tag is created only afterwards.
    & git push origin $branch
    Assert-LastCommand 'Pushing the branch'
    & git tag -a $tag -m "KiCadGen $tag"
    Assert-LastCommand 'Creating the tag'
    & git push origin $tag
    Assert-LastCommand 'Pushing the tag'
    Write-Host "Done. GitHub Actions is building the MSI: see the Actions tab, then the $tag Release." -ForegroundColor Green
}
finally {
    Pop-Location
}
