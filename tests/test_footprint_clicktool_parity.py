"""@brief Characterisation of the footprint draw mode (plan docs/canvas-clicktools-plan.md, §4 step 1).

Written against the code BEFORE the shared click-tool collector and router
are adopted, so the migration can prove "same records, same counts, except
the decisions of §3". Every test drives the real event path (QTest press /
move / release / key events on the preview viewport of a shown window) and
pins what the CURRENT code does: the committed `footprint_drawings`, the undo
depth, the recovery file, the collected clicks, the toasts, the marker items
in the scene and the per-phase call counts of `_refresh_draw_markers`,
`_edit_footprint_draft`, `_draw_preview`, `_checkpoint_footprint_draft` and
`save_footprint_draft`.

The scenarios that can be played without pointer geometry are also run
through the direct host API (`_on_footprint_draw_clicked`,
`_on_footprint_draw_finish`, `_cancel_footprint_drawing`) as a twin oracle:
the committed output must equal the one the events produced. The per-phase
records of the event run live in `tests/data/clicktool_golden/footprint_*.json`
(read by parsing the JSON, so line endings never matter), regenerated only
when `KICADGEN_REGEN_CLICKTOOL_GOLDEN` is set.

Tests that record TODAY's value of a decision of the plan (§3) carry the
decision id (C1, C2, C3, C6, C7, C8, C9, C10) in their name and say what
phase 2 flips.
"""
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt              # noqa: E402
from PyQt6.QtGui import QCursor, QHideEvent, QMouseEvent, QWheelEvent  # noqa: E402
from PyQt6.QtTest import QTest                                    # noqa: E402
from PyQt6.QtWidgets import QApplication                          # noqa: E402

#: Folder of the golden records; the env var regenerates them.
GOLDEN_DIR = Path(__file__).resolve().parent / "data" / "clicktool_golden"
REGEN_ENV = "KICADGEN_REGEN_CLICKTOOL_GOLDEN"

#: Scene scale of the footprint preview (px per mm).
PX = 20.0

#: Item role of the draw-mode markers (clicked dots, ghost, snap dot).
MARKER_ROLE = 0x44524D

#: Fields of a phase record that a direct-API run must reproduce.
COMMITTED_KEYS = ("drawings", "texts", "undo", "redo", "recovery", "clicks", "tool", "toasts")


# ── Golden helpers ─────────────────────────────────────────────────────

def _golden(name, phases):
    """Compare the phase records with `footprint_<name>.json` by PARSING both (no line-ending dependence)."""
    path = GOLDEN_DIR / f"footprint_{name}.json"
    text = json.dumps(phases, indent=2, sort_keys=True) + "\n"
    if os.environ.get(REGEN_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return
    assert path.exists(), f"golden {path.name} missing: run once with {REGEN_ENV}=1"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text), f"{path.name} differs from today's run"


def _golden_phases(name):
    """The recorded phases of an event run, parsed."""
    path = GOLDEN_DIR / f"footprint_{name}.json"
    assert path.exists(), f"golden {path.name} missing: run once with {REGEN_ENV}=1"
    return json.loads(path.read_text(encoding="utf-8"))


# ── Recorder ───────────────────────────────────────────────────────────

class _Rec:
    """Counts the window methods, collects toasts and snapshots the draw state phase by phase."""

    #: `_cancel_footprint_drawing` is deliberately NOT counted: it is a thin wrapper that
    #: different routes reach (a bound method captured by a signal, or a host callback), so
    #: its call count measures the wiring, not the behaviour.
    NAMES = ("_refresh_draw_markers", "_edit_footprint_draft", "_draw_preview",
             "_checkpoint_footprint_draft")

    def __init__(self, window, monkeypatch):
        from kicadgen.services import footprint_draft
        self.window = window
        self.calls = {}
        self.toasts = []
        self.phases = {}
        for name in self.NAMES:
            monkeypatch.setattr(window, name, self._wrap(name, getattr(window, name)))
        monkeypatch.setattr(footprint_draft, "save_footprint_draft",
                            self._wrap("save_footprint_draft", footprint_draft.save_footprint_draft))
        monkeypatch.setattr(window, "_draw_feedback_toast",
                            lambda title, body: self.toasts.append([title, body]))

    def _wrap(self, name, original):
        def counting(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            return original(*args, **kwargs)
        return counting

    def markers(self):
        """Class name -> count of the draw-mode marker items now in the scene."""
        found = {}
        for item in self.window.preview_scene.items():
            if item.data(MARKER_ROLE) is not None:
                found[type(item).__name__] = found.get(type(item).__name__, 0) + 1
        return dict(sorted(found.items()))

    def ghost_rect(self):
        """Rounded bounding rect of the dashed ghost path, or None."""
        from PyQt6.QtWidgets import QGraphicsPathItem
        for item in self.window.preview_scene.items():
            if item.data(MARKER_ROLE) is not None and isinstance(item, QGraphicsPathItem):
                rect = item.path().boundingRect()
                return [round(rect.x(), 3), round(rect.y(), 3), round(rect.width(), 3), round(rect.height(), 3)]
        return None

    def phase(self, name):
        """Close the running phase under `name`: counts since the previous phase, then the state."""
        window = self.window
        path = window._footprint_recovery_path()
        recovery = None
        if path is not None and path.exists():
            draft = json.loads(path.read_text(encoding="utf-8"))["draft"]
            recovery = {"texts": draft["texts"], "drawings": draft["drawings"]}
        record = {
            "calls": dict(sorted(self.calls.items())),
            "drawings": deepcopy(window.footprint_drawings),
            "texts": {k: list(v) for k, v in sorted(window.footprint_text_positions.items())},
            "undo": len(window._fp_drawings_undo),
            "redo": len(window._fp_drawings_redo),
            "recovery": recovery,
            "clicks": [list(p) for p in window._fp_draw_clicks],
            "tool": window._fp_draw_tool,
            "hover": None if window._fp_draw_hover is None else list(window._fp_draw_hover),
            "toasts": [list(t) for t in self.toasts],
            "markers": self.markers(),
            "checked": sorted(k for k, b in window._fp_draw_buttons.items() if b.isChecked()),
        }
        assert name not in self.phases, f"phase {name!r} recorded twice"
        self.phases[name] = record
        self.calls = {}
        self.toasts = []
        return record


# ── Fixture: a shown window with the preview tab in front ──────────────

@pytest.fixture
def fp(window, qapp, monkeypatch):
    """The preview tab shown and active, camera at 1 px per scene px, 1 mm drawing grid, auto_fit off."""
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_generate", lambda **_kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite", lambda **_kwargs: None)
    assert window._save_project_config(force=True), "the project folder exists: recovery files can be written"
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()
    assert window.preview_view.isVisible()
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    window.combo_fine_grid.setCurrentIndex(window.combo_fine_grid.findData(1))
    window._draw_preview()
    _place_camera(window)
    return window


