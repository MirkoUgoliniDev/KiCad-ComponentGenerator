# Plan: shared click tools for the symbol editor and the footprint preview

Phase B of `docs/canvas-gestures-plan.md`, written 2026-10-18 by the planning
agent (read-only) and completed by the coordinator with the decisions in §3.
The click tools are the last interaction code the two tabs still write twice:
tools that COLLECT POINTS by clicking (symbol: wire, shape tools, glyph
placing; footprint: draw mode). Method as in the previous phases: parity tests
written against the CURRENT code first, then the shared module, then one
adopter at a time with the suite green. Line numbers refer to commit
`27b93ed`; grep for the names.

Binding rules: `.claude/CLAUDE.md` (HANDOFF before code, generalise, Doxygen
`@brief/@param/@return/@note`, comments in English, agents on disjoint files,
coordinator commits).

## 1. Inventory

### Symbol (`kicadgen/gui/symbol_tab.py`)

Press routing `_SymbolView.mousePressEvent` ~638-720: `pointer_changed.emit`,
wiring left/right, placing left/right, rubber band, Ctrl+click, pan, drag.
Other handlers: `mouseDoubleClickEvent` ~626, `mouseMoveEvent`,
`mouseReleaseEvent`, `keyPressEvent`, `cancel_interaction`, `hideEvent`,
`leaveEvent`.

- **Wire (S13)**: `start_wire` ~2400; a left press emits `wire_point(point, Shift)` → `_wire_point` → `add_wire_point`; points are opaque (`[x, y]` on the 2.54 mm grid or `{x, y, anchor}` within the snap tolerance of a pin end, `_wire_snap`); orthogonal mode appends 1 or 2 points (`_next_wire_points`, corner first); Shift frees the segment; a point equal to the last is refused SILENTLY; preview = clicked dots + dashed segments (`_draw`) + a rubber band `_wire_cursor_item` reshaped IN PLACE in `_update_readout` from `_next_wire_points(cursor, Shift)` (no `_draw` per move: a perf property to keep); finish = right press, left double click (no point added), Enter → `finish_wire`; cancel = Escape; `finish_wire` with fewer than 2 points abandons; the tool is one-shot.
- **Shape tools (S14)** (`SHAPE_TOOLS`: line, rectangle, circle, arc, bezier, text): `start_shape(key)`; clicks route through the same signal to `add_shape_point` (drawing-grid snap `_grid_point`, 0.01 mm when `_snap` is off); fixed count (`shape_tool(key)[3]`, 0 = line) auto-finishes; polygon closes when a click equals the first point at 4+ points; preview = dots + dashed `_shape_outline`; line finishes by right press, double click or Enter; Escape cancels; a point equal to the last is refused silently; a degenerate shape ("Forma non valida") leaves the tool armed with the points cleared; a cancelled text prompt leaves it armed; shapes PERSIST after commit (several in a row), text is one-shot.
- **Glyph placing (S15)**: `_start_placing` sets `view.placing`, does NOT release the wire tool (latent conflict: the wire wins the press); a left press emits `place_requested(point)` (snapped by `snap_glyph_origin`); right press / Escape cancel; one-shot.
- `leaveEvent` hides handles only (the rubber band stays); `hideEvent` cancels rubber band / pan / drag but NOT an armed tool.
- State the tests read: `view.wiring`, `view.placing`, `tab._wire_points`, `tab._wire_cursor_item`, `tab._shape_tool`, `tab._shape_points`.

### Footprint (`kicadgen/gui/footprint_view.py` + `kicadgen/gui/main_window.py`)

