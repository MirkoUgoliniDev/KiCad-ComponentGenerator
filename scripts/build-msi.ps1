<#
.SYNOPSIS
Build dist\KiCadGen-<version>.msi from dist\KiCadGen with WiX 3.14.

.DESCRIPTION
Called by build-windows.ps1 after the executable has been built and checked.
heat harvests dist\KiCadGen into the AppFiles component group, candle and
light compile packaging\KiCadGen.wxs with the WixUI and WixUtil extensions.
WiX is looked for in C:\tools\wix314 and in Program Files; when missing, the
official 3.14 binaries are downloaded into C:\tools\wix314 (as in the
employee-scheduling project) and used ONLY if their SHA-256 matches the value
given by the maintainer (-WixSha256 or KICADGEN_WIX_SHA256). The hash is not
written in this script because it has not been verified against the release
page; without it the download is refused (fail closed).

light suppresses ICE61 on purpose: MajorUpgrade allows same-version upgrades
(a rebuilt MSI of the same version replaces the installed one), which ICE61
reports as a warning.

.PARAMETER Version
Program version, three numbers.

.PARAMETER WixSha256
Expected SHA-256 of wix314-binaries.zip, copied by the maintainer from the
official WiX 3.14.1 release page. Needed only when WiX must be downloaded.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$WixSha256 = $env:KICADGEN_WIX_SHA256
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$AppDir = Join-Path $Root 'dist\KiCadGen'
$Work = Join-Path $Root 'build\msi'
$Output = Join-Path $Root "dist\KiCadGen-$Version.msi"

function Assert-LastCommand([string]$description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$description failed with exit code $LASTEXITCODE."
    }
}

function Find-Wix {
    $candidates = @()
    if ($env:WIX) { $candidates += $env:WIX; $candidates += (Join-Path $env:WIX 'bin') }
    $candidates += 'C:\tools\wix314'
    $candidates += 'C:\Program Files (x86)\WiX Toolset v3.14\bin'
    $candidates += 'C:\Program Files\WiX Toolset v3.14\bin'
    foreach ($candidate in $candidates) {
        if (Test-Path (Join-Path $candidate 'candle.exe')) { return $candidate }
    }
    $expected = "$WixSha256".Trim().ToUpperInvariant()
    if ($expected -notmatch '^[0-9A-F]{64}$') {
        throw ('WiX 3.14 not found and no valid SHA-256 to verify a download. Install WiX 3.14, ' +
               'or pass -WixSha256 (or set KICADGEN_WIX_SHA256) with the hash of wix314-binaries.zip ' +
               'published on https://github.com/wixtoolset/wix3/releases/tag/wix3141rtm.')
    }
    Write-Host 'WiX 3.14 not found: downloading the official binaries (39 MB)...'
    $target = 'C:\tools\wix314'
    $zip = Join-Path $env:TEMP 'wix314-binaries.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip' -OutFile $zip
    $actual = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToUpperInvariant()
    if ($actual -ne $expected) {
        Remove-Item -Force $zip
        throw "WiX download rejected: SHA-256 $actual differs from the expected $expected."
    }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Expand-Archive -Path $zip -DestinationPath $target -Force
    if (-not (Test-Path (Join-Path $target 'candle.exe'))) { throw 'WiX download did not produce candle.exe.' }
    return $target
}

if (-not (Test-Path (Join-Path $AppDir 'KiCadGen.exe'))) {
    throw "Build the executable first: $AppDir\KiCadGen.exe not found."
}
$wix = Find-Wix
Write-Host "WiX: $wix"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
New-Item -ItemType Directory -Force -Path $Work | Out-Null

$python = Join-Path $Root '.venv-build\Scripts\python.exe'
& $python (Join-Path $Root 'tools\make_installer_assets.py') $Work
Assert-LastCommand 'Generating the installer images and license'

# heat: -ag stable component GUIDs computed at link time; -srd no root
# directory element; -sreg/-scom no registry or COM harvesting.
& (Join-Path $wix 'heat.exe') dir $AppDir -nologo -ag -srd -sfrag -sreg -scom `
    -cg AppFiles -dr INSTALLFOLDER -var var.SourceDir -out (Join-Path $Work 'AppFiles.wxs')
Assert-LastCommand 'heat (harvesting dist\KiCadGen)'

& (Join-Path $wix 'candle.exe') -nologo -arch x64 `
    "-dVersion=$Version" "-dSourceDir=$AppDir" `
    "-dIconFile=$(Join-Path $Root 'packaging\KiCadGen.ico')" `
    "-dLicenseRtf=$(Join-Path $Work 'license.rtf')" `
    "-dBannerBmp=$(Join-Path $Work 'banner.bmp')" `
    "-dDialogBmp=$(Join-Path $Work 'dialog.bmp')" `
    -ext WixUIExtension -ext WixUtilExtension -out "$Work\" `
    (Join-Path $Root 'packaging\KiCadGen.wxs') (Join-Path $Work 'AppFiles.wxs')
Assert-LastCommand 'candle'

& (Join-Path $wix 'light.exe') -nologo -ext WixUIExtension -ext WixUtilExtension `
    -cultures:en-us -spdb -sice:ICE61 -out $Output (Join-Path $Work 'KiCadGen.wixobj') (Join-Path $Work 'AppFiles.wixobj')
Assert-LastCommand 'light'

$size = [math]::Round((Get-Item $Output).Length / 1MB, 1)
Write-Host "MSI ready: $Output ($size MB)" -ForegroundColor Green
Write-Host 'Test it in a virtual machine or Windows Sandbox, never on the development machine.' -ForegroundColor Yellow