@pytest.fixture
def dialog(monkeypatch):
    """A fake `QInputDialog` for the text tool: `answer[0]` is what `getText` returns."""
    from kicadgen.gui import main_window as gui
    answer = [("Hi", True)]

    class _Fake:
        asked = []

        @staticmethod
        def getText(*args, **kwargs):
            _Fake.asked.append(1)
            return answer[0]

    monkeypatch.setattr(gui, "QInputDialog", _Fake)
    return answer


def _place_camera(window):
    view = window.preview_view
    view.resetTransform()
    view.centerOn(0, 0)
    view.auto_fit = False


def _vp(window, x_mm, y_mm, offset=(0, 0)):
    """Viewport point of a footprint point (mm), at the placed camera."""
    return window.preview_view.mapFromScene(QPointF(x_mm * PX, y_mm * PX)) + QPoint(*offset)


def _press(window, pos, button=Qt.MouseButton.LeftButton):
    QTest.mousePress(window.preview_view.viewport(), button, Qt.KeyboardModifier.NoModifier, pos)


def _release(window, pos, button=Qt.MouseButton.LeftButton):
    QTest.mouseRelease(window.preview_view.viewport(), button, Qt.KeyboardModifier.NoModifier, pos)


def _move(window, pos):
    """A button-less move that is always delivered.

    QTest sets the platform cursor for a button-less move and an unchanged
    cursor position produces no event: when the cursor already sits on
    `pos` (left there by a previous test) it is nudged by one pixel first,
    which snaps to the same node and so changes nothing.
    """
    viewport = window.preview_view.viewport()
    if QCursor.pos() == viewport.mapToGlobal(pos):
        QTest.mouseMove(viewport, pos + QPoint(1, 1))
    QTest.mouseMove(viewport, pos)


def _dblclick(window, pos):
    """The event a fast second click delivers: Qt sends a DOUBLE CLICK instead of the second press."""
    view = window.preview_view
    event = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(pos),
                        QPointF(view.viewport().mapToGlobal(pos)), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), event)


def _wheel(window, pos, steps):
    view = window.preview_view
    event = QWheelEvent(QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)), QPoint(),
                        QPoint(0, 120 * steps), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), event)


def _key(window, key):
    QTest.keyClick(window.preview_view, key)


# ── Drivers: the same script through events and through the host API ───