- **Draw mode (F6)**: `_toggle_footprint_draw_tool` ~14573 (arms, `set_draw_mode(True)`: pan_cancel, cancel_interaction, CrossCursor, focus); press stores `_draw_press` and emits `draw_clicked(x, y)`; host `_on_footprint_draw_clicked` ~14595 snaps with `_snapped_draw_point` (drawn grid through the first pad, 0.1 mm fallback); `draw_hovered` → `_on_footprint_draw_hovered` stores the snapped hover node; `_refresh_draw_markers` ~15678 rebuilds tagged items (yellow click dots, dashed ghost coloured by layer, snap dot); `leaveEvent` emits `draw_hover_left`; finish = fixed count reached, right press, double click (point THEN finish), Enter; cancel = Escape, hide, project load, undo/redo, apply, discard; a point equal to the last → toast "Punto coincidente con il precedente"; wrong count / fewer than 2 points / dialog cancelled → disarmed; `validate_footprint_drawings` rejection → disarmed + toast; `_on_footprint_draw_finish` builds the record, calls `_cancel_footprint_drawing` BEFORE applying (keep this order), then `_apply_footprint_drawings` → `_edit_footprint_draft`; always one-shot.
- **F6b (D10)**: a release farther than `QApplication.startDragDistance()` (manhattan) from the press adds the release point; a release at the press adds nothing.
- No polygon close, no Shift constraint, no placing, no orthogonal mode in the footprint.
- State the tests read: `window._fp_draw_tool`, `_fp_draw_clicks`, `_fp_draw_hover`, `view._draw_mode`.

## 2. Target design

New Qt-only module `kicadgen/gui/canvas_clicktools.py` (sibling of
`canvas_gestures.py`, `canvas_camera.py`, `canvas_grid.py`): no imports of
model, symbol_tab, footprint_view or main_window, no translated strings,
Doxygen on every public item.

```python
@dataclass(frozen=True)
class ToolSpec:
    key: str                              # "wire", "rectangle", ...
    count: int = 0                        # fixed number of points, 0 = open-ended
    close_after: int = 0                  # >0: with this many points, a click equal to points[0] finishes (symbol line: 4)
    persistent: bool = False              # stays armed after a committed shape
    drag_adds_release_point: bool = True  # press-move-release adds the release point (C1)

class ClickToolHost(Protocol):
    def tool_snap(self, spec, point: QPointF, free: bool, points: Sequence) -> list      # raw scene point -> the points a click would APPEND (0, 1 or 2); used for clicks AND hover, so ghost and click agree
    def tool_points_changed(self, spec, points: list) -> None                           # dots: after an add, a finish or an arm
    def tool_preview_changed(self, spec, points: list, preview: list) -> None           # hover: the points that would be appended; [] when none
    def tool_refused(self, spec, reason: str) -> None                                    # reason == "coincident"
    def tool_finished(self, spec, points: list, commit: bool) -> bool                    # the tab commits or abandons; True = stay armed with an empty list, False = disarm. Called with the collector already idle
    def tool_armed(self, spec) -> None
    def tool_disarmed(self, spec) -> None

class PointCollector(QObject):
    armed -> bool; spec -> ToolSpec | None; points -> list (copy); preview -> list
    def __init__(self, host, parent=None)
    def arm(self, spec) -> bool            # cancels any other armed tool first (tool_finished(commit=False) + tool_disarmed), then tool_armed(spec); never raises
    def disarm(self) -> None               # = cancel without a commit
    def click(self, point, *, free=False, quiet=False) -> bool
        # tool_snap first; [] → refused (reason "coincident" unless quiet); first appended == last collected → refused;
        # append; len >= count → finish(True); elif close_after and len >= close_after and appended == points[0] → finish(True); else tool_points_changed
    def finish(self, commit=True) -> bool  # capture (spec, points), clear points/preview, mark idle, THEN host.tool_finished; stay → re-arm the same spec silently (empty list, tool_points_changed); else tool_disarmed. If the host armed another tool inside the callback, the stay result is ignored
    def cancel(self) -> bool               # finish(False); False when idle
    def hover(self, point | None, *, free=False) -> None   # None clears (leave); emits tool_preview_changed only when the preview differs (!=)

class ClickToolRouter:
    def __init__(self, view, collector, camera)
    def press(self, event) -> bool; release(event); double_click(event); key(event); move(event)   # True = consumed
    def leave(self) -> None; hide(self) -> None
    def arm(self, spec) -> bool; disarm(self) -> None      # arm: setFocus, CrossCursor, camera.pan_cancel, the host's cancel_interaction hook
```

