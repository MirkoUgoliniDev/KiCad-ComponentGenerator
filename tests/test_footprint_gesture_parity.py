"""@brief Characterisation of the footprint preview gestures (plan docs/canvas-gestures-plan.md, §1b).

Every test drives the real event path (QTest press/move/release on the
preview viewport of a shown window) and pins what the CURRENT code does:
the committed `footprint_drawings` / `footprint_text_positions`, the undo
stack depth, the recovery file, and the per-phase counts of
`_draw_preview`, `_refresh_drawing_handles`, `_checkpoint_footprint_draft`
and `save_footprint_draft`. The per-phase records are also compared, byte
for byte, with the golden JSON under `tests/data/gesture_golden/`, which is
regenerated only when `KICADGEN_REGEN_GESTURE_GOLDEN` is set.

The tests at the end pin the decisions D1-D5, D8 and D9 of the plan (§1c)
as applied on 2026-10-17; each says what the footprint did before.
"""
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from kicadgen.core.footprint import validate_footprint_drawings

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt          # noqa: E402
from PyQt6.QtGui import QHideEvent, QKeyEvent, QWheelEvent    # noqa: E402
from PyQt6.QtTest import QTest                                # noqa: E402
from PyQt6.QtWidgets import QApplication                      # noqa: E402

from kicadgen.gui.footprint_view import (                     # noqa: E402
    HANDLE_KEY_ROLE, HOVER_KEEP_MARGIN_PX, TEXT_ANCHOR_ROLE, TEXT_KEY_ROLE,
)

#: Folder of the golden records; the env var regenerates them.
GOLDEN_DIR = Path(__file__).resolve().parent / "data" / "gesture_golden"
REGEN_ENV = "KICADGEN_REGEN_GESTURE_GOLDEN"

#: Scene scale of the footprint preview (px per mm, plan §1).
PX = 20.0

#: Role of the drawing handles and of the handle-drag ghost (main_window._screen_handle).
_GHOST_ROLE = 0x48474F


def _record(kind, points):
    return {"kind": kind, "layer": "F.SilkS", "width": 0.12, "points": [list(p) for p in points]}


class _Recorder:
    """Counts the window methods and the recovery write, phase by phase.

    `phase(name)` closes the running phase: the counts since the previous
    phase, the committed records, the undo depth and the recovery file are
    stored under `name`, so a test asserts them and the golden file keeps
    them. Same pattern as tests/test_symbol_property_drag_smooth.py::_Counter.
    """

    NAMES = ("_draw_preview", "_refresh_drawing_handles", "_checkpoint_footprint_draft")

    def __init__(self, window, monkeypatch):
        from kicadgen.services import footprint_draft
        self.window = window
        self.calls = {}
        self.phases = {}
        for name in self.NAMES:
            monkeypatch.setattr(window, name, self._wrap(name, getattr(window, name)))
        monkeypatch.setattr(footprint_draft, "save_footprint_draft",
                            self._wrap("save_footprint_draft", footprint_draft.save_footprint_draft))

    def _wrap(self, name, original):
        def counting(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            return original(*args, **kwargs)
        return counting

    def phase(self, name):
        """Close the running phase under `name` and return its record."""
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
        }
        assert name not in self.phases, f"phase {name!r} recorded twice"
        self.phases[name] = record
        self.calls = {}
        return record


