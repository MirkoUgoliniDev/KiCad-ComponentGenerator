# Plan: shared measuring tool for the footprint preview and the symbol editor

Written 2026-10-31 by the implementation agent, on the feature confirmed by
the user: a professional MEASURING TOOL («Misura») in the footprint preview
and, with the same code, in the symbol editor (user rule: same UX in every
tab, reuse the code). Method of `.claude/skills/canvas-shared-change`:
inventory, parity tests first, one shared component, adoption in both tabs.
Line numbers refer to the working tree of 2026-10-31 (branch `ADVANCED`,
after `cd69363`); grep for the names.

## 1. Inventory

### Footprint preview (`kicadgen/gui/main_window.py` + `kicadgen/gui/footprint_view.py`)

- Toolbar: `_build_preview_tab` ~14858; the header row adds zoom-in,
  zoom-out, fit (`_view_tool_button`, ~18103), undo/redo, cancel/apply/
  recover, the pinout table, «Griglia», the grid-unit combo, the fine-grid
  combo.
- Click tools: the window is the `ClickToolHost` of the preview's
  `PointCollector` (`footprint_view.attach_host` ~205); host callbacks
  `tool_snap` ~15176, `tool_points_changed`, `tool_preview_changed`,
  `tool_refused`, `tool_finished`, `tool_armed`, `tool_disarmed`; draw tool
  wrappers `_toggle_footprint_draw_tool`, `_cancel_footprint_drawing`;
  read-only state `_fp_draw_tool`, `_fp_draw_clicks`.
- Scene scale: 20 scene px per mm (`mm_to_px = 20.0`, Y down, origin at the
  footprint origin). Three drawers rebuild the whole scene with
  `preview_scene.clear()` after `_forget_preview_items()`:
  `_draw_preview` (pin header, ~20006), `_draw_preview_castellated` (~19828),
  `_draw_preview_package` (~20171). Each has the built `Footprint` the file
  will hold (`build_pin_header`, `_built_footprint(...)`).
- Overall dimensions («totale 22.50 mm», pitch, row spacing, margin chains):
  `_add_dim_h` / `_add_dim_v` (~20702), yellow, 5 px ticks, label above or
  rotated along the line, scene-sized text.
- Screen-constant marker: module function `_screen_dot` (~1225).
- Framing: `_preview_framing_rect` (~19679) unites every non-grid item.
- Key handling: `FootprintView.keyPressEvent` ~815 (router first, then pan,
  band, `escape_idle`).
- Project switch: the project-state restore calls
  `_cancel_footprint_drawing()` (~8341); undo/redo, apply, discard cancel
  the armed tool too.

### Symbol editor (`kicadgen/gui/symbol_tab.py`)

- Toolbar: `icon_button` factory ~1239 (QAction + 32 px icon-only button,
  retranslated through `_icon_buttons`); the view group zoom/fit/grid ~1286.
- Click tools: the tab is the `ClickToolHost` (`_collector`, `_router`
  ~1276); callbacks `tool_armed` ~2719 … `tool_finished` ~2823;
  `_wire_points` / `_shape_points` / `_shape_tool` read the collector.
- Scene scale: `SCENE_UNITS_PER_MM = 10.0`, scene y = −symbol y (Y up in
  the model). Body rectangle centred on the origin (`symbol.width`,
  `symbol.height`); pins `symbol.pins[i].x / .y` = OUTER end, the
  connection point KiCad uses.
- `_draw` ~5646 removes every top-level item but the kept pins and body
  (`_clear_scene`) and calls `_forget_scene_items`.
- Key handling: `_SymbolView.keyPressEvent` ~909 (router first, placing,
  pan/band, clear selection).
- Draft replaced: `set_data`, `_restore_history`, `reset_layout` call
  `self._router.disarm()` (C13).

## 2. Existing functions reused or factored (user instruction 2026-10-31)

