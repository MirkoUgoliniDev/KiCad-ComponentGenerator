# Plan: one shared gesture component for the symbol editor and the footprint preview

Written 2026-10-17 by the planning agent (read-only) for the coordinator; the
user asked for one graphics handling in both tabs («uniformare il
funzionamento della gestione grafica nelle due tab»). Line numbers refer to
the working tree at commit `e713c33` plus the live property drag of the same
evening. This file is the reference the implementing agents work from; the
decisions the user takes on the differences D1–D12 are recorded in HANDOFF.md.

Binding rules (.claude/CLAUDE.md): HANDOFF.md updated BEFORE code is touched;
fixes generalise (no name-based rules, no thresholds tuned to the case at
hand); Doxygen `@brief/@param/@return/@note` on every public method and Qt
override; comments in English; agents on disjoint files, one checkout, the
coordinator commits; a test that passes only on the observed case is not a
verification (positive + negative twin).

## 1. Inventory of every gesture, per tab

Scene scales: symbol `SCENE_UNITS_PER_MM = 10.0` (symbol_tab.py:27, Y up);
footprint 20 px/mm (main_window.py:15590, 15701, 18353; Y down as KiCad).

### 1a. Symbol editor (`kicadgen/gui/symbol_tab.py`)

Press routing `_SymbolView.mousePressEvent` 666-743: (1) `pointer_changed`
emitted first (676) so hidden handles are revealed and hit-testable; (2)
wiring left/right (677-686); (3) placing left/right (687-695); (4) rubber band
on Shift or `selection_mode` (697-705); (5) Ctrl+click toggles (706-718); (6)
pan on middle button or left on empty background (719-728); (7) drag on the
first item whose `data(0)` is a target tuple (732-742: `self.gesture = target`,
`auto_fit = False`, `grabMouse`, `gesture_started`). Moves 779-797, release
799-827 (the release re-sends the drop point as a move, 820-821, then
`gesture_finished(True)`), Escape 849-851, `cancel_interaction` 855-867,
`hideEvent` 869-876 (cancel, then `key_repeat_ended`).