Router rules (called FIRST in each handler, after the symbol's
`pointer_changed.emit`):

- **press**: not armed → False. If `camera.pan_active`: accept, add no point (C9). Left: record the press position, `collector.click(point, free=Shift)`, accept. Right: `collector.finish(True)`, accept. Middle: False (the camera pans).
- **release**: left, `drag_adds_release_point`, manhattan distance from the press ≥ `QApplication.startDragDistance()` → `collector.click(release point)`; clear the press position; accept. A release at the press adds nothing.
- **double click**: left → `collector.click(point, quiet=True)` then `collector.finish(True)` (a no-op if the click already finished); clear the press position (so the release after it adds nothing); accept.
- **key**: Escape → if `camera.pan_cancel()` return True (the pan takes the key and the tool survives), else `collector.cancel()`; Return/Enter → `collector.finish(True)`; accept both.
- **move**: armed, no pan, no drag → `collector.hover(point, free=Shift)`; the symbol still continues to `pointer_changed` (the router does not accept), the footprint accepts.
- **leave** → `collector.hover(None)`; **hide** → `collector.cancel()` (idempotent).

Interplay: `GestureController` is untouched (an armed tool consumes every
left press, so no drag starts; `router.arm()` calls the view's
`cancel_interaction` hook as `start_wire`/`start_shape`/`set_draw_mode` do
today); `Camera` is touched only through `pan_active` and `pan_cancel()`
(wheel/zoom while armed stay allowed); `CancelOnEscapeAndHide` is untouched
(it consumes Escape only when it cancelled a drag, so Escape reaches the
view's key handling). In the symbol's `keyPressEvent` the wiring/placing
branches are removed; in the footprint the existing pan/drag handling stays
first.

Tab-specific (stays): what a finished tool commits (`finish_wire`,
`finish_shape`, `_on_footprint_draw_finish`), snap functions, the arc click
order vs stored order, text dialogs, record building, ghost and dot
drawing and colours, status line (symbol) vs toast (footprint), the layer
combo, `_degenerate_drawing_text`, Shift-free segments (symbol wire only;
the footprint host ignores `free`), polygon close-on-first (symbol line
only).

Plug-in: the symbol tab builds `PointCollector(self)` and
`ClickToolRouter(view, collector, view.camera)` where the gesture controller
is set up; `view.wiring` becomes `collector.armed`, `tab._wire_points` /
`_shape_points` properties over `collector.points`, `_shape_tool` =
`collector.spec.key` for shapes; `start_wire`, `start_shape`,
`add_wire_point`, `add_shape_point`, `finish_wire`, `finish_shape`,
`shape_tool`, `_release_tools` stay as thin public wrappers (tests and
automation call them); the in-place rubber band (code in `_update_readout`)
moves into `tool_preview_changed`. The footprint window implements the
host (`tool_snap` = `[self._snapped_draw_point(x, y)]`, markers through
`_refresh_draw_markers`, the toast in `tool_refused`, the body of
`_on_footprint_draw_finish` in `tool_finished`, always returning False);
`_toggle_footprint_draw_tool`, `_cancel_footprint_drawing`,
`_on_footprint_draw_clicked/_finish/_hovered/_hover_left` stay as thin
wrappers; `_fp_draw_tool`, `_fp_draw_clicks`, `_fp_draw_hover` and
`_draw_mode` become read-only properties; `set_draw_mode` stays as an alias
that arms/disarms.

## 3. Differences and decisions (coordinator, 2026-10-18)

The user's rule is one behaviour in both tabs. Decided unless marked
«keep»; every change has a positive and a negative test and the parity tests
that recorded the old behaviour are updated with the reason.

| ID | Difference | Decision |
|---|---|---|
| C1 | press-move-release adds the release point (footprint only) | Unify with `drag_adds_release_point`: ON for the symbol shape tools (a drag draws a rectangle/circle/arc/line like the footprint) and the footprint tools; OFF for the symbol WIRE (a polyline would get a second point per drag). Flag to the user. |
| C2 | double click: symbol finishes only (two fast clicks on a rectangle give 1 point then a finish: "Forma non valida", VERIFY); footprint adds the point THEN finishes | Footprint rule in both, with `quiet=True` on the point (a genuine double click on the last vertex adds it once and shows no refusal). |
| C3 | duplicate point: symbol silent, footprint toast | Both refuse through `tool_refused`; the symbol reports on its status line. |
| C4 | right click / Enter with 0 points | NOT unified in the collector: `tool_finished` returns stay/disarm, each tab keeps its commit/refusal outcome; the call path is unified. |
| C5 | persistence after a shape | «keep» as `ToolSpec.persistent` (symbol shapes except text persist; everything else one-shot). Flag to the user. |
| C6 | leave: symbol rubber band stays, footprint ghost and snap dot cleared | Unify on the footprint rule (clear on leave, restore on the next move). |
| C7 | hide / tab switch while armed: symbol keeps the tool and its points, footprint disarms | Unify on the footprint rule (hide cancels), as drags already do. Flag: a wire in progress is lost on a tab switch. |
| C8 | Escape during a middle pan with a tool armed: symbol cancels the wire first (VERIFY), footprint cancels the pan and keeps the tool | Footprint rule in both (the pan takes the key). |
| C9 | left press during a middle pan while armed adds a point | Swallow it in both (`camera.pan_active`). |
| C10 | cursor | The router sets `CrossCursor` on arm and unsets it on disarm in both (cursor shapes cannot be asserted offscreen). |
| C11 | placing vs wire arming (placing never gets clicks if a wire is armed) | One armed tool at a time; placing STAYS OUTSIDE the router (a `count=1` tool would make Enter cancel it) but `_start_placing` calls `collector.disarm()` (fixes the latent conflict). |
| C12 | Enter while placing | unchanged (ignored). |
| X | the symbol's right press finishes a wire, but a Windows context-menu event may still open the pin/element menu afterwards (VERIFY) | If confirmed, `contextMenuEvent` ignores while a tool is armed (symbol-only fix, flagged). |
| C13 (review 2026-10-27, D1) | the draft is replaced under an armed tool: the footprint cancels its draw tool on undo/redo (`_step_footprint_history`, before looking at the stack) and on a project switch; the symbol only cancelled the drag (`view.cancel_interaction`), so a wire anchored to a pin of the OLD project could be committed into the NEW draft | Footprint rule in both: `SymbolTab.set_data`, `_restore_history` (undo/redo, also with nothing to undo) and `reset_layout` call `self._router.disarm()`; the points were snapped and anchored on the replaced draft. Tests in `tests/test_review_1027_canvas.py` (positive: undo, redo, reset, set_data; negative: an idle editor's host is not told, and the tool works after an undo). |
| C14 (review 2026-10-27, D4) | glyph placing (`_SymbolView.placing`, outside the router by C11) survived Hide and `cancel_interaction`: the first click in the re-shown tab dropped the glyph | `cancel_interaction` (hence `hideEvent`, `set_data`, `_start_placing`, the router's `on_arm` hook) clears `placing` and emits `place_requested(None)`, the same cancel path as Escape and the right click. Arming the wire while a glyph is armed therefore cancels the glyph (C11 in both directions). Tests: hide, cancel_interaction, arming; negative: without the hide the click places. |

## 4. Migration (each step leaves the suite green)

1. Parity tests first, against the CURRENT code, by the adopters in their own files: `tests/test_symbol_clicktool_parity.py` (real QTest scripts through `mousePressEvent`, `mouseDoubleClickEvent`, `keyClick`, `leaveEvent`: wire (clicks, orthogonal corner, Shift frees, anchored ends, duplicate refused silently, right click, double click, Enter, Escape), each shape (auto-finish at its count, polygon close, text one-shot), degenerate shape leaves the tool armed, persistence after a shape, invalid draft ignores clicks, leave leaves the band, hide keeps the tool (pins C7 as today), Escape during a middle pan with a wire armed (pins C8), placing with right click and Escape; each recording committed rows/graphics, `len(history)`, recovery writes, `_wire_points`/`_shape_points`, toolbar/palette state, status text, the rubber band path) and `tests/test_footprint_clicktool_parity.py` (click-click rectangle, circle, arc order, line with Enter, right click, Escape; two fast clicks and a double click on the last vertex (record whether a toast appears); press-move-release and the plain click; duplicate toast; fixed-count auto-finish; degenerate toast; hide disarms; leave clears the ghost; hover over a handle, middle pan, wheel). Each also runs the same script through the direct API (`add_wire_point`, `add_shape_point`, `_on_footprint_draw_clicked`) as a twin oracle: identical committed output. Golden JSON under `tests/data/clicktool_golden/`, regenerated only with an env var, read independent of line endings.
2. Agent M: `canvas_clicktools.py` + `tests/test_canvas_clicktools.py` (bare `QGraphicsView`, recording fake host: state machine, count auto-finish, close_after, finish/cancel, stay/disarm, host returns [], duplicate rule and `quiet`, re-entrant host (re-arm and arm another tool inside `tool_finished`), change-only preview emission, router precedence (pan_active swallows left, middle falls through, Escape asks `pan_cancel` first, release-through-drag distance, double click point-then-finish with quiet, leave, hide idempotent); negative twins).
3. Footprint adopts first (smaller), then the symbol wire and shape tools; each step: parity file + sentinels + kept-items test.
4. Decisions C1-C3, C6-C10, C11, X, one at a time with positive/negative tests; update the parity files.
5. Dead code removal (`wire_point`, `wire_finished`, `place_requested`, `draw_*` signals, `view.wiring`), documentation test, adversarial review, full suite in two halves, HANDOFF, commit with the user's OK.

Sentinels to keep green — symbol: `tests/test_symbol_palette_gui.py` (wire/shape/placing tests), `tests/test_symbol_editor_robustness.py` (invalid draft, rubber band), `tests/test_symbol_drawing_grid.py`, `tests/test_symbol_gesture_parity.py::test_escape_still_cancels_a_pan_a_band_a_wire_and_a_placing_glyph`, `tests/test_kept_scene_items.py`, `tests/test_symbol_glyphs.py`; footprint: `tests/test_footprint_drawings.py` (rectangle, line, arc order, two fast clicks, coinciding click and degenerate, press-move-release, grid snap, snap node and ghost, ghost colour), `tests/test_footprint_text_anchor.py`, `tests/test_footprint_gesture_parity.py` (Escape cancels a pan and the tool survives; D1 Escape), `tests/test_kept_scene_items.py`.

## 5. Work split (disjoint files)

| Agent | Exclusive files |
|---|---|
| M | `kicadgen/gui/canvas_clicktools.py`, `tests/test_canvas_clicktools.py` |
| S | `kicadgen/gui/symbol_tab.py`, `tests/test_symbol_clicktool_parity.py`, `tests/data/clicktool_golden/symbol_*` |
| F | `kicadgen/gui/footprint_view.py`, the draw handlers of `kicadgen/gui/main_window.py` (~14573-14690, ~15617-15700, the callers at project load / undo-redo / apply / discard, the draw-mode connections), `tests/test_footprint_clicktool_parity.py`, `tests/data/clicktool_golden/footprint_*` |
| Coordinator | HANDOFF.md, `tests/test_kept_scene_items.py`, `tests/test_symbol_gesture_parity.py` / `tests/test_footprint_gesture_parity.py` if a contract changes, commits, review, full suite |

Contract (binding for M, S, F):

1. `click`: `host.tool_snap` first, then the refusal check, then append, then (count or close) auto-finish, else `tool_points_changed`; `tool_refused` at most once per click.
2. `finish(commit)`: capture `(spec, points)`, clear points and preview, mark idle, THEN call `host.tool_finished(spec, points, commit)`, THEN act on the result (stay: re-arm the same spec without calling `tool_armed` again, empty list, `tool_points_changed`; not stay: `tool_disarmed`). If the host armed another tool inside the callback the stay result is ignored. A `cancel()` or `arm()` from inside the callback is safe.
3. `arm(spec)`: another armed tool is cancelled first with `tool_finished(commit=False)` and `tool_disarmed`; then `tool_armed(spec)`. Arming never raises.
4. `hover` emits `tool_preview_changed` only when the preview list changed (`!=`); clearing on leave emits once.
5. The collector does not copy points deeply; a host that keeps them copies (`finish_wire` does `deepcopy`).
6. A point is any equality-comparable object; `==` is used for the duplicate and close-on-first checks; wire dedupe stays in the host's `tool_snap` (isclose → `[]`).
7. The router never emits Qt signals and never touches the scene.
8. Router keyboard: Escape goes through `camera.pan_cancel()` first; Enter and Return are the only finish keys.
9. M documents which collector calls the host may make from inside each callback.

## 6. Traps

- Items deleted while referenced: `_wire_cursor_item` and the footprint markers are rebuilt by `_draw` / `_refresh_draw_markers`; `tool_preview_changed` must guard a deleted item (`sip.isdeleted`); extend `tests/test_kept_scene_items.py` to the shared objects.
- An exception in a slot aborts the app: the collector and router never raise on stale host state; finish clears state before calling out.
- Invalid draft (`_symbol is None`): `add_wire_point` returns False and keeps the points; `tool_snap` returns `[]`, the tool stays armed, the rubber band survives (robustness tests).
- Double click ordering: Qt may deliver press, release, double click, release: the release after the double click must not add a point (clear the press position on the double click). A fast-click double click on a fixed-count tool finishes on the click, so the following `finish(True)` is a no-op.
- The footprint host calls `_cancel_footprint_drawing` BEFORE applying: keep that order in `tool_finished`.
- The symbol's per-node commit (D12) is untouched: wire and shape commits still go through `_accept_geometry`; only click collection moves.
- Offscreen: `MouseMove` is delivered, Enter/Leave and cursors are not: call `leaveEvent` / `router.leave()` directly, never assert cursor shapes; a button-less `QTest.mouseMove` to the same point produces no event; never register fonts in the shared session; compare rows, positions and counts, not pixels.
- Private state the tests poke (`view.wiring`, `_wire_points`, `_shape_points`, `_shape_tool`, `_fp_draw_tool`, `_fp_draw_clicks`, `_fp_draw_hover`, `_draw_mode`): keep read-only properties; assignments in tests need the coordinator's call (grep tests for assignments first).
- The machine is loaded by parallel agents: run ONE test file per command with a fresh basetemp, give probes a timeout, never the whole suite in parallel.

## 7. Measurements (before/after, scratchpad `plan_ct/`)

Symbol while a wire/shape is armed: `_draw` calls per move must stay 0 and
the rubber band path equal (`scratchpad/p2/probe_symbol.py`, N=80);
footprint while armed: `_refresh_draw_markers` calls equal the number of
snapped-node changes, not the number of moves; per click per tab: `_draw` /
`_draw_preview` calls, `_accept_geometry` / `_edit_footprint_draft` calls,
history length, draft/recovery writes and committed records (golden JSON);
the same script through events vs the direct API gives identical output;
same-process pixel grabs of an armed wire mid-way and an armed footprint
rectangle with hover, before and after.
