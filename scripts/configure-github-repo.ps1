<#
.SYNOPSIS
Check, and with -Apply enforce, the security settings of the KiCadGen GitHub repository.

.DESCRIPTION
Without parameters the script only READS the repository settings and prints,
for every setting, the current value, the wanted value and whether it is
already right. Nothing is changed.

With -Apply it changes only the settings that differ, then reads everything
back from GitHub and prints the final table. It can be run any number of
times: settings already right are left alone.

What it enforces:
  - branches main and ADVANCED: no force push, no deletion (direct pushes by
    the owner stay allowed, so the usual merge-and-push workflow is unchanged);
  - release tags v*: cannot be moved or deleted once pushed;
  - secret scanning with push protection: a commit containing an API key is
    refused by GitHub;
  - Dependabot alerts and security updates;
  - private vulnerability reporting;
  - CodeQL code scanning (default setup);
  - GitHub Actions: only GitHub-owned actions, read-only token, Actions can
    not approve pull requests, approval needed before the workflows of ANY
    external contributor run;
  - Projects tab off (unused). Issues stay on: «Report a problem» uses them.

It needs the GitHub CLI (gh) logged in as the repository owner, with the
'repo' scope. It never touches code, branches content, releases or secrets.

.PARAMETER Apply
Change the settings that differ. Without it the script only reports.

.PARAMETER Repo
owner/name of the repository. Default: read from the 'origin' remote.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\scripts\configure-github-repo.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\configure-github-repo.ps1 -Apply
#>
param(
    [switch]$Apply,
    [string]$Repo = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent

#: Branches protected against force push and deletion.
$ProtectedBranches = @('main', 'ADVANCED')
#: Ruleset names: the script finds its own rulesets by these names.
$BranchRulesetName = 'kicadgen-protect-branches'
$TagRulesetName = 'kicadgen-protect-release-tags'

# -- GitHub CLI wrapper ---------------------------------------------------------

function Invoke-GhApi {
    <#
    .SYNOPSIS
    Call the GitHub REST API through gh; return @{ Ok; NotFound; Data; Error }.
    .NOTES
    Never throws for an HTTP error: callers decide. Native stderr must not
    become a terminating error under 'Stop' (Windows PowerShell 5.1 turns it
    into NativeCommandError), hence the local 'Continue'.
    The body goes through a UTF-8 file without BOM: piped into a native
    program, Windows PowerShell 5.1 re-encodes the string and GitHub answers
    «Problems parsing JSON» (seen on the first -Apply run, 2026-10-27).
    #>
    param([string]$Method, [string]$Path, $Body = $null)
    $ErrorActionPreference = 'Continue'
    $errFile = [System.IO.Path]::GetTempFileName()
    $bodyFile = $null
    try {
        $ghArgs = @('api', '-X', $Method, '-H', 'Accept: application/vnd.github+json', $Path)
        if ($null -ne $Body) {
            $bodyFile = [System.IO.Path]::GetTempFileName()
            $json = $Body | ConvertTo-Json -Depth 20 -Compress
            [System.IO.File]::WriteAllText($bodyFile, $json, (New-Object System.Text.UTF8Encoding($false)))
            $ghArgs += @('--input', $bodyFile)
        }
        $out = & gh @ghArgs 2> $errFile
        $code = $LASTEXITCODE
        # Keep gh's own lines, not the PowerShell error-record decoration around them.
        $err = (@(Get-Content -ErrorAction SilentlyContinue $errFile) |
                Where-Object { $_ -match '^\s*(gh: |\{|This API|To request)' }) -join ' '
    } finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $errFile
        if ($bodyFile) { Remove-Item -Force -ErrorAction SilentlyContinue $bodyFile }
    }
    $text = ($out | Out-String).Trim()
    $data = $null
    if ($text) {
        try { $data = $text | ConvertFrom-Json } catch { $data = $text }
    }
    return @{
        Ok       = ($code -eq 0)
        NotFound = ($code -ne 0 -and "$err$text" -match 'HTTP 404|Not Found')
        Data     = $data
        Error    = (("$err" + ' ' + "$text").Trim())
    }
}

function Assert-GhReady {
    <# Stop early, before any change, when gh is missing or not logged in. #>
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'GitHub CLI (gh) not found. Install it from https://cli.github.com and run: gh auth login'
    }
    $ErrorActionPreference = 'Continue'
    & gh auth status 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'gh is not logged in. Run: gh auth login' }
}

#: Token scopes the changes need: 'repo' for the settings, 'admin:repo_hook'
#: for the CodeQL default setup (GitHub refuses it otherwise).
$RequiredScopes = @('repo', 'admin:repo_hook')

