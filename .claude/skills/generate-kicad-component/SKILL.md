---
name: generate-kicad-component
description: Generate or update KiCad symbols, footprints and STEP models in this repository — the generators (pin header, castellated module, package, host mounting, symbol), the S-expression serializers and the KLC rules they must meet. Use it when a change touches kicadgen/generators/, kicadgen/core/ or what the .kicad_sym/.kicad_mod files contain; it routes the specialised parts to the matching skill.
---

# Generate KiCad component

## 1. Which skill for which part

| Change | Skill |
|---|---|
| What the analysis reads from datasheets and drawings (schemas, pinout, pads) | `document-extraction-change` |
| Photo → mechanical envelopes (calibration, detectors, crop readings) | `photo-pipeline-change` |
| STEP/WRL models, FreeCAD child, model reuse | `cad3d-change` |
| Symbol editor or footprint preview interaction | `canvas-shared-change` |
| Any visible text | `add-ui-text` |
| Checking the generated files | `verify-kicad-component` |

Always: `update-handoff` before touching code, `run-test-suite` before saying
done, `pre-commit-check` before a commit (only after the user's explicit OK).

## 2. Code map

- `kicadgen/core/sexpr.py`: the S-expression WRITER (`sx`, `SExpr`,
  `quote`, `format_number`). Never build KiCad text with f-strings in new
  code.
  `kicadgen/generators/symbol_glyphs.parse_sexpr` is the READER used by the
  checks and the symbol editor.
- `kicadgen/core/footprint.py`: `Footprint`, pads, graphics, property texts
  (`KICAD_VERSION`, `TEXT_ENVELOPE_LAYERS`, user drawing layers).
- `kicadgen/core/package_geometry.py`: `PackageGeometry`/`PinSpec`, source
  view, mirroring, orientation (`pins_seen_from_top`), body frame.
- Generators: `generators/pin_header.py`, `castellated_module.py`,
  `package.py` (`build_package`, `COURTYARD_MARGIN_MM`), `host_mounting.py`,
  `symbol.py` (`PIN_GRID_MM`, `TEXT_SIZE`, `PIN_LENGTH_STEP_MM`),
  `symbol_pins.py` (`pinout_on_pads`: symbol pins = footprint pads).
- `kicadgen/core/output_names.py`: one naming rule for symbol, footprint,
  `.pretty`, `.3dshapes` and model files.
- `kicadgen/services/klc_checks.py`: the KLC rules the program checks.
- KiCad 9 guide notes: `docs/reference/kicad9-getting-started-notes.md`.

## 3. Rules

- **Evidence only**: dimensions and pin data come from the documents, the
  photo or the user. `pitch` (adjacent pins in a row), `row_spacing`
  (perpendicular distance between rows), `body_height` (case thickness) and
  pin length are separate values; never reuse one for another. Estimates are
  recorded as estimates in the notes.
- **Pins are the pads**: symbol pin numbers equal the footprint's electrical
  pad numbers, compared as text («A1» is a pin number); names come from the
  pinout and stay aligned with the source.
- **KLC** (checked by `klc_checks.py`, KLC numbering): symbol pin ends on the
  100 mil (2.54 mm) grid, pin length 2.54 mm or 1.27 mm steps, texts 1.27 mm
  (S4.1, S4.2); body outline 0.254 mm with background fill (S3.3); unique
  pin numbers and positions (S4.3); designator, description, keywords,
  datasheet, footprint filters (S6.x); footprint F.Fab and F.CrtYd outlines
  (F5.2, F5.3), Reference/Value texts (F5.1), attribute and description (F5.4,
  F9.1). Courtyard margin 0.25 mm is the general value; family-specific
  clearances need the KLC text before becoming a rule.
- **Known divergence**: package footprints put the origin at the body centre,
  while KiCad's THT convention is pin 1 at (0, 0). Changing it is a user
  decision.
- **Generalise** (binding): no rule keyed on a part, brand or package name;
  run the `generality-auditor` agent before committing geometry or
  validation changes.
- **Delivery** goes through `kicadgen/services/kicad_library.py` (isolated
  preview, project library with `${KIPRJMOD}`, global library with absolute
  paths, rollback-safe); the checklist is in `verify-kicad-component`.

## 4. Safety

Do not overwrite source documents or other generated components. Never read
or commit `.env`, `config.json`, generated output or local KiCad state.
