---
name: cad3d-change
description: Change how KiCadGen builds the STEP/WRL models — kicadgen/cad3d/ (step_generator.py, freecad_build_body.py, freecad_parts.py, kicad_models.py, process_log.py, body_params.py, lead_geometry.py) and the FreeCAD child it launches headless. Use it for any change to body or lead geometry, model placement, colours, KiCad library model reuse, or how FreeCAD is found, run, logged, cancelled and its outputs published.
---

# Changing the 3D (STEP) generation

The STEP model is built by a FreeCAD child process run headless
(`freecadcmd freecad_build_body.py <json>`): the program writes a JSON
payload, FreeCAD builds and exports, the program publishes the files. Most
defects here appear only with a real FreeCAD, on a killed run or on a
second run over old files, so the checks below matter more than usual.

## 1. Read first

- `kicadgen/cad3d/step_generator.py`: payloads, placement
  (`place_component`, `clamp_component_position`, arbitrary rotations, not
  only multiples of 90 degrees), overlaps, colours (`colorize_step`),
  `find_freecadcmd`, staging (`STAGING_MARKER`, `staging_path`,
  `_published_outputs`), `Cad3DError`.
- `kicadgen/cad3d/freecad_build_body.py`, `freecad_parts.py`: code that runs
  INSIDE FreeCAD's Python (no project imports beyond what the script ships).
- `kicadgen/cad3d/process_log.py`: `run_logged` (timeout = seconds of
  SILENCE, cancellation, per-component log), `kill_process_tree`.
- `kicadgen/cad3d/kicad_models.py`: reuse of KiCad library models,
  `_find_freecadcmd`, size cache (`model_size_cache.py`).
- `kicadgen/services/installed_programs.py`: where FreeCAD and KiCad are
  found (per-user installs, declared Program Files, «Uninstall» registry).

## 2. Rules that changes must keep

- **A FreeCAD launch goes through `run_logged`**: logged, cancellable,
  contained in a Windows Job Object (`contain_process`/`release_process`)
  and killed with its whole process tree, never a bare `subprocess.run`.
  The run ends with the CHILD, not with its pipe: a descendant still holding
  stdout is ended after `EXIT_DRAIN_GRACE_S`, never reported as a timeout.
  Two launches keep their own loop on purpose, with the same machinery
  (`child_environment`, job, tree kill): the model-measuring child in
  `kicad_models.py` (silence = a finished model in `progress.jsonl` OR any
  output, the script beats at `heartbeat_interval(timeout)`, tail kept in
  `<user cache>/freecad-measure.log`) and the candidate preview in
  `gui/model_candidates_dialog.py` (a whole-run limit, since the child only
  prints its result at the end). A new launch uses `run_logged`.
- **Outputs are staged and published atomically**: write to the
  `.kicadgen-partial` sibling, rename on success, remove on failure. A kill
  must never leave a truncated STEP or an old file looking current.
- **Geometry comes from evidence**: body sizes from the documents or typed by
  the user, heights from a single top photo are estimates and say so in the
  notes. No invented body: without declared dimensions there is no body,
  never a 10 x 10 x 5 mm placeholder.
- **Names stay aligned**: footprint, symbol and model names follow
  `kicadgen/core/output_names.py`; the model reference in the footprint
  matches the published file.
- **Wording**: the user-facing name is «modello STEP», never «modello 3D»
  (four languages, `add-ui-text`).
- **Generalise**: no rule keyed on a part or package name; run the
  `generality-auditor` agent on geometry changes.

## 3. Tests without FreeCAD, then with it

The GitHub runner has no FreeCAD (`ci-green`): unit tests fake the child or
the payload capture (see `tests/test_cad3d_review_fixes.py`,
`tests/test_3d_output_staging.py`, `tests/test_freecad_*.py`). A fake only
proves the payload; it does not prove the model. After a geometry change,
when FreeCAD is installed locally, build one real model and check it:
`kicad_library.step_file_is_complete`, then KiCad's own import through
`kicadgen/services/component_check.py` (`pcb export step` of an isolated
preview board, solids present). Report which of the two you ran.

## 4. Verify

- Focused: `tests/test_cad3d*.py`, `tests/test_3d_output_staging.py`,
  `tests/test_freecad_*.py`, `tests/test_step_colors.py`,
  `tests/test_kicad_models.py`, `tests/test_model_*.py`, plus
  `tests/test_documentation.py`.
- Then the full suite (`run-test-suite`) and `update-handoff`.