def _golden(name, phases):
    """Compare the phase records with `footprint_<name>.json`, byte for byte (LF)."""
    path = GOLDEN_DIR / f"footprint_{name}.json"
    text = (json.dumps(phases, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if os.environ.get(REGEN_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text)
        return
    assert path.exists(), f"golden {path.name} missing: run once with {REGEN_ENV}=1"
    # The repository keeps *.json as text (LF); a CRLF checkout is normalised back.
    assert path.read_bytes().replace(b"\r\n", b"\n") == text, f"{path.name} differs from today's run"


# ── Fixture: a shown window with the preview tab in front ─────────────

@pytest.fixture
def fp(window, qapp, monkeypatch):
    """The preview tab shown and active, camera at 1 px per scene px, 1 mm drawing grid.

    Real QTest events need the viewport on screen: the window is shown with
    the preview tab in front. The camera is placed by hand (`auto_fit` off,
    scene origin at the viewport centre, scale 1) so a redraw keeps it and
    viewport points map to scene points one to one. Drawings and texts snap
    to the main 1 mm grid (fine divisor 1), whose origin the pin header
    puts at pin 1, the scene origin.
    """
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


def _place_camera(window):
    view = window.preview_view
    view.resetTransform()
    view.centerOn(0, 0)
    view.auto_fit = False


def _vp(window, scene_x, scene_y, offset=(0, 0)):
    """Viewport point of a scene point (px)."""
    return window.preview_view.mapFromScene(QPointF(scene_x, scene_y)) + QPoint(*offset)


def _press(window, pos, button=Qt.MouseButton.LeftButton):
    QTest.mousePress(window.preview_view.viewport(), button, Qt.KeyboardModifier.NoModifier, pos)


def _move(window, pos):
    """A move with the buttons QTest holds; a button-less move repeats the last one only if the point differs.

    QTest sets the platform cursor for a button-less move, and an unchanged
    cursor position produces no event: hover twice on the same point with a
    different point in between.
    """
    QTest.mouseMove(window.preview_view.viewport(), pos)


def _release(window, pos, button=Qt.MouseButton.LeftButton):
    QTest.mouseRelease(window.preview_view.viewport(), button, Qt.KeyboardModifier.NoModifier, pos)


def _group(window, key):
    return next(item for item in window.preview_scene.items()
                if item.data(TEXT_KEY_ROLE) == key and item.parentItem() is None)


def _square(group):
    return next(child for child in group.childItems() if child.data(TEXT_ANCHOR_ROLE) is not None)


def _glyph_box(window, group):
    """Viewport rect of the glyphs of a text group."""
    glyphs = next(child for child in group.childItems() if child.data(TEXT_ANCHOR_ROLE) is None)
    return window.preview_view.mapFromScene(glyphs.sceneBoundingRect()).boundingRect()


def _handles(window):
    return [item for item in window.preview_scene.items() if item.data(HANDLE_KEY_ROLE) is not None]


def _ghosts(window):
    return [item for item in window.preview_scene.items()
            if item.data(_GHOST_ROLE) is not None and item.data(HANDLE_KEY_ROLE) is None
            and item.data(TEXT_ANCHOR_ROLE) is None]


def _with_drawing(window, kind, points):
    window.footprint_drawings = validate_footprint_drawings([_record(kind, points)])
    window._draw_preview()
    _place_camera(window)


def _recovery_path(window):
    return window._footprint_recovery_path()


# ── F1: text drag ────────────────────────────────────────────────────

def test_f1_a_text_follows_the_pointer_live_and_is_committed_once_on_the_drop(fp, monkeypatch):
    """@brief F1: live `setPos` on every move, nothing committed; the drop snaps, pushes once, writes once."""
    window = fp
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    origin = QPointF(group.pos())
    start = _glyph_box(window, group).center()
    _press(window, start)
    assert window.preview_view._text_drag is not None and not window.preview_view.camera.pan_active
    assert _square(group).isVisible(), "the dragged text keeps its anchor square"
    rec.phase("press")
    pos = start
    for step in ((7, 3), (13, 9), (20, 8), (10, 10), (10, 10)):
        pos = pos + QPoint(*step)
        _move(window, pos)
        delta = pos - start
        assert (group.pos() - (origin + QPointF(delta))).manhattanLength() < 1e-9, "live follow"
    moves = rec.phase("moves")
    assert moves["calls"] == {}, "no redraw, no handles refresh, no checkpoint while dragging"
    assert moves["undo"] == 0 and moves["texts"] == {} and moves["recovery"] is None
    _release(window, pos)
    drop = rec.phase("drop")
    assert window.preview_view._text_drag is None
    expected = window._snapped_draw_point(origin.x() + (pos - start).x(), origin.y() + (pos - start).y())
    assert window.footprint_text_positions == {"Reference": expected}
    assert all(abs(v - round(v)) < 1e-9 for v in expected), "snapped on the 1 mm grid"
    # The redraw after an edit rebuilds the drawing handles once (`_draw_preview` tail).
    assert drop["calls"] == {"_draw_preview": 1, "_refresh_drawing_handles": 1,
                             "_checkpoint_footprint_draft": 1, "save_footprint_draft": 1}
    assert drop["undo"] == 1 and drop["recovery"]["texts"]["Reference"] == list(expected)
    assert window.has_pending_footprint_edits()
    _golden("f1_text_drag", rec.phases)


def test_f1_a_drop_on_the_stored_node_redraws_without_an_undo_push(fp, monkeypatch):
    """@brief F1 twin: a drop that snaps onto the stored position only redraws (equal → no edit)."""
    window = fp
    assert window._set_footprint_text_position("Reference", (3.0, -2.0))
    _place_camera(window)
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(4, -3))                 # within half a node
    _release(window, start + QPoint(4, -3))
    same = rec.phase("same_node")
    assert window.footprint_text_positions == {"Reference": (3.0, -2.0)}
    assert same["calls"] == {"_draw_preview": 1, "_refresh_drawing_handles": 1} and same["undo"] == 1,         "redraw only, no push, no write"
    # Negative twin: one node away is an edit.
    group = _group(window, "Reference")
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(20, 0))
    _release(window, start + QPoint(20, 0))
    moved = rec.phase("next_node")
    assert window.footprint_text_positions == {"Reference": (4.0, -2.0)}
    assert moved["undo"] == 2 and moved["calls"]["save_footprint_draft"] == 1
    _golden("f1_text_same_node", rec.phases)


def test_f1_a_click_without_a_move_changes_nothing_and_a_real_move_commits_once(fp, monkeypatch):
    """@brief A drop without a move changes nothing (2026-10-17, as the symbol's property drop).

    Was: the release re-sent the press point as a drop, so a plain click on
    a default text whose anchor is off the grid snapped it and pushed an
    undo. Now a zero displacement writes nothing: no snap, no push, no
    checkpoint, no file, no redraw; the text stays on its default anchor.
    Positive twin: a real move (one node) still commits once.
    """
    window = fp
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    origin = QPointF(group.pos())
    assert any(abs(v / PX - round(v / PX)) > 1e-6 for v in (origin.x(), origin.y())), "default anchor off the grid"
    start = _glyph_box(window, group).center()
    _press(window, start)
    _release(window, start)
    click = rec.phase("click")
    assert click["calls"] == {} and click["undo"] == 0 and click["recovery"] is None
    assert window.footprint_text_positions == {} and group.pos() == origin
    assert not window.has_pending_footprint_edits()
    _press(window, start)
    _move(window, start + QPoint(20, 0))
    _release(window, start + QPoint(20, 0))
    moved = rec.phase("one_node")
    assert moved["undo"] == 1 and moved["calls"]["save_footprint_draft"] == 1
    assert window.footprint_text_positions == {"Reference": window._snapped_draw_point(origin.x() + 20, origin.y())}
    _golden("f1_text_click", rec.phases)


