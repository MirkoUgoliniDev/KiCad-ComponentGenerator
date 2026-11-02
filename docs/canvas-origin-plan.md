# Drawing origin of the canvases

User request, 2026-11-02 (relay T7NS5D4-12 in the footprint preview):
«per un componente come relè sarebbe interessante avere il riferimento
coordinate qui» (pointing at the bottom-left corner of the body), then
«mi basta come riferimento per il disegno» — a reference for the drawing is
enough, the KiCad file keeps its own origin.

## Contract

- A drawing origin is a **view aid**. It never changes the `.kicad_mod`, is
  not a footprint-draft edit (no pending state, no undo step, no «Applica»)
  and is saved with the project at once (`footprint_drawing_origin`, like the
  grid unit is saved in the configuration). User decision of 2026-11-02:
  putting it in the draft would mark the footprint as not applied and block
  the installation for a change that does not touch the file.
- Coordinates read from the origin with **X to the right and Y upwards**, as a
  datasheet dimensioned from a body corner (user OK, 2026-11-02). KiCad's
  scene Y points down: `canvas_origin.to_drawing_mm` / `from_drawing_mm` are
  the only conversion.
- The origin is stored as a **key**, never as coordinates: `origin` (the
  footprint's own (0, 0), default), `center` (body centre when it differs),
  `corner:<i>` (the drawn body outline, turned with the body), `pad:<number>`.
  Every preview drawer (package, castellated module, pin-header matrix)
  re-registers the points (`MainWindow._set_footprint_origin_points`), so the
  origin follows the body and the pads. A key the drawing lacks falls back to
  `origin` for display and is kept, so it comes back with its point.
- Corners are named by where the user sees them from the body centre (screen
  Y down): «spigolo in basso a sinistra» etc.; a corner straight above the
  centre of a turned body is «spigolo in alto».
- The marker is painted by the view in `drawForeground` (never a scene item:
  the scene is cleared at every redraw and an overlay must not enter the
  framing or the hit tests). The readout is a `QLabel` child of the viewport.
- The grid passes through the chosen origin; with the default origin it keeps
  the drawer's own origin (first pad, text grid), as before.

## Where the user acts

- Right click on the background (nothing of the footprint under the
  pointer): «Imposta origine di disegno: <nearest point>» and «Ripristina
  origine del footprint». The nearest point is named before it is chosen.
- Right click on a pad: unchanged, the pad dialog; it now shows «Posizione X/Y»
  from the drawing origin (editable for package pads, one undo step together
  with shape and size) and «Usa questo pad come origine di disegno». A pad that
  is the origin has its position fields disabled (it is at 0, 0 by definition).
- With a package origin other than `body_center` the body centre is estimated
  from the pad field (`PackageGeometry.body_frame_offset`, declared heuristic):
  a typed position measured from a body point can re-centre the drawing. The
  window says so with a toast; distances between pads stay exact.

## Shared component and per-tab differences

`kicadgen/gui/canvas_origin.py` holds the conversions, `nearest_point`, the
marker painter and `readout_text`; it has no model and no translated text.

The pointer line is the symbol editor's, which existed before this work
(label UNDER the canvas, «X … Y … mm  |  zoom%», Y up, last point kept when the
pointer leaves, refreshed on every camera change). Both tabs now write it with
`readout_text`; the footprint view emits the same `pointer_changed`,
`pointer_left` and `camera_changed` signals as the symbol view. The footprint
line adds «da: <origin>». A first version put an overlay label inside the
viewport: the parity review of 2026-11-02 found it scrolled away with every
pan (a viewport child moves with `scrollContentsBy`) and duplicated the
symbol editor's readout, so it was replaced.

| Behaviour | Footprint preview | Symbol editor | Reason |
|---|---|---|---|
| Drawing origin choice | yes | no | The symbol has no body or pad to measure from; its origin is KiCad's own anchor. |
| Background right click | origin menu | nothing | Follows from the row above. |
| Pointer line | shared format + «da: <origin>» | shared format + pin details | Same function, each tab appends its own context. |

## Consequences to know

- The grid follows a chosen origin, so the SNAP of later drags (texts,
  drawings, pads) follows it too: the origin never writes the file, but it
  decides on which lattice the next edits land. Wanted for a datasheet
  dimensioned from that point; «Ripristina origine del footprint» gives the
  drawer's grid back.
- A key the drawing lacks (`_resolved_drawing_origin`) behaves exactly as the
  default, grid included.
- Pads sharing a number (split lands) get `pad:<n>`, `pad:<n>#2`, ...
- Only package pads have the position fields and the origin box in their
  dialog; pin-header pads (on their pitch grid) and castellated contacts (no
  pad dialog) are chosen as origin from the background menu, right-clicking
  next to them.

## Tests

`tests/test_canvas_origin.py` (conversions, nearest point),
`tests/test_footprint_drawing_origin.py` (marker, grid, readout, names on a
turned body, fallback of a missing pad, background menu vs pad click,
persistence, pad dialog exact landing through bottom view + 30° + mirror, the
re-centring toast with an estimated body centre).