function Get-MissingScopes {
    <# Scopes of $RequiredScopes that the gh token lacks, read from 'gh auth status'. #>
    $ErrorActionPreference = 'Continue'
    $status = (& gh auth status 2>&1 | Out-String)
    $line = ($status -split "`n" | Where-Object { $_ -match 'Token scopes' } | Select-Object -First 1)
    $have = @([regex]::Matches("$line", "'([^']+)'") | ForEach-Object { $_.Groups[1].Value })
    return @($RequiredScopes | Where-Object { $have -notcontains $_ })
}

function Resolve-Repo {
    <# owner/name from -Repo or from the origin remote URL. #>
    param([string]$Given)
    if ($Given) { return $Given }
    $url = (& git -C $Root remote get-url origin).Trim()
    if ($url -match 'github\.com[:/](?<r>[^/]+/[^/]+?)(\.git)?$') { return $Matches['r'] }
    throw "Cannot read owner/name from the origin remote '$url'. Pass -Repo owner/name."
}

# -- desired rulesets ------------------------------------------------------------

function New-BranchRuleset {
    @{
        name          = $BranchRulesetName
        target        = 'branch'
        enforcement   = 'active'
        bypass_actors = @()
        conditions    = @{ ref_name = @{ include = @($ProtectedBranches | ForEach-Object { "refs/heads/$_" }); exclude = @() } }
        rules         = @(@{ type = 'deletion' }, @{ type = 'non_fast_forward' })
    }
}

function New-TagRuleset {
    @{
        name          = $TagRulesetName
        target        = 'tag'
        enforcement   = 'active'
        bypass_actors = @()
        conditions    = @{ ref_name = @{ include = @('refs/tags/v*'); exclude = @() } }
        rules         = @(@{ type = 'deletion' }, @{ type = 'update' })
    }
}

function Get-RulesetState {
    <#
    .SYNOPSIS
    Find a ruleset by name; return @{ Id; Matches; Summary }.
    .NOTES
    Matches means: active, no bypass, the same refs and at least the wanted rules.
    #>
    param([string]$Repo, [hashtable]$Wanted)
    $list = Invoke-GhApi GET "repos/$Repo/rulesets?per_page=100"
    if (-not $list.Ok) { return @{ Id = $null; Matches = $false; Summary = "unreadable: $($list.Error)" } }
    $found = @($list.Data) | Where-Object { $_.name -eq $Wanted.name } | Select-Object -First 1
    if (-not $found) { return @{ Id = $null; Matches = $false; Summary = 'missing' } }
    $full = (Invoke-GhApi GET "repos/$Repo/rulesets/$($found.id)").Data
    $rules = @($full.rules | ForEach-Object { $_.type }) | Sort-Object
    $refs = @($full.conditions.ref_name.include) | Sort-Object
    $wantRules = @($Wanted.rules | ForEach-Object { $_.type }) | Sort-Object
    $wantRefs = @($Wanted.conditions.ref_name.include) | Sort-Object
    $missingRules = @($wantRules | Where-Object { $rules -notcontains $_ })
    $ok = ($full.enforcement -eq 'active') -and (@($full.bypass_actors).Count -eq 0) -and
          ($missingRules.Count -eq 0) -and (($refs -join ',') -eq ($wantRefs -join ','))
    $summary = "$($full.enforcement); refs $($refs -join ', '); rules $($rules -join ', '); bypass $(@($full.bypass_actors).Count)"
    return @{ Id = $found.id; Matches = $ok; Summary = $summary }
}

# -- the checks ------------------------------------------------------------------

