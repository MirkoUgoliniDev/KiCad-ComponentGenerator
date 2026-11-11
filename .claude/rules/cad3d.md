---
paths:
  - "kicadgen/cad3d/**"
  - "kicadgen/gui/model_candidates_dialog.py"
  - "kicadgen/services/installed_programs.py"
---

# STEP/WRL generation (FreeCAD child process)

Facts that every change here must keep. Procedure, liveness details and the
tests with and without FreeCAD: `cad3d-change` skill.

- **A FreeCAD launch goes through `process_log.run_logged`**: logged,
  cancellable, contained in a Windows Job Object and killed with its whole
  process tree. Never a bare `subprocess.run`. The two launches that keep their
  own loop (model measuring in `kicad_models.py`, candidate preview in
  `gui/model_candidates_dialog.py`) are deliberate; a new launch uses
  `run_logged`.
- **A user interruption is `Cad3DCancelled`**: recognised by type, never by its
  text, which is translated.
- **Outputs are staged and published atomically**: written to the
  `.kicadgen-partial` sibling, renamed on success, removed on failure. A kill
  never leaves a truncated STEP or an old file looking current.
- **Geometry comes from evidence**: body sizes from the documents or typed by
  the user; heights from a single top photo are estimates and say so in the
  notes. Without declared dimensions there is no body, never a placeholder.
- **Arbitrary rotations**: placement, board limits and overlaps hold for angles
  that are not multiples of 90 degrees.
- **Names stay aligned**: footprint, symbol and model names follow
  `kicadgen/core/output_names.py`; the model reference in the footprint
  matches the published file.
- **Code inside FreeCAD's Python** (`freecad_build_body.py`,
  `freecad_parts.py`) imports nothing of the project beyond what the script
  ships.
- **Wording**: the user-facing name is «modello STEP», never «modello 3D».
- The GitHub runner has no FreeCAD: unit tests fake the child (`ci-green`
  skill); a geometry change is also tried with a real FreeCAD locally.
