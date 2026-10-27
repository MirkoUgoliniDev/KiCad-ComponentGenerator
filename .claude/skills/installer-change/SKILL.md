---
name: installer-change
description: Change anything about the Windows executable or the MSI — kicadgen/app_paths.py, packaging/ (PyInstaller spec, WiX source, icon), scripts/build-*.ps1, scripts/publish-release.ps1, tools/make_*.py or .github/workflows/release.yml. Use it because this area is not exercised by a normal run: it carries its own verification, and skipping it means the error surfaces on a user's machine.
---

# Changing the Windows executable or the installer

`python main.py` never runs through PyInstaller or WiX. A resource missing
from the spec, a path written under Program Files or a broken `.ps1` passes
every normal check and fails only once installed.

## 1. Know which file owns the change

```
kicadgen/app_paths.py           where the program reads and writes (sources vs installed)
packaging/KiCadGen.spec         PyInstaller: resources, hidden imports, version info
packaging/KiCadGen.wxs          WiX: product, UpgradeCode, shortcuts, UI, FreeCAD/KiCad search
scripts/build-windows.ps1       build env (+ constraints), seed DB, licenses, icon, PyInstaller, smoke test
scripts/build-msi.ps1           heat / candle / light; WiX download only with a verified SHA-256
scripts/publish-release.ps1     version, commit, tag, push
tools/make_seed_db.py           seed profile DB generated from the code and verified
tools/collect_licenses.py       license files of the build env + CPython -> _internal\licenses
tools/make_app_icon.py          SVG -> .ico
tools/make_installer_assets.py  banner, dialog image, license RTF
requirements/release-constraints.txt  exact versions of release builds (pip -c)
.github/workflows/release.yml   tag -> build job (read-only) -> publish job (write) -> Release
docs/PACKAGING-WINDOWS-MSI.md   and docs/INSTALLATION-WINDOWS.md
```

## 2. Respect the invariants

- **Never write next to the program.** Every writable path goes through
  `app_paths` (`user_data_dir`, `data_file`, `default_projects_dir`); from the
  sources they return the repository paths, unchanged.
- **Read-only resources keep the package layout** in the spec (the code finds
  `kicadgen/gui/assets` and the FreeCAD scripts relative to its modules). The
  `kicadgen/cad3d/*.py` files must stay REAL files: freecadcmd runs them.
- **The UpgradeCode never changes** (`tests/test_packaging.py` guards it).
- **The seed profile database is generated from the code** by
  `tools/make_seed_db.py` and verified (defaults equal `factory_defaults()`,
  no local path in its metadata): never the local copy, never the tracked one.
- **Every bundled component's license ships** in `_internal\licenses`
  (`tools/collect_licenses.py`); a new bundled library needs its entry in
  `THIRD_PARTY_NOTICES.md` too.
- **The release workflow keeps write access out of the build job**; the tag
  goes through the `TAG` environment variable, never `${{ }}` in a script.
- **The YOLO weights are opt-in** (`-IncludeYoloModel`) until their license
  is clarified.
- **Uninstall never removes user data** (`%LOCALAPPDATA%\KiCadGen`, `Documents\KiCadGen`).

## 3. Verify

```
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_packaging.py tests/test_app_paths.py
.\scripts\build-windows.ps1 -Package app-image     # includes KiCadGen.exe --check-runtime
.\scripts\build-windows.ps1                        # + MSI
```

For PowerShell, parse without executing (also done by test_packaging):

```
powershell -NoProfile -Command "[void][System.Management.Automation.Language.Parser]::ParseFile('scripts/build-windows.ps1',[ref]$null,[ref]$null)"
```

A quick run of the built executable without touching your own data:

```
$env:KICADGEN_DATA_DIR = "$env:TEMP\kicadgen-try"; .\dist\KiCadGen\KiCadGen.exe
```

**Never test the MSI by installing it on the development machine.** Use a
virtual machine or Windows Sandbox: install, generate a component with
FreeCAD, install a newer MSI over it, uninstall, check the user data is kept.

## 4. If you changed a path or a name

Check the counterpart: app_paths vs the spec vs the WiX source vs the docs.
A file the program looks for where the package does not put it is the
classic failure here, and it only shows up once installed.
