# Footprint: back layers, anchor move, vertex editing, keepout zones — plan

Date: 2026-11-09. Branch `FOOTPRINT-ADVANCE` (= `main` at `c26d6b6`).
User request: «ok implementa anche quelle cose nuove, ma poi prima di dire ok rifai sempre controlli avversariali»,
design confirmed in chat: back layers B.*, «Sposta ancora», add/remove vertex, keepout zones; TABLES OUT OF SCOPE.

## 1. Features and behaviour

### F1 — Back layers B.SilkS and B.Fab
- `core/footprint_layers.py`: two drawable layers, `side="back"`, KLC widths as their front twins (B.SilkS 0.12 mm,
  B.Fab 0.10 mm, texts 1.0/0.15), colours from KiCad 10's built-in theme (`common/settings/builtin_color_themes.h`:
  read the real values, do not guess; GitLab raw is reachable with curl, klc.kicad.org is behind Cloudflare).
- Writer: a text on a back layer is written MIRRORED as KiCad does (`(justify ... mirror)` inside `effects`); the same
  for a text box on a back layer. Check the exact syntax against a real footprint of the installed KiCad 10 library
  (`C:\Program Files\KiCad\10.0\share\kicad\footprints`, grep `B.SilkS` texts) and with `kicad-cli fp export svg`.
