---
paths:
  - "kicadgen/services/kicad_library.py"
  - "kicadgen/services/kicad_export_history.py"
  - "kicadgen/services/klc_checks.py"
  - "kicadgen/generators/**"
  - "kicadgen/core/sexpr.py"
  - "kicadgen/core/footprint.py"
  - "kicadgen/core/output_names.py"
  - "tests/test_kicad_library.py"
---

# KiCad output and packaging

Facts that every change here must keep. Generators and serializers:
`generate-kicad-component` skill; audit of one component's files:
`verify-kicad-component` skill and the `kicad-library-verifier` agent; KiCad
tab and installation workflow: the `kicad-workflow-reviewer` agent before
publishing.

- Symbol pin numbers and names stay aligned with the source pinout; the symbol
  pins are exactly the footprint pads, the pinout only names them.
- Pin pitch and row spacing are different values: never reuse one for the
  other without evidence.
- Footprint, symbol and model names stay consistent
  (`kicadgen/core/output_names.py`).
- Generated KiCad output stays isolated until it has been reviewed in the
  KiCad tab.
- **Preview never modifies `fp-lib-table`.** Project installation uses
  `${KIPRJMOD}`; global installation uses explicit absolute paths with
  overwrite confirmation.
- Unrelated library entries are preserved and every packaging operation is
  rollback-safe.
- Geometric thresholds of the checks are published ones (KLC, KiCad's DRC)
  and cite their source.

## Validation for packaging changes

Run `tests/test_kicad_library.py`, validate the SQLite translation catalog
for all four languages, and use KiCad CLI read-only render/export checks when
KiCad is installed. The GitHub runner has no KiCad: a test must not assume it
(`ci-green` skill).