def test_f1_a_jiggle_inside_the_defaults_snap_cell_writes_nothing_and_another_node_commits_once(fp, monkeypatch):
    """@brief A drop is a move only when its SNAPPED point differs from the SNAP of the default anchor.

    Same rule as the symbol's `move_property` («back onto the default's cell
    writes nothing»): the comparison goes through the one grid rule, not
    against the raw off-grid anchor. Precondition asserted: the default is
    off the grid and the jiggle keeps the snap. Negative twins: a move that
    changes the snap commits once; a zero displacement writes nothing.
    """
    window = fp
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    origin = QPointF(group.pos())
    cell = window._snapped_draw_point(origin.x(), origin.y())
    assert any(abs(v / PX - round(v / PX)) > 1e-6 for v in (origin.x(), origin.y())), "default anchor off the grid"
    jig = QPoint(2, 0)
    assert window._snapped_draw_point(origin.x() + 2, origin.y()) == cell, "precondition: same snap cell"
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + jig)
    _release(window, start + jig)
    nothing = rec.phase("jiggle")
    assert nothing["calls"] == {} and nothing["undo"] == 0 and nothing["recovery"] is None
    assert window.footprint_text_positions == {} and not window.has_pending_footprint_edits()
    assert _group(window, "Reference").pos() == origin, "put back on its anchor"
    # Negative twin 1: a move that changes the snap commits once.
    start = _glyph_box(window, _group(window, "Reference")).center()
    _press(window, start)
    _move(window, start + QPoint(40, 0))
    _release(window, start + QPoint(40, 0))
    moved = rec.phase("other_node")
    assert window._snapped_draw_point(origin.x() + 40, origin.y()) != cell
    assert moved["undo"] == 1 and moved["calls"]["save_footprint_draft"] == 1
    assert window.footprint_text_positions == {"Reference": window._snapped_draw_point(origin.x() + 40, origin.y())}
    # Negative twin 2: once STORED, a jiggle inside the stored cell is a redraw without an edit.
    stored = dict(window.footprint_text_positions)
    start = _glyph_box(window, _group(window, "Reference")).center()
    _press(window, start)
    _move(window, start + QPoint(1, 1))
    _release(window, start + QPoint(1, 1))
    again = rec.phase("stored_cell")
    assert window.footprint_text_positions == stored and again["undo"] == 1
    assert "save_footprint_draft" not in again["calls"]
    _golden("f1_text_jiggle", rec.phases)


# ── F2: drawing point drag ───────────────────────────────────────────

def test_f2_a_point_drag_steps_on_the_grid_with_one_ghost_per_node(fp, monkeypatch):
    """@brief F2: handles on hover; each new node redraws the ghost once, a move within the node nothing; the drop commits once."""
    window = fp
    _with_drawing(window, "rectangle", ((0, 8), (4, 10)))
    rec = _Recorder(window, monkeypatch)
    assert _handles(window) == [], "no handles at rest"
    _move(window, _vp(window, 4 * PX, 9 * PX))                     # on the right edge
    hover = rec.phase("hover")
    assert len(_handles(window)) == 2 and hover["calls"] == {"_refresh_drawing_handles": 1}
    _press(window, _vp(window, 4 * PX, 10 * PX))                    # the second corner's handle
    assert window.preview_view._handle_drag == (0, 1)
    press = rec.phase("press")
    assert press["calls"] == {"_refresh_drawing_handles": 1}
    _move(window, _vp(window, 5 * PX, 11 * PX))
    assert window._fp_handle_drag == (0, 1, (5.0, 11.0))
    node1 = rec.phase("node_1")
    assert node1["calls"] == {"_refresh_drawing_handles": 1} and len(_ghosts(window)) == 1
    _move(window, _vp(window, 5 * PX, 11 * PX, offset=(3, -2)))    # same node
    same = rec.phase("same_node")
    assert same["calls"] == {}, "an identical snapped step redraws nothing"
    _move(window, _vp(window, 6 * PX, 12 * PX))
    node2 = rec.phase("node_2")
    assert node2["calls"] == {"_refresh_drawing_handles": 1}
    for phase in (node1, same, node2):
        assert phase["undo"] == 0 and phase["drawings"][0]["points"] == [[0, 8], [4, 10]], "nothing committed mid-drag"
        assert phase["recovery"] is None
    _release(window, _vp(window, 6.3 * PX, 11.8 * PX))
    drop = rec.phase("drop")
    assert window.footprint_drawings[0]["points"] == [[0, 8], [6.0, 12.0]]
    assert window._fp_handle_drag is None and window.preview_view._handle_drag is None
    assert drop["undo"] == 1 and drop["calls"]["save_footprint_draft"] == 1
    assert drop["calls"]["_draw_preview"] == 1 and drop["calls"]["_checkpoint_footprint_draft"] == 1
    assert drop["recovery"]["drawings"][0]["points"] == [[0, 8], [6.0, 12.0]]
    assert _ghosts(window) == []
    _golden("f2_point_drag", rec.phases)


def test_f2_a_refused_drop_shows_a_toast_and_edits_nothing(fp, monkeypatch):
    """@brief F2 negative: a point dropped where the shape degenerates is refused with a toast."""
    window = fp
    _with_drawing(window, "circle", ((0, 8), (3, 8)))
    shown = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: shown.append(title))
    rec = _Recorder(window, monkeypatch)
    _move(window, _vp(window, 3 * PX, 8 * PX))                     # on the rim
    assert len(_handles(window)) == 2
    _press(window, _vp(window, 3 * PX, 8 * PX))                    # the rim handle
    assert window.preview_view._handle_drag == (0, 1)
    _move(window, _vp(window, 1 * PX, 8 * PX))
    _release(window, _vp(window, 0, 8 * PX))                       # onto the centre
    refused = rec.phase("refused")
    assert shown == ["Spostamento scartato."]
    assert window.footprint_drawings[0]["points"] == [[0, 8], [3, 8]]
    assert refused["undo"] == 0 and refused["recovery"] is None
    assert "save_footprint_draft" not in refused["calls"] and "_draw_preview" not in refused["calls"]
    assert not window.has_pending_footprint_edits()
    _golden("f2_refused_drop", rec.phases)


def test_f2_a_drop_without_a_move_changes_nothing(fp, monkeypatch):
    """@brief F2 negative: press and release on the handle: handles refreshed, no edit, no write."""
    window = fp
    _with_drawing(window, "rectangle", ((0, 8), (4, 10)))
    rec = _Recorder(window, monkeypatch)
    _move(window, _vp(window, 4 * PX, 9 * PX))
    _press(window, _vp(window, 4 * PX, 10 * PX))
    _release(window, _vp(window, 4 * PX, 10 * PX))
    still = rec.phase("press_release")
    assert window.footprint_drawings[0]["points"] == [[0, 8], [4, 10]]
    # Hover, press, the release's own step (the release always passes through
    # a move: plan section 4 rule 3, as the symbol editor) and the drop refresh.
    assert still["undo"] == 0 and still["calls"] == {"_refresh_drawing_handles": 4}
    assert still["recovery"] is None and _ghosts(window) == []
    _golden("f2_drop_without_move", rec.phases)