- Preview: back layers drawn UNDER the front ones, in their colours; back texts drawn mirrored (as KiCad's top view).
  Layer panel lists them, visibility toggles, palette may make them active.
- Checks (`services/footprint_checks.py`): silk clearance per side — B.SilkS ink against pads with copper on the back
  (through-hole pads, `*.Cu`, `B.Cu`), F.SilkS against front copper; stroke width rule on B.SilkS too; pin-1 mark
  rule may be satisfied by a front OR back mark? NO: KLC F5.1 wants it on F.SilkS — keep the rule front-only.
- Out of scope: B.CrtYd in the palette (courtyard stays generator-only, user decision 2026-11-06), B.Cu drawings,
  flipping the whole footprint.

### F2 — «Sposta ancora» (move the footprint anchor)
- The draft gets a new field `anchor` = (ax, ay) mm in the GENERATOR's coordinates (y down), default (0, 0).
  Nothing stored is rewritten: drawings, texts, pad overrides and package pads stay in generator coordinates.
- Writing (`Footprint.shift_origin(ax, ay)` or equivalent, applied once when the .kicad_mod is built): every pad,
  graphic, zone and the Reference/Value positions move by (-ax, -ay), so the chosen point is (0, 0) in the file.
- STEP model: the `(model ...)` node gets the matching offset so the 3D body stays on the pads. KiCad's model offset
  is in 3D axes (Y UP): footprint shift (-ax, -ay) with y down -> `(offset (xyz -ax ay 0))`. THE SIGN MUST BE
  VERIFIED with KiCad (e.g. `kicad-cli pcb export step` / `pcb export vrml` of a board with the footprint, bounding
  box of the model vs pads), not assumed. `services/kicad_library.attach_3d_model` gains the offset; every caller
  (preview board, project install, global install) passes the anchor; `component_check` and any reader of the
  model offset must agree.
- GUI: palette/menu command «Sposta ancora»: one click, snapped like a drawing click (pads, grid, objects), sets the
  anchor; one undo step; Escape cancels. The preview shows the footprint anchor marker (KiCad: a small cross in a
  circle) at (ax, ay); the drawing origin option «footprint origin» follows the anchor. Applica writes it, Annulla
  restores it, the project file and the recovery draft save it.
- Out of scope: rotating the footprint, moving the anchor of a KiCad-library-reused model differently from ours.

### F3 — Add / remove vertex
- Lines (polylines) and polygons, in the footprint AND in the symbol editor (user rule «stessa UX in tutte le schede»:
  the symbol's polylines/polygons get the same entries, through the shared menu layer).
- Right click on a SEGMENT of a selected/hovered line or polygon: «Aggiungi vertice» inserts a vertex at the clicked
  point projected on that segment (snapped to the grid as the drag of a vertex is).
- Right click on a VERTEX: «Rimuovi vertice»; refused with a toast when the line would go below 2 points or the
  polygon below 3, or the polygon would have no area.
- One undo step each. Pure helpers in a Qt-free module (`core/vertex_edit.py`): `insert_vertex(points, segment,
  point, closed)`, `remove_vertex(points, index, closed, min_points)`, `nearest_segment(points, point, closed)`.
- Menu entries built by `gui/canvas_menus.py` (`add_vertex_entry`, `remove_vertex_entry`), translated.

### F4 — Keepout zones (rule areas)
- New drawing kind `zone`: a closed polygon (>= 3 points, with area), `layers` one of ["F.Cu"], ["B.Cu"],
  ["F.Cu", "B.Cu"], and `keepout` flags {tracks, vias, pads, copperpour, footprints} each True (= not_allowed).
  Defaults: tracks, vias, copperpour not allowed; pads and footprints allowed (KiCad's rule-area defaults; VERIFY in
  KiCad's source or behaviour and say where the values came from).
- Writer: KiCad 10 syntax exactly as the installed library writes it, e.g.
  `Button_Switch_SMD.pretty/SW_SPST_SKQG_WithoutStem.kicad_mod`: `(zone (layer "F.Cu") (uuid ..) (hatch full 0.508)
  (connect_pads (clearance 0)) (min_thickness 0.254) (keepout (tracks not_allowed) ...) (placement (enabled no)
  (sheetname "")) (fill (thermal_gap 0.508) (thermal_bridge_width 0.508) (island_removal_mode 0)) (polygon (pts ..)))`;
  two layers -> `(layers "F.Cu" "B.Cu")`. Find a library footprint with a two-layer keepout to copy that form.
  Verified with kicad-cli (export svg must succeed and draw it; a test that skips without KiCad).
- GUI: palette tool «Zona di esclusione» (polygon clicks, closes like the polygon tool), then a small dialog: layers
  (F.Cu / B.Cu / both) and the five flags; drawn hatched in the preview in the copper colour; properties dialog and
  panel edit layers and flags; vertices editable (F3); translate/rotate/mirror/copy/delete like other drawings.
- Checks: zones are not silkscreen ink and not courtyard content (no false findings); a pad inside a keepout that
  forbids pads -> warning (KLC has no rule: KiCad's DRC would flag it; say so in the message).
- Out of scope: tables (user, 2026-11-09), filled copper zones, zone priorities.

## 2. Work split (disjoint files), waves

Wave 1, in parallel:
- **W1-core**: `kicadgen/core/footprint_layers.py`, `kicadgen/core/footprint.py`, NEW `kicadgen/core/vertex_edit.py`,
  `kicadgen/services/footprint_checks.py`, their tests. F1 layers + mirrored back texts + per-side checks; F2
  `shift_origin` on the Footprint and every graphic kind (+ zones); F3 helpers; F4 record validation, `FpZone`
  writer, `drawing_graphics`, `drawing_bounds`/`turn`/`mirror`/`translate` for zones, checks.
- **W1-delivery**: `kicadgen/services/kicad_library.py` (`attach_3d_model` offset), `kicadgen/services/component_check.py`
  if it reads model offsets, and the KiCad verification of the offset SIGN (kicad-cli). Tests. Does NOT touch the GUI.
- **W1-menus**: `kicadgen/gui/canvas_menus.py` (vertex entries), `kicadgen/gui/symbol_tab.py` +
  `kicadgen/gui/symbol_glyphs.py` (add/remove vertex on the symbol's polylines/polygons, with W1-core's helpers —
  the helper signatures in F3 are the contract; W1-menus may stub-import and the coordinator reconciles).

Wave 2 (after wave 1):
- **W2-footprint**: `kicadgen/gui/main_window.py`, `kicadgen/gui/footprint_view.py`, `kicadgen/gui/workers.py`:
  B layers in panel/palette/preview, «Sposta ancora» tool + anchor marker + draft field + undo + project save +
  writer/attach calls with the anchor, vertex menu in the footprint, zone tool + dialog + preview + properties.

Coordinator: translations (`services/i18n.py`, four languages), goldens regenerated on purpose, focused tests,
full suite in two halves on frozen code, ADVERSARIAL REVIEW (user rule 2026-11-09: always, before saying OK):
read-only agents on disjoint scopes + canvas-parity-reviewer + generality-auditor + kicad-library-verifier on a
generated component with back drawings, an anchor and a zone; fixes; suite again. Commit only on the user's OK.

## 3. Contracts

1. Stored coordinates never change because of the anchor; only the writer and the model offset apply it.
2. A file written with anchor (0, 0) is byte-identical (except uuids) to today's.
3. Back texts are mirrored in the file and in the preview; front texts unchanged.
4. Every record kind round-trips through `validate_footprint_drawings` (zone included); unknown layers/flags refused.
5. Vertex insert/remove is one undo step and never leaves an invalid record (min points, polygon area).
6. Zone syntax parses in KiCad 10 (kicad-cli) and matches the library's own form.
7. The STEP model stays on the pads after an anchor move: verified with KiCad, not by reasoning.
8. Positive and negative test for every rule (CLAUDE.md «Correzioni»).

## 4. Corrections after implementation (2026-11-09)

Two premises of this plan were wrong when checked against KiCad 10; the code follows KiCad:
- F4 defaults: KiCad 10's `ZONE_SETTINGS()` forbids tracks, vias AND pads, and allows copper pours and footprints
  (`pcbnew/zone_settings.cpp`); numbers from KiCad 10's defaults (hatch edge 0.5, min_thickness 0.25), form of the
  library's `SODIMM-200_1.8V_Card_edge.kicad_mod`.
- F4 checks: KiCad's DRC deflates a rule area by `DRCEpsilon` = 0.0005 mm (a length) and then flags any contact
  (`copper_meets_keepout`), not an overlap area; only plated through-hole pads have copper on both sides, NPTH and
  other pads have the copper layers they name, apertures without copper have none (verified with `kicad-cli pcb drc`).
- KiCad does NOT refuse self-intersecting zone outlines; this tool does (`polygon_is_simple`), because the DRC's
  deflation of a crossed outline depends on Clipper2's orientation handling.
- User decision A1: in the symbol editor a line ending on its first point is closed for vertex editing.
