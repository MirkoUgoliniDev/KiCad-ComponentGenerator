<#
.SYNOPSIS
Build the KiCadGen Windows executable (PyInstaller) and, with -Package msi,
the MSI installer (WiX 3.14).

.DESCRIPTION
Steps, each one stopping the build on failure:
  1. dedicated build environment .venv-build (never the development .venv);
  2. initial profile database taken from git HEAD (never the local copy,
     which holds the developer's own profiles and translations);
  3. application icon rendered from kicadgen/gui/assets/app-icon.svg;
  4. PyInstaller one-folder build into dist\KiCadGen;
  5. smoke test: KiCadGen.exe --check-runtime must exit with 0;
  6. (-Package msi) MSI into dist\KiCadGen-<version>.msi.

Never test the installer by installing it on the development machine:
use a virtual machine or Windows Sandbox.

.PARAMETER Package
app-image: only the executable folder. msi: executable and installer.

.PARAMETER IncludeYoloModel
Ship models\pcb_yolov8s.onnx; off by default (license to be clarified).
#>
param(
    [ValidateSet('app-image', 'msi')]
    [string]$Package = 'msi',
    # Include models\pcb_yolov8s.onnx. OFF by default: the license of those
    # weights is ambiguous (MIT model card, AGPL metadata) and they must not be
    # redistributed until it is clarified.
    [switch]$IncludeYoloModel
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

function Assert-LastCommand([string]$description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$description failed with exit code $LASTEXITCODE."
    }
}

function Get-AppVersion {
    $text = Get-Content (Join-Path $Root 'kicadgen\__init__.py') -Raw
    $match = [regex]::Match($text, '__version__\s*=\s*"(\d+\.\d+\.\d+)"')
    if (-not $match.Success) { throw 'Version not found in kicadgen\__init__.py.' }
    return $match.Groups[1].Value
}

$version = Get-AppVersion
Write-Host "KiCadGen $version - build ($Package)" -ForegroundColor Cyan

# -- 1. Build environment -------------------------------------------------------
$venv = Join-Path $Root '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'
function Find-BasePython {
    # Python 3.13, the version the project is developed and tested with.
    # KICADGEN_BUILD_PYTHON wins; then the standard install; then every
    # python.exe on PATH. Each candidate is ASKED its version.
    $candidates = @()
    if ($env:KICADGEN_BUILD_PYTHON) { $candidates += $env:KICADGEN_BUILD_PYTHON }
    $candidates += 'C:\Program Files\Python313\python.exe'
    $candidates += @(Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object Source)
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path $candidate)) { continue }
        $answer = & $candidate -c "import sys; print(sys.version_info[:2] == (3, 13))" 2>$null
        if ($answer -eq 'True') { return $candidate }
    }
    throw 'Python 3.13 not found. Install it or set KICADGEN_BUILD_PYTHON to its python.exe.'
}

if (-not (Test-Path $python)) {
    $base = Find-BasePython
    Write-Host "Creating the build environment .venv-build with $base..."
    & $base -m venv $venv
    Assert-LastCommand 'Creating .venv-build'
}
Write-Host 'Installing build requirements...'
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $Root 'requirements-build.txt')
Assert-LastCommand 'Installing requirements-build.txt'

# -- 2. Initial profile database from git HEAD ---------------------------------
$seed = Join-Path $Root 'build\seed\data'
New-Item -ItemType Directory -Force -Path $seed | Out-Null
# Bytes written by Python: a PowerShell redirection would re-encode the binary.
& $python -c "import subprocess,sys; open(sys.argv[1],'wb').write(subprocess.check_output(['git','show','HEAD:data/analysis_profiles.sqlite3']))" (Join-Path $seed 'analysis_profiles.sqlite3')
Assert-LastCommand 'Extracting data/analysis_profiles.sqlite3 from git HEAD'

# -- 3. Icon ---------------------------------------------------------------------
& $python (Join-Path $Root 'tools\make_app_icon.py') (Join-Path $Root 'packaging\KiCadGen.ico')
Assert-LastCommand 'Rendering the application icon'

# -- 4. PyInstaller ---------------------------------------------------------------
if ($IncludeYoloModel) {
    if (-not (Test-Path (Join-Path $Root 'models\pcb_yolov8s.onnx'))) { throw 'models\pcb_yolov8s.onnx not found.' }
    Write-Host 'Including the YOLO weights (license must allow redistribution).' -ForegroundColor Yellow
    $env:KICADGEN_BUNDLE_YOLO_MODEL = '1'
} else {
    $env:KICADGEN_BUNDLE_YOLO_MODEL = '0'
}
& $python -m PyInstaller --noconfirm --clean --log-level WARN `
    --distpath (Join-Path $Root 'dist') --workpath (Join-Path $Root 'build\pyinstaller') `
    (Join-Path $Root 'packaging\KiCadGen.spec')
Assert-LastCommand 'PyInstaller build'
$exe = Join-Path $Root 'dist\KiCadGen\KiCadGen.exe'
if (-not (Test-Path $exe)) { throw "Executable not produced: $exe" }

# -- 5. Smoke test ---------------------------------------------------------------
Write-Host 'Checking the executable (--check-runtime)...'
$check = Start-Process -FilePath $exe -ArgumentList '--check-runtime' -Wait -PassThru -NoNewWindow
if ($check.ExitCode -ne 0) { throw "KiCadGen.exe --check-runtime failed (exit $($check.ExitCode))." }
Write-Host "Executable ready: $exe" -ForegroundColor Green

if ($Package -eq 'app-image') { return }

# -- 6. MSI (phase 3) --------------------------------------------------------------
& (Join-Path $PSScriptRoot 'build-msi.ps1') -Version $version
Assert-LastCommand 'Building the MSI'