# ── F3: whole-drawing drag ───────────────────────────────────────────

def test_f3_the_body_drag_moves_every_point_by_the_snapped_delta(fp, monkeypatch):
    """@brief F3: the grab node is snapped at the press; each node redraws once; the drop commits the delta."""
    window = fp
    _with_drawing(window, "rectangle", ((0, 8), (4, 10)))
    rec = _Recorder(window, monkeypatch)
    _move(window, _vp(window, 4 * PX, 9 * PX))
    rec.phase("hover")
    _press(window, _vp(window, 4 * PX, 9 * PX))                    # the body, not a handle
    assert window.preview_view._handle_drag == (0, -1) and window._fp_handle_origin == (4.0, 9.0)
    press = rec.phase("press")
    assert press["calls"] == {"_refresh_drawing_handles": 1}
    _move(window, _vp(window, 5 * PX, 9 * PX))
    assert window._fp_handle_drag == (0, -1, (1.0, 0.0))
    node1 = rec.phase("node_1")
    assert node1["calls"] == {"_refresh_drawing_handles": 1} and len(_ghosts(window)) == 1
    _move(window, _vp(window, 5 * PX, 9 * PX, offset=(-4, 5)))
    assert rec.phase("same_node")["calls"] == {}
    _move(window, _vp(window, 6 * PX, 10 * PX))
    node2 = rec.phase("node_2")
    assert node2["calls"] == {"_refresh_drawing_handles": 1} and node2["undo"] == 0
    _release(window, _vp(window, 6.2 * PX, 10.1 * PX))
    drop = rec.phase("drop")
    assert window.footprint_drawings[0]["points"] == [[2.0, 9.0], [6.0, 11.0]]
    assert drop["undo"] == 1 and drop["calls"]["save_footprint_draft"] == 1
    assert window._fp_handle_origin is None and window._fp_handle_drag is None
    # Negative twin: a drop on the grab node is a zero delta: nothing changes.
    _press(window, _vp(window, 6 * PX, 10 * PX))
    _release(window, _vp(window, 6 * PX, 10 * PX))
    zero = rec.phase("zero_delta")
    assert window.footprint_drawings[0]["points"] == [[2.0, 9.0], [6.0, 11.0]]
    assert zero["undo"] == 1 and "save_footprint_draft" not in zero["calls"]
    _golden("f3_body_drag", rec.phases)


# ── F4 / F5: camera ──────────────────────────────────────────────────

def test_f4_a_left_press_on_empty_space_pans_and_edits_nothing(fp, monkeypatch):
    """@brief F4: the camera follows the pointer; `auto_fit` drops on a real move only; no edit."""
    window = fp
    view = window.preview_view
    rec = _Recorder(window, monkeypatch)
    centre = view.viewport().rect().center()
    start = QPoint(centre.x() - 300, centre.y() + 250)            # empty background
    assert view._text_item_at(start) == (None, None) and view._drawing_at(start) < 0
    before = view.mapToScene(centre)
    _press(window, start)
    assert view.camera.pan_active and view._text_drag is None and view._handle_drag is None
    assert view.auto_fit is False, "placed by hand in the fixture"
    view.auto_fit = True
    _release(window, start)
    assert view.auto_fit is True, "negative: a press without a move keeps the automatic fit"
    _press(window, start)
    _move(window, start + QPoint(50, -30))
    assert view.auto_fit is False, "a real move: the user placed the camera"
    after = view.mapToScene(centre)
    # `centerOn` lands on whole scroll steps: up to a couple of px of rounding.
    assert (after - before - QPointF(-50, 30)).manhattanLength() <= 4
    _release(window, start + QPoint(50, -30))
    pan = rec.phase("pan")
    assert pan["calls"] == {} and pan["undo"] == 0 and pan["recovery"] is None
    assert window.footprint_text_positions == {} and window.footprint_drawings == []
    _golden("f4_pan", rec.phases)


def _wheel(window, pos, steps):
    view = window.preview_view
    event = QWheelEvent(QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)), QPoint(),
                        QPoint(0, 120 * steps), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), event)


def test_d2_the_wheel_is_refused_during_a_pan_and_zooms_after_its_release(fp, monkeypatch):
    """@brief D2 for the pan (2026-10-17, as the symbol editor): no zoom while the camera is being dragged.

    A rescale under a pan would move the camera by a delta measured in two
    scales. Positive: a notch during the pan leaves the scale alone and the
    pan goes on. Negative twin: the same notch after the release zooms.
    """
    window = fp
    view = window.preview_view
    centre = view.viewport().rect().center()
    start = QPoint(centre.x() - 300, centre.y() + 250)
    _press(window, start)
    _move(window, start + QPoint(20, 10))
    assert view.camera.pan_active
    _wheel(window, start + QPoint(20, 10), 1)
    assert view.transform().m11() == pytest.approx(1.0), "refused during the pan"
    assert view.camera.pan_active, "the pan goes on"
    _release(window, start + QPoint(20, 10))
    assert not view.camera.pan_active
    _wheel(window, start, 1)
    assert view.transform().m11() == pytest.approx(1.2), "released: the wheel zooms again"
    assert window.footprint_text_positions == {} and window.footprint_drawings == []


