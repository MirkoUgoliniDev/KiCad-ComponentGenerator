---
name: verify-kicad-component
description: Read-only audit of a generated KiCad component (symbol, footprint, STEP/WRL) and of its delivery — pin-pad match, KLC findings, geometry against the evidence, model placement, file layout, isolated preview and project/global installation. The single checklist the kicad-library-verifier and kicad-workflow-reviewer agents follow. Use it before publishing a component or after a change to the generators or the KiCad tab.
---

# Verify KiCad component

Read-only: do not edit files, regenerate output, repair a failing component,
launch an interactive KiCad, change a global KiCad configuration, or commit.

## 1. Files and names

- `<component>.kicad_sym`, `<component>.pretty/<component>.kicad_mod`,
  `<component>.3dshapes/<component>.step` (and `.wrl`) exist with the names of
  `kicadgen/core/output_names.py`; the footprint's model reference names the
  published file.
- No path, secret or machine-specific setting inside the files.

## 2. Symbol and footprint against the evidence

- Symbol pin numbers = electrical pad numbers of the footprint, as text
  (alphanumeric pads such as «A1» included); no pin without a pad, no pad
  without a pin. Names match the source pinout; unnamed pins are reported,
  not guessed.
- Pad count, numbering, pitch, row spacing, drill, pad size and body size
  match the documents; estimates are marked as estimates in the notes.
- F.Fab outline = body, F.SilkS outline outside it, F.CrtYd around
  everything, Reference/Value present. Courtyard margin (user decision
  2026-11-03, KLC F5.3): **0.5 mm for pin headers** (connectors,
  `generators/pin_header.py`), **0.25 mm for packages and castellated
  modules** (`generators/package.py`). The margin follows the generator,
  never the part's name.
- KLC: run `kicadgen/services/klc_checks.check_all` (or read the
  «Controlli KiCad (KLC)» panel) and report each finding with its code.
  Reference values: `docs/reference/kicad9-getting-started-notes.md`.

## 3. Model

- Placement, scale and orientation of the STEP relative to the footprint;
  body dimensions and visible pins agree with the footprint.
- `kicadgen/services/kicad_library.step_file_is_complete` on the STEP.

## 4. KiCad CLI, read-only

`kicadgen/services/component_check.verify_component` runs these with
`kicad-cli` (found by `system_dependencies.find_kicad_cli`) into a scratch
folder:

- `kicad-cli fp export svg --footprint <name> -o <scratch> <lib>.pretty`
- `kicad-cli sym export svg -o <scratch> <component>.kicad_sym`
- `kicad-cli pcb export step --no-board-body -o <scratch>/assembly.step <preview>.kicad_pcb`
  on an isolated preview board, then check that the export holds solids.

Without KiCad installed, say so: it is an environment limit, not a pass.

## 5. Delivery (KiCad tab)

- The KiCad tab is available to standard users (not only in an expert mode).
- Actions need the footprint; without a STEP model they still work and warn
  (user decision 2026-11-12).
- Isolated preview: the user's project and global library tables are never
  touched; `.kicad-preview` holds its own one-row `${KIPRJMOD}` fp/sym tables,
  a schematic with the symbol, and the board, which resolves the copied STEP
  through `${KIPRJMOD}`.
- Project installation: portable `${KIPRJMOD}` references. Global
  installation: absolute model paths, only the intended library entry
  changed, other entries preserved.
- Explicit overwrite confirmation, table backups, atomic staging, rollback
  and cleanup of temporary files on failure.
- Every visible text present in IT, EN, FR and DE; LLM prompts unchanged by
  packaging work.

## 6. Tests and report

Run the relevant tests (`tests/test_kicad_library.py`,
`tests/test_klc_checks.py`, `tests/test_symbol_pins_from_pads.py`, the
generator's tests). Report confirmed defects first, each with file, line,
severity and a concrete correction; then estimates, missing evidence and
environment limits. A component that renders is not thereby production-ready.