function Get-Checks {
    <#
    .SYNOPSIS
    Read every setting; return a list of checks with their current state and fix.
    .NOTES
    Each check: Name, Current (text), Wanted (text), Ok (bool or $null when the
    state cannot be read), Fix (scriptblock run by -Apply).
    #>
    param([string]$Repo)
    $checks = New-Object System.Collections.Generic.List[object]

    $repoInfo = (Invoke-GhApi GET "repos/$Repo").Data
    $sa = $repoInfo.security_and_analysis

    # Branch and tag rulesets.
    $branchWanted = New-BranchRuleset
    $b = Get-RulesetState $Repo $branchWanted
    $checks.Add([pscustomobject]@{
        Name = "Branches $($ProtectedBranches -join ', '): no force push / deletion"
        Current = $b.Summary; Wanted = 'active, no bypass'; Ok = $b.Matches
        Fix = { param($r) $s = Get-RulesetState $r (New-BranchRuleset)
                if ($s.Id) { Invoke-GhApi PUT "repos/$r/rulesets/$($s.Id)" (New-BranchRuleset) }
                else { Invoke-GhApi POST "repos/$r/rulesets" (New-BranchRuleset) } }
    })
    $tagWanted = New-TagRuleset
    $t = Get-RulesetState $Repo $tagWanted
    $checks.Add([pscustomobject]@{
        Name = 'Release tags v*: cannot be moved or deleted'
        Current = $t.Summary; Wanted = 'active, no bypass'; Ok = $t.Matches
        Fix = { param($r) $s = Get-RulesetState $r (New-TagRuleset)
                if ($s.Id) { Invoke-GhApi PUT "repos/$r/rulesets/$($s.Id)" (New-TagRuleset) }
                else { Invoke-GhApi POST "repos/$r/rulesets" (New-TagRuleset) } }
    })

    # Dependabot alerts first: security updates need them.
    $alerts = Invoke-GhApi GET "repos/$Repo/vulnerability-alerts"
    $checks.Add([pscustomobject]@{
        Name = 'Dependabot alerts'
        Current = $(if ($alerts.Ok) { 'enabled' } elseif ($alerts.NotFound) { 'disabled' } else { 'unreadable' })
        Wanted = 'enabled'; Ok = $(if ($alerts.Ok) { $true } elseif ($alerts.NotFound) { $false } else { $null })
        Fix = { param($r) Invoke-GhApi PUT "repos/$r/vulnerability-alerts" }
    })
    foreach ($item in @(
            @{ Key = 'secret_scanning'; Name = 'Secret scanning' },
            @{ Key = 'secret_scanning_push_protection'; Name = 'Push protection (refuse commits with secrets)' },
            @{ Key = 'dependabot_security_updates'; Name = 'Dependabot security updates' })) {
        $status = $sa.($item.Key).status
        $key = $item.Key
        $checks.Add([pscustomobject]@{
            Name = $item.Name
            Current = $(if ($status) { $status } else { 'unknown' })
            Wanted = 'enabled'; Ok = $(if ($status) { $status -eq 'enabled' } else { $null })
            Fix = [scriptblock]::Create("param(`$r) Invoke-GhApi PATCH `"repos/`$r`" @{ security_and_analysis = @{ $key = @{ status = 'enabled' } } }")
        })
    }

    $pvr = Invoke-GhApi GET "repos/$Repo/private-vulnerability-reporting"
    $pvrOn = $(if ($pvr.Ok) { [bool]$pvr.Data.enabled } else { $null })
    $checks.Add([pscustomobject]@{
        Name = 'Private vulnerability reporting'
        Current = $(if ($null -eq $pvrOn) { 'unreadable' } elseif ($pvrOn) { 'enabled' } else { 'disabled' })
        Wanted = 'enabled'; Ok = $pvrOn
        Fix = { param($r) Invoke-GhApi PUT "repos/$r/private-vulnerability-reporting" }
    })

    $cs = Invoke-GhApi GET "repos/$Repo/code-scanning/default-setup"
    $csState = $(if ($cs.Ok) { $cs.Data.state } else { $null })
    $checks.Add([pscustomobject]@{
        Name = 'CodeQL code scanning (default setup)'
        Current = $(if ($csState) { $csState } else { 'unreadable' })
        Wanted = 'configured'; Ok = $(if ($csState) { $csState -eq 'configured' } else { $null })
        Fix = { param($r) Invoke-GhApi PATCH "repos/$r/code-scanning/default-setup" @{ state = 'configured'; query_suite = 'default' } }
    })

    # GitHub Actions.
    $perm = (Invoke-GhApi GET "repos/$Repo/actions/permissions").Data
    $sel = $null
    if ($perm.allowed_actions -eq 'selected') { $sel = (Invoke-GhApi GET "repos/$Repo/actions/permissions/selected-actions").Data }
    $actionsOk = ($perm.enabled -eq $true) -and ($perm.allowed_actions -eq 'selected') -and
                 ($null -ne $sel) -and ($sel.github_owned_allowed -eq $true) -and ($sel.verified_allowed -eq $false) -and
                 (@($sel.patterns_allowed).Count -eq 0)
    $checks.Add([pscustomobject]@{
        Name = 'Actions: only GitHub-owned actions'
        Current = $(if ($sel) { "selected (github_owned=$($sel.github_owned_allowed), verified=$($sel.verified_allowed), patterns=$(@($sel.patterns_allowed).Count))" } else { "$($perm.allowed_actions)" })
        Wanted = 'selected, GitHub-owned only'; Ok = $actionsOk
        Fix = { param($r)
                Invoke-GhApi PUT "repos/$r/actions/permissions" @{ enabled = $true; allowed_actions = 'selected' }
                Invoke-GhApi PUT "repos/$r/actions/permissions/selected-actions" @{ github_owned_allowed = $true; verified_allowed = $false; patterns_allowed = @() } }
    })
    $wf = (Invoke-GhApi GET "repos/$Repo/actions/permissions/workflow").Data
    $checks.Add([pscustomobject]@{
        Name = 'Actions token: read-only, cannot approve PRs'
        Current = "$($wf.default_workflow_permissions), approve=$($wf.can_approve_pull_request_reviews)"
        Wanted = 'read, approve=False'
        Ok = ($wf.default_workflow_permissions -eq 'read') -and ($wf.can_approve_pull_request_reviews -eq $false)
        Fix = { param($r) Invoke-GhApi PUT "repos/$r/actions/permissions/workflow" @{ default_workflow_permissions = 'read'; can_approve_pull_request_reviews = $false } }
    })
    $fork = Invoke-GhApi GET "repos/$Repo/actions/permissions/fork-pr-contributor-approval"
    $policy = $(if ($fork.Ok) { $fork.Data.approval_policy } else { $null })
    $checks.Add([pscustomobject]@{
        Name = 'Approval before workflows of external contributors run'
        Current = $(if ($policy) { $policy } else { 'unreadable' })
        Wanted = 'all_external_contributors'; Ok = $(if ($policy) { $policy -eq 'all_external_contributors' } else { $null })
        Fix = { param($r) Invoke-GhApi PUT "repos/$r/actions/permissions/fork-pr-contributor-approval" @{ approval_policy = 'all_external_contributors' } }
    })

    # Unused features.
    $checks.Add([pscustomobject]@{
        Name = 'Projects tab (unused)'
        Current = $(if ($repoInfo.has_projects) { 'on' } else { 'off' })
        Wanted = 'off'; Ok = (-not $repoInfo.has_projects)
        Fix = { param($r) Invoke-GhApi PATCH "repos/$r" @{ has_projects = $false } }
    })

    return $checks
}