def test_a_middle_press_during_a_drag_does_not_start_a_pan(fp, monkeypatch):
    """@brief As the symbol editor: another button over a running drag neither pans nor ends it.

    Negative twin: with no drag running, a middle press pans.
    """
    window = fp
    view = window.preview_view
    group = _group(window, "Reference")
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(40, 20))
    _press(window, start + QPoint(40, 20), Qt.MouseButton.MiddleButton)
    assert not view.camera.pan_active, "no pan over a drag"
    assert view._text_drag is not None, "the drag goes on"
    _release(window, start + QPoint(40, 20), Qt.MouseButton.MiddleButton)
    _release(window, start + QPoint(40, 20))
    assert view._text_drag is None and not view.camera.pan_active
    assert "Reference" in window.footprint_text_positions
    centre = view.viewport().rect().center()
    idle = QPoint(centre.x() - 300, centre.y() + 250)
    _press(window, idle, Qt.MouseButton.MiddleButton)
    assert view.camera.pan_active, "idle: the middle button pans"
    _release(window, idle, Qt.MouseButton.MiddleButton)


def test_escape_cancels_a_pan_and_does_not_also_cancel_the_armed_tool(fp, monkeypatch):
    """@brief As the symbol editor: Escape cancels a camera pan in progress (the camera stays where it is).

    The key is consumed by the pan, so an armed drawing tool survives that
    Escape; the next Escape (no pan) cancels the tool. Negative twin: with
    nothing running, Escape reaches the tool as before (see the D1 test).
    """
    window = fp
    view = window.preview_view
    centre = view.viewport().rect().center()
    start = QPoint(centre.x() - 300, centre.y() + 250)
    _press(window, start)
    _move(window, start + QPoint(30, 10))
    assert view.camera.pan_active
    at = view.mapToScene(centre)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view.camera.pan_active, "Escape cancelled the pan"
    _move(window, start + QPoint(80, 40))
    assert view.mapToScene(centre) == at, "the camera stays where it was and no longer follows"
    _release(window, start + QPoint(80, 40))
    # With a tool armed, a middle-button pan is cancelled first; the tool survives that Escape.
    window._toggle_footprint_draw_tool("circle")
    _press(window, start, Qt.MouseButton.MiddleButton)
    assert view.camera.pan_active and window._fp_draw_tool == "circle"
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view.camera.pan_active and window._fp_draw_tool == "circle", "the pan took the Escape"
    _release(window, start, Qt.MouseButton.MiddleButton)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert window._fp_draw_tool is None, "no pan running: Escape cancels the tool"


def test_f5_the_wheel_zooms_about_the_pointer_within_the_clamp_and_edits_nothing(fp, monkeypatch):
    """@brief F5: one notch scales by 1.2 keeping the scene point under the pointer; scale clamped to [0.02, 40]."""
    window = fp
    view = window.preview_view
    rec = _Recorder(window, monkeypatch)
    pos = _vp(window, 2 * PX, 3 * PX)
    under = view.mapToScene(pos)
    _wheel(window, pos, 1)
    assert view.transform().m11() == pytest.approx(1.2)
    assert (view.mapToScene(pos) - under).manhattanLength() < 2.5, "anchored on the pointer (centerOn rounding)"
    assert view.auto_fit is False
    _wheel(window, pos, -1)
    assert view.transform().m11() == pytest.approx(1.0)
    for _ in range(40):
        _wheel(window, pos, 1)
    assert view.transform().m11() == pytest.approx(40.0), "clamped high"
    for _ in range(80):
        _wheel(window, pos, -1)
    assert view.transform().m11() == pytest.approx(0.02), "clamped low"
    zoom = rec.phase("zoom")
    assert zoom["calls"] == {} and zoom["undo"] == 0 and zoom["recovery"] is None
    _golden("f5_wheel", rec.phases)


# ── F8 / F9: hover ───────────────────────────────────────────────────

def test_f8_drawing_handles_are_built_on_a_hit_kept_within_the_margin_and_removed_at_rest(fp, monkeypatch):
    """@brief F8: hit → handles; within 12 px of the bounds kept (hysteresis); beyond → removed; leave → removed."""
    window = fp
    view = window.preview_view
    _with_drawing(window, "line", ((0, 8), (8, 8)))
    rec = _Recorder(window, monkeypatch)
    bottom = view.mapFromScene(view._drawing_bounds(0)).boundingRect().bottom()
    _move(window, _vp(window, 4 * PX, 8 * PX))
    assert len(_handles(window)) == 2 and view.hovered_drawing() == 0
    _move(window, QPoint(_vp(window, 4 * PX, 8 * PX).x(), bottom + HOVER_KEEP_MARGIN_PX - 1))
    assert len(_handles(window)) == 2 and view.hovered_drawing() == 0, "kept just inside the margin"
    inside = rec.phase("inside")
    assert inside["calls"] == {"_refresh_drawing_handles": 1}, "built once, not rebuilt while kept"
    _move(window, QPoint(_vp(window, 4 * PX, 8 * PX).x(), bottom + HOVER_KEEP_MARGIN_PX + 3))
    assert _handles(window) == [] and view.hovered_drawing() == -1, "removed just beyond the margin"
    outside = rec.phase("outside")
    assert outside["calls"] == {"_refresh_drawing_handles": 1}
    _move(window, _vp(window, 4 * PX, 8 * PX))
    assert len(_handles(window)) == 2
    view.leaveEvent(QEvent(QEvent.Type.Leave))
    assert _handles(window) == [], "the pointer left the view: at rest again"
    rest = rec.phase("rest")
    assert rest["undo"] == 0 and rest["recovery"] is None and rest["calls"] == {"_refresh_drawing_handles": 2}
    _golden("f8_hover_drawing", rec.phases)


