# Canvas hit test: outline hit (H1), closing handle (H2), one precedence

Shared by the symbol editor (`kicadgen/gui/symbol_tab.py`) and the footprint
preview (`kicadgen/gui/footprint_view.py` + `kicadgen/gui/main_window.py`)
through `kicadgen/gui/canvas_hit.py` and `kicadgen/core/vertex_edit.py`.
User decisions of 2026-11-09 («Sì, come KiCad», «Sì, una maniglia sola»),
coordinator decisions of the same evening after the parity and adversarial
reviews.

## H1: unfilled shapes are hit on their outline

KiCad's source of truth is `EDA_SHAPE::hitTest` (common/eda_shape.cpp): an
UNFILLED circle is hit by `abs(radius - dist) <= maxdist`, an unfilled
rectangle by its four sides, an unfilled polygon by `CollideEdge`, a segment
or a Bézier by `TestSegmentHit`; a FILLED shape also inside; `maxdist =
width / 2 + accuracy`.

Qt's default `QGraphicsItem.shape()` of a rectangle, ellipse, polygon or path
holds the interior, and for an OPEN path also the area closed by its chord.
`canvas_hit` replaces it:

- `OutlineRectItem`, `OutlineEllipseItem`, `OutlinePolygonItem`,
  `OutlinePathItem` (`OutlineHitMixin`): `shape()` is the outline stroked at
  the pen width (the `width / 2` term, round caps and joins), united with the
  interior only after `set_interior_hit(True)`. The interior is DECLARED by
  the record's fill, never inferred from the brush. Pens must be non-cosmetic
  (the stroke is in item units).
- The `accuracy` term is the query margin in screen pixels,
  `canvas_menus.CONTEXT_HIT_MARGIN_PX` (4 px), applied by `items_near` /
  `pick`, never baked into the shape (a shape cannot depend on the zoom
  without invalidating the scene index).
- Footprint: rectangles, polygons, circles, arcs, curves, the keepout zone
  (`ZoneOutlineItem`, never an interior) and the editable body outline (its
  stroke moves the body, `_body_part_at`). Lines are segment items (their Qt
  shape is already the stroke); text boxes stay hit inside (KiCad's
  `PCB_TEXTBOX::HitTest` is the box).
- Symbol: the primitives of user-drawn shapes (`shape:*`) and of wires.

### Glyph exception (coordinator decision, 2026-11-09)

A library glyph (resistor, capacitor, motor, a symbol loaded from a KiCad
library) is a placed SYMBOL, not a drawn shape: KiCad selects a symbol by its
body. Its primitives keep the hit they always had, the interior included
(`interior` declared for every primitive of a record that is neither
`is_shape` nor `is_wire`). The split is the record's kind, never its size or
name. Tests: `test_a_library_glyph_is_selected_and_dragged_by_a_press_inside_its_unfilled_body`
(positive: resistor rectangle, motor circle; negative: a drawn empty
rectangle lets the press through).

## One precedence for both tabs and both buttons

`canvas_hit.pick(view, pos, classify, margin)`: the items EXACTLY under the
point first; only when none is a target, the items within the margin. Inside
a pass the lowest rank wins, ties by z order (topmost):

| rank | symbol (`data(0)`) | footprint (`_classify`) |
|---|---|---|
| `RANK_HANDLE` (0) | `vertex`, `shape_point`, `resize` | drawing handle, visible body resize handle |
| `RANK_TEXT` (1) | `property` (Reference/Value, its anchor) | Reference/Value text |
| `RANK_PIN` (2) | `pin` | package pad (left/right), pin-header pad (right click only) |
| `RANK_STROKE` (3) | `graphic` | drawing |

Texts rank above pins and pads (coordinator decision after the verification,
D2, 2026-11-09): a text is the small target laid ON TOP of the component.
With pads first, a Value moved over a 5 x 5 mm pad could be neither dragged
nor reset («Posizione predefinita»), because every pixel of its glyphs was
also the pad's. A pin or pad with no text over it still beats a stroke.

The same pick serves the left press (`press_target`), the right click
(`_context_target` / `menu_target`), the Ctrl+click toggle (texts left out; a
handle toggles its element) and the hover feedback. The footprint keeps two
extra rules: a body resize handle over a pad whose COPPER is exactly under
the pointer is dropped and the pick runs again without body handles (so the
pad, or a text or drawing handle over it, gets the press, every button), and
after the pick come the body outline's stroke, an editable dimension, then
the background. The left press goes down the ranks: a text first when one
lies under the pointer, then a pad, then a stroke; a gesture the host refuses
(`gesture_begin` returns None, e.g. a pad of a part mounted on a pin header)
hands the press to the next kind.

CONTRACTS CHANGED: the symbol's pins and texts get the margin on a left press
(only strokes had it); the footprint's right click finds pads within the
margin (it was exact only); texts rank above pins and pads in both tabs (D2;
before, the pads and pins won); a selected wire's end handle sitting on a pin
end now opens the wire's menu (the handle ranks above the pin, as the left
press already took it).