| Need | Existing code | Decision |
|---|---|---|
| Dimension line (line, ticks, label, colour) | `MainWindow._add_dim_h` / `_add_dim_v` (private) | **Factored** into `canvas_measure.add_dimension_line`; the two methods are now thin wrappers with the same items in the same order (pixel-identical: same item types, pens, positions; the parity test pins every item). The measure overlay draws with the same function and its own `DimensionStyle` (arrows, screen-constant label). |
| Screen-constant dot | module function `main_window._screen_dot` | **Factored** into `canvas_measure.screen_dot`; `_screen_dot` keeps its signature and only adds its role and z. |
| Click collection, Shift = free, Escape/Enter/right click, hide, pan precedence | `canvas_clicktools` (`ToolSpec`, `PointCollector`, `ClickToolRouter`) | **Reused** (`MEASURE_SPEC`, an open-ended persistent tool on each tab's existing collector, finished by `MeasureTool.points_changed` at two or three points). One router addition: `ClickToolRouter.cancel_tool()`, the router's own Escape cancel (press record forgotten, then `collector.cancel()`), now public so the measure's Escape ladder uses the same path (review D1). |
| Grid snap when no geometric target is near | footprint `_snapped_draw_point`; symbol `_grid_point` | **Removed from the measure path** (user feedback 2026-10-31: the first version fell through to the grid when a click on a pad's copper was more than 10 px from its centre). With no target in reach the point is free. |
| Pad copper outline (hit area) | `core.package_geometry.pad_outline` (rotated rectangle) and `_rotate_kicad` (KiCad pad rotation) | **Reused** by `canvas_measure.pad_polygon` (rect/roundrect/trapezoid/custom = `pad_outline`; circle; oval = stadium turned with `_rotate_kicad`). |
| Element context menu | `canvas_menus.build_element_menu`, `exec_element_menu`, `delete_entry` | **Reused** for «Rimuovi misura» (remove measure): same menu look, same test hook. |
| Toolbar buttons and icons | footprint `_view_tool_button`; symbol `icon_button`; `gui/assets/*.svg` | **Reused**; one new icon `measure-ruler.svg` in the style of the other assets (24×24, stroke `#cdd6f4`, width 1.7); «Cancella misure» uses `delete.svg`. |
| Point-to-segment projection | only `castellated_module._point_segment_distance` (private, distance only, generator module) | **New** Qt-free `segment_foot` / `line_foot` in `canvas_measure` (a GUI module must not import a generator's private helper; it also needs the foot point, not just the distance). `drawing_transform` has only rotations/mirrors. |
| Escape semantics | `ClickToolRouter.key` (Escape = cancel) | Reused; the measure tool adds one step BEFORE it (see §4, D-M3). |

## 3. Component API (`kicadgen/gui/canvas_measure.py`)

Pure, Qt-free (unit-testable without a scene):

```python
KIND_CENTRE, KIND_CORNER, KIND_EDGE, KIND_FREE, KIND_ALIGNED, KIND_PLACE   # anchor kinds
@dataclass(frozen=True) class MeasureAnchor: x, y, kind, label, edge, shape, axis ('x'|'y'|''), guides
@dataclass(frozen=True) class SnapTargets: centres ((x, y, label, hit polygon), ...), corners, edges   # shape boxes precomputed
def pad_polygon(x, y, size_x, size_y, shape, rotation_deg) / circle_polygon / point_in_polygon
def segment_foot / line_foot
def snap_point(x, y, targets, scale, *, free=False) -> MeasureAnchor        # first point
class GuideIndex(targets): nearest_x / nearest_y (sorted lists, bisect)    # built once per target change
def snap_second(x, y, targets, index, first, scale, *, free=False)        # second point, with guides
@dataclass(frozen=True) class Measurement: first, second, start, end, distance_mm, dx_mm, dy_mm, perpendicular, axis, extensions
def measure(first, second, units_per_mm)                  # true distance / perpendicular
def measure_linear(first, second, units_per_mm, place)    # horizontal or vertical dimension
def measure_points(points, units_per_mm)                  # the one the collected anchors describe
def measurement_label(m, translate)
```

Qt drawing and glue:

```python
DimensionStyle, add_dimension_line, DIMENSION_STYLE_HORIZONTAL / _VERTICAL, MEASURE_STYLE / _HIGHLIGHT_STYLE / _PREVIEW_STYLE
screen_dot, anchor_marker, bounds_without_measures, MEASURE_ITEM_ROLE
class MeasureOverlay: add / remove / clear / count / measurements / hovered / set_hovered / measurement_at /
                      sync / redraw / forget_items / show_transient / clear_transient / items / label
class MeasureTool: snap / points_changed / preview_changed / finished / disarmed / escape / tag /
                   hover_measure / remove_hovered / context_menu / clear / sync / forget_items; .router
MEASURE_KEY = "measure"; MEASURE_SPEC = ToolSpec("measure", count=0, persistent=True)
```

Hosts dispatch their existing `ClickToolHost` callbacks on
`spec.key == MEASURE_KEY` to the `MeasureTool` and only build the targets;
their views call `escape` (before the router's keys), `context_menu` (right
press), `hover_measure` (button-less move, leave) and their Delete shortcut
calls `remove_hovered` first.

## 4. Contract and decisions

- D-M1 Snap targets. Footprint: pad centres = `Pad.at` of the BUILT
  footprint (what the .kicad_mod holds; label = pad number), corners and
  edges of the F.Fab body outline (`FpRect` → 4 corners / 4 edges, `FpLine`
  → 2 end points / 1 edge). Symbol: pin connection points (outer end,
  label = pin number), corners and edges of the body rectangle.
  CHANGED 2026-10-31 (user feedback «il metro deve intercettare il centro
  del pad»): every centre carries a HIT SHAPE — the pad's copper outline
  (`pad_polygon` from the pad's shape, size and rotation) or a pin end
  circle of `PIN_MEASURE_HIT_MM` (0.635 mm, a quarter of the pin grid, so
  neighbours never overlap). A point INSIDE a shape takes that centre
  (nearest centre if several contain it); only when no shape contains it
  does a shape whose outline is within `PAD_MARGIN_PX` (6 screen px) take
  it (second review, finding 3: on a QFN exposed pad a 0.8 x 0.3 mm lead
  0.25 mm away used to win). Centres win over everything. Else a body corner within `CORNER_PX` (12 px), else an
  edge foot within `EDGE_PX` (10 px), else a FREE point — no grid fallback
  any more. Shift forces a free point. Screen reaches are divided by the
  view's current scale.
- D-M2 Measurement. Straight distance between the two anchors, ΔX and ΔY
  as absolute values in mm (the symbol scene is Y-flipped; magnitudes read
  the same in both tabs). When the SECOND anchor is an edge, the first point
  is projected perpendicularly on that edge's LINE and the foot is the end
  point (perpendicular distance, label prefixed «⊥»); symmetrically when only
  the FIRST is an edge. Two edges: perpendicular when parallel (body width),
  else straight between the feet. Two decimals.
- D-M3 Escape ladder (documented choice): with a first point pending,
  Escape drops it and the tool stays armed; armed with no pending point and
  measures shown, Escape clears the measures and the tool stays armed;
  armed, idle, no measures: Escape disarms (the router's own rule). A pan in
  progress takes Escape first (router rule C8). Right click and Enter
  disarm (router finish with fewer than 2 points), the pending point is
  dropped, the measures stay. Hide (tab switch) disarms (C7) and keeps the
  measures. Every cancel of the ladder goes through the router
  (`ClickToolRouter.cancel_tool()`, the same path as the router's own
  Escape), so a press recorded before an Escape adds no point at its release
  (review 2026-10-31, D1: Esc during a press-drag used to add the release
  point quietly).
- D-M4 Persistence. Measures persist across redraws of the SAME geometry
  (language change, grid change, hover rebuilds, a redraw after an edit
  elsewhere). They are dropped when the snap targets change (the footprint
  or symbol was regenerated or edited: a measure of the old geometry would
  lie), on a project switch (`MainWindow._apply_project_state` clears the
  measures of BOTH tabs), and by «Cancella misure» (clear measures). A symbol
  reload (`SymbolTab.set_data`) does not clear them by itself: the same
  geometry keeps them, another drops them through the targets (review
  2026-10-31, A1, aligned on the footprint). When the targets change, a
  pending first point is dropped too and the tool stays armed (A3).
  Decision a (second review, 2026-10-31): a transiently INVALID draft
  (nothing can be built: footprint parameters that do not validate, a
  header that does not build, a symbol draft with an invalid row) does NOT
  drop the measures, the targets or a pending point — as the wire and
  shape tools keep their points — (`MeasureTool.sync(None)`); they are
  dropped only when a valid new geometry has different targets.
  Decision b: a package whose footprint does not build but whose pads are
  drawn takes its snap targets from the drawn pads and body outline
  (`MainWindow._package_measure_targets`), so measuring still works.
- D-M5 Overlay only. Measures never enter the draft, the undo history, the
  recovery file, the project file or the generated KiCad files. Items are
  not selectable, movable or focusable, accept no mouse button, carry no
  hit-test role of either tab, and never frame a camera: the footprint's
  `_preview_framing_rect` and the symbol's scene rect (`_draw`,
  `_settle_after_gesture`, through `bounds_without_measures`) skip items with
  `MEASURE_ITEM_ROLE` (review 2026-10-31, D2).
- D-M6 Look. Teal `#94e2d5` (Catppuccin Mocha teal: distinct from the blue
  pads and the green handles, readable on the dark base); cosmetic 1.4 px
  line, 9 px screen arrowheads at both ends, a screen-constant label (dark
  box, teal text) centred on the line and offset to its side, z 60 (above
  pads, texts and drawing markers).
  CHANGED 2026-11-03: the preview's automatic dimensions are no longer
  yellow. They use the same teal (`canvas_measure.DIMENSION_COLOR =
  MEASURE_COLOR`), because yellow is almost KiCad's silkscreen colour and
  read as printed silkscreen (user: «non deve essere stampato sul pcb ... ad
  uso interno», it must not look printed on the PCB, it is for internal
  use). Every on-screen-only measurement now shares one colour, which must
  never equal a KiCad layer colour. Measures and dimensions stay told apart
  by their shape (arrowheads and boxed label vs. ticks and bare label), not
  by colour.
- D-M7 The tool stays armed after each measure (`persistent`);
  a press-move-release measures from the press to the release (C1 rule).
  A double click finishes like any click tool (point, then finish): with
  one point it disarms. Arming a draw/wire/shape tool disarms the measure
  (one armed tool at a time); the footprint's undo/redo, apply and discard
  disarm it as they disarm every click tool (C13); the measures themselves
  follow D-M4.
- D-M8 A repeated anchor (the same pad clicked twice) is refused like a
  repeated drawing point: toast in the footprint, status line in the
  symbol, with «Scegli un altro punto da misurare.» (choose another point).
- D-M9 Labels are composed when the measures are drawn: a language change
  shows in the next redraw of the canvas (both tabs redraw on a language
  change: the preview through its drawers, the symbol through
  `retranslate` -> `_changed`).

- D-M10 Linear dimensions (user, 2026-10-31: «selezionare il centro di
  un pad e poi, quando mi sposto verso destra e intercetto la verticale del
  pad successivo, lo deve evidenziare — per fare le quote in modo
  professionale»). Interaction model:
  1. First click: a centre (anywhere on its copper), a corner, an edge or a
     free point.
  2. Moving to the second point, the target under the pointer is
     highlighted. With no target hit, ALIGNMENT GUIDES: the pointer's X
     within `GUIDE_PX` (8 px) of a centre's or corner's X snaps X to it, a
     dashed vertical guide runs through that target and the pad is
     highlighted; likewise Y with a horizontal guide; both at once take the
     crossing. The first anchor's own X / Y is never a guide (a zero-length
     dimension; the guide lookup walks past a run of such coordinates,
     finding 4). Order for the second point: centre or corner hit > edge
     foot (the perpendicular case) > guides > free.
  3. A second point taken through a vertical guide gives a HORIZONTAL
     dimension (|ΔX|), through a horizontal guide a VERTICAL one (|ΔY|). A
     second centre or corner hit is linear when one delta is at least
     `LINEAR_DOMINANCE` (3) times the other, else it is the true distance.
  4. A linear dimension waits for a THIRD click that places its line (Y of
     a horizontal, X of a vertical) with a live preview; it is drawn
     datasheet style: two extension lines (gap `EXTENSION_GAP_MM` at the
     anchor, overshoot `EXTENSION_OVERSHOOT_MM` past the line), the
     dimension line with arrows (shared `add_dimension_line`), the value
     «12.70 mm». Escape at that stage cancels that dimension; right click
     or Enter commit it placed through the first anchor.
  5. Any other second point (free diagonal, two targets with no dominant
     delta) finishes a two-click true distance with ΔX / ΔY; centre ->
     edge stays the perpendicular distance (D-M2), drawn from the centre to
     the foot, which is perpendicular to that edge.
  The spec is open-ended (`count=0`): `MeasureTool.points_changed`
  finishes the collector at two points (non-linear) or three (linear), a
  call the collector contract allows from that callback.
- D-M11 Hover feedback: the hovered target is highlighted (pad or pin hit
  shape outlined and lightly filled, edge segment thickened, corner square)
  with a screen-constant tag «Pad 11 · centro» (symbol «Pin 3 ·
  connessione»), «Spigolo» (corner), «Bordo» (edge), «Allineato: Pad 2»
  (aligned), «Punto libero» (free point). After the first click the dashed
  preview dimension follows every snapped move.
- D-M12 Removal of one measure (user, 2026-10-31: «deve essere possibile
  rimuoverlo dopo averlo tracciato»): hovering a measure (within
  `REMOVE_TOLERANCE_PX` of its dimension or extension lines, or on its
  label box) highlights it (`MEASURE_HIGHLIGHT_STYLE`); Delete removes the
  hovered measure before the tab's own Delete; a right press on a measure
  opens the shared element menu with «Rimuovi misura» and never the pad,
  pin or element menu underneath. Hit test: `MeasureOverlay.measurement_at`,
  the overlay's own (its items still accept no mouse button).
  Second review (2026-10-31): the measure menu opens only when no click
  tool has collected points (finding 1: the right press belongs to the
  router, which finishes the line tool, the wire or the measure's own
  placement); not during a rubber band in either tab (finding 2); Delete
  looks up the measure under the cursor at the key press, so a zoom or a
  pan since the last hover never removes a measure no longer under the
  pointer (finding 5); «Cancella misure» also drops a pending point
  (finding 6).
- Cost: the hit shapes' bounding boxes and the guide index (sorted X / Y
  lists) are built when the targets change, never per move; a move does a
  box pre-filter, a polygon test on the few pads in reach and two bisects.
  The symbol caches its targets by pin positions and body size, so a
  redraw that moves nothing rebuilds no pin circle.

## 5. Deliberate per-tab differences

| Difference | Reason |
|---|---|
| Scene units per mm: footprint 20, symbol 10 (Y flipped) | Each scene's own scale; the tool takes the factor from the host and always shows mm. |
| Targets: pads + F.Fab outline vs pins + body rectangle | The objects each tab draws. |
| Centre hit shape: pad copper outline vs pin end circle (0.635 mm) | What each tab connects to: a pad's copper, a pin's connection point. |
| Centre tag: «Pad N · centro» vs «Pin N · connessione» | The word each tab uses for the object. |
| Refusal feedback: footprint toast, symbol status line | Same as the existing click tools (decision C3 of the click-tools plan). |
| Hint on arm: symbol status line only | The footprint preview has no status line (as for the draw tools). |

Inherited behaviours (review 2026-10-31, A2), not new differences: they come
from how each tab already treats every click tool, and change with those
tools if the click-tools plan unifies them.

| Behaviour | Footprint | Symbol |
|---|---|---|
| «Applica» / «Annulla» (discard) of the draft | disarm the tool (`_cancel_footprint_drawing`, as for the palette tools) | do not disarm it |
| Arming with nothing drawn | arms; every point is free (no targets) | refuses to arm without a symbol (as `start_wire` / `start_shape`) |

## 6. Tests

- `tests/test_footprint_measure_parity.py`, `tests/test_symbol_measure_parity.py`:
  written BEFORE the change against the current code: the preview's
  dimension items (type, geometry, text, rotation, colour, order), a draw
  tool commit, Escape idle, the symbol wire/shape tools; then the measure
  tool in each tab through real events: snap to pads / pins, perpendicular
  to a body edge, Shift, persistence, Escape ladder, Esc during a
  press-drag (D1), framing with and without measures (D2), reload and
  project switch (A1), pending point on a geometry change (A3), not in the
  undo history, the draft or the generated KiCad text; linear dimensions
  through the guides (horizontal and vertical), placement, right click and
  Enter commit at placement, Escape at placement, free diagonal; removal by
  menu and by Delete, Delete with nothing hovered reaching the tab's own
  handler, Delete after a pan; a right click over a measure finishing the
  line tool / the wire; the rubber band; hide; «Cancella misure» with a
  pending point; a transiently invalid draft; the package build fallback.
- `tests/test_canvas_measure.py`: pad hit areas (inside beats a neighbour's
  margin), corner and edge reaches, no grid fallback, Shift free, pad
  polygons, guides (including a run of excluded coordinates), dominance,
  linear and perpendicular values, labels, the overlay (add, remove, hover,
  hit test, sync, hover feedback) and the glue's two- and three-click flows,
  Escape ladder and removal; negative twins.

## Changes of 2026-11-02 (user: edit the measured value, pads dragged)

- D-M4 CHANGED. Measures no longer vanish when the geometry changes: each
  is taken again between the same targets where they are now
  (`reanchor_measurement`: a centre, corner or edge is found by its position
  in the old targets and taken at the same index in the new ones, with the
  same count and, for a centre, the same label). The value is recomputed,
  so a measure never shows the old number on new geometry. A measure with
  an aligned or free end, or whose target is gone, is still dropped.
- «Modifica quota…» (right-click menu, or a double click on the measure):
  the host's `on_edit` asks a value and moves ONE pad along the measured
  direction (`edit_displacement`): the second clicked centre (the first is
  the reference), else the only centre; a linear dimension changes only its
  axis; a centre-to-edge measure moves the centre perpendicular to the edge,
  never the edge. A measure without a centre is not editable (the entry is
  greyed out).
- Deliberate tab difference: only the footprint preview passes `on_edit`
  (package pads move freely). The symbol editor's pins sit on the 2.54 mm
  pin grid, where a typed arbitrary distance has no valid result; its
  measures stay read-only until the user decides otherwise.

## Editable dimensions (2026-11-03)

- The two body sides of a package preview (`MainWindow.BODY_DIM_X`, the
  horizontal side, and `BODY_DIM_Y`, the vertical one) are EDITABLE
  dimensions: `_add_dim_h` / `_add_dim_v` pass an `edit_key` to the shared
  `canvas_measure.add_dimension_line`, which tags every item of the
  dimension with `DIMENSION_EDIT_ROLE` (value: the key). Computed
  dimensions (pitch, row spacing, margin chains, totals) carry no key and
  stay read-only.
- A double click on such a dimension emits
  `FootprintView.dimension_edit_requested(key)`; a right click emits
  `dimension_context_requested(key, global_pos)`, which opens the shared
  element menu (`canvas_menus`) with the same «Modifica quota…» entry as a
  measure. Both end in `MainWindow._edit_body_dimension`: the typed value
  becomes that body size, the body stays centred, the pads keep their
  coordinates, one undo step; a body that no longer builds or a size out
  of range is refused with a toast and changes nothing. Only orientations
  of 0/90/180/270 degrees are editable (`_body_dimensions_editable`).
- Reach and feedback are those of a measure: `editable_dimension_at` uses
  the measures' screen tolerance (REMOVE_TOLERANCE_PX), and a hovered
  dimension is painted in MEASURE_HIGHLIGHT_COLOR
  (`set_dimension_highlight`), so the user sees which teal numbers answer.
- Priority: the dimension is looked up after the measuring tool (the click
  router handles a measure first) and, in the event order, after handles,
  texts, drawings and pads, so a Value text or a pad lying over a body
  number keeps its own double click and menu (review 2026-11-03, C1); it is
  never taken during a running pan or rubber band (C2). With nothing of the
  footprint under the pointer the background menu opens instead.
- Deliberate tab difference: the symbol editor draws no editable
  dimensions.