| # | Gesture | Trigger | Live / stepped | Snap | Commit | Cancel | Hover | Undo | Files |
|---|---|---|---|---|---|---|---|---|---|
| S1 | Pin drag | `('pin', n)` 732-742; `_begin_gesture` 3952-3967 | stepped on the 2.54 mm grid (`_move_gesture` 4545-4552 → `place_pin` 4120-4155) | `round(v/GRID_MM)*GRID_MM` 4138 + glyph terminal 4141-4146; occupied swapped 4149-4151 | each node `_accept_geometry` 4047-4072 writes the draft and redraws, no history (3761), no checkpoint (2259); identical repeats skipped by `_gesture_step_repeats` 4001-4023; release `_end_gesture(True)` 4559-4590 → `_changed()` → `_record` + `_checkpoint_draft` | Escape 849 / hide 869 / `_set_body_locked` 3882 / `set_data` → `cancel_interaction` → `_end_gesture(False)` → `_populate(before)` 4583 | none | 1 | recovery JSON once on release (`save_draft` 2280) |
| S2 | Multi-pin drag | pin in the selection 3964-3967 | stepped | `_transform_selected` 3937-3950 | as S1 | as S1 | none | 1 | as S1 |
| S3 | Element body drag (glyph / wire / shape, group) | `('graphic', i)` 3968-3979 | stepped | per member 4480-4516 | as S1 (4517) | as S1 | handles of the selected element 4604-4686 | 1 | as S1 |
| S4 | Wire vertex drag | `('vertex', i, pos)` → `move_wire_vertex` 3368 | stepped | `_wire_snap` 2561 | as S1 | as S1 | vertex squares 4686-4718, `_update_handle_visibility` 4737-4770 | 1 | as S1 |
| S5 | Shape point drag | `('shape_point', i, pos)` → `move_shape_point` 3203 | stepped | drawing grid `_grid_point` 2701-2704 | as S1 | as S1 | shape-point squares | 1 | as S1 |
| S6 | Property drag (Reference / Value) | `('property', key)` 3984-3990, live items `_property_items[key]` | live (`_move_gesture` 4525-4544: `setPos`, `_gesture_last_point`) | release only: `move_property` 4157-4204, pin grid 4189-4191; back onto the default's cell writes nothing 4200-4202 | release `_end_gesture` 4584-4588 → `move_property(last)` once | `_populate(before)` | anchor square 5177-5182 kept visible while dragged; redraw mid-drag re-binds 5186-5196 | 1 | one `save_draft` on the drop |
| S7 | Body resize | `('resize', hx, hy)` 4553-4557 → `resize_body` 4429-4450 | stepped | even multiples of 2.54, min 4 units 4442-4443 | as S1 | as S1 | 8 ring handles 5199-5212 | 1 | as S1 |
| S8 | Rubber band | Shift+left or `selection_mode` 697-705; release 804-811 → `_select_region` 3841-3869 | live band | — | selection only | Escape / `cancel_interaction` | band | 0 | none |
| S9 | Ctrl+click toggle | 706-718 | click | — | selection | — | — | 0 | none |
| S10 | Pan | middle, or left on empty 719-728; `_pan_to` 765-769 | live | — | camera; `auto_fit=False` 723 | Escape 849, `cancel_interaction` 862-865 | hand cursor | 0 | none |
| S11 | Wheel zoom | 1035-1043, refused during a drag or pan (1041); `zoom` 1025-1033 refuses outside [0.15, 20] | — | — | camera | — | — | 0 | none |
| S12 | Fit / auto-fit | `fit` 1005-1023 (`_fitted_camera`), `resizeEvent`/`showEvent` 1045-1061 | — | — | camera | — | — | 0 | none |
| S13 | Wire tool | `start_wire` 2533; clicks 677-681 → `add_wire_point` 2635-2656; right click / double click / Enter finish; Escape cancels 839-843 | click-collect, rubber band path 3909-3925 | pin grid / anchors (`_next_wire_points` 2585) | `finish_wire` 2886 → one `_accept_geometry` | Escape → `finish_wire(False)` | dashed band | 1 | checkpoint on finish |
| S14 | Shape tools | `start_shape` 2667, `add_shape_point` 2737-2763 | click-collect + ghost 3910-3913 | drawing grid | `finish_shape` 2765 | Escape | ghost | 1 | checkpoint on finish |
| S15 | Glyph placing | `placing` 687-695 → `_place_requested` 2931 | click | pin grid | one accept | Escape 844-847 / right click | — | 1 | checkpoint |
| S16 | Keyboard | shortcuts 1384-1410 through `_key_action` 2296-2316: arrows → `_nudge_selected` 4225-4232, R / Shift+R, Delete, Ctrl+Z/Y/Shift+Z, Ctrl+C/V/D | per press; auto-repeat defers the checkpoint (`_defer_checkpoint` 2312, 2261-2263) | pin grid | each press a full commit; checkpoint on the first press and once when the repeat ends (`key_repeat_ended` 637-652, 875 → `_flush_checkpoint` 2318-2321) | — | — | 1 per press | first press writes, repeats deferred |
| S17 | Hover squares | `pointer_changed` → `_update_readout` 3904-3908 → `_update_handle_visibility` 4737-4770: groups `(items, base rect, ring, owner)`; `HANDLE_HOVER_MARGIN=12` px / view scale; dragged owner stays revealed; `pointer_left` 771-777 hides all | — | — | visibility only | — | — | 0 | none |
| S18 | Context menu | 745-763, ignored during a drag (750-752) | — | — | — | — | — | — | — |

### 1b. Footprint preview (`kicadgen/gui/footprint_view.py` + `kicadgen/gui/main_window.py`)

Press routing `FootprintView.mousePressEvent` 440-513: draw mode (447-457);
right click → context menus (458-477, even during a drag); left: visible
handle first (483), else text group (484-492: `_text_drag = (item, key, press
scene point, item.pos())`, square forced shown 489), else drawing body
`(index, -1)` (493-506 → `handle_pressed`), else pan (507-511). Moves
515-558, release 573-618, double click 620-638, keys 640-654 (draw mode
only), hide 656-669, `cancel_interaction` 425-438 (called by the window).