function Show-Checks {
    param($Checks, [string]$Title)
    Write-Host ''
    Write-Host $Title -ForegroundColor Cyan
    foreach ($c in $Checks) {
        if ($c.Ok -eq $true) { $mark = '[ OK ]'; $colour = 'Green' }
        elseif ($null -eq $c.Ok) { $mark = '[ ?? ]'; $colour = 'Yellow' }
        else { $mark = '[FIX ]'; $colour = 'Red' }
        Write-Host ("{0} {1}" -f $mark, $c.Name) -ForegroundColor $colour
        Write-Host ("        now: {0}" -f $c.Current)
        if ($c.Ok -ne $true) { Write-Host ("       want: {0}" -f $c.Wanted) }
    }
}

# -- main ------------------------------------------------------------------------

Assert-GhReady
$Repo = Resolve-Repo $Repo
$info = Invoke-GhApi GET "repos/$Repo"
if (-not $info.Ok) { throw "Cannot read repos/$Repo : $($info.Error)" }
if (-not $info.Data.permissions.admin) { throw "The logged-in gh account is not an admin of $Repo." }
Write-Host "Repository: $Repo ($($info.Data.visibility))"

$checks = Get-Checks $Repo
Show-Checks $checks 'Current settings'
$todo = @($checks | Where-Object { $_.Ok -ne $true })

if (-not $Apply) {
    Write-Host ''
    if ($todo.Count -eq 0) { Write-Host 'Everything is already as wanted.' -ForegroundColor Green }
    else { Write-Host "$($todo.Count) setting(s) to change. Run again with -Apply to change them." -ForegroundColor Yellow }
    exit 0
}

$missing = @(Get-MissingScopes)
if ($missing.Count -gt 0) {
    Write-Host ''
    Write-Host "The gh login lacks the scope(s): $($missing -join ', '). Nothing was changed." -ForegroundColor Red
    Write-Host 'Grant them (a browser page opens), then run this script again with -Apply:' -ForegroundColor Yellow
    Write-Host "  gh auth refresh -h github.com -s $($missing -join ',')"
    exit 1
}

$failures = @()
foreach ($c in $todo) {
    Write-Host "Applying: $($c.Name)"
    foreach ($result in @(& $c.Fix $Repo)) {
        if ($result -is [hashtable] -and -not $result.Ok) {
            $failures += "$($c.Name): $($result.Error)"
            Write-Host "  failed: $($result.Error)" -ForegroundColor Red
        }
    }
}

$after = Get-Checks $Repo
Show-Checks $after 'Settings after -Apply (read back from GitHub)'
$left = @($after | Where-Object { $_.Ok -ne $true })

Write-Host ''
Write-Host 'Not handled by this script, check by hand:' -ForegroundColor Cyan
Write-Host '  - two-factor authentication on your GitHub account (Settings > Password and authentication);'
Write-Host '  - repository variable WIX314_SHA256 for the release build (official hash of wix314-binaries.zip).'

if ($failures.Count -gt 0 -or $left.Count -gt 0) {
    Write-Host ''
    Write-Host "$($left.Count) setting(s) still not as wanted." -ForegroundColor Red
    exit 1
}
Write-Host ''
Write-Host 'All settings are as wanted.' -ForegroundColor Green
exit 0