def test_f9_the_text_square_shows_at_margin_minus_1_and_hides_at_margin_plus_3(fp, monkeypatch):
    """@brief F9: the anchor square follows the 12 px reveal margin around glyphs and anchor; one text at a time."""
    window = fp
    rec = _Recorder(window, monkeypatch)
    reference, value = _group(window, "Reference"), _group(window, "Value")
    box = _glyph_box(window, reference)
    y = box.center().y()
    _move(window, QPoint(box.right() + HOVER_KEEP_MARGIN_PX - 1, y))
    assert _square(reference).isVisible() and not _square(value).isVisible()
    assert window.preview_view.hovered_text() == "Reference"
    _move(window, QPoint(box.right() + HOVER_KEEP_MARGIN_PX + 3, y))
    assert not _square(reference).isVisible() and window.preview_view.hovered_text() == ""
    _move(window, QPoint(box.center().x(), box.top() - HOVER_KEEP_MARGIN_PX + 1))
    assert _square(reference).isVisible()
    _move(window, QPoint(box.center().x(), box.top() - HOVER_KEEP_MARGIN_PX - 3))
    assert not _square(reference).isVisible()
    other = _glyph_box(window, value).center()
    _move(window, other)
    assert _square(value).isVisible() and not _square(reference).isVisible(), "the other text's square only"
    window.preview_view.leaveEvent(QEvent(QEvent.Type.Leave))
    assert not _square(value).isVisible()
    hover = rec.phase("hover")
    assert hover["calls"] == {} and hover["undo"] == 0, "visibility only: no redraw, no handles, no write"
    _golden("f9_hover_text", rec.phases)


# ── Decisions D1-D5, D8, D9 (2026-10-17): the shared contract ────────
#
# Each test below first pinned TODAY's behaviour before the shared
# controller; it now pins the decision the user approved, with a positive
# case and a negative twin that the rule must tell apart structurally.


def test_d1_escape_cancels_a_text_drag_and_still_cancels_a_drawing_when_idle(fp, monkeypatch):
    """@brief D1: Escape while a drag runs cancels it (text back, nothing committed); idle, Escape reaches the draw tool.

    Was: Escape outside draw mode was ignored and the drop still committed.
    Now, as in the symbol editor, `CancelOnEscapeAndHide` consumes an Escape
    only when a drag runs; the negative twin shows an idle Escape still
    cancelling the armed drawing tool through the view's own key handler.
    """
    window = fp
    view = window.preview_view
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    origin = QPointF(group.pos())
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(40, 20))
    assert (group.pos() - origin).manhattanLength() > 40
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert view._text_drag is None and not view.gestures.active, "Escape cancelled the drag"
    assert group.pos() == origin, "the text is back where it was (D5)"
    _release(window, start + QPoint(40, 20))
    cancelled = rec.phase("escape")
    assert cancelled["undo"] == 0 and window.footprint_text_positions == {} and cancelled["calls"] == {}
    # Negative twin: no drag runs, so Escape is not consumed and the armed
    # drawing tool sees it.
    window._toggle_footprint_draw_tool("circle")
    assert window._fp_draw_tool == "circle"
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert window._fp_draw_tool is None, "idle: Escape still cancels the tool"
    _golden("d1_escape", rec.phases)


def test_d2_the_wheel_is_refused_during_a_drag_and_zooms_again_after_the_drop(fp, monkeypatch):
    """@brief D2: a wheel notch mid-drag changes nothing (scale, drag, delta); after the drop it zooms as before.

    Was: the wheel rescaled the scene under the drag, so the next move
    re-measured the delta in the new scene and the text jumped.
    """
    window = fp
    view = window.preview_view
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    origin = QPointF(group.pos())
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(40, 20))
    _wheel(window, start + QPoint(40, 20), 1)
    assert view.transform().m11() == pytest.approx(1.0), "refused: no rescale under the drag"
    assert view._text_drag is not None
    _move(window, start + QPoint(41, 20))
    assert (group.pos() - (origin + QPointF(41, 20))).manhattanLength() < 1e-6, "still the pointer's delta"
    _release(window, start + QPoint(41, 20))
    drop = rec.phase("drop")
    assert drop["undo"] == 1
    # Negative twin: idle, the same notch zooms.
    _wheel(window, start, 1)
    assert view.transform().m11() == pytest.approx(1.2)
    _golden("d2_wheel", rec.phases)


def test_d3_a_right_click_is_ignored_during_a_drag_and_opens_the_menu_when_idle(fp, monkeypatch):
    """@brief D3: the right button mid-drag opens nothing and leaves the drag intact; idle, it opens the text's menu.

    Was: the menu opened over the running drag.
    """
    from kicadgen.gui import main_window as gui
    window = fp
    view = window.preview_view
    opened = []

    class FakeAction:
        def setEnabled(self, _value):
            pass

    class FakeMenu:
        def __init__(self, *_a, **_k):
            self.actions = []

        def addAction(self, *args):
            self.actions.append(args[-1])
            return FakeAction()

        def exec(self, *_a):
            opened.append(list(self.actions))
            return None

        # CONTRACT CHANGED 2026-10-27 (review B6): the text menu is released
        # with `deleteLater` once answered, like the QMenu it stands for.
        def deleteLater(self):
            pass

    monkeypatch.setattr(gui, "QMenu", FakeMenu)
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(40, 20))
    _press(window, start + QPoint(40, 20), Qt.MouseButton.RightButton)
    assert opened == [], "no menu over a drag"
    assert view._text_drag is not None, "the drag goes on"
    _release(window, start + QPoint(40, 20), Qt.MouseButton.RightButton)
    _release(window, start + QPoint(40, 20))
    drop = rec.phase("drop")
    assert drop["undo"] == 1
    # Negative twin: idle, the right click on the text opens its menu.
    group = _group(window, "Reference")
    _press(window, _glyph_box(window, group).center(), Qt.MouseButton.RightButton)
    _release(window, _glyph_box(window, group).center(), Qt.MouseButton.RightButton)
    assert len(opened) == 1
    _golden("d3_right_click", rec.phases)