| # | Gesture | Trigger | Live / stepped | Snap | Commit | Cancel | Hover | Undo | Files |
|---|---|---|---|---|---|---|---|---|---|
| F1 | Text drag (REF** / Value) | 484-492 | live: `setPos(origin + delta)` 520-530 (RuntimeError → drag dropped 524-528) | release only: `_on_footprint_text_moved` 18861-18872 → `_snapped_draw_point` 15588-15592 (drawing grid through the first pad, 0.1 mm fallback) | release 598-610: `text_moved` → `_set_footprint_text_position` 18832-18859 (equal → `_draw_preview()` only; else `_edit_footprint_draft({"texts"})` 14802-14833 → undo push + `_footprint_draft_changed` 14857-14875 + `_checkpoint_footprint_draft` 15153-15180) | `cancel_interaction` 425 (from `_end_footprint_drawing_interaction` 15309-15322) and `hideEvent` 661-664; no Escape; the displaced item stays until the next redraw | anchor square via `text_hovered` → `_refresh_text_anchor_squares` 15736-15750; shown while dragged (489, 569) | 1 | recovery on the drop; `.kicad_mod` only on «Applica» |
| F2 | Drawing point drag | handle `HANDLE_KEY_ROLE` 483, 498-506 → `_on_footprint_handle_pressed` 15759-15766 | stepped: `handle_dragged` 542-545 → `_on_footprint_handle_dragged` 15774-15793 (identical step skipped 15789; ghost + handles via `_refresh_drawing_handles` 15680-15723) | `_snapped_draw_point` | drop 15795-15833: unchanged → handles only; refused → toast 15826; else `_apply_footprint_drawings` 14675 → `_edit_footprint_draft` | `cancel_interaction` + `_fp_handle_cancelled` 15318-15319 (drop ignored 15804-15806); hide 663 | handles built on hover, removed otherwise (15694-15696); ghost 15704-15714 | 1 | recovery on drop |
| F3 | Whole-drawing drag | `_drawing_at` 316-325 → `(index, -1)` 493-497 | stepped | grab node snapped at press 15763; delta 15768-15772 | as F2 (zero delta → nothing 15813-15815) | as F2 | as F2 | 1 | as F2 |
| F4 | Pan | left on empty / middle 507-511; `auto_fit=False` only on a real move 533-534 | live | — | camera | hide 661 | hand cursor | 0 | none |
| F5 | Wheel zoom | 275-281, also during a drag; `zoom` 258-273 clamps to [0.02, 40] | — | — | camera | — | — | 0 | none |
| F6 | Draw mode | `set_draw_mode` 124-138; press → `draw_clicked` 447-453 → `_on_footprint_draw_clicked` 14581-14604; press-move-release beyond `startDragDistance` adds the release point 591-597; double click 632-637; right click finish 454-457; Enter / Escape 645-654; ghost `draw_hovered` 538-541 → 15594-15601 | click-collect + ghost | drawing grid | `_on_footprint_draw_finish` 14606-14641 | `_cancel_footprint_drawing` 14568-14579 (also on undo/redo 14844, hide 665-667) | snap marker + ghost 15649 | 1 | recovery on finish |
| F7 | Keyboard | Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z / Ctrl+D / Delete 14487-14495 on the hovered drawing 15453-15470; no arrows, no rotate, no auto-repeat deferral | — | — | each press `_edit_footprint_draft` → checkpoint | — | — | 1 per press | a held Ctrl+Z writes the recovery file on every repeat |
| F8 | Hover drawings | `_drawing_at` within `HIT_MARGIN_PX=4` (283-291, 316-325), kept within bounds + `HOVER_KEEP_MARGIN_PX=12` (346-350, 551-554) → 15752-15757 | — | — | handles created / removed | — | — | 0 | none |
| F9 | Hover texts | `_text_near` 367-384: glyph rect ∪ anchor (352-365), 12 px margin (`_near_scene_rect` 336-344) | — | — | square visibility | — | — | 0 | none |
| F10 | Context menus | right click 458-477, also during a drag | — | — | — | — | — | — | — |
| F11 | Applica / Annulla / Recupera | 14884 / 14933 / 15243 | — | — | — | — | — | — | project file + `.kicad_mod` |

### 1c. Differences between the tabs

Unjustified (candidates for unification; each one is a user decision because
the goal says «identical to today»; D1–D5 and D9 are defects or defect-adjacent):