### The E key (verification D4)

E with nothing selected edits what a left press at the pointer would take:
the symbol's `_hovered_graphic` and the footprint's
`FootprintView.drawing_under_pointer`. Both read the pointer from ONE shared
`canvas_hit.PointerTracker` per view (`view.pointer`; set on each mouse move,
cleared on leave), never a stored scene point: the cursor moves neither when
the camera does nor when the layout moves or resizes the viewport (a side
panel expanding), so the tracker keeps the move as a SCREEN point and maps it
through the viewport's CURRENT geometry and then the CURRENT camera at query
time. After an auto-fit, a scroll or a programmatic zoom without a mouse move
the answer is what lies under the cursor now (kept in scene units, a second
Delete after the auto-fit deleted a drawing the refit had carried away from
the cursor, verification 2026-11-09; the symbol had the same defect for E,
review 2026-11-09 point 1). When the last move was a real one (the platform
cursor stood on its point) and the viewport is still under the mouse, the
platform cursor (`QCursor.pos()`) is read instead; a synthetic move from a
test does not match it, so its stored point stays the answer. A wheel zoom
anchored under the mouse keeps the same scene point under the cursor, so it
keeps the same answer. The footprint used to read the hovered drawing that
reveals the handles, kept through its bounding rectangle plus a hysteresis,
so E inside an empty outline opened it. The handle reveal is unchanged.
The footprint's Delete, R / Shift+R, Ctrl+D, Ctrl+C and the arrows with
nothing selected (`MainWindow._fp_targets`) take the same target, so inside
an empty outline they act on nothing and inside a filled one on the shape
(same class as D4, fixed 2026-11-09).

With nothing selected, the first arrow nudge of the drawing under the pointer
also SELECTS it (`MainWindow._nudge_footprint_drawings`), as KiCad acts on
the item under the cursor and then keeps it: a held arrow moves the drawing
every step instead of stopping when it slides out from under the pointer
(before: a filled 10 x 8 mm rectangle stopped after 6 steps, a stroke after
1, a shape-dependent stop). One undo step per nudge, as before; with a
selection the arrows move the selection and ignore the pointer. The drawing
is selected only when the nudge moved it: a nudge the validation refuses
leaves the selection as it was (review 2026-11-09, point 2).

### A camera change re-reads the pointer (review 2026-11-09, points 4 and 5)

A camera change moves the scene under a still pointer. On `camera_changed`
both tabs re-read the pointer from the tracker: the coordinate line shows the
point under the cursor now (`FootprintView.pointer_scene_point`,
`_SymbolView.pointer_scene_point`; with the pointer outside the view the line
keeps its last point, as on leave), and the hover runs again at that point
(`FootprintView.refresh_hover`, `SymbolTab._on_camera_changed`), so the
handles revealed for a drawing the camera carried away are hidden: a handle
is the first rank of the press pick and could otherwise take E, Delete or a
press.

A camera change is a zoom, a fit that ran, or a programmatic scroll: the
shared `Camera.scroll_by` emits `changed` (`MainWindow._scroll_preview_mm`,
the body commit's and the undo's re-centring, goes through it; before, it set
the scroll bars directly and nothing re-read the pointer, VER5 B). A pointer
pan emits nothing: it owns the pointer, and its own moves refresh what
follows it. The symbol editor makes no programmatic scroll.

### One refresh rule for both tabs (coordinator decision, VER5 C)

On a camera change the readout's POINT, the hover handles, the tool preview
and the reach cursor (`ReachFeedback.refresh`: the cursor and an open tooltip
of what a press takes now, VER5 E) follow the pointer only in an IDLE view
(`is_idle` of both views: no tool armed, placement, pan, drag or band). With
a tool armed the tool's preview follows its next real move, as the
footprint already did; the readout line is only rewritten with its last
point (its scale-dependent precision). CONTRACT CHANGED: the symbol editor
refreshed all of them in every state (`SymbolTab._on_camera_changed`).

### Never through a hidden layer (VER5 A)

The footprint's auto-fit runs inside `MainWindow._draw_preview`, BEFORE
`_after_preview_drawn` hides the items of the layers switched off in the
«Layer» panel. The redraw is bracketed by `FootprintView.begin_scene_update`
/ `end_scene_update`: a camera change inside it (the public
`camera_changed`, then the idle hover) waits and runs ONCE on the complete
scene. Before, the hover ran mid-draw on the still-visible items, revealed
the handles of a drawing on a hidden layer, and Delete with nothing selected
removed that invisible drawing. Independently of the timing: a drawing on a
hidden layer shows no handles (`_refresh_drawing_handles` reads the layer
preference, `_fp_drawing_layer_shown`); hiding the hovered drawing's layer
drops its handles and the hover (`_apply_preview_layer_visibility`); the
hover's hysteresis keeps only a drawing still shown; and
`drawing_under_pointer` / `_fp_targets` never answer a drawing whose items
are all hidden (`FootprintView.drawing_shown`). Qt's own item query already
skips hidden strokes.