def test_d4_a_redraw_under_a_text_drag_rebinds_the_new_item_and_the_drop_commits(fp, monkeypatch):
    """@brief D4: a redraw mid-drag re-binds the new text item at the pointer; the drop commits origin + delta.

    Was: the redraw deleted the dragged item, the next move dropped the
    drag and the release committed nothing (crash report 2026-10-13 fix).
    Now, as in the symbol editor, `_draw_footprint_texts` re-binds the new
    group and the controller re-applies the last delta. Negative twin: a
    handle drag holds indices, not items, and never needed the re-bind.
    """
    window = fp
    view = window.preview_view
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    origin = QPointF(group.pos())
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(10, 5))
    window._draw_preview()
    _place_camera(window)
    group = _group(window, "Reference")
    assert view._text_drag is not None and view._text_drag[0] is group, "re-bound to the new item"
    assert (group.pos() - (origin + QPointF(10, 5))).manhattanLength() < 1e-6, "resumed at the pointer"
    _move(window, start + QPoint(40, 20))
    assert (group.pos() - (origin + QPointF(40, 20))).manhattanLength() < 1e-6
    _release(window, start + QPoint(40, 20))
    text = rec.phase("text_redraw")
    expected = window._snapped_draw_point(origin.x() + 40, origin.y() + 20)
    assert window.footprint_text_positions == {"Reference": expected} and text["undo"] == 1
    # Twin: a handle drag survives a plain redraw by its indices.
    _with_drawing(window, "rectangle", ((0, 8), (4, 10)))
    rec.calls = {}
    _move(window, _vp(window, 4 * PX, 9 * PX))
    _press(window, _vp(window, 4 * PX, 10 * PX))
    _move(window, _vp(window, 5 * PX, 11 * PX))
    window._draw_preview()
    _place_camera(window)
    assert view._handle_drag == (0, 1)
    _release(window, _vp(window, 6 * PX, 12 * PX))
    handle = rec.phase("handle_redraw")
    assert window.footprint_drawings[0]["points"] == [[0, 8], [6.0, 12.0]] and handle["undo"] == 2
    _golden("d4_redraw", rec.phases)


def test_d5_a_cancelled_text_drag_puts_the_text_back_and_a_drop_keeps_it(fp, monkeypatch):
    """@brief D5: `cancel_interaction` (a list change under the drag) puts the text back where it was.

    Was: the item stayed where the pointer had taken it until the next
    redraw. Negative twin: a drop (not a cancel) keeps the moved text and
    commits it.
    """
    window = fp
    view = window.preview_view
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    origin = QPointF(group.pos())
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(40, 20))
    window._end_footprint_drawing_interaction()
    assert view._text_drag is None
    assert group.pos() == origin, "back where it was"
    _release(window, start + QPoint(40, 20))
    cancel = rec.phase("cancel")
    assert cancel["undo"] == 0 and window.footprint_text_positions == {} and cancel["calls"] == {}
    assert group.pos() == origin
    _press(window, start)
    _move(window, start + QPoint(40, 20))
    _release(window, start + QPoint(40, 20))
    drop = rec.phase("drop")
    assert drop["undo"] == 1 and "Reference" in window.footprint_text_positions
    _golden("d5_cancel", rec.phases)


def test_d9_hide_during_a_handle_drag_cancels_it_once_and_the_next_drag_is_clean(fp, monkeypatch):
    """@brief D9: Hide cancels the handle drag through the one cancel path: no stale flag, no ghost, no ignored drop.

    Was: the view forgot the drag but the window kept `_fp_handle_active`,
    so the next list change marked a drag that no longer existed and a
    drop reaching the window without a press was ignored. Now the window's
    flag is the controller's state and the ghost goes with the cancel.
    Negative twin: the mouse drag that follows commits normally.
    """
    window = fp
    view = window.preview_view
    _with_drawing(window, "rectangle", ((0, 8), (4, 10)))
    rec = _Recorder(window, monkeypatch)
    _move(window, _vp(window, 4 * PX, 9 * PX))
    _press(window, _vp(window, 4 * PX, 10 * PX))
    _move(window, _vp(window, 5 * PX, 11 * PX))
    assert len(_ghosts(window)) == 1
    view.hideEvent(QHideEvent())
    assert view._handle_drag is None and window._fp_handle_active is False
    assert window._fp_handle_drag is None and _ghosts(window) == [], "the ghost went with the cancel"
    _release(window, _vp(window, 6 * PX, 12 * PX))
    assert window.footprint_drawings[0]["points"] == [[0, 8], [4, 10]], "no drop after the cancel"
    window._apply_footprint_drawings(validate_footprint_drawings([_record("rectangle", ((0, 8), (4, 10)))]))
    _place_camera(window)
    assert not hasattr(window, "_fp_handle_cancelled"), "no flag left to go stale"
    stale = rec.phase("hide")
    assert stale["undo"] == 1
    _move(window, _vp(window, 3 * PX, 10 * PX))               # the bottom edge: a new hover point
    assert len(_handles(window)) == 2
    _press(window, _vp(window, 4 * PX, 10 * PX))
    _move(window, _vp(window, 5 * PX, 11 * PX))
    _release(window, _vp(window, 6 * PX, 12 * PX))
    mouse = rec.phase("mouse_drag")
    assert window.footprint_drawings[0]["points"] == [[0, 8], [6.0, 12.0]] and mouse["undo"] == 2
    _golden("d9_hide", rec.phases)


def test_d9_hide_during_a_text_drag_puts_the_text_back(fp, monkeypatch):
    """@brief D5/D9: Hide cancels a text drag: the square hides and the text is back where it was."""
    window = fp
    view = window.preview_view
    rec = _Recorder(window, monkeypatch)
    group = _group(window, "Reference")
    origin = QPointF(group.pos())
    start = _glyph_box(window, group).center()
    _press(window, start)
    _move(window, start + QPoint(40, 20))
    view.hideEvent(QHideEvent())
    assert view._text_drag is None and not _square(group).isVisible()
    assert group.pos() == origin
    _release(window, start + QPoint(40, 20))
    hide = rec.phase("hide")
    assert hide["undo"] == 0 and hide["calls"] == {}
    _golden("d9_hide_text", rec.phases)


def _repeat_shortcut(window, shortcut):
    """Play one auto-repeat of a held shortcut key: Qt's ShortcutOverride (repeat) then the activation."""
    view = window.preview_view
    QApplication.sendEvent(view, QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Z,
                                           Qt.KeyboardModifier.ControlModifier, "", True))
    shortcut.activated.emit()