- **D1 Escape**: cancels a drag in the symbol (849-851); nothing in the footprint (640-654 only in draw mode).
- **D2 Wheel during a drag**: blocked in the symbol (1041), allowed in the footprint (275-281): the live text keeps the old delta in a rescaled scene.
- **D3 Right click during a drag**: ignored in the symbol (750-752); the footprint opens a context menu with the drag still set.
- **D4 Redraw under a live drag**: the symbol re-binds and resumes at the pointer (5186-5196); the footprint silently drops the drag (524-528, 605-606).
- **D5 Cancel of a live drag**: the symbol puts the text back; the footprint leaves the item displaced until the next redraw.
- **D6 Press without a prior move**: the symbol reveals the handles on the press (676); footprint handles exist only after a hover, so such a press pans.
- **D7 Mouse capture**: symbol `grabMouse` (739); footprint relies on Qt's implicit grab. No visible difference; per-view flag.
- **D8 Keyboard**: the symbol defers the recovery write during auto-repeat; the footprint writes on every repeat of Ctrl+Z. No nudge/rotate in the footprint (no selection model).
  *Update 2026-10-29 (review C3/C8)*: the footprint now defers the write like the symbol (`DeferredCheckpoint`), has the arrow nudge (one drawing-grid step, Y down, through the drag-drop translate path) and R / Shift+R (clockwise / counter-clockwise quarter turn) on its selection (`IndexSelection`, else the hovered drawing), one undo step per press or repeat; every edit command pressed during a drag cancels the drag once through the gesture controller, then applies.