class _Events:
    """Plays a script through real viewport events."""

    def __init__(self, window):
        self.w = window

    def move(self, x, y):
        _move(self.w, _vp(self.w, x, y))

    def click(self, x, y):
        _press(self.w, _vp(self.w, x, y))
        _release(self.w, _vp(self.w, x, y))

    def fast_second_click(self, x, y):
        """The second click of a double click: only a double-click event (and its release) arrives."""
        _dblclick(self.w, _vp(self.w, x, y))
        _release(self.w, _vp(self.w, x, y))

    def drag(self, a, b):
        """Press at `a`, one move half way, release at `b`."""
        _press(self.w, _vp(self.w, *a))
        _move(self.w, _vp(self.w, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        _release(self.w, _vp(self.w, *b))

    def right(self):
        pos = _vp(self.w, 0, 0)
        _press(self.w, pos, Qt.MouseButton.RightButton)
        _release(self.w, pos, Qt.MouseButton.RightButton)

    def enter(self):
        _key(self.w, Qt.Key.Key_Return)

    def escape(self):
        _key(self.w, Qt.Key.Key_Escape)


class _Direct:
    """Plays the same script through the window's host methods."""

    def __init__(self, window):
        self.w = window

    def move(self, x, y):
        self.w._on_footprint_draw_hovered(x * PX, y * PX)

    def click(self, x, y):
        self.w._on_footprint_draw_clicked(x * PX, y * PX)

    def fast_second_click(self, x, y):
        self.w._on_footprint_draw_clicked(x * PX, y * PX)
        self.w._on_footprint_draw_finish()

    def drag(self, a, b):
        self.w._on_footprint_draw_clicked(a[0] * PX, a[1] * PX)
        self.w._on_footprint_draw_clicked(b[0] * PX, b[1] * PX)

    def right(self):
        self.w._on_footprint_draw_finish()

    def enter(self):
        self.w._on_footprint_draw_finish()

    def escape(self):
        self.w._cancel_footprint_drawing()


# ── Scenarios (shared by the event run and the twin) ───────────────────

def _sc_rectangle(w, d, rec, dialog):
    """Click-click rectangle: auto-finish at the second click, one commit, tool disarmed."""
    w._toggle_footprint_draw_tool("rectangle")
    rec.phase("armed")
    d.click(1, 1)
    rec.phase("first")
    d.click(5, 4)
    rec.phase("closed")


def _sc_rectangle_hovered(w, d, rec, dialog):
    """The same rectangle with a pointer move before each click, as a user does."""
    w._toggle_footprint_draw_tool("rectangle")
    rec.phase("armed")
    d.move(1, 1)
    rec.phase("hover_1")
    d.click(1, 1)
    rec.phase("first")
    d.move(3, 2)
    rec.phase("hover_2")
    d.move(5, 4)
    d.click(5, 4)
    rec.phase("closed")


def _sc_circle(w, d, rec, dialog):
    w._toggle_footprint_draw_tool("circle")
    d.click(0, 0)
    d.click(3, 0)
    rec.phase("closed")


def _sc_arc(w, d, rec, dialog):
    """Click order start, end, mid; stored order start, mid, end."""
    w._toggle_footprint_draw_tool("arc")
    d.click(0, 0)
    d.click(4, 0)
    d.move(2, 2)
    rec.phase("two_clicks_and_hover")
    d.click(2, 2)
    rec.phase("closed")


def _sc_line_enter(w, d, rec, dialog):
    w._toggle_footprint_draw_tool("line")
    for point in ((0, 0), (2, 0), (2, 2)):
        d.click(*point)
    rec.phase("three_points")
    d.enter()
    rec.phase("finished")


def _sc_line_right(w, d, rec, dialog):
    w._toggle_footprint_draw_tool("line")
    for point in ((0, 0), (3, 0)):
        d.click(*point)
    d.right()
    rec.phase("finished")


def _sc_text(w, d, rec, dialog):
    w._toggle_footprint_draw_tool("text")
    d.click(2, 3)
    rec.phase("placed")


def _sc_text_cancelled(w, d, rec, dialog):
    dialog[0] = ("   ", True)
    w._toggle_footprint_draw_tool("text")
    d.click(2, 3)
    rec.phase("blank")
    dialog[0] = ("Hi", False)
    w._toggle_footprint_draw_tool("text")
    d.click(2, 3)
    rec.phase("dismissed")


def _sc_flat_rectangle(w, d, rec, dialog):
    """Degenerate at the validator: a toast says why, the tool is disarmed, nothing is stored."""
    w._toggle_footprint_draw_tool("rectangle")
    d.click(0, 2)
    d.click(4, 2)
    rec.phase("flat")


def _sc_collinear_arc(w, d, rec, dialog):
    w._toggle_footprint_draw_tool("arc")
    d.click(0, 0)
    d.click(4, 0)
    d.click(2, 0)
    rec.phase("collinear")


def _sc_short_line(w, d, rec, dialog):
    """Fewer than two points at the finish: disarmed, no toast, nothing stored."""
    w._toggle_footprint_draw_tool("line")
    d.click(1, 1)
    d.enter()
    rec.phase("one_point")


def _sc_empty_finish(w, d, rec, dialog):
    """Finish with no point at all (C4): disarmed, quietly."""
    w._toggle_footprint_draw_tool("line")
    d.right()
    rec.phase("empty_right")
    w._toggle_footprint_draw_tool("rectangle")
    d.enter()
    rec.phase("empty_enter")


def _sc_duplicate(w, d, rec, dialog):
    """C3 today: a click on the node just collected is refused WITH a toast; the tool stays armed."""
    w._toggle_footprint_draw_tool("line")
    d.click(2, 2)
    rec.phase("first")
    d.click(2, 2)
    rec.phase("duplicate")
    d.click(4, 2)
    rec.phase("next")
    d.enter()
    rec.phase("finished")


def _sc_fast_double_click(w, d, rec, dialog):
    """Two fast clicks on a circle: press, release, then only a double click."""
    w._toggle_footprint_draw_tool("circle")
    d.click(0, 0)
    d.fast_second_click(3, 0)
    rec.phase("closed")


def _sc_double_click_last_vertex(w, d, rec, dialog):
    """C2 today: a genuine double click on the LAST vertex of a line gives the coincident-point toast, then finishes."""
    w._toggle_footprint_draw_tool("line")
    d.click(0, 0)
    d.click(3, 0)
    rec.phase("two_points")
    d.fast_second_click(3, 0)
    rec.phase("double_click")


def _sc_double_click_new_vertex(w, d, rec, dialog):
    """Negative twin of C2: a double click on a NEW node adds it once, finishes, and shows no toast."""
    w._toggle_footprint_draw_tool("line")
    d.click(0, 0)
    d.click(3, 0)
    d.fast_second_click(3, 3)
    rec.phase("double_click")


def _sc_escape(w, d, rec, dialog):
    w._toggle_footprint_draw_tool("circle")
    d.click(0, 0)
    rec.phase("one_click")
    d.escape()
    rec.phase("escaped")


def _sc_drag_rectangle(w, d, rec, dialog):
    """C1 today (ON for every tool): press-move-release beyond the drag distance draws the rectangle."""
    w._toggle_footprint_draw_tool("rectangle")
    d.drag((0, 0), (6, 4))
    rec.phase("dragged")


#: Scenarios the direct host API can replay with identical committed output.
#: `double_click_last_vertex` is not among them since C2: the direct API has no quiet click.
TWIN_SCENARIOS = {
    "rectangle": _sc_rectangle,
    "rectangle_hovered": _sc_rectangle_hovered,
    "circle": _sc_circle,
    "arc": _sc_arc,
    "line_enter": _sc_line_enter,
    "line_right": _sc_line_right,
    "text": _sc_text,
    "text_cancelled": _sc_text_cancelled,
    "flat_rectangle": _sc_flat_rectangle,
    "collinear_arc": _sc_collinear_arc,
    "short_line": _sc_short_line,
    "empty_finish": _sc_empty_finish,
    "duplicate": _sc_duplicate,
    "fast_double_click": _sc_fast_double_click,
    "double_click_new_vertex": _sc_double_click_new_vertex,
    "escape": _sc_escape,
    "drag_rectangle": _sc_drag_rectangle,
}


EVENT_ONLY_SCENARIOS = {"double_click_last_vertex": _sc_double_click_last_vertex}


def _play(window, monkeypatch, dialog, name, driver):
    rec = _Rec(window, monkeypatch)
    ({**TWIN_SCENARIOS, **EVENT_ONLY_SCENARIOS})[name](window, driver(window), rec, dialog)
    return rec


def _committed(phases):
    return {phase: {key: record[key] for key in COMMITTED_KEYS} for phase, record in phases.items()}


# ── Events: each scenario pins its values, then the golden ─────────────

def test_a_click_click_rectangle_closes_on_the_second_click_and_commits_once(fp, monkeypatch, dialog):
    """@brief Rectangle: 2 clicks, auto-finish, tool disarmed, one undo step, one recovery write."""
    rec = _play(fp, monkeypatch, dialog, "rectangle", _Events)
    p = rec.phases
    assert p["armed"]["tool"] == "rectangle" and p["armed"]["checked"] == ["rectangle"]
    assert p["first"]["clicks"] == [[1.0, 1.0]] and p["first"]["drawings"] == []
    assert p["first"]["calls"] == {"_refresh_draw_markers": 1}, "one marker refresh per accepted click"
    assert p["first"]["markers"] == {"QGraphicsEllipseItem": 1}, "the clicked dot (no ghost without a hover)"
    closed = p["closed"]
    assert closed["tool"] is None and closed["clicks"] == [] and closed["checked"] == []
    assert closed["drawings"] == [{"kind": "rectangle", "layer": "F.SilkS", "width": 0.12,
                                   "points": [[1.0, 1.0], [5.0, 4.0]]}]
    assert closed["undo"] == 1 and closed["recovery"]["drawings"] == closed["drawings"]
    assert closed["calls"]["save_footprint_draft"] == 1 and closed["calls"]["_edit_footprint_draft"] == 1
    assert closed["toasts"] == []
    _golden("rectangle", rec.phases)


def test_a_hovered_rectangle_refreshes_the_markers_only_when_the_snapped_node_changes(fp, monkeypatch, dialog):
    """@brief Hover: ghost and snap dot rebuilt per snapped-node change, not per move (a perf property to keep)."""
    rec = _play(fp, monkeypatch, dialog, "rectangle_hovered", _Events)
    p = rec.phases
    assert p["hover_1"]["calls"] == {"_refresh_draw_markers": 1} and p["hover_1"]["hover"] == [1.0, 1.0]
    assert p["hover_1"]["markers"] == {"QGraphicsEllipseItem": 1}, "snap dot only: one point has no ghost"
    assert p["first"]["calls"] == {"_refresh_draw_markers": 1}
    assert p["hover_2"]["hover"] == [3.0, 2.0] and p["hover_2"]["markers"] == {"QGraphicsEllipseItem": 2, "QGraphicsPathItem": 1}
    assert p["closed"]["drawings"][0]["points"] == [[1.0, 1.0], [5.0, 4.0]]
    _golden("rectangle_hovered", rec.phases)


def test_moves_within_one_node_do_not_rebuild_the_markers(fp, monkeypatch, dialog):
    """@brief Marker refreshes equal the number of snapped-node changes over a run of moves (BEFORE number)."""
    window = fp
    rec = _Rec(window, monkeypatch)
    window._toggle_footprint_draw_tool("rectangle")
    rec.phase("armed")
    track = [(0, 0), (3, 2), (6, 4), (0, 0), (6, 4), (14, 0), (-6, -2), (-14, -3), (0, 25), (25, 0), (26, 1)]
    nodes, changes, last = [], 0, None
    for dx, dy in track:
        pos = _vp(window, 2, 2, offset=(dx, dy))
        scene = window.preview_view.mapToScene(pos)
        node = window._snapped_draw_point(scene.x(), scene.y())
        changes += node != last
        last = node
        nodes.append(list(node))
        _move(window, pos)
    moves = rec.phase("moves")
    assert moves["calls"] == {"_refresh_draw_markers": changes}, "one refresh per snapped-node change"
    assert changes < len(track), "the track has moves inside one node"
    assert changes > 1, "and moves that change it"
    _golden("hover_node_changes", rec.phases)


def test_a_circle_stores_the_clicked_centre_and_rim(fp, monkeypatch, dialog):
    rec = _play(fp, monkeypatch, dialog, "circle", _Events)
    assert rec.phases["closed"]["drawings"][0]["points"] == [[0.0, 0.0], [3.0, 0.0]]
    assert rec.phases["closed"]["tool"] is None
    _golden("circle", rec.phases)


def test_an_arc_is_clicked_start_end_mid_and_stored_start_mid_end(fp, monkeypatch, dialog):
    """@brief Click order vs stored order: the ghost uses the click order, the record the KiCad one."""
    rec = _play(fp, monkeypatch, dialog, "arc", _Events)
    p = rec.phases
    assert p["two_clicks_and_hover"]["clicks"] == [[0.0, 0.0], [4.0, 0.0]]
    assert p["two_clicks_and_hover"]["hover"] == [2.0, 2.0]
    assert p["two_clicks_and_hover"]["markers"] == {"QGraphicsEllipseItem": 3, "QGraphicsPathItem": 1}
    assert p["closed"]["drawings"][0]["points"] == [[0.0, 0.0], [2.0, 2.0], [4.0, 0.0]]
    _golden("arc", rec.phases)


def test_the_arc_ghost_follows_the_click_order(fp, monkeypatch, dialog):
    """@brief The ghost with start, end and the hovered mid is the SAME arc the record stores."""
    window = fp
    rec = _Rec(window, monkeypatch)
    window._toggle_footprint_draw_tool("arc")
    _Events(window).click(0, 0)
    _Events(window).click(4, 0)
    _Events(window).move(2, 2)
    ghost = rec.ghost_rect()
    assert ghost is not None
    # An arc through (0,0), (2,2), (4,0) bulges up to y = 2 mm (40 px) and spans 4 mm (80 px).
    assert ghost[2] == pytest.approx(80.0, abs=0.5) and ghost[3] == pytest.approx(40.0, abs=0.5)
    # Negative twin: mid placed at the start's side gives a different, taller or wider shape.
    _Events(window).move(2, 4)
    other = rec.ghost_rect()
    assert other != ghost


def test_a_line_collects_points_and_enter_finishes_it(fp, monkeypatch, dialog):
    rec = _play(fp, monkeypatch, dialog, "line_enter", _Events)
    p = rec.phases
    assert len(p["three_points"]["clicks"]) == 3 and p["three_points"]["drawings"] == []
    assert p["three_points"]["tool"] == "line", "an open-ended tool stays armed until asked to finish"
    assert p["finished"]["drawings"][0]["points"] == [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]]
    assert p["finished"]["tool"] is None and p["finished"]["undo"] == 1
    _golden("line_enter", rec.phases)


def test_a_right_click_finishes_a_line(fp, monkeypatch, dialog):
    rec = _play(fp, monkeypatch, dialog, "line_right", _Events)
    assert rec.phases["finished"]["drawings"][0]["points"] == [[0.0, 0.0], [3.0, 0.0]]
    assert rec.phases["finished"]["tool"] is None
    _golden("line_right", rec.phases)


def test_the_text_tool_asks_for_the_text_once_the_point_is_clicked(fp, monkeypatch, dialog):
    """@brief Text: one click, then the dialog; the snapped node is the bottom-left anchor."""
    from kicadgen.gui import main_window as gui
    rec = _play(fp, monkeypatch, dialog, "text", _Events)
    record = rec.phases["placed"]["drawings"][0]
    assert record["kind"] == "text" and record["text"] == "Hi" and record["points"] == [[2.0, 3.0]]
    assert record["anchor"] and rec.phases["placed"]["tool"] is None
    assert len(gui.QInputDialog.asked) == 1
    _golden("text", rec.phases)


def test_a_blank_or_dismissed_text_dialog_disarms_without_a_drawing(fp, monkeypatch, dialog):
    rec = _play(fp, monkeypatch, dialog, "text_cancelled", _Events)
    for name in ("blank", "dismissed"):
        assert rec.phases[name]["drawings"] == [] and rec.phases[name]["tool"] is None
        assert rec.phases[name]["undo"] == 0 and rec.phases[name]["toasts"] == []
    _golden("text_cancelled", rec.phases)


def test_a_flat_rectangle_and_a_collinear_arc_are_refused_with_a_toast_and_disarm(fp, monkeypatch, dialog):
    """@brief Degenerate drawing: validator refusal, toast with the reason, tool disarmed, no undo step."""
    rect = _play(fp, monkeypatch, dialog, "flat_rectangle", _Events)
    flat = rect.phases["flat"]
    assert flat["drawings"] == [] and flat["tool"] is None and flat["undo"] == 0
    assert len(flat["toasts"]) == 1 and flat["toasts"][0][0] == fp.i18n.text("Disegno scartato.")
    assert flat["toasts"][0][1] == fp.i18n.text("La forma è degenere (raggio nullo o lato nullo): "
                                                "clicca punti distinti sulla griglia.")
    _golden("flat_rectangle", rect.phases)


def test_a_collinear_arc_toast_names_the_aligned_points(fp, monkeypatch, dialog):
    rec = _play(fp, monkeypatch, dialog, "collinear_arc", _Events)
    phase = rec.phases["collinear"]
    assert phase["drawings"] == [] and phase["tool"] is None and len(phase["toasts"]) == 1
    assert phase["toasts"][0][1] == fp.i18n.text("Arco senza centro: i tre punti sono allineati o quasi. "
                                                 "Sposta il punto intermedio fuori dalla retta.")
    _golden("collinear_arc", rec.phases)


def test_a_finish_with_too_few_points_disarms_quietly(fp, monkeypatch, dialog):
    """@brief One point on a line, or none at all (C4 today): disarmed, no toast, no drawing, no undo."""
    short = _play(fp, monkeypatch, dialog, "short_line", _Events)
    assert short.phases["one_point"]["tool"] is None and short.phases["one_point"]["toasts"] == []
    assert short.phases["one_point"]["drawings"] == [] and short.phases["one_point"]["undo"] == 0
    _golden("short_line", short.phases)


def test_c4_a_finish_with_no_point_disarms_quietly_today(fp, monkeypatch, dialog):
    """@brief C4 today: right click or Enter on an empty list disarms (the tab's own outcome, kept by the plan)."""
    rec = _play(fp, monkeypatch, dialog, "empty_finish", _Events)
    for name in ("empty_right", "empty_enter"):
        assert rec.phases[name]["tool"] is None and rec.phases[name]["toasts"] == []
        assert rec.phases[name]["drawings"] == [] and rec.phases[name]["undo"] == 0
    _golden("empty_finish", rec.phases)


def test_c3_a_click_on_the_node_just_collected_is_refused_with_a_toast_today(fp, monkeypatch, dialog):
    """@brief C3 today: duplicate point -> toast «Punto coincidente...», tool armed, no marker refresh, no new point.

    Phase 2 keeps this for the footprint (the symbol joins it through `tool_refused`).
    Negative twin: a click on another node is accepted without a toast.
    """
    rec = _play(fp, monkeypatch, dialog, "duplicate", _Events)
    p = rec.phases
    assert p["duplicate"]["clicks"] == [[2.0, 2.0]] and p["duplicate"]["tool"] == "line"
    assert p["duplicate"]["toasts"] == [[fp.i18n.text("Punto coincidente con il precedente."),
                                         fp.i18n.text("Con la griglia attiva i click si agganciano ai suoi nodi: "
                                                      "clicca un altro nodo o scegli una griglia più fine.")]]
    assert p["duplicate"]["calls"] == {}, "a refused click rebuilds nothing"
    assert p["next"]["clicks"] == [[2.0, 2.0], [4.0, 2.0]] and p["next"]["toasts"] == []
    assert p["finished"]["drawings"][0]["points"] == [[2.0, 2.0], [4.0, 2.0]]
    _golden("duplicate", rec.phases)


def test_the_duplicate_rule_compares_with_the_last_point_only(fp, monkeypatch, dialog):
    """@brief A line may come back to an EARLIER node: only the immediately repeated node is refused."""
    window = fp
    rec = _Rec(window, monkeypatch)
    driver = _Events(window)
    window._toggle_footprint_draw_tool("line")
    for point in ((0, 0), (3, 0), (0, 0)):
        driver.click(*point)
    assert window._fp_draw_clicks == [(0.0, 0.0), (3.0, 0.0), (0.0, 0.0)] and rec.toasts == []


def test_two_fast_clicks_on_a_circle_close_it_through_the_double_click(fp, monkeypatch, dialog):
    """@brief Press, release, DOUBLE CLICK (no second press): the double click is the missing point, then the finish."""
    rec = _play(fp, monkeypatch, dialog, "fast_double_click", _Events)
    closed = rec.phases["closed"]
    assert closed["drawings"][0]["points"] == [[0.0, 0.0], [3.0, 0.0]] and closed["tool"] is None
    assert closed["toasts"] == [], "the finish after a fixed-count close is a no-op, with no refusal"
    assert closed["undo"] == 1 and closed["calls"]["save_footprint_draft"] == 1
    _golden("fast_double_click", rec.phases)


def test_c2_a_double_click_on_the_last_vertex_of_a_line_finishes_it_without_a_toast(fp, monkeypatch, dialog):
    """@brief C2 (flipped in phase 2): the double click counts as a QUIET point, then finishes the line.

    Was: the double click went through the same path as a click, so a
    genuine double click on the last vertex of a line showed the
    «Punto coincidente» toast before finishing. Now the router clicks with
    `quiet=True`: the repeated vertex is not added twice and nothing is said.
    Negative twin: a plain second CLICK on the same node (press, not a
    double click) still shows the toast (C3, see the duplicate test).
    """
    rec = _play(fp, monkeypatch, dialog, "double_click_last_vertex", _Events)
    p = rec.phases
    assert p["two_points"]["toasts"] == []
    last = p["double_click"]
    assert last["drawings"][0]["points"] == [[0.0, 0.0], [3.0, 0.0]], "the vertex is not added twice"
    assert last["tool"] is None and last["undo"] == 1
    assert last["toasts"] == [], "C2: no toast on a genuine double click"
    _golden("double_click_last_vertex", rec.phases)


def test_a_double_click_on_a_new_node_adds_it_once_and_finishes_without_a_toast(fp, monkeypatch, dialog):
    rec = _play(fp, monkeypatch, dialog, "double_click_new_vertex", _Events)
    last = rec.phases["double_click"]
    assert last["drawings"][0]["points"] == [[0.0, 0.0], [3.0, 0.0], [3.0, 3.0]]
    assert last["toasts"] == [] and last["tool"] is None and last["undo"] == 1
    _golden("double_click_new_vertex", rec.phases)


def test_escape_cancels_the_shape_in_progress_and_clears_its_markers(fp, monkeypatch, dialog):
    rec = _play(fp, monkeypatch, dialog, "escape", _Events)
    p = rec.phases
    assert p["one_click"]["markers"] == {"QGraphicsEllipseItem": 1}
    esc = p["escaped"]
    assert esc["tool"] is None and esc["clicks"] == [] and esc["markers"] == {} and esc["checked"] == []
    assert esc["drawings"] == [] and esc["undo"] == 0 and esc["recovery"] is None
    _golden("escape", rec.phases)


def test_c1_press_move_release_beyond_the_drag_distance_adds_the_release_point_today(fp, monkeypatch, dialog):
    """@brief C1 today (ON for every tool): the release of a drag is the next drawing point.

    Phase 2 keeps it ON in the footprint (`drag_adds_release_point=True`).
    Negative twin: a release within the drag distance of its press adds
    nothing, and the plain click adds exactly its press point.
    """
    rec = _play(fp, monkeypatch, dialog, "drag_rectangle", _Events)
    assert rec.phases["dragged"]["drawings"][0]["points"] == [[0.0, 0.0], [6.0, 4.0]]
    assert rec.phases["dragged"]["tool"] is None
    _golden("drag_rectangle", rec.phases)


def test_c1_a_release_within_the_drag_distance_adds_nothing_and_a_refused_press_still_drags(fp, monkeypatch, dialog):
    """@brief C1 contract: a drag starts from a press that leaves a shape in progress, accepted OR refused.

    Pressing on the node just collected is a refusal (toast), but the shape
    is still in progress, so dragging away adds the release point, as the
    old footprint did (the router first dropped this; M restored it).
    Negative twins: a release within the drag distance adds nothing; a
    press that COMPLETED a shape (nothing in progress) followed by a drag
    adds nothing.
    """
    window = fp
    rec = _Rec(window, monkeypatch)
    window._toggle_footprint_draw_tool("circle")
    start = _vp(window, 2, 2)
    near = start + QPoint(QApplication.startDragDistance() // 2, 0)
    _press(window, start)
    _release(window, near)
    jitter = rec.phase("jitter")
    assert jitter["clicks"] == [[2.0, 2.0]] and jitter["tool"] == "circle", "a small jitter is a plain click"
    far = start + QPoint(QApplication.startDragDistance() * 6, 0)
    _press(window, start)                                     # same node as the first click: refused
    _release(window, far)                                     # a drag: the release point is the next point
    drag = rec.phase("refused_press_then_drag")
    assert drag["tool"] is None and len(drag["drawings"]) == 1 and drag["drawings"][0]["kind"] == "circle"
    assert len(drag["toasts"]) == 1 and drag["toasts"][0][0] == fp.i18n.text("Punto coincidente con il precedente.")
    # Negative twin: the press that completes the rectangle starts no drag.
    window._toggle_footprint_draw_tool("rectangle")
    _press(window, _vp(window, 0, 0))
    _release(window, _vp(window, 0, 0))
    _press(window, _vp(window, 4, 3))                         # completes the shape
    _release(window, _vp(window, 4, 3) + QPoint(QApplication.startDragDistance() * 6, 0))
    done = rec.phase("completing_press_then_drag")
    assert done["tool"] is None and len(done["drawings"]) == 2 and done["undo"] == 2
    assert done["drawings"][1]["points"] == [[0.0, 0.0], [4.0, 3.0]]
    window._toggle_footprint_draw_tool("line")
    rec.phase("line_armed")
    _press(window, _vp(window, 6, 6))
    _move(window, _vp(window, 9, 6))
    _release(window, _vp(window, 12, 6))
    line = rec.phase("line_drag")
    assert line["clicks"] == [[6.0, 6.0], [12.0, 6.0]] and line["tool"] == "line" and line["toasts"] == []
    _golden("drag_jitter_circle", rec.phases)


def test_a_release_after_a_double_click_adds_nothing(fp, monkeypatch, dialog):
    """@brief The release that follows a double click does not add a point: the press position was consumed."""
    window = fp
    rec = _Rec(window, monkeypatch)
    window._toggle_footprint_draw_tool("line")
    driver = _Events(window)
    driver.click(0, 0)
    driver.click(3, 0)
    driver.fast_second_click(3, 3)
    p = rec.phase("after")
    assert p["drawings"][0]["points"] == [[0.0, 0.0], [3.0, 0.0], [3.0, 3.0]] and p["clicks"] == []


# ── Hide, leave, pan, cursor, handles, wheel ───────────────────────────

def test_c7_hide_disarms_the_tool_and_clears_the_markers_today(fp, monkeypatch, dialog):
    """@brief C7 today (the footprint cancels on hide; the plan unifies the symbol onto it): direct hide and a real tab switch."""
    from kicadgen.gui import main_window as gui
    window = fp
    rec = _Rec(window, monkeypatch)
    driver = _Events(window)
    window._toggle_footprint_draw_tool("line")
    driver.click(0, 0)
    driver.move(2, 1)
    assert rec.markers()
    window.preview_view.hideEvent(QHideEvent())
    direct = rec.phase("hide_event")
    assert direct["tool"] is None and direct["clicks"] == [] and direct["markers"] == {} and direct["checked"] == []
    assert direct["drawings"] == [] and direct["undo"] == 0
    window.preview_view.viewport().unsetCursor()
    # A real tab switch hides the view through the event loop.
    window._toggle_footprint_draw_tool("rectangle")
    driver.click(1, 1)
    window.tabs.setCurrentIndex(0)
    QApplication.processEvents()
    assert not window.preview_view.isVisible()
    tab = rec.phase("tab_switch")
    assert tab["tool"] is None and tab["clicks"] == [] and tab["markers"] == {}
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    QApplication.processEvents()
    # Negative twin: hiding while NOT armed changes nothing and does not raise.
    window.preview_view.hideEvent(QHideEvent())
    assert window._fp_draw_tool is None
    _golden("hide", rec.phases)


def test_c6_leaving_the_view_clears_the_ghost_and_snap_dot_but_keeps_the_clicks_today(fp, monkeypatch, dialog):
    """@brief C6 today (already the plan's rule): leave -> no hover, no ghost, no snap dot; the next move restores them."""
    window = fp
    rec = _Rec(window, monkeypatch)
    driver = _Events(window)
    window._toggle_footprint_draw_tool("rectangle")
    driver.click(1, 1)
    driver.move(4, 3)
    shown = rec.phase("hovering")
    assert shown["markers"] == {"QGraphicsEllipseItem": 2, "QGraphicsPathItem": 1} and shown["hover"] == [4.0, 3.0]
    window.preview_view.leaveEvent(QEvent(QEvent.Type.Leave))
    left = rec.phase("left")
    assert left["hover"] is None and left["clicks"] == [[1.0, 1.0]] and left["tool"] == "rectangle"
    assert left["markers"] == {"QGraphicsEllipseItem": 1}, "only the clicked dot stays"
    assert left["calls"] == {"_refresh_draw_markers": 1}
    window.preview_view.leaveEvent(QEvent(QEvent.Type.Leave))
    again = rec.phase("left_again")
    assert again["calls"] == {}, "a second leave rebuilds nothing"
    driver.move(5, 3)
    back = rec.phase("back")
    assert back["hover"] == [5.0, 3.0] and back["markers"] == {"QGraphicsEllipseItem": 2, "QGraphicsPathItem": 1}
    _golden("leave", rec.phases)


def test_c8_escape_during_a_middle_pan_keeps_the_tool_and_its_clicks_today(fp, monkeypatch, dialog):
    """@brief C8 today (already the plan's rule): the pan takes the Escape; the tool and its clicks survive; the next Escape cancels."""
    window = fp
    view = window.preview_view
    rec = _Rec(window, monkeypatch)
    driver = _Events(window)
    window._toggle_footprint_draw_tool("line")
    driver.click(0, 0)
    start = _vp(window, 6, 6)
    _press(window, start, Qt.MouseButton.MiddleButton)
    _move(window, start + QPoint(20, 10))
    assert view.camera.pan_active
    driver.escape()
    survived = rec.phase("escape_in_pan")
    assert not view.camera.pan_active and survived["tool"] == "line" and survived["clicks"] == [[0.0, 0.0]]
    _release(window, start + QPoint(20, 10), Qt.MouseButton.MiddleButton)
    driver.escape()
    gone = rec.phase("escape_idle")
    assert gone["tool"] is None and gone["clicks"] == []
    _golden("escape_in_pan", rec.phases)


def test_c9_a_left_press_during_a_middle_pan_is_swallowed(fp, monkeypatch, dialog):
    """@brief C9 (flipped in phase 2): with a tool armed, a left press while the middle button pans adds no point.

    Was: the view treated the press as a drawing point and the pan went on.
    Now the router swallows a left press while `camera.pan_active`.
    Negative twin: once the pan is released the same press adds the point.
    """
    window = fp
    view = window.preview_view
    rec = _Rec(window, monkeypatch)
    window._toggle_footprint_draw_tool("line")
    rec.phase("armed")
    start = _vp(window, 6, 6)
    _press(window, start, Qt.MouseButton.MiddleButton)
    _move(window, start + QPoint(20, 10))
    assert view.camera.pan_active
    _press(window, _vp(window, 2, 2))
    _release(window, _vp(window, 2, 2))
    during = rec.phase("left_during_pan")
    assert view.camera.pan_active, "the pan goes on"
    assert during["clicks"] == [], "C9: the press was swallowed"
    assert during["calls"] == {} and during["toasts"] == []
    _release(window, start + QPoint(20, 10), Qt.MouseButton.MiddleButton)
    assert not view.camera.pan_active
    _press(window, _vp(window, 2, 2))
    _release(window, _vp(window, 2, 2))
    assert len(rec.phase("after_pan")["clicks"]) == 1, "idle again: the press adds its point"
    _golden("left_during_pan", rec.phases)


def test_c10_the_cross_cursor_is_set_when_armed_and_unset_when_disarmed_today(fp, monkeypatch, dialog):
    """@brief C10 today: `set_draw_mode` owns the viewport cursor (readable offscreen through `viewport().cursor()`)."""
    window = fp
    view = window.preview_view
    driver = _Events(window)
    idle = view.viewport().cursor().shape()
    assert idle != Qt.CursorShape.CrossCursor
    window._toggle_footprint_draw_tool("rectangle")
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    driver.click(1, 1)
    driver.click(4, 3)                                   # auto-finish: disarmed
    assert view.viewport().cursor().shape() == idle
    window._toggle_footprint_draw_tool("circle")
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    driver.escape()
    assert view.viewport().cursor().shape() == idle
    window._toggle_footprint_draw_tool("circle")
    window._toggle_footprint_draw_tool("circle")         # the same button again disarms
    assert view.viewport().cursor().shape() == idle and window._fp_draw_tool is None


def test_c10_the_router_owns_the_cursor_and_touches_only_what_it_set(fp, monkeypatch, dialog):
    """@brief C10 (flipped in phase 2): the router sets the CrossCursor, keeps it through a cancelled pan, unsets only its own.

    Positive: after an Escape that cancels a middle pan the armed tool still
    shows the cross. Negative twin: hiding an IDLE view leaves a cursor
    somebody else set on the viewport alone (`hideEvent` used to unset it
    unconditionally).
    """
    window = fp
    view = window.preview_view
    window._toggle_footprint_draw_tool("line")
    start = _vp(window, 6, 6)
    _press(window, start, Qt.MouseButton.MiddleButton)
    _move(window, start + QPoint(20, 10))
    assert view.camera.pan_active
    _key(window, Qt.Key.Key_Escape)
    assert not view.camera.pan_active and window._fp_draw_tool == "line"
    assert view.viewport().cursor().shape() == Qt.CursorShape.CrossCursor, "the pan reset it, the router set it again"
    _release(window, start + QPoint(20, 10), Qt.MouseButton.MiddleButton)
    _key(window, Qt.Key.Key_Escape)
    assert window._fp_draw_tool is None
    assert view.viewport().cursor().shape() != Qt.CursorShape.CrossCursor
    view.viewport().setCursor(Qt.CursorShape.WhatsThisCursor)
    view.hideEvent(QHideEvent())
    assert view.viewport().cursor().shape() == Qt.CursorShape.WhatsThisCursor, "not the router's: left alone"
    view.viewport().unsetCursor()


def test_a_right_press_during_a_middle_pan_does_not_finish_the_shape(fp, monkeypatch, dialog):
    """@brief C9 for the right button: swallowed during a pan (the old view finished the shape); idle it finishes."""
    window = fp
    view = window.preview_view
    driver = _Events(window)
    window._toggle_footprint_draw_tool("line")
    driver.click(0, 0)
    driver.click(3, 0)
    start = _vp(window, 6, 6)
    _press(window, start, Qt.MouseButton.MiddleButton)
    _move(window, start + QPoint(20, 10))
    assert view.camera.pan_active
    _press(window, start, Qt.MouseButton.RightButton)
    _release(window, start, Qt.MouseButton.RightButton)
    assert window._fp_draw_tool == "line" and window.footprint_drawings == []
    _release(window, start + QPoint(20, 10), Qt.MouseButton.MiddleButton)
    driver.right()
    assert window._fp_draw_tool is None and len(window.footprint_drawings) == 1


def test_arming_another_tool_switches_and_forgets_the_clicks(fp, monkeypatch, dialog):
    """@brief One armed tool at a time: arming `circle` over a half-drawn rectangle drops its clicks; the button follows."""
    window = fp
    driver = _Events(window)
    window._toggle_footprint_draw_tool("rectangle")
    driver.click(1, 1)
    window._toggle_footprint_draw_tool("circle")
    assert window._fp_draw_tool == "circle" and window._fp_draw_clicks == []
    assert [k for k, b in window._fp_draw_buttons.items() if b.isChecked()] == ["circle"]
    assert window.footprint_drawings == []


def test_draw_mode_wins_over_a_visible_handle_and_stops_the_handle_hover(fp, monkeypatch, dialog):
    """@brief A press on a hovered drawing's handle in draw mode is a drawing click, not a handle drag.

    Pinned today (no decision id: the plan leaves it as is). The handles
    shown before arming stay on screen, and moves in draw mode do not
    refresh them.
    """
    from kicadgen.core.footprint import validate_footprint_drawings
    from kicadgen.gui.footprint_view import HANDLE_KEY_ROLE
    window = fp
    view = window.preview_view
    window.footprint_drawings = validate_footprint_drawings(
        [{"kind": "rectangle", "layer": "F.SilkS", "width": 0.12, "points": [[0, 8], [4, 10]]}])
    window._draw_preview()
    _place_camera(window)
    rec = _Rec(window, monkeypatch)
    _move(window, _vp(window, 4, 9))
    handles = [i for i in window.preview_scene.items() if i.data(HANDLE_KEY_ROLE) is not None]
    assert len(handles) == 2
    window._toggle_footprint_draw_tool("circle")
    rec.phase("armed")
    _press(window, _vp(window, 4, 10))
    assert view._handle_drag is None, "no handle drag in draw mode"
    _release(window, _vp(window, 4, 10))
    clicked = rec.phase("press_on_handle")
    assert clicked["clicks"] == [[4.0, 10.0]] and clicked["drawings"][0]["points"] == [[0, 8], [4, 10]]
    _move(window, _vp(window, 12, 0))
    after = [i for i in window.preview_scene.items() if i.data(HANDLE_KEY_ROLE) is not None]
    assert len(after) == len(handles), "the handle hover is not refreshed in draw mode"
    _golden("handle_hover", rec.phases)


def test_the_wheel_zooms_in_draw_mode_and_keeps_the_clicks(fp, monkeypatch, dialog):
    """@brief The wheel stays allowed with a tool armed: scale changes, the collected points and the tool stay."""
    window = fp
    view = window.preview_view
    rec = _Rec(window, monkeypatch)
    driver = _Events(window)
    window._toggle_footprint_draw_tool("rectangle")
    driver.click(1, 1)
    driver.move(3, 3)
    rec.phase("before")
    _wheel(window, _vp(window, 3, 3), 1)
    assert view.transform().m11() == pytest.approx(1.2)
    zoomed = rec.phase("zoom")
    assert zoomed["clicks"] == [[1.0, 1.0]] and zoomed["tool"] == "rectangle" and zoomed["drawings"] == []
    _place_camera(window)
    _golden("wheel", rec.phases)


def test_a_middle_press_pans_in_draw_mode_without_adding_a_point(fp, monkeypatch, dialog):
    """@brief The middle button pans a tool-armed view; no point; the release ends the pan."""
    window = fp
    view = window.preview_view
    window._toggle_footprint_draw_tool("line")
    start = _vp(window, 6, 6)
    _press(window, start, Qt.MouseButton.MiddleButton)
    assert view.camera.pan_active and window._fp_draw_clicks == []
    _release(window, start, Qt.MouseButton.MiddleButton)
    assert not view.camera.pan_active and window._fp_draw_tool == "line"


def test_undo_redo_apply_and_discard_disarm_the_tool_and_forget_the_clicks(fp, monkeypatch, dialog):
    """@brief The callers outside the view that cancel drawing: undo, redo, applying the draft and discarding it.

    Each needs a pending edit (apply and discard return early without one),
    so a rectangle is committed first. Negative twin: apply with nothing
    pending leaves the armed tool alone.
    """
    window = fp
    driver = _Events(window)

    def commit_one():
        window._toggle_footprint_draw_tool("rectangle")
        driver.click(0, 0)
        driver.click(3, 2)
        assert window.has_pending_footprint_edits() and window._fp_draw_tool is None

    commit_one()
    for name, call in (("undo", window._undo_footprint_drawing),
                       ("redo", window._redo_footprint_drawing),
                       ("apply", window.apply_footprint_draft),
                       ("discard", window.discard_footprint_draft)):
        if name == "discard":
            window.footprint_drawings = list(window.footprint_drawings)   # keep the draft pending
            if not window.has_pending_footprint_edits():
                commit_one()
        if name == "apply" and not window.has_pending_footprint_edits():
            commit_one()
        window._toggle_footprint_draw_tool("line")
        driver.click(8, 8)
        assert window._fp_draw_clicks, name
        call()
        assert window._fp_draw_tool is None and window._fp_draw_clicks == [], name
        assert not any(b.isChecked() for b in window._fp_draw_buttons.values()), name
        QApplication.processEvents()
    # Negative twin: nothing pending -> apply returns early and the armed tool stays.
    assert not window.has_pending_footprint_edits()
    window._toggle_footprint_draw_tool("line")
    driver.click(8, 8)
    window.apply_footprint_draft()
    assert window._fp_draw_tool == "line" and window._fp_draw_clicks == [(8.0, 8.0)]


# ── Twin oracle: the direct host API produces the same committed output ─

@pytest.mark.parametrize("name", sorted(TWIN_SCENARIOS))
def test_the_direct_host_api_commits_exactly_what_the_events_committed(fp, monkeypatch, dialog, name):
    """@brief Twin oracle: the scenario replayed through `_on_footprint_draw_clicked` / `_finish` / `_cancel`
    gives the committed records, undo depth, recovery file, clicks, tool and toasts of the event run (golden)."""
    rec = _play(fp, monkeypatch, dialog, name, _Direct)
    golden = _golden_phases(name)
    direct = json.loads(json.dumps(_committed(rec.phases)))
    assert direct == _committed(golden)
