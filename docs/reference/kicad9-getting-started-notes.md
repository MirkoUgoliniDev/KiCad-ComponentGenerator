# KiCad 9 «Getting Started» — notes for the generators

Source: *Getting Started in KiCad* (KiCad 9.0), The KiCad Team,
<https://docs.kicad.org/9.0/en/getting_started_in_kicad/getting_started_in_kicad.pdf>,
chapters «Creating New Symbols», «Creating New Footprints» and «Linking
Symbols, Footprints, and 3D Models» (PDF pages 42-53). The document is
licensed under GPL v3 or later, or Creative Commons Attribution 3.0; these
notes are a short paraphrase with attribution, not a copy. The PDF itself is
not stored in the repository: it is a 55-page user tutorial (schematic,
board, routing, fabrication) and only these pages concern library parts.

These are the guide's teaching values. Where the KiCad Library Conventions
(KLC, <https://klc.kicad.org>) are stricter, the KLC and
`kicadgen/services/klc_checks.py` win.

## Symbols

- KiCad connects a wire only to a pin on a **50 mil (1.27 mm)** grid: that is
  the hard limit of the program. The KLC is stricter (S4.1): pin ends on the
  **100 mil (2.54 mm)** grid, pin length 2.54 mm (longer in 1.27 mm steps),
  texts 1.27 mm. The project follows the KLC (`PIN_GRID_MM = 2.54` in
  `kicadgen/generators/symbol.py` and `klc_checks.py`).
- Finer grids are for graphics only; pins go back on the pin grid.
- Keywords, Value and footprint filters help the part be found and matched
  (KLC S6.2, S6.3).

## Footprints

- KiCad's Y axis points down.
- **Through-hole convention: pin 1 at (0, 0), pin 1 top left.** The project's
  package footprints put the origin at the body centre
  (`PackageGeometry.body_frame_offset`, `generators/package.py`); the drawing
  origin chosen in the preview is a view setting only (user decision
  2026-11-02, HANDOFF). This divergence is known; changing the file origin is
  a user decision, not a fix.
- Drill = largest pin dimension (the diagonal of a rectangular pin) + 0.2 mm;
  pad = drill + 2 x annular ring, the ring at least 0.15 mm and preferably
  larger (0.3 mm in the guide) for soldering and strength.
- Layers: the exact part outline on **F.Fab** (line 0.10 mm), a slightly
  larger outline on **F.Silkscreen** (line 0.12 mm, drawn 0.11 mm outside the
  fab line: half of each width), a **F.Courtyard** around everything with
  **0.25 mm** clearance (`COURTYARD_MARGIN_MM` in `generators/package.py`).
  The KLC sets other clearances for some families; confirm on the KLC before
  making that a rule.
- Courtyards of neighbouring parts should not intersect.

## Symbols, footprints and 3D models

- A symbol may name a default footprint and footprint filters.
- The footprint stores its 3D model file names with scale, rotation, offset
  and opacity. STEP is the accurate format, VRML the render format; KiCad's
  libraries list one name and substitute the other on a STEP export.
- KiCad's own models are made with FreeCAD and the StepUp workbench; this
  project drives FreeCAD headless instead (`kicadgen/cad3d/`, skill
  `cad3d-change`).