- **D9 Hide mid handle drag** (footprint): the view forgets the drag (663) but the window keeps `_fp_handle_active = True`, so the next list change sets `_fp_handle_cancelled` and one future drop is ignored (15318-15319, 15804-15806). Latent defect.
- **D10 Draw tools**: footprint press-move-release adds the release point (591-597); symbol wire/shape tools add points on press only.
- **D11 Zoom**: refuse outside [0.15, 20] (symbol) vs clamp to [0.02, 40] (footprint); scene scales differ; parameters.
- **D12 Stepped feedback**: the symbol commits the draft at every node (`has_pending_edits()` True mid-drag); the footprint shows a ghost and commits at the drop. End results identical; model-driven vs item-driven; keep.
- **D13 Auto-fit at the press of a drag** (review 2026-10-27, finding D2; decision of the coordinator, recorded for the user): the symbol switched `auto_fit` off right before `controller.press` and put it back when the host refused (symbol_tab.py `mousePressEvent`); the footprint never touched it, so a text or drawing drag with auto-fit still on refitted the camera at the drop (the scale changed under the user's hand). **Symbol rule in both tabs**: `FootprintView._press_gesture` switches `camera.auto_fit` off before `gestures.press(...)` and restores it when the press is refused. A pan is unchanged (footprint: `keep_auto_fit=True`, off only on a real move; symbol: off at the pan press). Tests in `tests/test_review_1027_canvas.py`: a drag with auto-fit on keeps the scale and the corner at the drop and leaves auto-fit off; the host sees auto-fit already off in `gesture_begin`; negative: a refused press leaves auto-fit on.

Justified (keep): pins, elements, vertices, shape points and the body step on
their grids (KiCad); text snap grids differ by user decision (symbol
properties on the 2.54 mm grid, footprint texts on the drawing grid through
the first pad); footprint drawing handles appear on a stroke hit with
hysteresis (no selection model) while the symbol reveals the selected
element's handles; the 12 px margin is shared and pinned; rubber band,
Ctrl+click, nudge/rotate only in the symbol; footprint handles are created on
hover and removed at rest (`_handles(window) == []` asserted).

## 2. Target design

### 2a. Module `kicadgen/gui/canvas_gestures.py`

Qt-only, no model imports, no translated strings. Both views keep their
classes and signals and delegate to these objects. The module owns: the state
machine (idle → dragging → idle), the live-vs-stepped policy, "release always
passes through a move", a single `end` per drag, state cleared BEFORE host
callbacks (re-entrancy), cancel from Escape/hide/host, the hover reveal rule,
the auto-repeat deferral, the identical-step memo. Hit-testing, snapping and
committing stay in the hosts.

```python
HOVER_MARGIN_PX = 12      # symbol_tab.HANDLE_HOVER_MARGIN and footprint_view.HOVER_KEEP_MARGIN_PX become aliases
HANDLE_SIZE_PX = 6

class Identity:
    """Equality by identity, for StepMemo tokens (the symbol's `last[2] is self._draft`, 4022)."""
    def __init__(self, obj); def __eq__(self, other) -> bool

class StepMemo:
    """What the last step of the running drag committed (generalises `_gesture_step_repeats` 4001-4028)."""
    def start(self) -> None
    def stop(self) -> None                        # repeats() is False when stopped
    def repeats(self, kind: str, key, token=()) -> bool
    def remember(self, kind: str, key, token=()) -> None  # no-op when stopped

@dataclass
class DragPlan:
    policy: str                                   # 'live' | 'stepped'
    live_items: list[tuple[QGraphicsItem, QPointF]] = []   # (item, pos at press); 'live' only
    cursor: Qt.CursorShape | None = None
    hover_owner: tuple | None = None              # hover group kept revealed while dragging

class GestureHost(Protocol):
    def gesture_begin(self, target: tuple, point: QPointF) -> DragPlan | None   # None refuses the press
    def gesture_step(self, target: tuple, point: QPointF) -> None               # 'stepped': every move AND the release point
    def gesture_end(self, target: tuple, last: QPointF | None, commit: bool) -> None  # exactly once per drag

class GestureController(QObject):
    started = pyqtSignal(object, object); moved = pyqtSignal(object, object); finished = pyqtSignal(bool)
    def __init__(self, view: QGraphicsView, host: GestureHost, *, grab_mouse: bool)
    target: tuple | None; plan: DragPlan | None; last_point: QPointF | None; start_point: QPointF | None
    memo: StepMemo
    def press(self, target, point) -> bool
    def move(self, point) -> bool
    def release(self, point) -> bool            # self.move(point); then _finish(commit=True)
    def cancel(self) -> bool                    # idle → False; else _finish(commit=False); never touches items
    def rebind_live_items(self, items) -> None  # redraw mid-drag: new items + re-apply the last delta
    def forget_items(self) -> None              # before scene.clear(): drop every item reference

class CancelOnEscapeAndHide(QObject):          # event filter: Escape while a drag runs, and Hide, call controller.cancel()

class HoverGroups:
    def __init__(self, view, margin_px=HOVER_MARGIN_PX)
    groups: list[tuple[list[QGraphicsItem], QRectF, bool, tuple]]
    def clear(self) -> None
    def add(self, items, base: QRectF, ring: bool, owner: tuple) -> None
    def revealed(self, point: QPointF | None, dragged_owner: tuple | None) -> set[tuple]
    def apply(self, point, dragged_owner, *, setter=None) -> set[tuple]

class KeyRepeatFilter(QObject):                # ShortcutOverride records isAutoRepeat; non-repeat KeyRelease, FocusOut, Hide emit repeat_ended
    repeat_ended = pyqtSignal()
    def take(self) -> bool

class DeferredCheckpoint:
    def __init__(self, keys: KeyRepeatFilter, write: Callable[[], None])
    deferring: bool; pending: bool
    def run(self, action) -> None
    def request(self) -> bool                     # True → write now; False → deferred (pending=True)
    def flush(self) -> None

def bind_shortcuts(view, bindings: list[tuple[str, Callable]], runner, context) -> list[QShortcut]
```

Tab-specific: press routing and target discovery; every snap and commit
function (`place_pin`, `move_property`, `resize_body`, `move_wire_vertex`,
`move_shape_point`, `_transform_selected`, graphic group rule 4456-4518;
`_snapped_draw_point`, `_set_footprint_text_position`,
`_on_footprint_handle_dropped`); draft/undo/checkpoint/status; redraws; the
frozen scene rect during a drag (`_gesture_scene_rect` 3962, 5213-5216); the
footprint drawing hover-on-hit (F8) and on-demand handle building; the symbol
rubber band; pan/zoom/fit (phase C); click tools (phase B).

### 2b. How each view plugs in

Symbol: `SymbolTab.__init__` creates the controller (host = the tab,
`grab_mouse=True`), `HoverGroups`, `KeyRepeatFilter`, `DeferredCheckpoint`,
`CancelOnEscapeAndHide`. `_SymbolView.mousePressEvent` 732-742 → `controller.press`;
793-796 → `move`; 819-826 → `release`; `cancel_interaction` keeps the rubber
band/pan part and calls `controller.cancel()`; `gesture` stays a read-only
property returning `controller.target` (read in 696, 720, 750, 1041, 4755 and
by tests). `event`/`take_key_repeat`/`keyReleaseEvent`/`focusOutEvent` 612-652
are replaced by the filter. `_begin_gesture` → `gesture_begin` returning a
`DragPlan('live', ...)` for properties and `DragPlan('stepped', ...)` otherwise
(owner mapping of 4758-4760); `_move_gesture` → `gesture_step` minus the
property branch; `_end_gesture` → `gesture_end` keeping 4580-4590; the state
clearing 4575-4579 moves into the controller. `_gesture_step_repeats` →
`memo.repeats('accept', rows, token=(Identity(self._draft), self.status.text()))`.
`_draw` 5186-5196 → `rebind_live_items`. `_forget_scene_items` → `hover.clear()`
+ `controller.forget_items()`. `_handle_groups` → `hover.groups` (alias for
tests/test_kept_scene_items.py). Keys 1384-1410 → `bind_shortcuts(...,
runner=self._checkpoint.run)`; `_checkpoint_draft` 2259-2266 → `if not
self._checkpoint.request(): return` after the existing guards.

Footprint: `FootprintView.__init__` creates the controller (`grab_mouse=False`)
with the MainWindow as host (methods `gesture_begin/step/end` next to
15759-15833; the signals `handle_pressed/dragged/dropped` and `text_moved`
may stay as the host's emissions so tests calling
`window._on_footprint_handle_dropped(...)` keep passing). Targets: `('text',
key)` → live plan with the group as live item, cursor SizeAll, hover owner;
`('handle', i, p)` and `('drawing', i)` → stepped. `gesture_step` = 15774-15793
with the identical-step check through the memo; `gesture_end(commit=True)` =
15795-15833; `commit=False` = `cancel_interaction` + `_end_footprint_drawing_interaction`
semantics (D9 closes by construction). `_text_drag`/`_handle_drag` disappear;
`_pan` stays. Text hover: `HoverGroups.add([square], reveal_area, False,
('text', key))` from `_draw_footprint_texts` 18801-18830. Drawing hover (F8)
stays in `FootprintView` in phase A. Keys 14487-14495 → `bind_shortcuts`
(deferral = D8, user decision).

### 2c. Mapping of the guarantees

Undo/history unchanged (symbol records only when `_gesture_before is None`,
3761; footprint pushes once on the drop). Recovery checkpoints unchanged
(symbol early-return 2259; footprint on the drop). `StepMemo` reproduces
«only while a drag runs» and «nothing outside a drag». Escape/hide via the
cancel filter, repeat end via the key filter; hide must cancel the drag
BEFORE the flush (today 874-875): install `KeyRepeatFilter` first,
`CancelOnEscapeAndHide` second (Qt runs filters in reverse installation
order), pinned by a unit test. Kept-items rule: the shared objects hold only
Python-built items added with `addItem`; `forget_items()`/`clear()` are called
before every `scene.clear()`; `_screen_handle` 1224-1247 must switch to
`QGraphicsRectItem` + `addItem` if `HoverGroups` registers footprint handles;
the coordinator extends `tests/test_kept_scene_items.py` to walk `vars()` of
the shared objects. `_exposed_region`/`_grid_indices`/`drawBackground`,
`_pin_items`, `kicad_text_item` untouched.

## 3. Migration sequence (every step leaves the suite green)

0. Coordinator: commit today's live property drag; write the plan summary and
   the assignment into HANDOFF.md before any code.
1. Characterisation (golden) tests first, by agents 2 and 3 in their own
   files, run and committed against the CURRENT code:
   `tests/test_symbol_gesture_parity.py` (real QTest scripts for S1-S8, S10,
   S11; committed rows, history, recovery JSON, per-phase counts, twin oracle
   through the direct APIs, negatives: Escape, hide, `set_data`, wheel, right
   click, redraw mid-drag, hover margin−1/+2) and
   `tests/test_footprint_gesture_parity.py` (F1-F5, F8, F9; negatives: cancel
   mid-drag, hide, redraw, Escape as today). Golden JSON under
   `tests/data/gesture_golden/*.json`, regenerated only with an explicit env
   var.
2. Agent 1: `kicadgen/gui/canvas_gestures.py` + `tests/test_canvas_gestures.py`
   (bare `QGraphicsView` + recording fake host: state machine,
   release-through-move, single end, re-entrancy, live setPos math, rebind,
   deleted-item skip, memo incl. `Identity`, hover rule incl. ring and dragged
   owner, filter order, key repeat take/clear, deferred run/flush; negative
   twins).
3. Symbol adopts `KeyRepeatFilter` + `DeferredCheckpoint` + `bind_shortcuts`
   (sentinels: test_symbol_editor_perf_a_key_repeat.py,
   test_symbol_tab.py::test_precise_pin_edit_and_keyboard_step,
   test_symbol_grid_paint_region.py first-nudge test).
4. Symbol adopts `StepMemo` (sentinels: test_perf_p2_symbol_drag.py,
   test_symbol_property_drag_smooth.py pin test).
5. Symbol adopts `GestureController` + cancel filter (sentinels:
   test_symbol_tab.py 128, 168, 220, 284, 370, 434;
   test_symbol_advanced.py::test_group_mouse_drag_is_one_undo_step;
   test_symbol_property_drag_smooth.py; test_symbol_property_anchor.py 295-343;
   test_symbol_editor_perf_a_incremental.py drag test; test_kept_scene_items.py;
   the parity file).
6. Symbol adopts `HoverGroups` (sentinels: test_symbol_handles_hover.py — lines
   125-131 assign `tab.view.gesture` directly: the coordinator rewrites them
   to drive the controller; test_symbol_property_anchor.py 257-310).
7. Footprint adopts `GestureController` + memo + cancel path (sentinels:
   test_footprint_text_positions.py 188, 210 (pokes `view._pan`/`view._text_drag`);
   test_footprint_drawings.py 431, 546, 592, 608, 784, 804, 822, 861, 887;
   test_footprint_property_anchor.py 226-296, 506-580; test_footprint_draft.py
   155, 180, 449; test_preview_grid.py::test_footprint_zoom_anchor_pan_and_fit).
8. Footprint text hover through `HoverGroups` (sentinels:
   test_footprint_property_anchor.py 506-580, the reveal-margin test).
9. Decisions D1–D5, D8, D9 applied one by one, each with its positive/negative
   test, only after the user's OK; the parity tests that recorded «today's»
   behaviour for them are updated with the new contract and the reason.
10. Optional phase B (click tools S13/S14/S15 and F6, incl. D10) and phase C
    (`Camera` helper for pan/zoom/fit; sentinels:
    test_symbol_tab.py::test_cad_pan_zoom_anchor_and_fit_do_not_edit_symbol,
    test_symbol_editor_perf_a_incremental.py::test_auto_fit_refits_only_when_the_camera_changed,
    test_preview_grid.py 10/80,
    test_footprint_drawings.py::test_an_edit_keeps_the_camera_once_the_user_has_zoomed).
11. Dead code removal, documentation test, full suite in two halves (HANDOFF
    113), HANDOFF counts, commit.

## 4. Work split (disjoint files) and the fixed contract

| Agent | Files (exclusive) | Deliverable |
|---|---|---|
| 1 (shared) | `kicadgen/gui/canvas_gestures.py`, `tests/test_canvas_gestures.py` | the API of §2a exactly, Doxygen complete, no imports from symbol_tab/footprint_view/main_window |
| 2 (symbol) | `kicadgen/gui/symbol_tab.py`, `tests/test_symbol_gesture_parity.py`, `tests/data/gesture_golden/symbol_*.json` | parity tests first (against the current code), then steps 3-6 |
| 3 (footprint) | `kicadgen/gui/footprint_view.py`, `kicadgen/gui/main_window.py`, `tests/test_footprint_gesture_parity.py`, `tests/data/gesture_golden/footprint_*.json` | parity tests first, then steps 7-8 |
| Coordinator | `tests/test_kept_scene_items.py`, `tests/test_symbol_handles_hover.py` (125-131), `tests/test_footprint_text_positions.py` (210-219), HANDOFF.md, commits, full suite, adversarial review, decisions D1-D12 with the user |

Agents 2 and 3 start on the parity tests while agent 1 writes the module; they
adopt it only after agent 1's unit tests are green (the coordinator signals
the hand-over).