def test_d8_a_held_ctrl_z_writes_the_recovery_file_once_on_release(fp, monkeypatch):
    """@brief D8: repeats of a held Ctrl+Z undo on every repeat but write once, when the key is released.

    Was: every activation wrote (and fsynced) the recovery file. Qt cannot
    synthesise an auto-repeat key for a widget from a test, so each repeat
    is played as Qt delivers it: a ShortcutOverride carrying the repeat flag
    (read by the shared `KeyRepeatFilter`), then the shortcut activation.
    Negative twin: three separate Ctrl+Z clicks (no repeat flag) write three times.
    """
    from PyQt6.QtGui import QKeySequence
    window = fp
    view = window.preview_view
    for x in (5, 6, 7, 8, 9, 10, 11, 12):
        window._apply_footprint_drawings(validate_footprint_drawings([_record("line", ((0, 8), (x, 8)))]))
    assert len(window._fp_drawings_undo) == 8
    undo = next(s for s in window._fp_shortcuts if s.key() == QKeySequence("Ctrl+Z"))
    rec = _Recorder(window, monkeypatch)
    view.setFocus()
    QApplication.processEvents()
    QTest.keyClick(view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)     # the real first press
    first = rec.phase("first_press")
    assert first["undo"] == 7 and first["calls"]["save_footprint_draft"] == 1
    for _ in range(3):
        _repeat_shortcut(window, undo)
    held = rec.phase("three_repeats")
    assert held["undo"] == 4 and held["redo"] == 4, "every repeat undoes"
    assert "save_footprint_draft" not in held["calls"], "no write while the key repeats"
    assert window._fp_checkpoint.pending
    QApplication.sendEvent(view, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Z,
                                           Qt.KeyboardModifier.ControlModifier, "", False))
    released = rec.phase("release")
    assert released["calls"] == {"save_footprint_draft": 1} and not window._fp_checkpoint.pending
    assert released["recovery"]["drawings"][0]["points"] == [[0, 8], [8, 8]], "the file holds the last state"
    # Negative twin: separate clicks are not a repeat: each writes (one
    # edit stays pending, so every click saves rather than removing the file).
    for _ in range(3):
        QTest.keyClick(view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    clicks = rec.phase("three_clicks")
    assert clicks["undo"] == 1 and clicks["calls"]["save_footprint_draft"] == 3
    _golden("d8_ctrl_z", rec.phases)


def _hold_ctrl_z(window):
    """Leave a deferred checkpoint pending: undo steps by a held Ctrl+Z, the first press real, the rest repeats."""
    from PyQt6.QtGui import QKeySequence
    view = window.preview_view
    for x in (5, 6, 7, 8):
        window._apply_footprint_drawings(validate_footprint_drawings([_record("line", ((0, 8), (x, 8)))]))
    undo = next(s for s in window._fp_shortcuts if s.key() == QKeySequence("Ctrl+Z"))
    view.setFocus()
    QApplication.processEvents()
    QTest.keyClick(view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    for _ in range(2):
        _repeat_shortcut(window, undo)
    assert window._fp_checkpoint.pending, "precondition: a write is deferred"


def test_d8_a_deferred_write_lands_under_the_project_that_was_edited(fp, monkeypatch):
    """@brief A write deferred by a held key is flushed BEFORE the project name (and recovery path) changes.

    Without it the pending flag survived into the next project and the old
    draft could be written under the new path when the key was released.
    Negative twin: with nothing deferred, a name change writes nothing.
    """
    window = fp
    old_path = window._footprint_recovery_path()
    _hold_ctrl_z(window)
    stale = json.loads(old_path.read_text(encoding="utf-8"))["draft"]["drawings"][0]["points"]
    assert stale == [[0, 8], [7, 8]], "the file still holds the state of the first press"
    rec = _Recorder(window, monkeypatch)
    window.field_name.setText("Altro_progetto")
    flushed = rec.phase("rename")
    assert flushed["calls"].get("save_footprint_draft") == 1 and not window._fp_checkpoint.pending
    assert json.loads(old_path.read_text(encoding="utf-8"))["draft"]["drawings"][0]["points"] == [[0, 8], [5, 8]], \
        "the last state landed in the OLD project's file"
    new_path = window._footprint_recovery_path()
    assert new_path != old_path and (new_path is None or not new_path.exists()), "nothing under the next project"
    # Negative twin: nothing pending, a further rename writes nothing.
    window.field_name.setText("Terzo_progetto")
    assert rec.phase("rename_again")["calls"].get("save_footprint_draft") is None


def test_d8_a_deferred_write_is_flushed_on_switch_and_on_close(fp, monkeypatch):
    """@brief The same flush runs at the start of `_switch_project` and when `closeEvent` accepts the close; a refused close flushes nothing."""
    window = fp
    path = window._footprint_recovery_path()
    _hold_ctrl_z(window)
    flushes = []
    real = window._flush_footprint_checkpoint
    monkeypatch.setattr(window, "_flush_footprint_checkpoint", lambda: flushes.append(1) or real())
    monkeypatch.setattr(window, "_reset_project_runtime_state", lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        window._switch_project("Altro", {})
    assert flushes == [1] and not window._fp_checkpoint.pending, "flushed before anything else of the switch"
    assert json.loads(path.read_text(encoding="utf-8"))["draft"]["drawings"][0]["points"] == [[0, 8], [5, 8]]
    # Negative twin: a plain edit elsewhere flushes nothing.
    flushes.clear()
    window._set_footprint_text_position("Value", (1.0, 9.0))
    assert flushes == []
    # Close: refused (the user stays) flushes nothing; accepted flushes once.
    monkeypatch.setattr(window, "_can_leave_project", lambda *a, **k: False)
    window.close()
    assert flushes == [] and not getattr(window, "_window_closed", False)
    monkeypatch.setattr(window, "_can_leave_project", lambda *a, **k: True)
    window.close()
    assert flushes == [1] and window._window_closed