### One pointer per key press (VER5 D)

`FootprintView.refresh_body_hover` and the measure's Delete
(`canvas_measure.MeasureTool._index_under_cursor`) read the view's
`PointerTracker`, as E and the keyboard fallback, never `QCursor.pos()`
directly (a bare view without a tracker, in the measure's unit tests, still
reads the platform cursor).

### Accepted trade-off: a pad wholly inside a moved text's box

Texts rank above pads and pins (D2) and are hit on their box, as KiCad's
`EDA_TEXT::TextHitTest` uses the text box. So a small pad or pin lying WHOLLY
inside the box of a Reference/Value moved over it cannot be reached by a
press, the right-click menu or the hover. KiCad answers the overlap with a
disambiguation menu ("clarify selection"); we do not have one. Workarounds:
Ctrl+click (texts are left out of the toggle) or a rubber band selects the
pad, then E or the arrows act on it; or move the text away first. Accepted by
the coordinator (verification 2026-11-09), not a defect to fix now.

## Hover feedback follows the press reach

`canvas_hit.ReachFeedback`: in an idle view (no armed tool, placement, pan,
drag or band) the cursor and the tooltip are those of the item `pick` would
take, so three pixels off a thin stroke the move cursor and the tooltip show
where the press takes it; beyond the margin the arrow comes back and Qt's own
tooltip query finds nothing. A cursor set there is undone before the next base
mouse move, so `QGraphicsView` never stores it as the "original" cursor of an
item. Tooltips through each view's `viewportEvent` (`reach_tooltip`).

`ReachFeedback.release` undoes that cursor when the view stops being idle:
each view's `cancel_interaction` (which also runs before a glyph placement
starts and before a click tool is armed) and the start of a drag. Without it
the move cursor stayed through a whole glyph placement (verification D3).

The footprint's hover follows the press's ladder, refusals included
(`FootprintView._press_pick`, the host's side-effect-free
`gesture_accepts`): with pads that cannot move, a drawn line crossing a pad
shows ITS cursor and tooltip, because the press takes it (verification D5).
A pad with nothing else under the pointer keeps Qt's own tooltip (its pin
name), as it has a menu.

## Cursor while a tool is armed (plan C10)

`canvas_clicktools._ToolCursorKeeper`: the cross in both tabs over any item
while a tool is armed; the closed hand while a pan runs, armed or not.

## H2: one handle at the closing point

`vertex_edit.has_closing_repeat` / `handle_points` / `move_handle_point`: a
closed outline whose stored points repeat the first one at the end shows ONE
handle there, and dragging it moves the first and the last point together.
The symbol decides "closed" with ONE predicate, `symbol_glyphs._shape_closed`
(a polygon always, a line when it ends on its first point with 3+ vertices),
for the handles, the handle drag and the vertex add/remove alike, so a
polygon stored without its closing repeat is still closed for all of them.
The footprint's line follows `is_closed_outline`; its polygon stores no
repeat (one handle per corner).

## Tests

`tests/test_canvas_hit.py` (shapes, pick), `tests/test_canvas_clicktools.py`
(keeper), and the twins `tests/test_symbol_outline_hit.py` /
`tests/test_footprint_outline_hit.py`: inside vs outline, filled, margin near
and far with the real cursor and tooltip, open L / arc / curve chord
negatives, right click / Ctrl+click / double click inside, H2 closed and open
outlines, the cross with a tool armed and the pan's hand, and the precedence
(a pin/pad 2 px away beats a stroke 3 px away; a stroke exactly under the
pointer beats a pin/pad within the margin), each for the left press and the
right click; a text over a pin/pad takes the press, the right click and the
hover, a pin/pad without a text over it beats a stroke crossing it (D2); the
reach cursor undone by a glyph placement and by `cancel_interaction` (D3);
E on a stroke and inside an empty outline (D4); the hover of an unmovable pad
(D5). `tests/test_review_1029_canvas.py` presses on a drawn rectangle's
stroke since H1 (its interior is the background). VER5:
`tests/test_footprint_camera_refresh.py` and its twin
`tests/test_symbol_camera_refresh.py` (hidden layer through a refit redraw,
the deferral, hiding a layer with handles revealed, the hidden-items
fallback, the programmatic scroll, the idle rule with a tool armed, the body
hover's pointer, the reach cursor after a zoom), with unit tests of
`Camera.scroll_by`, `ReachFeedback.refresh` and the tracker's live branch in
`tests/test_canvas_camera.py` / `tests/test_canvas_hit.py`, and the measure's
Delete through the tracker in `tests/test_canvas_measure.py`.