Contract semantics (binding for the three):

1. `press(target, point)` returns False and does nothing while a drag runs or when `gesture_begin` returns None; on success, in this order: `memo.start()`, `host.gesture_begin` BEFORE the grab (a refusal leaves no capture), grab mouse (if `grab_mouse`), set cursor (if any), `started.emit(target, point)`.
2. `move(point)`: 'live' → `item.setPos(origin + (point - start))` for every live item whose wrapper is not deleted (`sip.isdeleted`), never raising; 'stepped' → `host.gesture_step(target, point)`; both store `last_point` and emit `moved`. Idle → False.
3. `release(point)`: `move(point)` then finish with `commit=True` (820-821; footprint 603).
4. Finish order: capture `(target, last)`, clear `target/plan/live_items/last_point/start_point`, `memo.stop()`, release mouse and cursor, THEN `host.gesture_end(target, last, commit)`, THEN `finished.emit(commit)`. A `cancel()`/`press()` from inside `gesture_end` must be safe (idle at that moment).
5. `cancel()` does not move items; the picture is the host's (`_populate(before)` or nothing).
6. `rebind_live_items(items)` replaces the live list and re-applies `last_point - start_point` if a move happened; no-op when idle.
7. `forget_items()` drops every item reference without touching the drag state.
8. `HoverGroups.revealed(point, dragged_owner)`: owner revealed iff `owner == dragged_owner`, else iff point is not None and `base.adjusted(-m,-m,m,m).contains(point)` and not (`ring` and `base.adjusted(m,m,-m,-m).contains(point)`), with `m = margin_px / abs(view.transform().m11() or 1.0)` (4753-4768).
9. `StepMemo`: `repeats` compares `(kind, key, token)` with `==`; `Identity` compares with `is`; `remember` outside start/stop stores None.
10. `KeyRepeatFilter.take()` returns and clears the flag recorded by the last ShortcutOverride; `repeat_ended` on a non-auto-repeat KeyRelease, on FocusOut and on Hide.
11. `DeferredCheckpoint.request()` returns False and sets `pending` only while `deferring`; `flush()` calls `write()` only when `pending`; `write()` clears `pending` through `request()` returning True.
12. Filter installation order: key filter first, cancel filter second, so that Hide cancels the drag before the repeat flush.

