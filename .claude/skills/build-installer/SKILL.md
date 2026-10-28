---
name: build-installer
description: Produce the Windows setup of KiCadGen (KiCadGen.exe and KiCadGen-X.Y.Z.msi), verify it, and publish a release on GitHub only on the user's explicit request. Use it when the user wants an installer, a setup to distribute, a new version or a GitHub release. To change how the package is made, use installer-change instead.
---

# Building and publishing the setup

The full reference is `docs/PACKAGING-WINDOWS-MSI.md`; read it first. This
skill is the operational sequence. It does not change the packaging: if a
step fails because of the spec, the WiX source, a script or `app_paths`,
switch to the `installer-change` skill.

## 0. What the user gets

| Output | Content |
|---|---|
| `dist\KiCadGen\` | `KiCadGen.exe` + `_internal\` (portable folder, no installation) |
| `dist\KiCadGen-X.Y.Z.msi` | per-machine installer into `C:\Program Files\KiCadGen`, Start menu and desktop shortcuts, upgrade over older versions |

FreeCAD and KiCad are **not** bundled: the user installs them separately;
the installer only says whether it found them. No digital signature:
SmartScreen shows «unknown publisher».

## 1. Preconditions (check, do not assume)

- Clean working tree on the branch the user wants to release from (by the
  user's choice the release is made from the current branch).
- Full suite green (`run-test-suite` skill).
- Python 3.13 from python.org on the machine, or `KICADGEN_BUILD_PYTHON`.
- WiX 3.14 in `C:\tools\wix314` or Program Files. If missing, the script
  downloads it **only** with the official SHA-256 (`-WixSha256` /
  `KICADGEN_WIX_SHA256`; in CI the repository variable `WIX314_SHA256`).
  Never invent or copy the hash from an unofficial source: ask the user to
  take it from the official release page.
- Version: `kicadgen.__version__` (`kicadgen/__init__.py`), three numbers.

## 2. Local build

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows.ps1 -Package app-image   # executable only, faster
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows.ps1                      # executable + MSI
```

`-IncludeYoloModel` ships `models\yolo\pcb_yolov8s.onnx`: **do not use it**
until the user has cleared the weights' license (ambiguous MIT/AGPL). The
build uses `.venv-build` (never `.venv`), the pinned versions of
`requirements/release-constraints.txt`, a seed database generated from the
code (never the developer's own) and ends with `KiCadGen.exe --check-runtime`.
It takes several minutes: run it in the background.

Report: MSI path and size, the `--check-runtime` result, light warnings other
than the expected ICE60 (ICE61 is suppressed on purpose).

## 3. Try it without touching the developer's data

```powershell
$env:KICADGEN_DATA_DIR = "$env:TEMP\kicadgen-try"; .\dist\KiCadGen\KiCadGen.exe
```

**Never install the MSI on the development machine.** The install test is
done by the user in a virtual machine or Windows Sandbox (Sandbox is not
enabled on this machine): give them this checklist.

1. Install; the last page reports FreeCAD/KiCad found or not.
2. Start from the shortcut, open or create a project, generate a component
   with FreeCAD installed; check the STEP model and the KiCad tab.
3. Switch the language; open the guide and the license dialog.
4. Install a newer MSI over it (or the same version rebuilt): one entry in
   Settings > Apps, the custom folder is proposed again.
5. Uninstall: `%LOCALAPPDATA%\KiCadGen` and `Documents\KiCadGen` remain.

## 4. Publish (only on the user's explicit request)

Publishing writes to GitHub (commit, tag, Release) and cannot be quietly
undone. Ask before running it, naming the version and the branch.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\publish-release.ps1 -Version X.Y.Z
```

It sets the version, commits `release: vX.Y.Z`, tags and pushes. The tag
starts `.github/workflows/release.yml`: tests in two halves, MSI build in a
read-only job, Release created by the `publish` job. Follow the run with
`gh run watch` and report the Release URL; if the run fails, report the log,
do not delete or move the tag without the user.

### 4b. Manual release (when the GitHub suite is red)

v1.0.0 (2026-10-27) was published this way: the locally built MSI attached
by hand, because the release workflow would have failed at the test step
(see the `ci-green` skill). Only on the user's explicit request:

```powershell
gh release create vX.Y.Z dist\KiCadGen-X.Y.Z.msi --target main --title "KiCadGen vX.Y.Z" --notes-file notes.md
```

Write the notes by hand (what it is, requirements, SmartScreen, what is not
included, the SHA-256 of the MSI). The tag still triggers `release.yml`,
whose run will be red: say so to the user. **Tags `v*` are permanent** (the
repository ruleset forbids moving or deleting them): a wrong release means
the next number, never a re-tag. `releases/latest` always points to the
newest non-prerelease, so links in posts stay valid.

## 5. Before a first public release

Run the `release-readiness-reviewer` agent and bring its findings to the
user. Ask the user to run `scripts/configure-github-repo.ps1` (read-only
without `-Apply`): it shows whether the branch and tag rulesets, secret
scanning, Dependabot and the Actions restrictions are in place, and the
release tags are protected only once it has been applied. Points still open in HANDOFF.md that the user decides: WiX SHA-256,
code signing (cost), YOLO weights license, how to offer the GPL/LGPL sources
with each release, VM install test.

## 6. Record

Write the result in HANDOFF.md (`update-handoff` skill): version, branch,
MSI size, checks run, what the user tested in the VM, Release URL.
