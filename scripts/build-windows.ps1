<#
.SYNOPSIS
Build the KiCadGen Windows executable (PyInstaller) and, with -Package msi,
the MSI installer (WiX 3.14).

.DESCRIPTION
Steps, each one stopping the build on failure:
  1. dedicated build environment .venv-build (never the development .venv),
     recreated when its Python is not 3.13, with the exact versions of
     requirements/release-constraints.txt;
  2. initial profile database generated from the code by
     tools/make_seed_db.py (never a copy of data/analysis_profiles.sqlite3,
     which can hold stale defaults and developer metadata) and verified;
  3. license files of the build environment and of CPython collected by
     tools/collect_licenses.py into build\licenses (shipped as
     _internal\licenses);
  4. application icon rendered from kicadgen/gui/assets/app-icon.svg;
  5. PyInstaller one-folder build into dist\KiCadGen;
  6. smoke test: KiCadGen.exe --check-runtime must exit with 0;
  7. (-Package msi) MSI into dist\KiCadGen-<version>.msi.

The caller's current location is restored when the script ends, also on
failure.

Never test the installer by installing it on the development machine:
use a virtual machine or Windows Sandbox.

.PARAMETER Package
app-image: only the executable folder. msi: executable and installer.

.PARAMETER IncludeYoloModel
Ship every models\yolo\*.onnx (the official setup does); off by default so that a
checkout without weights still builds.
#>
param(
    [ValidateSet('app-image', 'msi')]
    [string]$Package = 'msi',
    # Include every models\yolo\*.onnx (never a .pt: the package has no PyTorch).
    # OFF by default so that a checkout without the weights still builds; the
    # official setup is built with it (user, 2026-10-28, after the license
    # review of THIRD_PARTY_NOTICES.md).
    [switch]$IncludeYoloModel
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
# PyInstaller and the tools resolve paths from the repository root; the
# caller's location comes back in the finally block at the end.
Push-Location $Root
try {

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

function Test-BuildPython {
    # An existing .venv-build made with another Python (e.g. before an
    # upgrade) would build a different runtime than the one documented.
    if (-not (Test-Path $python)) { return $false }
    $answer = & $python -c "import sys; print(sys.version_info[:2] == (3, 13))" 2>$null
    return ($answer -eq 'True')
}

if ((Test-Path $venv) -and -not (Test-BuildPython)) {
    Write-Host '.venv-build is not a Python 3.13 environment: recreating it...' -ForegroundColor Yellow
    Remove-Item -Recurse -Force $venv
}
if (-not (Test-Path $python)) {
    $base = Find-BasePython
    Write-Host "Creating the build environment .venv-build with $base..."
    & $base -m venv $venv
    Assert-LastCommand 'Creating .venv-build'
}
Write-Host 'Installing build requirements...'
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $Root 'requirements/build.txt') `
    -c (Join-Path $Root 'requirements/release-constraints.txt')
Assert-LastCommand 'Installing requirements/build.txt'

# -- 2. Initial profile database generated from the code -------------------------
$seed = Join-Path $Root 'build\seed\data\analysis_profiles.sqlite3'
& $python (Join-Path $Root 'tools\make_seed_db.py') $seed
Assert-LastCommand 'Generating the seed profile database (tools\make_seed_db.py)'

# -- 3. License files --------------------------------------------------------------
& $python (Join-Path $Root 'tools\collect_licenses.py') (Join-Path $Root 'build\licenses')
Assert-LastCommand 'Collecting the license files (tools\collect_licenses.py)'

# -- 4. Icon ---------------------------------------------------------------------
& $python (Join-Path $Root 'tools\make_app_icon.py') (Join-Path $Root 'packaging\KiCadGen.ico')
Assert-LastCommand 'Rendering the application icon'

# -- 5. PyInstaller ---------------------------------------------------------------
if ($IncludeYoloModel) {
    # The detectors (models\yolo) and the SMD network (models\smd), ONNX only.
    $weights = @(Get-ChildItem -Path (Join-Path $Root 'models\yolo'), (Join-Path $Root 'models\smd') -Filter '*.onnx' -File -ErrorAction SilentlyContinue)
    if ($weights.Count -eq 0) { throw 'No models\yolo\*.onnx found; export the checkpoints with tools\export_yolo_onnx.py (and tools\export_smd_onnx.py) or drop the switch.' }
    foreach ($file in $weights) {
        Write-Host ("Including the weights {0}\{1} ({2:N1} MB)." -f $file.Directory.Name, $file.Name, ($file.Length / 1MB)) -ForegroundColor Yellow
    }
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

# -- 6. Smoke test ---------------------------------------------------------------
Write-Host 'Checking the executable (--check-runtime)...'
$check = Start-Process -FilePath $exe -ArgumentList '--check-runtime' -Wait -PassThru -NoNewWindow
if ($check.ExitCode -ne 0) { throw "KiCadGen.exe --check-runtime failed (exit $($check.ExitCode))." }
Write-Host "Executable ready: $exe" -ForegroundColor Green

if ($Package -eq 'app-image') { return }

# -- 7. MSI --------------------------------------------------------------
& (Join-Path $PSScriptRoot 'build-msi.ps1') -Version $version
Assert-LastCommand 'Building the MSI'
}
finally {
    Pop-Location
}