## 5. Risks and traps

- Qt deletes items the controller references (`scene.clear()` 18342/18351, `_clear_scene` 4914-4931): only Python-built items registered, `forget_items()`/`clear()` before every clear, `sip.isdeleted` in `move()`, kept-items walk extended to the shared objects.
- An exception in a slot aborts the app: no new exception paths in signal handlers; the controller never raises on stale items; host guards kept where they existed; unit test with a deleted-item move.
- PyQt6 `QRegion` is not iterable: relevant only to `_grid_indices` 919-951, untouched (phase C must not move `paintEvent`/`drawBackground` out of `_SymbolView`).
- Offscreen hover limits: MouseMove is delivered, Enter/Leave and cursors are not; tests call `leaveEvent`/`pointer_left.emit()` directly and never assert cursor shapes.
- Order-dependent tests / shared session: fresh tab/window per test; `no_application_font_in_the_shared_session` forbids fonts: compare positions/rows, not pixels, except same-process before/after grabs.
- Private attributes poked by tests (`view.gesture` assignment, `view._pan/_text_drag`, `tab._handle_groups/_property_items/_gesture_live`): read-only aliases where cheap; otherwise the coordinator updates the contract consciously.
- Re-entrancy (`gesture_end` → `move_property` → `_changed` → `_draw`; `_apply_footprint_drawings` → `_footprint_draft_changed` → `view.cancel_interaction()` inside the drop): contract item 4; unit-tested with a re-entering host.
- Filter order (item 12) and the removal of the symbol's `event()` override: ShortcutOverride must still be seen before the shortcut activates (an event filter on the view sees it first).
- D12 is not a bug: the symbol must keep committing the draft at each node (pixel identity of the incremental pin redraw depends on `_accept_geometry` → `_draw` per node).
- Documentation test: `@brief` everywhere; comments in English.
- Full suite in two halves (HANDOFF 113).

