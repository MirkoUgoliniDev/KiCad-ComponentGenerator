# Plan: shared camera and grid for the symbol editor and the footprint preview

Phase C of `docs/canvas-gestures-plan.md`, widened on 2026-10-17 after a check
of the code: besides the camera (zoom, wheel, pan, fit) the two views also
paint their background grid twice (`_SymbolView.drawBackground`,
`FootprintView.drawBackground`), and only the symbol's version lays out the
grid just where Qt paints (`_exposed_region`, `_grid_indices`). User rule: one
reusable graphics handling, not code rewritten per tab; behaviour and pixels
identical to today except the differences listed in §3.

Binding rules: `.claude/CLAUDE.md` (HANDOFF before code, generalise, Doxygen
`@brief/@param/@return/@note` on every public item, comments in English,
agents on disjoint files, coordinator commits). Method: parity tests written
against the CURRENT code first, then the shared module, then adoption, each
step with the suite green.

## 1. Inventory (current code)

| Piece | Symbol (`kicadgen/gui/symbol_tab.py`, `_SymbolView`) | Footprint (`kicadgen/gui/footprint_view.py` + `main_window.py`) |
|---|---|---|
| Zoom | `zoom(factor, position=None)` ~1003: result outside [0.15, 20] is REFUSED (no change), `auto_fit=False`, anchor point kept fixed (`mapToScene` before/after + `centerOn`), emits `camera_changed` | `zoom(factor, anchor=None)` ~333: result CLAMPED to [0.02, 40], `auto_fit=False`, same anchor technique, no signal |
| Wheel | `wheelEvent` ~1013: `1.2 ** clamp(delta/120, -4, 4)` about the pointer, refused during a drag or a pan, always accepted | `wheelEvent` ~350: `1.2 ** (delta/120)` (no clamp), refused during a drag or a pan, always accepted |
| Pan | press 690-699 (middle, or left on empty background, only when `gesture is None`): `_pan_button`, `_pan_last`, `auto_fit=False`, closed-hand cursor, `viewport().grabMouse()`; `_pan_to` 738-742 moves the SCROLLBARS by the pixel delta; release 785-791 final move, releaseMouse, unsetCursor; Escape ends it (keyPressEvent) | press 589-593 (left or middle): `_pan`, `_pan_button`, closed-hand cursor, NO grabMouse; move 609-615: `centerOn(center + mapToScene(last) - mapToScene(pos))`, `auto_fit=False` only when the delta is not null; release ~674-677; Escape ends it (added 2026-10-17) |
| Fit | `fit()` 983-1001: `auto_fit=True`, `fitInView(sceneRect, KeepAspectRatio)` skipped when `_camera()` (scene rect, viewport size, transform, scroll) is unchanged since the last fit (memo `_fitted_camera`), emits `camera_changed`; `resizeEvent`/`showEvent` refit while `auto_fit` | `MainWindow._fit_preview(force)` ~18180: frames the non-grid items + 12 px margin, sets the scene rect, honours `auto_fit` unless `force`, `resetTransform()` then `fitInView`, `auto_fit=True`; no memo; no refit on resize |
| Grid | `drawBackground` ~901-976: pin-grid lines `GRID_MM*10` scene units, origin 0; fine dots every `drawing_grid_mm` (skipped when equal to the pin grid or `< FINE_GRID_MIN_PX` on screen), round-cap 1.5 px cosmetic pen, antialiasing OFF for the dots; only the lines/columns/rows that reach the paint region are laid out (`paintEvent` keeps `_exposed_region`, `_grid_indices` uses `QRegion.intersects`, `GRID_PAINT_MARGIN_PX=2`) | `drawBackground` ~280-322: lines at `step` from `origin` (first pad), `_draw_fine_dots` (fine divisor n, nodes on a main line skipped, 1.5 px round cap), `fine_dots_visible` rule (`FINE_GRID_MIN_SCREEN_PX`); lays out the whole exposed rect |

## 2. Target design

Two new Qt-only modules (no model imports, no translated strings), siblings of
`canvas_gestures.py`:

### `kicadgen/gui/canvas_camera.py`

