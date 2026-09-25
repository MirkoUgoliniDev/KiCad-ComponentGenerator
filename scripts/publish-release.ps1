<#
.SYNOPSIS
Publish KiCadGen version X.Y.Z: set the version, commit, tag vX.Y.Z and push.

.DESCRIPTION
The push of the tag starts .github/workflows/release.yml, which runs the
tests, builds the MSI on Windows and attaches it to the GitHub Release.
Modelled on employee-scheduling/scripts/publish-release.ps1. By the user's
choice (2026-09-25) the release is made from the CURRENT branch, not only
from main; the branch is printed and must be up to date with GitHub.

.PARAMETER Version
Three numbers, e.g. 1.0.0 (a leading v is accepted).
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version
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
    & git tag -a $tag -m "KiCadGen $tag"
    Assert-LastCommand 'Creating the tag'
    & git push origin $branch
    Assert-LastCommand 'Pushing the branch'
    & git push origin $tag
    Assert-LastCommand 'Pushing the tag'
    Write-Host "Done. GitHub Actions is building the MSI: see the Actions tab, then the $tag Release." -ForegroundColor Green
}
finally {
    Pop-Location
}