## 6. Size and measurements

Lines (estimate): `canvas_gestures.py` new ≈ 450-550; `tests/test_canvas_gestures.py` ≈ 350; `symbol_tab.py` ≈ 320 touched, net ≈ −130; `footprint_view.py` ≈ 220 touched, net ≈ −90; `main_window.py` ≈ 140 touched; parity tests ≈ 300 + 300; coordinator test edits ≈ 30.

Measurements BEFORE and AFTER each adoption step (outputs under the scratchpad `plan/` folder): `scratchpad/p2/probe_symbol.py` (`N=80`, ms per event, no new frames in the per-move path except `GestureController.move`); `scratchpad/a/probe_keys4.py` (`N=900`, draft writes 10/0/1); `scratchpad/a2/probe_drag.py property|pin` (per-move counts line-identical); `scratchpad/probe_grid_edits.py` (bg paints, full-viewport, sceneRectChanged identical); pixel grabs at rest / hovered / mid property drag / mid pin drag compared to PNGs saved BEFORE; a footprint probe (`plan/probe_fp_drag.py`, to write) counting `_draw_preview`, `_refresh_drawing_handles`, `_checkpoint_footprint_draft`, `save_footprint_draft` per move and per drop for F1/F2/F3, plus `len(window._fp_drawings_undo)` and the committed records.
