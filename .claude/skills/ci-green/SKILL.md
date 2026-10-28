---
name: ci-green
description: Make the GitHub Actions test suite of KiCadGen pass honestly — what the windows-latest runner lacks compared with the developer's PC, how to read a failed run, and how to fix a test that assumes KiCad, FreeCAD or untracked model weights. Use it whenever a GitHub run is red, before a release, and when adding a test that touches an external program or a model file.
---

# Keeping the GitHub suite green

The `Tests` workflow (`.github/workflows/tests.yml`) runs on every push and
the release workflow repeats it before building the MSI. Until 2026-10-27 it
had **never** passed: about fifty tests assumed the developer's machine.

## 1. What the runner does NOT have

| On the developer's PC | On `windows-latest` |
|---|---|
| KiCad 10 (`kicad-cli`, the symbol library `share/kicad/symbols`) | nothing: `symbol_glyphs.catalogue()` falls back to `_builtin_glyphs()` (no `body_hint`) |
| FreeCAD (`freecadcmd.exe`) | nothing: `_freecad_available` false, 3D group off, STEP never built |
| `models/yolo/*.pt`, `models/smd/smd_classifier.pt` (git-ignored) | nothing: the default detector `yolo_smd` cannot load its weights |
| PyTorch, Ultralytics (optional requirements) | not installed: only `requirements.txt` + `requirements/yolo.txt` |
| A desktop session, Italian locale | `QT_QPA_PLATFORM=offscreen`, en-US, `TEMP` in `runner.temp` |

Pinned versions are the same (`-c requirements/release-constraints.txt`), so a
test that passes in `.venv-build` with `QT_QPA_PLATFORM=offscreen` but fails
on GitHub fails because of a missing program or file, not a library.

## 2. Reading a failed run

```
gh run list -R MirkoUgoliniDev/KiCad-ComponentGenerator --workflow Tests --limit 10
gh run view <id> -R MirkoUgoliniDev/KiCad-ComponentGenerator --log-failed > run.log
grep -o -E "(FAILED|ERROR) tests/[a-z_0-9]+\.py" run.log | sort | uniq -c | sort -rn
grep -E "Z E {2,}[A-Za-z]" run.log | sed 's/.*Z E *//' | sort | uniq -c | sort -rn
```

Map each distinct message to its test (the `_____ test_name _____` header
above it). A fixture that does `next(...)` over the glyph catalogue, a test
that opens a QMessageBox because weights are missing, an `AttributeError` on
a widget that exists only with FreeCAD: all environmental.

## 3. Honest fixes, in order of preference

1. **The test needs its own data, not an install.** A test of glyph
   geometry should load a small symbol library fixture under `tests/data/`
   (point `kicad_symbols_dir` at it with `monkeypatch`), not the installed
   KiCad; a test of the detector UI should pass a fake model path, as
   `tests/test_automatic_generation.py::use_ai_detector` already does.
2. **Skip with the reason, at module or fixture level**, only when the
   behaviour under test *is* the external program:
   `pytest.importorskip`, or `pytest.skip("KiCad symbol library not installed")`
   decided from `kicad_symbols_dir() is None` / `find_freecadcmd()` raising.
   A skip must say what is missing; a silent pass is worse than a failure.
3. **Never** make the test pass by loosening the assertion, by catching the
   exception, or by adding the weights to the repository (licence, size).

Keep the pair of cases: the positive case runs where the program exists (the
developer's PC), the negative case (absent program -> clean skip or clean
fallback) must run on the runner too.

## 4. Reproduce the runner locally before pushing

```
QT_QPA_PLATFORM=offscreen .venv-build/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/claude-test-ci tests/<file>.py
```

To simulate the missing programs, use the test's own monkeypatches
(`installed_programs._uninstall_entries`, `shutil.which`,
`installed_programs._FALLBACK_ROOTS`, `kicad_symbols_dir`) rather than
uninstalling anything. Then push and read the run: green on GitHub is the
only proof.

## 5. Why it matters

A release tag `v*` is permanent (repository ruleset) and the release
workflow refuses to build while the suite is red: a red CI means every
release must be published by hand (see `build-installer`).