```python
class Camera(QObject):
    changed = pyqtSignal()                         # after a zoom, a pan step or a fit that moved the picture
    def __init__(self, view: QGraphicsView, *, limits: tuple[float, float], max_notches: float = 4.0)
    auto_fit: bool                                 # read/write; the views expose it as a property delegating here (`view.auto_fit` stays the public name: main_window and tests read and write it)
    def scale(self) -> float                       # abs(view.transform().m11())
    def zoom(self, factor: float, anchor: QPoint | None = None) -> bool
        # new scale = clamp(scale*factor, *limits); anchor (viewport point, default centre) kept fixed;
        # auto_fit False; emits changed; returns False and changes nothing when the clamped scale equals the current one
    def wheel(self, event, *, blocked: bool = False) -> None
        # always event.accept(); zoom(1.2 ** clamp(angleDelta().y()/120, -max_notches, max_notches), pointer) unless blocked or delta == 0
    pan_active -> bool; pan_button -> Qt.MouseButton | None
    def pan_begin(self, button, pos: QPoint, *, grab_mouse: bool) -> None   # auto_fit False, closed-hand cursor, grabMouse if asked
    def pan_move(self, pos: QPoint) -> None                                 # moves the picture by the pixel delta since the last position (auto_fit False only when the delta is not null)
    def pan_end(self, pos: QPoint) -> bool                                  # final move, releaseMouse (only if grab_mouse), unsetCursor; False when idle
    def pan_cancel(self) -> bool                                            # Escape/hide: same cleanup without a move
    def fit(self, rect: QRectF, *, force: bool = False, reset: bool = False) -> bool
        # skipped (False) when auto_fit is off and not force, or when the camera key is unchanged since the last fit and not force;
        # reset=True calls resetTransform() first (footprint); fitInView(rect, KeepAspectRatio); auto_fit True; emits changed
    def camera_key(self) -> tuple                                           # scene rect, viewport size, transform, scroll values
```

Hosts keep: the press routing that decides WHEN a pan starts (symbol: middle or
left on background with no drag; footprint: left or middle with no drag and the
draw mode rules), the Escape hook (call `pan_cancel`), `resizeEvent`/`showEvent`
refit in the symbol, and the framing rectangle (symbol: scene rect; footprint:
non-grid items + margin in `_fit_preview`).

### `kicadgen/gui/canvas_grid.py`

```python
class ExposedRegion:                               # replaces the symbol's paintEvent override + _exposed_region
    def __init__(self, view: QGraphicsView)       # installs a paint hook on the view (event filter on the viewport or a paintEvent wrapper: M decides, documented); `region` is the paint event's QRegion or None outside a paint
    region -> QRegion | None
    def indices(self, rect: QRectF, step: float, axis: int, origin: float = 0.0, margin_px: int = 2) -> list[int]
        # the grid positions origin + i*step inside rect that reach the region (same rule as `_SymbolView._grid_indices`): the full range outside a paint, for an empty region or a whole-viewport region
@dataclass
class GridStyle:
    line_color: QColor; dot_color: QColor | None; dot_width: float = 1.5; dots_antialiased: bool = False
def paint_grid(painter, rect, *, exposed: ExposedRegion, step: float, origin: tuple[float, float] = (0.0, 0.0),
               style: GridStyle, fine_step: float | None = None, skip_dots_on_lines: bool = False) -> None
        # lines every `step` from `origin` spanning `rect` (cosmetic 1-px pen), then, if `fine_step`, dots at the fine nodes
        # (nodes on a main line skipped when `skip_dots_on_lines`); only the lines/columns/rows that reach the paint region are laid out
def dots_legible(fine_step: float, scale: float, min_px: float) -> bool   # the shared `fine_step * scale >= min_px` rule
```

Each view keeps its own grid MODEL (symbol: pin grid + drawing grid combo and
`fine_grid_visible`; footprint: `_grid` tuple, divisor, origin through the first
pad, `snap_to_grid`) and calls `paint_grid` with its parameters. The pixel
output must be byte-identical to today's for both views.

## 3. Differences removed (user asked for one behaviour; each with a positive and a negative test)

- **D13 Zoom beyond the limit**: both tabs CLAMP to their limits (the symbol used to refuse the whole step, so a wheel step that would overshoot did nothing and the user could not reach the limit). Limits stay per-tab parameters because the scene scales differ (symbol 10 scene units/mm, footprint 20 px/mm): [0.15, 20] and [0.02, 40].
- **D14 Wheel notches**: both tabs clamp one wheel event to ±4 notches (the footprint had no clamp, so a free-spinning wheel could jump by 1.2^n).
- **D15 Fit memo and refit**: the footprint gets the symbol's memo (a redraw that leaves the camera unchanged does not call `fitInView` again) and refits on resize while `auto_fit` is on (the symbol already does). Check that the footprint's `_fit_preview` semantics (grid ignored in the framing, scene rect from content, `force` for «Adatta alla vista») are preserved exactly.
- **D16 Grid region awareness for the footprint**: the footprint lays out only the grid that reaches the paint region (pixels identical, less work).
- **D17 One pan strategy** (added after agent F measured it): the footprint panned with `centerOn(mapToScene(viewport().rect().center()) + delta)`, whose centre is (479, 368) for a 960x738 viewport while the true middle is (480, 369): every pan step, even a null one or the release, drifted the picture by (-1, -1) scroll steps, so the scene crept away from the pointer. Both tabs now pan through the scrollbars (pointer delta subtracted from the scroll values): no drift, a null move and the release do not move the picture, 200 one-pixel steps equal one 200 px move.
- **D18 Exact zoom centre**: `zoom` re-centres on the exact viewport middle (`viewportTransform().inverted()[0].map(QPointF(w/2, h/2))`) instead of `viewport().rect().center()`, which had the same off-by-one creep in both tabs (cumulative: tens of scroll steps at scale 20).
- **D19 No auto_fit side effect without a change** (found by the adversarial review): a zoom that does not change the scale (factor 1.0, or already at the limit) and a wheel event with delta 0 no longer switch `auto_fit` off; the old footprint switched it off in those cases and the old symbol did not.
- Kept as parameters (not differences): zoom limits, grid origin and fine-grid model, which pans start a gesture, the framing rectangle.
- Known fragility (not a defect): the pixel goldens under `tests/data/camera_golden/` store SHA hashes of raster grabs; they depend on the Qt raster/antialiasing implementation and break on a different Qt or PyQt version, not only on a regression. Regenerate with `KICADGEN_REGEN_CAMERA_GOLDEN=1` after checking the diff. The numeric goldens (scale, centre, scroll) are robust.

