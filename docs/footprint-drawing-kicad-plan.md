# Plan: KiCad-grade drawing tools for the footprint preview (and the symbol editor)

Written 2026-11-08 by the coordinator after three read-only inventories
(footprint drawing, symbol drawing, KiCad writer and checks). Base commit
`bd83c9a` on `FOOTPRINT-ADVANCE` (= `main`). Line numbers refer to that commit;
grep for the names, they move.

User request (2026-11-08): «spremiti un po il cervello e cerca di relaizzare uno
s trumento al pari di Kicad», then «fai un piano super dettagliato ... poi scatena
tutti gli agenti esperti che ti servono ... poi però fai un acontrollo avversariale».

Binding rules: `.claude/CLAUDE.md` (HANDOFF before code, generalise, Doxygen
`@brief/@param/@return/@note`, comments in English, agents on disjoint files,
coordinator integrates and commits only on the user's OK), user memory
«Stessa UX in tutte le schede» (what is built for the footprint is replicated in
the symbol editor through shared code), `canvas-shared-change` skill
(parity tests, shared components, deliberate differences written down).

## 0. Reference: what KiCad 9/10 offers in the footprint editor

| KiCad feature | KiCadGen today | Target |
|---|---|---|
| Select tool (arrow) at the top of the right toolbar | none: "no tool armed" is select | explicit Select button, Esc returns to it |
| Active tool clearly highlighted, step message in the status bar | autoRaise checked button, hardly visible; footprint shows no hint | highlighted button in the layer colour + per-step hint under the canvas in both tabs |
| Line, arc, rectangle, circle, polygon, Bezier, text, text box | line (closes into polygon at 4+ points), rect, circle, arc, text; Bezier only in the symbol | + dedicated Polygon tool, Bezier and Text box in the footprint |
| Dimensions: aligned, orthogonal, radial, centre, leader | aligned/orthogonal (one tool + measure export) | + radial, centre mark, leader |
| Per-layer default line width and text size (Board Setup > Text & Graphics) | every drawing 0.12 mm, even on F.Fab | KLC table per layer, editable width before drawing |
| Line modes free / 90° / 45°, cycled by **Shift+Space** | none (symbol wire only: orthogonal toggle) | angle mode in both tabs, Shift+Space and a panel control |
| Magnetic snapping to pads, endpoints, midpoints, centres, intersections, with a marker | measure tool only (`SnapTargets`); drawing tools snap to the grid only | object snap for every drawing tool in both tabs, with a typed marker |
| Active layer chosen in the Appearance/Layers panel | layer visibility boxes and draw-layer combo are unrelated | click a layer name to make it the active drawing layer, synced with the combo |
| Properties panel of the selection (live) | modal «Proprietà elemento…» dialog | live «Proprietà» group in both tabs, one undo step per committed field |
| Hotkeys: Ctrl+Shift+L line, Ctrl+Shift+A arc, Ctrl+Shift+C circle, Ctrl+Shift+P polygon, Ctrl+Shift+T text, Ctrl+Shift+H dimension, Ctrl+Shift+M measure, E properties, Backspace removes the last point | none for tools | same keys, listed in tooltips and in «Scorciatoie da tastiera…» |

Correction of the chat proposal: in KiCad plain **Space** resets the local
coordinates; the line mode cycles with **Shift+Space**. We follow KiCad.

## 1. Scope

### In scope (phases A-D)

**A — Panel and tool usability (both tabs)**
- A1 Shared tool grid widget `kicadgen/gui/canvas_toolpalette.py`: exclusive
  checkable buttons, a Select button first, icons tinted in a given colour,
  active button drawn with a filled background and a 2 px border in the accent
  colour, tooltip = hint + shortcut, configurable columns.
- A2 Footprint «Disegno» group rebuilt: tool grid (6 columns), layer combo,
  stroke width spin box (layer default shown, «Predefinito KLC» reset), fill
  check box (enabled only for fillable tools), angle mode control
  (Libero / 45° / 90°), «Aggancia agli oggetti» check box.
- A3 Side panel order and sizes: «Layer», «Disegno», «Proprietà», «Controlli»;
  the checks list no longer takes the whole height when it holds one line
  (stretch moves to «Proprietà»; the checks list grows with its rows up to a cap,
  then scrolls).
- A4 Per-step hints (`kicadgen/gui/canvas_tool_hints.py`): one function
  `step_hint(key, points_so_far)` returning the Italian source text of the
  next action («Clic sul centro», «Clic su un punto del bordo», «Doppio clic o
  Invio per finire, Backspace toglie l'ultimo punto»...). Footprint: a hint label
  left of the pointer readout under the canvas. Symbol: its existing status
  label. The footprint toast stays for refusals/results (deliberate difference
  C-§2 of the click-tools plan stays: toast vs status for RESULTS; hints are now
  shared).
- A5 Backspace removes the last collected point (shared `PointCollector`),
  KiCad behaviour; with no point left it does nothing.
- A6 Angle constraint free/45°/90° (pure helper + router state), cycled by
  Shift+Space, mirrored by the panel control, saved in the configuration per
  tab. Applies to the segment from the previous collected point to the pointer
  for line/polygon/dimension/leader and to the second corner of nothing else
  (rectangle and circle are unaffected: their geometry is axis-bound or radial).
  The symbol wire keeps its own orthogonal button (deliberate difference, its
  corner insertion is a wire rule, not an angle limit).
- A7 Shortcuts as KiCad (table in §0) bound in both tabs through
  `canvas_gestures.bind_shortcuts`; rectangle has no KiCad default: Ctrl+Shift+R
  (declared as ours); Bezier Ctrl+Shift+B (ours); text box Ctrl+Shift+X (ours).
  Listed in the window's «Scorciatoie da tastiera…» dialog.
- A8 Shift while clicking/hovering = free point (no object snap, no angle
  constraint, grid still applied when the grid snap is on). The footprint
  host today ignores `free`; this becomes shared behaviour.

**B — Precision**
- B1 Pure object snap `kicadgen/core/object_snap.py`: candidates of kind
  `pad_centre`/`pin_end`, `endpoint`, `corner`, `midpoint`, `centre`,
  `quadrant`, `intersection` (segment-segment only), `edge` (foot of the
  perpendicular). Built from generic primitives (points, segments, circles,
  arcs) so both tabs feed it; `nearest(point_mm, candidates, radius_mm)` with a
  fixed priority (pad/pin > endpoint/corner > intersection > midpoint > centre >
  quadrant > edge) and distance within the same priority. The radius is a UI
  reach in screen pixels (10 px, like KiCad's magnet), converted with the
  current scale: it is not a geometric threshold.
- B2 Footprint adapter: pads (centre + outline corners), user drawings
  (endpoints, midpoints, rectangle corners, polygon vertices, circle centre and
  quadrants, arc ends/centre, dimension feet), generated F.Fab body
  (corners/edges), drawing origin. The drawing being drawn contributes its own
  collected points (closing a polygon on the first vertex).
- B3 Symbol adapter: pin outer/inner ends, element terminals, shape vertices,
  midpoints, centres, body corners.
- B4 Typed snap marker drawn while hovering (reuse
  `canvas_measure.anchor_marker` shapes: ring = centre, square =
  endpoint/corner, triangle = midpoint, diamond = edge, cross = intersection,
  filled ring = pad/pin) in both tabs.
- B5 Active layer from the Layer panel (footprint): clicking a drawable layer's
  name selects it (bold, accent bar), sync both ways with the combo; making a
  hidden layer active shows it; non-drawable rows (pads, holes, courtyard,
  texts, numbers, dimensions, reference image) are not selectable.
- B6 New drawable layer `Dwgs.User` (KiCad "User.Drawings"), with its colour.
  Back-side layers are OUT (§1, out of scope).

**C — Live Properties panel (both tabs)**
- C1 Shared widget `kicadgen/gui/canvas_properties.py`: `FieldSpec(key, label,
  kind=float|int|choice|bool|text, value, mixed, minimum, maximum, step,
  decimals, suffix, choices, enabled)`; `PropertiesPanel.set_fields(specs)`
  rebuilds only when the field set changes, otherwise updates values without
  emitting; emits `committed(key, value)` on editingFinished / toggled /
  activated, never on programmatic updates; mixed values shown as an empty
  field with placeholder «(vari)».
- C2 Footprint: fields from the selection (one drawing: same fields as the
  dialog `_edit_drawing_properties`, MW:19601; several drawings: layer, width,
  fill when all fillable; pads keep their own dialog in this phase, so a
  selected pad shows number, position and size read-only). One committed field = one undo step through
  the existing `set_drawing_properties` path. The dialog and the panel share the
  value/record builders (`_drawing_properties_values`,
  `_drawing_properties_record`) so they cannot diverge.
- C3 Symbol: the same widget under its palette (width 150 → the panel column
  becomes 200 px wide; the parity test `palette width <= 160` is a contract to
  update consciously), fields of the selected element (X, Y, width, fill, line
  style, size/radius, text).
- C4 Hook: the footprint has no selection signal; the single place is
  `_refresh_drawing_selection()` (MW:19009) and `_commit_drawings`. The symbol
  equivalent is found by the symbol integrator (selection setters).

**D — New drawing kinds (footprint) and dimensions**
- D1 `polygon` tool (explicit, count 0, finish on double click / Enter /
  click on the first vertex, ≥3 points) in both tabs. Footprint stores
  `fp_poly`; symbol stores a closed polyline (C12 of the click-tools plan stays).
- D2 Bezier in the footprint: kind `curve`, 4 points (start, c1, c2, end),
  written as `(fp_curve (pts (xy)(xy)(xy)(xy)) (stroke) (layer) (uuid))`;
  checks sample it into segments.
- D3 Text box: kind `textbox`, 2 corners + text, written as `fp_text_box`
  (exact KiCad 10 syntax to be taken from KiCad's own writer and verified with
  `kicad-cli fp export svg`), border on/off, margins default.
- D4 Dimensions: `radial` (centre + point on the circle + leader length),
  `center` (centre mark, two points), `leader` (arrow point, elbow, text).
  One «Quota» button with a drop-down for the five types (aligned, orthogonal,
  radial, centre, leader) like KiCad's; the last used type stays. Preview ink,
  checks ink and writer share one geometry function per type (as
  `dimension_ink_segments` does for aligned/orthogonal).
- D5 Per-layer defaults table `kicadgen/core/footprint_layers.py` (Qt-free):
  `DrawLayer(name, label_it, colour, width_mm, text_mm, text_thickness_mm,
  drawable, side)`: F.SilkS 0.12 / 1.0 / 0.15 (KLC F5.1), F.Fab 0.10 / 1.0 / 0.15
  (KLC F5.2), F.CrtYd 0.05 (KLC F5.3, not drawable: the generator writes it,
  user decision of 2026-11-06), Cmts.User 0.10 / 1.0 / 0.15 and Dwgs.User
  0.10 / 1.0 / 0.15 (KiCad's "Other layers" defaults). `USER_DRAW_LAYERS`,
  `USER_DRAW_WIDTH_MM`, the 0.05 literals in the generators and
  `KICAD_LAYER_COLORS` read from it. New drawings take the layer default; an
  explicit width typed in the panel overrides it for that layer until reset.
- D6 Check rule «tratto di serigrafia troppo sottile»: F.SilkS stroke under
  0.10 mm (common fab minimum printable silkscreen line, published threshold in
  `footprint_checks.py` with its source comment); `graphic_segments` learns
  curve, text box border and the new dimension inks.

### Out of scope (reported, not authorised — need a user decision)
- Back-side layers (B.SilkS, B.Fab...): every check and the generators are
  front-only; adding them needs flip semantics in the checks.
- Moving the footprint anchor (KiCad "Place footprint anchor"): the origin is
  chosen by each generator per KLC (pin 1 for THT headers, body centre for
  modules); the view-only drawing origin already covers "grid origin".
- Courtyard drawable from the palette: excluded by the user on 2026-11-06.
- Tables, images on layers, zones/keepouts, custom pad shapes from polygons.
- Vertex editing of an existing polyline/polygon (KiCad: drag a vertex handle,
  add/remove corners): candidate for a later phase.

## 2. Architecture and file ownership (disjoint, for parallel agents)

| Wave | Agent | Files it alone edits | Delivers |
|---|---|---|---|
| 1 | W1-core | `kicadgen/core/footprint.py`, new `kicadgen/core/footprint_layers.py`, `kicadgen/services/footprint_checks.py`, `kicadgen/generators/package.py`, `pin_header.py`, `castellated_module.py` (only the 0.05 literals) + tests `tests/test_footprint_layers_table.py`, `tests/test_footprint_new_kinds.py`, `tests/test_footprint_checks_new_kinds.py` | D2, D3, D4 writer/validator/transforms/bounds/ink, D5, D6, B6 |
| 1 | W1-snap | new `kicadgen/core/object_snap.py`, new `kicadgen/core/draw_constraints.py` + `tests/test_object_snap.py`, `tests/test_draw_constraints.py` | B1, A6 pure part |
| 1 | W1-tools | `kicadgen/gui/canvas_clicktools.py`, new `kicadgen/gui/canvas_tool_hints.py` + `tests/test_canvas_clicktools.py` (additions), `tests/test_canvas_tool_hints.py` | A4 texts, A5, A6 router state + Shift+Space, A8 contract, D1 spec semantics |
| 1 | W1-widgets | new `kicadgen/gui/canvas_toolpalette.py`, new `kicadgen/gui/canvas_properties.py`, `kicadgen/gui/canvas_measure.py` (only: export `anchor_marker` kinds for the snap marker if needed) + tests `tests/test_canvas_toolpalette.py`, `tests/test_canvas_properties.py`; a single keyword addition to `symbol_tab.glyph_icon` (`stroke` colour) | A1, C1, B4 marker helper |
| 2 | W2-footprint | `kicadgen/gui/main_window.py`, `kicadgen/gui/footprint_view.py`, `kicadgen/gui/workers.py` + footprint tests | A2, A3, A4-A8, B2, B4, B5, C2, D1-D4 GUI |
| 2 | W2-symbol | `kicadgen/gui/symbol_tab.py`, `kicadgen/generators/symbol_glyphs.py` + symbol tests | A1-A8, B3, B4, C3, D1 in the symbol |
| 3 | coordinator | `kicadgen/services/i18n.py` (all new strings, 4 languages, from the agents' reports), goldens regenerated on purpose, docs, HANDOFF, `.claude/CLAUDE.md` baseline | integration, full suite |
| 4 | reviewers (read-only) | — | adversarial review: `adversarial-reviewer` ×4 on disjoint scopes, `canvas-parity-reviewer`, `generality-auditor`, `kicad-library-verifier` on a component with every new kind |
| 5 | fixers | disjoint files per finding group | fixes, then suite |

Rules for every agent: never touch a file outside its list; no `git` writes;
new UI strings are NOT added to `i18n.py` by agents: each agent lists them in
its report with IT/EN/FR/DE; run only focused tests with its own
`--basetemp=.venv/claude-<agent>`; downloads (KiCad sources for syntax) only
into the session scratchpad, never into the repository (trap of commit 40b2f1d).

## 3. Contracts (what the tests must pin)

1. One user action = one undo step (draw, property commit, layer change).
2. Ghost and click agree: the snapped/constrained point shown while hovering is
   the point a click adds (`tool_snap` serves both).
3. Shift = free: no object snap, no angle constraint; grid rounding stays when
   the grid snap is on.
4. Object snap priority and reach are the same in both tabs; the marker kind
   says what was caught.
5. The angle mode is per tab, saved, cycled by Shift+Space while a tool is
   armed and by the panel control at any time.
6. Backspace removes exactly the last collected point and refreshes the ghost;
   with zero points it is a no-op (no disarm).
7. Writer: every new kind round-trips through `validate_footprint_drawings`
   unchanged; unknown keys are dropped; the S-expression is accepted by
   `kicad-cli fp export svg` (test skipped without KiCad) and the exported SVG
   contains the expected ink.
8. Checks: silk clearance and courtyard enclosure see curve, text box border
   and the new dimension inks; a silk stroke under the published minimum is a
   finding with a remedy.
9. Per-layer default width: a new F.Fab line is 0.10 mm, F.SilkS 0.12 mm, user
   layers 0.10 mm; a typed width wins until «Predefinito KLC».
10. Active layer: clicking a drawable layer row sets the combo and vice versa;
    non-drawable rows are not selectable; activating a hidden layer shows it.
11. Properties panel: programmatic refresh never commits; a commit equals the
    dialog's result for the same input (shared builders); mixed values are not
    written unless edited.
12. Hints: each armed tool shows the hint of its next step and updates after
    every collected point and Backspace.

13. Precedence (decided 2026-11-08 after the review): object snap within
    reach > angle constraint > grid. An object the user aims at wins over the
    45°/90° ray; Shift skips both. Pinned by a test with snap AND angle on.
14. No edge (perpendicular foot) snapping for drawing tools in either tab
    (coordinator decision 2026-11-08): a foot on every nearby stroke would
    pull almost every click off the grid. Body edges only feed intersections.
    `SnapIndex` keeps edge support for other callers.
15. The tool's own collected points are snap candidates except the LAST one
    (snapping onto it gives a zero-length segment refused as coincident); the
    first one closes a polygon. One shared classifier (`object_snap`).
16. Coordinates in the Properties panel and in the drawing dialog use the
    drawing origin with Y up, like the pad dialog and the pointer readout.
17. E with several pads (pins) selected opens the first one's dialog in both tabs.

Positive AND negative test for every rule (CLAUDE.md «Correzioni»): e.g. the
snap catches an endpoint within reach and does NOT catch it one pixel beyond;
the angle constraint snaps a 40° segment to 45° and leaves a 40° segment free
with Shift; the silk rule flags 0.08 mm and not 0.12 mm.

## 4. Deliberate differences between the tabs (kept, with reason)

- Symbol wire: own orthogonal mode with corner insertion and pin anchoring
  (wire semantics); shapes use the shared angle mode.
- Footprint: toasts for results, symbol: status line (click-tools plan §2);
  hints are shared.
- Layers exist only in the footprint (layer combo, active layer, tinted icons);
  symbol icons keep the theme colour.
- Polygon storage: `fp_poly` in the footprint, closed polyline in the symbol (C12).
- Free point modifier: Shift in both tabs (our long-standing symbol-wire
  convention), where KiCad uses Ctrl for a free angle and Shift to disable
  the grid. Kept on purpose for continuity; declared in `ClickToolHost`.
- Polygon tool spec: `ToolSpec('polygon', count=0, close_after=4, min_points=3)`
  (`close_after` counts the closing click, so 4 = three vertices + the click
  on the first one), the same rule the line already uses.
- Dimensions, text box, KLC layer widths: footprint only (the symbol has no
  dimensions in KiCad's symbol editor; its text box is out of scope here).

## 5. Fragile points to tell the user

- `fp_text_box` and the radial/centre/leader dimension syntax are taken from
  KiCad's writer; without KiCad installed the CLI test is skipped, so the
  syntax is verified only on this PC.
- Snap reach (10 px) and priority are UX choices, not physics; declared in code.
- The silk minimum width (0.10 mm) is a fab-capability convention, not KLC's
  0.12 mm recommendation: the rule flags under 0.10 and the default stays 0.12.
- Goldens of the click-tool and palette tests record button sets and hint
  strings: they will be regenerated deliberately and the diff reviewed.

## 6. Order of work and verification

1. HANDOFF section (done before code), this plan.
2. Wave 1 in parallel (4 agents), each with focused tests green.
3. Wave 2 in parallel (2 agents), integrating wave 1; parity tests updated on
   purpose, goldens regenerated only for intended changes.
4. Coordinator: translations (4 languages) into `i18n.py`, catalog tests,
   `tests/test_documentation.py`, then the full suite in two halves
   (`tools/run_test_halves.py both`), code frozen.
5. Adversarial review (wave 4), findings verified, fixes (wave 5), suite again.
6. Report to the user; commit only on explicit OK.