## 4. Migration (every step leaves the suite green)

1. Parity tests first, against the CURRENT code, by the two adopting agents in their own files: numbers (scale after zoom at several anchors and factors incl. limit hits, wheel deltas, pan deltas and camera centre, fit rect/transform/scroll, memo behaviour, auto_fit transitions, signals) and PIXELS (same-process grabs of the grid at several zooms and fine-grid settings, with and without a sparse paint region; compare with the saved PNG of the same process before the swap). The existing sentinels stay green: `tests/test_symbol_tab.py::test_cad_pan_zoom_anchor_and_fit_do_not_edit_symbol`, `tests/test_symbol_editor_perf_a_incremental.py::test_auto_fit_refits_only_when_the_camera_changed`, `tests/test_symbol_grid_paint_region.py`, `tests/test_symbol_drawing_grid.py`, `tests/test_preview_grid.py`, `tests/test_footprint_drawings.py::test_an_edit_keeps_the_camera_once_the_user_has_zoomed`, `tests/test_symbol_gesture_parity.py` (S10, S11), `tests/test_footprint_gesture_parity.py` (F4, F5).
2. Module agent: `canvas_camera.py`, `canvas_grid.py` and their unit tests (bare `QGraphicsView`, offscreen).
3. Symbol adopts the camera, then the grid. 4. Footprint adopts the camera (incl. `_fit_preview`), then the grid.
5. D13-D16 applied with their tests; parity tests that recorded the old behaviour updated with the reason.
6. Dead code removed; adversarial review; full suite in two halves; HANDOFF; commit with the user's OK.

## 5. Work split (disjoint files)

| Agent | Files (exclusive) |
|---|---|
| M (modules) | `kicadgen/gui/canvas_camera.py`, `kicadgen/gui/canvas_grid.py`, `tests/test_canvas_camera.py`, `tests/test_canvas_grid.py` |
| S (symbol) | `kicadgen/gui/symbol_tab.py`, `tests/test_symbol_camera_grid_parity.py`, `tests/data/camera_golden/symbol_*` |
| F (footprint) | `kicadgen/gui/footprint_view.py`, the camera/fit/grid parts of `kicadgen/gui/main_window.py` (`_fit_preview`, `_draw_grid` and what feeds `FootprintView.set_grid`), `tests/test_footprint_camera_grid_parity.py`, `tests/data/camera_golden/footprint_*` |
| Coordinator | HANDOFF.md, commits, the cross-tab tests that poke private camera attributes, the adversarial review, the full suite |

## 6. Traps

- PyQt6 `QRegion` is not iterable and has no `rects()`: use `QRegion.intersects` per candidate (see `_SymbolView._grid_indices`).
- `fitInView` resets and rescales the transform and repaints the whole viewport: keep the memo.
- `view.auto_fit` is read and written from outside (`main_window` ~8093, 18209-18213, tests): keep it as a public property of each view delegating to the camera.
- Pan through scrollbars (symbol) and through `centerOn` (footprint) agree only while the scene rect allows the move (the symbol's is ±1e6, the footprint's is the content rect + 12 px): the parity tests decide whether one strategy reproduces both; if not, keep a documented `strategy` parameter instead of changing a behaviour.
- Qt does not deliver Enter/Leave or cursors offscreen; compare numbers and pixels, not cursor shapes (except the cursor assertions G1 already made).
- Shared QApplication: never register fonts in the shared test session (conftest guard); build a fresh view per test.
- Offscreen button-less `QTest.mouseMove` to the same point produces no event.
