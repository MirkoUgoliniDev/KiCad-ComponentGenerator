"""@brief Minimal drawing palette of the footprint preview (user, 2026-10-13).

Free drawings (line, rectangle, circle, arc, text) on a chosen layer
(F.SilkS printed, F.Fab documentation, Cmts.User comments) live beside the
generated footprint, like the pad overrides: kept in the footprint draft
until «Applica» (user, 2026-10-14), then saved in the project under
`footprint_drawings`, validated on load, drawn in the 2D preview and
appended to the exported .kicad_mod. No numbered pads: a pad must match a
symbol pin, so it is not free drawing.
"""
import math
from copy import deepcopy

import pytest

from kicadgen.core.footprint import (
    FpArc, FpCircle, FpLine, FpRect, FpText, Footprint, USER_DRAW_LAYERS,
    drawing_graphics, validate_footprint_drawings,
)

pytest.importorskip("PyQt6")


def record(kind="line", points=((0, 0), (5, 5)), layer="F.SilkS", **extra):
    base = {"kind": kind, "layer": layer, "width": 0.12,
            "points": [list(p) for p in points]}
    base.update(extra)
    return base


# ── Validation ───────────────────────────────────────────────────────

def test_validation_keeps_one_well_formed_record_per_kind():
    raw = [record(),
           record("rectangle", ((0, 0), (3, 2))),
           record("circle", ((0, 0), (2, 0))),
           record("arc", ((0, 0), (1, 1), (2, 0))),
           record("text", ((1, 1),), text=" USB ", size=1.0)]
    clean = validate_footprint_drawings(raw)
    assert [r["kind"] for r in clean] == ["line", "rectangle", "circle", "arc", "text"]
    assert clean[4]["text"] == "USB", "text is stripped"
    assert all(r["layer"] in USER_DRAW_LAYERS for r in clean)


@pytest.mark.parametrize("bad", [
    record(layer="F.Cu"),                                   # copper is not free drawing
    record(layer="F.CrtYd"),                                # DRC layer excluded
    record(kind="pad"),                                     # unknown kind
    record(points=((0, 0),)),                               # a line needs two points
    record("rectangle", ((0, 0), (0, 5))),                  # flat rectangle
    record("circle", ((1, 1), (1, 1))),                     # zero radius
    record("arc", ((0, 0), (0, 0), (2, 0))),                # repeated arc point
    record(points=((0, float("nan")), (1, 1))),             # non-finite coordinate
    record(points=((0, True), (1, 1))),                     # bool is not a number
    record(points=((0, 501), (1, 1))),                      # outside the 500 mm bound
    record(width=0),                                        # degenerate stroke
    record("text", ((0, 0),), text="  ", size=1.0),         # blank text
    record("text", ((0, 0),), text="a\nb", size=1.0),       # two lines
    record("text", ((0, 0),), text="ok", size=0),           # non-positive size
    "not a dict",
])
def test_validation_drops_what_it_cannot_trust(bad):
    assert validate_footprint_drawings([bad]) == []
    assert validate_footprint_drawings(None) == [] and validate_footprint_drawings({}) == []


def test_validation_keeps_the_valid_records_of_a_partly_bad_list():
    clean = validate_footprint_drawings([record(), record(layer="F.Cu")])
    assert len(clean) == 1 and clean[0]["kind"] == "line"


# ── Conversion and export ────────────────────────────────────────────

def test_records_become_the_matching_graphics():
    items = drawing_graphics(validate_footprint_drawings([
        record(points=((0, 0), (5, 0), (5, 5))),
        record("rectangle", ((0, 0), (3, 2)), layer="F.Fab"),
        record("circle", ((0, 0), (0, 2.5)), layer="Cmts.User"),
        record("arc", ((0, 0), (1, 1), (2, 0))),
        record("text", ((1, -1),), text="USB", size=1.2),
    ]))
    lines = [i for i in items if isinstance(i, FpLine)]
    assert len(lines) == 2, "one FpLine per polyline segment"
    rect = next(i for i in items if isinstance(i, FpRect))
    assert rect.layer == "F.Fab"
    circle = next(i for i in items if isinstance(i, FpCircle))
    assert circle.radius == pytest.approx(2.5) and circle.layer == "Cmts.User"
    arc = next(i for i in items if isinstance(i, FpArc))
    assert (arc.start, arc.mid, arc.end) == ((0, 0), (1, 1), (2, 0))
    text = next(i for i in items if isinstance(i, FpText))
    assert text.kind == "user" and text.text == "USB" and text.size == pytest.approx(1.2)


def test_fp_arc_serializes_start_mid_end_stroke_and_layer():
    rendered = str(FpArc(start=(0, 0), mid=(1, 1), end=(2, 0), layer="F.SilkS").to_sexpr())
    for piece in ("fp_arc", "(start 0 0)", "(mid 1 1)", "(end 2 0)",
                  '(layer "F.SilkS")', "(width 0.12)"):
        assert piece in rendered


def test_the_worker_appends_the_drawings_to_the_written_file(tmp_path, qapp):
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.gui.workers import GenerateWorker
    params = PinHeaderParams(name="H", rows=2, cols=3, pitch=2.54, row_spacing=2.54,
                             drill=1.0, pad_diameter=1.7)
    worker = GenerateWorker(params, tmp_path)
    worker.drawings = validate_footprint_drawings([
        record("arc", ((0, 0), (1, 1), (2, 0))),
        record("text", ((0, -3),), text="USB", layer="F.Fab", size=1.0),
    ])
    written = []
    worker.done.connect(written.append)
    worker.run()
    assert written, "the footprint was written"
    text = (tmp_path / "H.pretty" / "H.kicad_mod").read_text(encoding="utf-8")
    assert "fp_arc" in text and '"USB"' in text and '(layer "F.Fab")' in text


def test_without_drawings_the_file_gains_nothing():
    assert drawing_graphics([]) == []
    assert "fp_arc" not in Footprint(name="X").render()


# ── Window ───────────────────────────────────────────────────────────

@pytest.fixture
def preview(window, monkeypatch):
    """The window with its preview drawn; saves and regenerations recorded."""
    saves, generations = [], []
    real_save = window._save_project_config
    monkeypatch.setattr(window, "_save_project_config",
                        lambda force=False: saves.append(force) or real_save(force=force))
    monkeypatch.setattr(window, "_generate", lambda **kwargs: generations.append(kwargs) or True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    # The .kicad_mod rewrite runs on the next event-loop turn (see
    # test_the_rewrite_after_an_edit_is_deferred_and_coalesced): these tests
    # check WHAT is rewritten, so the turn is run right away.
    monkeypatch.setattr(window, "_schedule_footprint_rewrite",
                        lambda **_kwargs: window._generate(footprint_only=True))
    window._draw_preview()
    return window, saves, generations


def _main_grid_snap(window):
    """Drawings snap to the main grid itself (divisor 1): coarse-grid cases."""
    window.combo_fine_grid.setCurrentIndex(window.combo_fine_grid.findData(1))


def _drawing_items(window):
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    return [item for item in window.preview_scene.items()
            if item.data(DRAWING_KEY_ROLE) is not None]


def test_an_armed_rectangle_closes_on_its_second_click_and_persists(preview):
    window, saves, generations = preview
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    window._toggle_footprint_draw_tool("rectangle")
    assert window._fp_draw_tool == "rectangle"
    window._on_footprint_draw_clicked(1.23 * 20, 1.17 * 20)
    window._on_footprint_draw_clicked(4.94 * 20, 2.81 * 20)
    assert window._fp_draw_tool is None, "the tool disarms itself when the shape closes"
    # Drawings take the fine grid (1/10 of the 1 mm grid), not the pad grid.
    assert window.footprint_drawings == [{"kind": "rectangle", "layer": "F.SilkS",
                                          "width": 0.12, "points": [[1.2, 1.2], [4.9, 2.8]]}]
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): an edit stays in the
    # draft (nothing saved, nothing rewritten); «Applica» saves it once and
    # rewrites the .kicad_mod once.
    assert saves == [] and generations == []
    assert _drawing_items(window), "the preview draws the drafted rectangle"
    assert window._project_state()["footprint_drawings"] == [], "the file keeps the applied list"
    assert window.apply_footprint_draft()
    assert saves == [True] and generations == [{"footprint_only": True}]
    assert window._project_state()["footprint_drawings"] == window.footprint_drawings


def test_the_line_collects_clicks_until_asked_to_finish(preview):
    window, _saves, generations = preview
    window.chk_grid.setChecked(False)
    window._toggle_footprint_draw_tool("line")
    for x, y in ((0, 0), (2, 0), (2, 2)):
        window._on_footprint_draw_clicked(x * 20, y * 20)
    assert window.footprint_drawings == [] and len(window._fp_draw_clicks) == 3
    window._on_footprint_draw_finish()
    assert len(window.footprint_drawings) == 1
    assert window.footprint_drawings[0]["points"] == [[0, 0], [2, 0], [2, 2]]
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): an edit stays in the
    # draft (nothing saved, nothing rewritten); «Applica» saves it once and
    # rewrites the .kicad_mod once.
    assert not generations
    window.apply_footprint_draft()
    assert generations, "the .kicad_mod is rewritten"


def test_escape_cancels_without_saving_anything(preview):
    window, saves, _generations = preview
    window._toggle_footprint_draw_tool("circle")
    window._on_footprint_draw_clicked(0, 0)
    window._cancel_footprint_drawing()
    assert window._fp_draw_clicks == [] and window.footprint_drawings == []
    assert saves == []
    assert not window._fp_draw_buttons["circle"].isChecked()


def test_a_degenerate_circle_is_not_kept(preview):
    window, saves, _generations = preview
    window.chk_grid.setChecked(False)
    window._toggle_footprint_draw_tool("circle")
    window._on_footprint_draw_clicked(1 * 20, 1 * 20)
    window._on_footprint_draw_clicked(1.01 * 20, 1.01 * 20)  # same point after snap
    window._on_footprint_draw_finish()
    assert window.footprint_drawings == [] and saves == []


def test_the_arc_stores_start_mid_end_from_start_end_mid_clicks(preview):
    window, _saves, _generations = preview
    window.chk_grid.setChecked(False)
    window._toggle_footprint_draw_tool("arc")
    for x, y in ((0, 0), (4, 0), (2, 2)):   # start, end, mid: the symbol's order
        window._on_footprint_draw_clicked(x * 20, y * 20)
    assert window.footprint_drawings[0]["points"] == [[0, 0], [2, 2], [4, 0]]


def test_the_layer_selector_decides_the_record_layer(preview):
    window, _saves, _generations = preview
    window.combo_fp_draw_layer.setCurrentIndex(
        window.combo_fp_draw_layer.findData("Cmts.User"))
    window.chk_grid.setChecked(False)
    window._toggle_footprint_draw_tool("rectangle")
    window._on_footprint_draw_clicked(0, 0)
    window._on_footprint_draw_clicked(3 * 20, 2 * 20)
    assert window.footprint_drawings[0]["layer"] == "Cmts.User"


def test_the_context_menu_deletes_the_drawing(preview, monkeypatch):
    window, saves, generations = preview
    window.footprint_drawings = validate_footprint_drawings([record()])
    window._draw_preview()
    assert _drawing_items(window)
    # Harness changed with phase C (docs/canvas-menus-plan.md): the menu is
    # now built by the shared `canvas_menus` module, so the fake replaces
    # `QMenu.exec` (used by both) instead of the window's `QMenu` name. The
    # contract checked below is unchanged.
    from PyQt6.QtWidgets import QMenu
    monkeypatch.setattr(QMenu, "exec", lambda menu, *_a: next(
        a for a in menu.actions() if "Elimina" in a.text()))
    window._footprint_drawing_context_menu(0)
    assert window.footprint_drawings == []
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): an edit stays in the
    # draft (nothing saved, nothing rewritten); «Applica» saves it once and
    # rewrites the .kicad_mod once.
    assert not saves and not generations
    window.apply_footprint_draft()
    assert saves and generations


def test_drawings_survive_the_project_file_and_malformed_entries_are_dropped(window):
    state = deepcopy(window._project_state())
    state["footprint_drawings"] = [record(), record(layer="F.Cu"), "junk"]
    window._apply_project_state(state)
    assert len(window.footprint_drawings) == 1
    assert window.footprint_drawings[0]["kind"] == "line"
    state["footprint_drawings"] = None
    window._apply_project_state(state)
    assert window.footprint_drawings == []


def test_the_generation_worker_receives_a_copy_of_the_drawings(window, monkeypatch):
    from kicadgen.gui import main_window as gui
    from test_package_project_gui import acquire
    acquire(window)
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: started.append(worker))
    window.footprint_drawings = validate_footprint_drawings([record()])
    window._generate(footprint_only=True)
    assert started and started[0].drawings == window.footprint_drawings
    started[0].drawings[0]["layer"] = "F.Fab"
    assert window.footprint_drawings[0]["layer"] == "F.SilkS", "the worker got a copy"


def test_the_mounting_row_keeps_the_combo_short_with_the_diameters_beside_it(window):
    """User, 2026-10-13: combo at its contents, Pad/Foro on the same row."""
    from PyQt6.QtWidgets import QComboBox
    combo = window.combo_preview_mounting
    assert combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToContents
    # The diameters widget sits in the same horizontal row as the combo.
    assert window.preview_header_dimensions.parentWidget() is combo.parentWidget()


def test_real_mouse_clicks_draw_a_circle_through_the_view(preview):
    """Regression (user, 2026-10-13 screenshot): palette armed, clicks, nothing drawn."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    window, _saves, generations = preview
    view = window.preview_view
    view.resize(800, 600)
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("inch"))
    window._toggle_footprint_draw_tool("circle")

    def press(scene_x, scene_y, button=Qt.MouseButton.LeftButton):
        pos = view.mapFromScene(QPointF(scene_x, scene_y))
        event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(pos),
                            QPointF(view.viewport().mapToGlobal(pos)), button, button,
                            Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(event)

    press(0, 0)
    assert window._fp_draw_clicks == [(0.0, 0.0)]
    press(2.54 * 3 * 20, 0)
    assert window._fp_draw_tool is None, "the circle closed on its second click"
    assert window.footprint_drawings and window.footprint_drawings[0]["kind"] == "circle"
    assert _drawing_items(window), "the circle is drawn in the preview"
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): an edit stays in the
    # draft (nothing saved, nothing rewritten); «Applica» saves it once and
    # rewrites the .kicad_mod once.
    assert not generations
    window.apply_footprint_draft()
    assert generations


def test_two_fast_clicks_still_draw_the_circle(preview):
    """Regression (user, 2026-10-13: «non funziona»): a fast second click is a
    DOUBLE CLICK for Qt (press, release, dblclick: no second press), so the
    view must treat it as the missing point, not only as a finish request."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    window, _saves, _generations = preview
    view = window.preview_view
    view.resize(800, 600)
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("inch"))
    window._toggle_footprint_draw_tool("circle")

    def event_at(kind, scene_x, scene_y):
        pos = view.mapFromScene(QPointF(scene_x, scene_y))
        return QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)

    view.mousePressEvent(event_at(QEvent.Type.MouseButtonPress, 0, 0))
    # The second physical click, fast: Qt delivers only a double click.
    view.mouseDoubleClickEvent(event_at(QEvent.Type.MouseButtonDblClick, 2.54 * 2 * 20, 0))
    assert window.footprint_drawings and window.footprint_drawings[0]["kind"] == "circle", \
        "the fast second click closed the circle instead of silently cancelling it"
    assert window._fp_draw_tool is None


def test_a_coinciding_click_and_a_degenerate_shape_are_said_not_silent(preview, monkeypatch):
    """On a coarse grid two clicks can snap to the same node: the user is told."""
    window, saves, _generations = preview
    shown = []
    monkeypatch.setattr(window, "_draw_feedback_toast",
                        lambda title, body: shown.append(title))
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("inch"))
    _main_grid_snap(window)
    window._toggle_footprint_draw_tool("circle")
    window._on_footprint_draw_clicked(0, 0)
    window._on_footprint_draw_clicked(0.9 * 20, 0.9 * 20)  # same 2.54 node
    assert len(shown) == 1 and window._fp_draw_tool == "circle", \
        "the click is refused with a message, the tool stays armed"
    window._cancel_footprint_drawing()
    # Degenerate at finish time: a flat rectangle (both clicks on the same x).
    # Was set through `_fp_draw_clicks`, now read-only: the clicks go through the host.
    window._toggle_footprint_draw_tool("rectangle")
    window._on_footprint_draw_clicked(0, 0)          
    window._on_footprint_draw_clicked(0, 5 * 20)
    assert len(shown) == 2 and window.footprint_drawings == [] and saves == []


def test_press_drag_release_draws_the_rectangle_like_a_cad(preview):
    """Regression (user, 2026-10-13, second «non funziona»): the release point
    of a drag is the next drawing point; a plain click's release adds nothing."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    window, _saves, _generations = preview
    view = window.preview_view
    view.resize(800, 600)
    window.chk_grid.setChecked(False)
    window._toggle_footprint_draw_tool("rectangle")

    def send(kind, scene_x, scene_y):
        pos = view.mapFromScene(QPointF(scene_x, scene_y))
        event = QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.MouseButtonPress: view.mousePressEvent,
         QEvent.Type.MouseMove: view.mouseMoveEvent,
         QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)

    send(QEvent.Type.MouseButtonPress, 0, 0)
    send(QEvent.Type.MouseMove, 60, 40)
    send(QEvent.Type.MouseButtonRelease, 120, 80)
    assert window.footprint_drawings and window.footprint_drawings[0]["kind"] == "rectangle"
    assert window.footprint_drawings[0]["points"] == [[0, 0], [6.0, 4.0]]

    # A plain click (release where pressed) is one point only.
    window._toggle_footprint_draw_tool("circle")
    send(QEvent.Type.MouseButtonPress, 0, 0)
    send(QEvent.Type.MouseButtonRelease, 0, 0)
    assert window._fp_draw_clicks == [(0.0, 0.0)] and window._fp_draw_tool == "circle"


def test_a_text_drag_survives_a_redraw_under_it(preview):
    """Crash report 2026-10-13 08:29: the dragged item was deleted by a redraw."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    window, _saves, _generations = preview
    view = window.preview_view
    view.resize(800, 600)
    item = next(i for i in window.preview_scene.items()
                if i.data(TEXT_KEY_ROLE) is not None and i.parentItem() is None)
    start = view.mapFromScene(item.pos())

    def mouse(kind, pos):
        return QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)

    view.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, start))
    window._draw_preview()  # deletes every item, the dragged one included
    view.mouseMoveEvent(mouse(QEvent.Type.MouseMove, start + QPointF(30, 30).toPoint()))
    view.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, start + QPointF(30, 30).toPoint()))
    assert view._text_drag is None


def test_clicks_snap_to_the_nodes_of_the_drawn_grid_not_to_the_origin(preview):
    """User, 2026-10-13: the drawn grid passes through the first pad; a click
    must land on ITS nodes, not on multiples of the footprint origin."""
    window, _saves, _generations = preview
    view = window.preview_view
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("inch"))
    view.set_grid(2.54 * 20, (0.7 * 20, -0.3 * 20), True)   # origin off the mm lattice
    _main_grid_snap(window)
    window._toggle_footprint_draw_tool("rectangle")
    window._on_footprint_draw_clicked(0.9 * 20, -0.1 * 20)
    window._on_footprint_draw_clicked((0.7 + 2.54 * 2 + 0.4) * 20, (-0.3 + 2.54 - 0.5) * 20)
    points = window.footprint_drawings[0]["points"]
    assert points == [[0.7, -0.3], [pytest.approx(5.78), pytest.approx(2.24)]]
    # The fine grid keeps the same origin: its nodes are origin + k x 0.254.
    window.combo_fine_grid.setCurrentIndex(window.combo_fine_grid.findData(10))
    view.set_grid(2.54 * 20, (0.7 * 20, -0.3 * 20), True)   # the redraw reset it
    window._toggle_footprint_draw_tool("circle")
    window._on_footprint_draw_clicked(0.9 * 20, -0.1 * 20)
    x, y = window._fp_draw_clicks[0]
    assert (x, y) == (pytest.approx(0.954), pytest.approx(-0.046))
    window._cancel_footprint_drawing()
    # Grid hidden: tenth of a millimetre from the origin.
    window.chk_grid.setChecked(False)
    view.set_grid(2.54 * 20, (0.7 * 20, -0.3 * 20), False)
    window._toggle_footprint_draw_tool("circle")
    window._on_footprint_draw_clicked(1.234 * 20, 0)
    assert window._fp_draw_clicks == [(1.2, 0.0)]


def _marker_items(window):
    return [item for item in window.preview_scene.items() if item.data(0x44524D) is not None]


def test_the_pointer_shows_the_snap_node_and_the_ghost_shape(preview):
    """User, 2026-10-13: like the symbol editor, show the node the click would
    take and the dashed outline of the shape in progress."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsPathItem
    window, _saves, _generations = preview
    view = window.preview_view
    view.resize(800, 600)
    assert view.hasMouseTracking() and view.viewport().hasMouseTracking()
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    view.set_grid(20.0, (0.0, 0.0), True)
    _main_grid_snap(window)
    window._toggle_footprint_draw_tool("rectangle")

    def move_to(scene_x, scene_y):
        pos = view.mapFromScene(QPointF(scene_x, scene_y))
        view.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(pos),
                                        QPointF(view.viewport().mapToGlobal(pos)),
                                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                        Qt.KeyboardModifier.NoModifier))

    # Before any click: only the snap dot, on the nearest grid node.
    move_to(1.3 * 20, 0.6 * 20)
    assert window._fp_draw_hover == (1.0, 1.0)
    dots = [i for i in _marker_items(window) if isinstance(i, QGraphicsEllipseItem)]
    assert len(dots) == 1 and dots[0].pos() == QPointF(20.0, 20.0)
    assert dots[0].brush().color().name() == "#f9e2af", "yellow, as in the symbol editor"
    assert not [i for i in _marker_items(window) if isinstance(i, QGraphicsPathItem)]
    # After the first click: clicked dot, snap dot and the dashed rectangle.
    window._on_footprint_draw_clicked(1.0 * 20, 1.0 * 20)
    move_to(4.2 * 20, 2.9 * 20)
    dots = [i for i in _marker_items(window) if isinstance(i, QGraphicsEllipseItem)]
    ghosts = [i for i in _marker_items(window) if isinstance(i, QGraphicsPathItem)]
    assert len(dots) == 2 and len(ghosts) == 1
    assert ghosts[0].pen().style() == Qt.PenStyle.DashLine
    assert ghosts[0].path().boundingRect() == QRectF_(20.0, 20.0, 60.0, 40.0)
    # The pointer leaves: marker and ghost go, the clicked dot stays.
    view.leaveEvent(None)
    assert window._fp_draw_hover is None
    assert len(_marker_items(window)) == 1
    # Disarming clears everything.
    window._cancel_footprint_drawing()
    assert _marker_items(window) == []


def QRectF_(x, y, w, h):
    from PyQt6.QtCore import QRectF
    return QRectF(x, y, w, h)


def _handles(window):
    from kicadgen.gui.footprint_view import HANDLE_KEY_ROLE
    return [i for i in window.preview_scene.items() if i.data(HANDLE_KEY_ROLE) is not None]


def test_hovering_a_drawing_reveals_its_handles_and_a_drag_moves_the_point(preview):
    """User, 2026-10-13: «quando passo sopra il disegno devono apparire i punti
    di ancoraggio», draggable on the grid like in the symbol editor."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    window, saves, generations = preview
    view = window.preview_view
    view.resize(800, 600)
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    view.set_grid(20.0, (0.0, 0.0), True)
    _main_grid_snap(window)
    window.footprint_drawings = validate_footprint_drawings([
        record("rectangle", ((0, 0), (4, 3)))])
    window._draw_preview()
    saves.clear(), generations.clear()
    assert _handles(window) == [], "no handles until the pointer is over the drawing"

    def send(kind, scene_x, scene_y, button=Qt.MouseButton.NoButton):
        pos = view.mapFromScene(QPointF(scene_x, scene_y))
        event = QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                            button, button, Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.MouseButtonPress: view.mousePressEvent,
         QEvent.Type.MouseMove: view.mouseMoveEvent,
         QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)

    send(QEvent.Type.MouseMove, 4 * 20, 1.5 * 20)        # on the right edge
    assert len(_handles(window)) == 2, "one handle per stored point, no extra centre anchor"
    send(QEvent.Type.MouseMove, 10 * 20, 10 * 20)        # away from it
    assert _handles(window) == []
    send(QEvent.Type.MouseMove, 4 * 20, 1.5 * 20)
    # Drag the second corner from (4, 3) to the node nearest (6.3, 4.8).
    send(QEvent.Type.MouseButtonPress, 4 * 20, 3 * 20, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseMove, 5 * 20, 4 * 20, Qt.MouseButton.LeftButton)
    assert window._fp_handle_drag == (0, 1, (5.0, 4.0))
    send(QEvent.Type.MouseButtonRelease, 6.3 * 20, 4.8 * 20, Qt.MouseButton.LeftButton)
    assert window.footprint_drawings[0]["points"] == [[0, 0], [6.0, 5.0]]
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): an edit stays in the
    # draft (nothing saved, nothing rewritten); «Applica» saves it once and
    # rewrites the .kicad_mod once.
    assert saves == [] and generations == []
    window.apply_footprint_draft()
    assert saves == [True] and generations == [{"footprint_only": True}]
    assert window._fp_handle_drag is None


def test_a_handle_drop_that_flattens_the_shape_is_refused(preview, monkeypatch):
    window, saves, _generations = preview
    shown = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: shown.append(title))
    window.chk_grid.setChecked(False)
    window.footprint_drawings = validate_footprint_drawings([record("circle", ((0, 0), (3, 0)))])
    window._draw_preview()
    saves.clear()
    window._on_footprint_handle_dropped(0, 1, 0, 0)       # rim onto the centre
    assert window.footprint_drawings[0]["points"] == [[0, 0], [3, 0]]
    assert shown == ["Spostamento scartato."] and saves == []
    # Out-of-range indices are ignored, never raise.
    window._on_footprint_handle_dropped(5, 0, 0, 0)
    window._on_footprint_handle_dragged(0, 9, 0, 0)


def test_the_body_moves_the_whole_drawing_without_a_centre_anchor(preview):
    """User, 2026-10-13: the element moves as in the symbol editor, by its
    body; the solid centre anchor was too loud («più discreto») and is gone."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QGraphicsItem
    window, saves, generations = preview
    view = window.preview_view
    view.resize(800, 600)
    # CONTRACT CHANGED 2026-10-15 (audit item 4): only a DOCUMENTED body draws
    # its outline; this scenario was built over a body (the dimension labels
    # then lie outside it, away from the rectangle drawn below), so the
    # component stands for one whose analysis declared its body. Without a
    # body see test_a_drawing_moves_by_its_body_without_a_component_body.
    window._body_size_declared = True
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    view.set_grid(20.0, (0.0, 0.0), True)
    _main_grid_snap(window)
    # Away from REF** (y = -2 mm) and the value (y = 2 mm), which a press
    # would take first.
    window.footprint_drawings = validate_footprint_drawings([
        record("rectangle", ((0, 4), (4, 6)))])
    window._draw_preview()
    saves.clear(), generations.clear()

    def send(kind, scene_x, scene_y, button=Qt.MouseButton.NoButton):
        pos = view.mapFromScene(QPointF(scene_x, scene_y))
        event = QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                            button, button, Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.MouseButtonPress: view.mousePressEvent,
         QEvent.Type.MouseMove: view.mouseMoveEvent,
         QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)

    send(QEvent.Type.MouseMove, 4 * 20, 5 * 20)           # hover the right edge
    from kicadgen.gui.footprint_view import HANDLE_KEY_ROLE
    handles = _handles(window)
    assert len(handles) == 2 and not any(str(h.data(HANDLE_KEY_ROLE)).endswith(":-1") for h in handles)
    # Same style as the symbol editor's handles: yellow outline, translucent
    # green fill, constant screen size.
    for handle in handles:
        assert handle.pen().color().name() == "#f9e2af"
        assert handle.brush().color().name() == "#a6e3a1"
        assert handle.brush().color().alpha() == 150
        assert handle.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
    # The body shows the move cursor, as an element of the symbol editor.
    assert all(item.cursor().shape() == Qt.CursorShape.SizeAllCursor for item in _drawing_items(window))
    # Grab the body (the drawn edge) and drop it two nodes right, one down.
    send(QEvent.Type.MouseButtonPress, 4 * 20, 5 * 20, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseMove, 5 * 20, 5 * 20, Qt.MouseButton.LeftButton)
    assert window._fp_handle_drag == (0, -1, (1.0, 0.0))
    send(QEvent.Type.MouseButtonRelease, 6.2 * 20, 6.1 * 20, Qt.MouseButton.LeftButton)
    assert window.footprint_drawings[0]["points"] == [[2.0, 5.0], [6.0, 7.0]]
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): an edit stays in the
    # draft (nothing saved, nothing rewritten); «Applica» saves it once and
    # rewrites the .kicad_mod once.
    assert saves == [] and generations == []
    window.apply_footprint_draft()
    assert saves == [True] and generations == [{"footprint_only": True}]
    # A drop on the grab node changes nothing: no new draft, no save.
    send(QEvent.Type.MouseButtonPress, 6 * 20, 6 * 20, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseButtonRelease, 6 * 20, 6 * 20, Qt.MouseButton.LeftButton)
    assert window.footprint_drawings[0]["points"] == [[2.0, 5.0], [6.0, 7.0]] and len(saves) == 1
    assert not window.has_pending_footprint_edits()
    # A text has one point only: its handle is the only one.
    window.footprint_drawings = validate_footprint_drawings([record("text", ((1, 1),), text="X")])
    window._draw_preview()
    window._on_footprint_drawing_hovered(0)
    assert len(_handles(window)) == 1


def test_undo_and_redo_walk_the_drawing_history_like_the_symbol_editor(preview):
    """User rule, 2026-10-13: what the symbol editor has, the preview has too."""
    window, saves, _generations = preview
    window.chk_grid.setChecked(False)
    def enabled(button):
        # The preview tab is disabled before an acquisition: judge the
        # button's own state, not an ancestor's.
        return button.isEnabledTo(button.parentWidget())

    assert not enabled(window.btn_fp_undo) and not enabled(window.btn_fp_redo)
    window._toggle_footprint_draw_tool("rectangle")
    window._on_footprint_draw_clicked(0, 0)
    window._on_footprint_draw_clicked(3 * 20, 2 * 20)
    window._toggle_footprint_draw_tool("circle")
    window._on_footprint_draw_clicked(0, 0)
    window._on_footprint_draw_clicked(2 * 20, 0)
    assert [r["kind"] for r in window.footprint_drawings] == ["rectangle", "circle"]
    assert enabled(window.btn_fp_undo) and not enabled(window.btn_fp_redo)
    window._undo_footprint_drawing()
    assert [r["kind"] for r in window.footprint_drawings] == ["rectangle"]
    assert enabled(window.btn_fp_redo)
    window._undo_footprint_drawing()
    assert window.footprint_drawings == [] and not enabled(window.btn_fp_undo)
    window._redo_footprint_drawing()
    assert [r["kind"] for r in window.footprint_drawings] == ["rectangle"]
    # A new edit after an undo forgets the redo branch.
    window._toggle_footprint_draw_tool("text")
    window.footprint_drawings = window.footprint_drawings  # unchanged
    window._apply_footprint_drawings(window.footprint_drawings + validate_footprint_drawings(
        [record("text", ((1, 1),), text="X")]))
    assert not enabled(window.btn_fp_redo)
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): an edit stays in the
    # draft (nothing saved, nothing rewritten); «Applica» saves it once and
    # rewrites the .kicad_mod once.
    assert saves == [], "every step, undo and redo included, stays in the draft"
    assert window.apply_footprint_draft()
    assert saves == [True], "one save for the whole draft"


def test_delete_and_duplicate_act_on_the_hovered_drawing(preview):
    window, _saves, _generations = preview
    view = window.preview_view
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    view.set_grid(20.0, (0.0, 0.0), True)
    window.footprint_drawings = validate_footprint_drawings([record("rectangle", ((0, 0), (3, 2)))])
    window._draw_preview()
    window._delete_hovered_drawing()          # nothing hovered: nothing happens
    assert len(window.footprint_drawings) == 1
    window._on_footprint_drawing_hovered(0)
    window._duplicate_hovered_drawing()
    assert len(window.footprint_drawings) == 2
    assert window.footprint_drawings[1]["points"] == [[1.0, 1.0], [4.0, 3.0]], \
        "the copy sits one grid step right and down, as in the symbol editor"
    window._on_footprint_drawing_hovered(1)
    window._delete_hovered_drawing()
    assert len(window.footprint_drawings) == 1
    assert window._fp_hovered_drawing == -1, "nothing is hovered after an edit redraws the scene"


def test_layer_and_width_change_through_the_menu_keep_the_record_valid(preview):
    window, saves, _generations = preview
    window.footprint_drawings = validate_footprint_drawings([record("circle", ((0, 0), (2, 0)))])
    window._draw_preview()
    saves.clear()
    window._set_drawing_field(0, "layer", "F.Fab")
    window._set_drawing_field(0, "width", 0.25)
    assert window.footprint_drawings[0]["layer"] == "F.Fab"
    assert window.footprint_drawings[0]["width"] == 0.25
    window._set_drawing_field(0, "layer", "F.Cu")       # not a drawing layer: refused
    window._set_drawing_field(0, "width", 0)
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): an edit stays in the
    # draft (nothing saved, nothing rewritten); «Applica» saves it once and
    # rewrites the .kicad_mod once.
    assert window.footprint_drawings[0]["layer"] == "F.Fab" and saves == []
    window.apply_footprint_draft()
    assert len(saves) == 1
    state = deepcopy(window._project_state())
    window._apply_project_state(state)
    assert not window.btn_fp_undo.isEnabledTo(window.btn_fp_undo.parentWidget()),         "a project load starts a fresh history"


# ── Fine drawing grid and grab fluidity (user, 2026-10-13) ────────────

def _send(view, kind, scene_x, scene_y, button=None, offset=(0, 0)):
    from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    button = Qt.MouseButton.NoButton if button is None else button
    pos = view.mapFromScene(QPointF(scene_x, scene_y)) + QPoint(*offset)
    event = QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                        button, button, Qt.KeyboardModifier.NoModifier)
    {QEvent.Type.MouseButtonPress: view.mousePressEvent,
     QEvent.Type.MouseMove: view.mouseMoveEvent,
     QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)


def _zoomed_out(window, scale):
    view = window.preview_view
    view.resize(800, 600)
    view.resetTransform()
    view.scale(scale, scale)
    view.centerOn(0, 0)


def test_a_handle_is_grabbed_a_few_screen_pixels_off_at_a_far_zoom(preview):
    """Profiling 2026-10-13: the hit test ignored the view transform, so a
    6 px handle was a 1 px target at «fit» zooms and a near miss panned the
    camera. Positive: 3 px off still grabs. Negative: 12 px off does not."""
    from PyQt6.QtCore import QEvent, Qt
    window, _saves, _generations = preview
    window.chk_grid.setChecked(False)
    window.footprint_drawings = validate_footprint_drawings([record("rectangle", ((0, 0), (4, 3)))])
    window._draw_preview()
    _zoomed_out(window, 0.12)
    view = window.preview_view
    window._on_footprint_drawing_hovered(0)
    _send(view, QEvent.Type.MouseButtonPress, 4 * 20, 3 * 20, Qt.MouseButton.LeftButton, offset=(3, 2))
    assert view._handle_drag == (0, 1) and not view.camera.pan_active
    view._handle_drag = None    # no drop: a drop would redraw and refit the view
    _send(view, QEvent.Type.MouseButtonPress, 4 * 20, 3 * 20, Qt.MouseButton.LeftButton, offset=(12, 12))
    assert view._handle_drag is None and view.camera.pan_active, "far away: the camera pans"
    view.camera.pan_cancel()


def test_handles_stay_while_the_pointer_is_near_the_drawing(preview):
    """Like the symbol editor's reveal area: leaving the stroke by a few px
    keeps the handles (no flicker), going well away hides them."""
    from PyQt6.QtCore import QEvent
    window, _saves, _generations = preview
    window.chk_grid.setChecked(False)
    window.footprint_drawings = validate_footprint_drawings([record("line", ((0, 0), (8, 0)))])
    window._draw_preview()
    _zoomed_out(window, 1.0)
    view = window.preview_view
    _send(view, QEvent.Type.MouseMove, 4 * 20, 0)
    assert len(_handles(window)) == 2
    _send(view, QEvent.Type.MouseMove, 4 * 20, 0, offset=(0, 9))       # off the stroke, near
    assert len(_handles(window)) == 2 and view.hovered_drawing() == 0
    _send(view, QEvent.Type.MouseMove, 4 * 20, 0, offset=(0, 60))      # well away
    assert _handles(window) == [] and view.hovered_drawing() == -1


def test_a_text_inside_a_rectangle_is_dragged_not_the_rectangle(preview):
    """Reference/Value sit above the drawings: a press on them drags the text."""
    from PyQt6.QtCore import QEvent, Qt
    window, _saves, _generations = preview
    window.chk_grid.setChecked(False)
    window.footprint_drawings = validate_footprint_drawings([
        record("rectangle", ((-20, -20), (20, 20)))])
    window._draw_preview()
    _zoomed_out(window, 1.0)
    view = window.preview_view
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    text = next(i for i in window.preview_scene.items()
                if i.data(TEXT_KEY_ROLE) == "Value" and i.parentItem() is None)
    _send(view, QEvent.Type.MouseButtonPress, text.pos().x(), text.pos().y(), Qt.MouseButton.LeftButton)
    assert view._text_drag is not None and view._handle_drag is None
    view._text_drag = None


def test_the_fine_grid_dots_show_only_when_legible_and_pads_keep_the_main_grid(preview):
    window, _saves, _generations = preview
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    view = window.preview_view
    view.set_grid(20.0, (0.0, 0.0), True)
    from kicadgen.gui.footprint_view import DEFAULT_FINE_GRID_DIVISOR, FINE_GRID_MIN_SCREEN_PX
    assert view.fine_divisor() == DEFAULT_FINE_GRID_DIVISOR == 10
    assert window.combo_fine_grid.currentData() == 10
    assert view.fine_step() == 2.0 and view.grid_step() == 20.0, "the main grid is unchanged"
    # 2 scene px per fine step: legible from scale 2.5 (5 px) up.
    _zoomed_out(window, FINE_GRID_MIN_SCREEN_PX / 2.0 + 0.5)
    assert view.fine_dots_visible()
    _zoomed_out(window, FINE_GRID_MIN_SCREEN_PX / 2.0 - 0.5)
    assert not view.fine_dots_visible()
    window.combo_fine_grid.setCurrentIndex(window.combo_fine_grid.findData(1))
    _zoomed_out(window, 40)
    assert not view.fine_dots_visible(), "divisor 1: the drawing grid IS the main grid"
    assert window._config["draw_grid_divisor"] == 1


def test_the_rewrite_after_an_edit_is_deferred_and_coalesced(window, qapp, monkeypatch):
    """The drop paints first; the .kicad_mod is rewritten on the next turn,
    once for a burst of edits."""
    generations = []
    monkeypatch.setattr(window, "_generate", lambda **kwargs: generations.append(kwargs) or True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): edits alone rewrite
    # nothing; the rewrite follows «Applica», still deferred and coalesced.
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    window._apply_footprint_drawings(validate_footprint_drawings([record(), record("circle", ((0, 0), (1, 0)))]))
    qapp.processEvents()
    assert generations == [], "a draft is not written"
    window.apply_footprint_draft()
    window._set_footprint_text_position("Reference", (0.0, -3.0))
    window.apply_footprint_draft()
    assert generations == [], "nothing rewritten before the event loop turns"
    qapp.processEvents()
    assert generations == [{"footprint_only": True}], "one rewrite for the burst"
    # Negative: no footprint yet, nothing to rewrite.
    monkeypatch.setattr(window, "_project_has_footprint", lambda: False)
    window._apply_footprint_drawings([])
    window.apply_footprint_draft()
    qapp.processEvents()
    assert generations == [{"footprint_only": True}]


def test_an_edit_keeps_the_camera_once_the_user_has_zoomed(preview):
    """User, 2026-10-13: «quando ridimensiono un oggetto (il cerchio) non
    mantiene lo zoom e lo cambia». Like the symbol editor's auto_fit: the
    preview follows the footprint until the user zooms or pans; after that a
    redraw (the drop, then the end of the .kicad_mod rewrite) keeps the
    camera, and «Adatta alla vista» or another project brings the fit back."""
    from PyQt6.QtCore import QPointF
    window, _saves, _generations = preview
    view = window.preview_view
    view.resize(800, 600)
    window.footprint_drawings = validate_footprint_drawings([record("circle", ((0, 0), (1, 0)))])
    window._draw_preview()
    fitted = view.transform().m11()
    # Negative: never zoomed, a bigger circle refits the view.
    assert view.auto_fit
    window._apply_footprint_drawings(validate_footprint_drawings([record("circle", ((0, 0), (30, 0)))]))
    assert view.transform().m11() != fitted, "auto-fit follows the footprint"
    # Positive: after a zoom, the resize keeps scale and centre.
    view.zoom(2.0)
    assert not view.auto_fit
    scale = view.transform().m11()
    centre = view.mapToScene(view.viewport().rect().center())
    window._on_footprint_handle_dropped(0, 1, 50 * 20, 0)          # rim moved: radius 50 mm
    assert window.footprint_drawings[0]["points"][1] == [50.0, 0.0]
    redraws = []
    real_draw = window._draw_preview
    window._cad_result_is_current = lambda: True
    window._draw_preview_3d = lambda: None
    window._draw_preview = lambda: redraws.append(1) or real_draw()
    window._on_generated("x.kicad_mod")                             # the rewrite ends: redraw again
    assert redraws, "the end of the rewrite really redraws the preview"
    assert view.transform().m11() == scale
    after = view.mapToScene(view.viewport().rect().center())
    assert abs(after.x() - centre.x()) < 1 and abs(after.y() - centre.y()) < 1
    # «Adatta alla vista»: fit again and follow the footprint again.
    window._fit_preview(force=True)
    assert view.auto_fit and view.transform().m11() != scale
    # A pan counts as placing the camera too.
    from PyQt6.QtCore import QEvent, Qt
    _send(view, QEvent.Type.MouseButtonPress, -5000, -5000, Qt.MouseButton.MiddleButton)
    _send(view, QEvent.Type.MouseMove, -5000, -5000, Qt.MouseButton.MiddleButton, offset=(30, 0))
    _send(view, QEvent.Type.MouseButtonRelease, -5000, -5000, Qt.MouseButton.MiddleButton, offset=(30, 0))
    assert not view.auto_fit


# ── KiCad layer colours and the layer menu (user, 2026-10-13) ─────────

def test_drawings_generated_silk_and_selector_use_the_kicad_default_colours(preview):
    """User: «a seconda del tipo dovrebbe avere un colore diverso», «i colori
    predefiniti di KiCad». Every layer its own colour, the same table for the
    user drawings, the generated silkscreen and the selector swatches."""
    from PyQt6.QtWidgets import QGraphicsLineItem, QGraphicsRectItem
    from kicadgen.gui.main_window import KICAD_LAYER_COLORS
    window, _saves, _generations = preview
    assert KICAD_LAYER_COLORS == {"F.SilkS": "#f2eda1", "F.Fab": "#afafaf", "Cmts.User": "#5994dc"}
    assert len(set(KICAD_LAYER_COLORS.values())) == 3, "one distinct colour per layer"
    window.footprint_drawings = validate_footprint_drawings([
        record("line", ((0, 0), (5, 0)), layer=layer) for layer in KICAD_LAYER_COLORS])
    window._draw_preview()
    by_index = {item.data(0x445257): item.pen().color().name() for item in _drawing_items(window)}
    assert by_index == {0: "#f2eda1", 1: "#afafaf", 2: "#5994dc"}
    # The generated silkscreen takes the same silkscreen colour.
    generated = [i for i in window.preview_scene.items()
                 if isinstance(i, (QGraphicsLineItem, QGraphicsRectItem)) and i.data(0x445257) is None
                 and i.pen().color().name() == "#f2eda1"]
    assert generated, "the generated silkscreen is drawn in the KiCad silkscreen colour"
    # Each selector entry shows its colour.
    combo = window.combo_fp_draw_layer
    for row in range(combo.count()):
        icon = combo.itemIcon(row)
        assert not icon.isNull()
        colour = icon.pixmap(12, 12).toImage().pixelColor(6, 6).name()
        assert colour == KICAD_LAYER_COLORS[combo.itemData(row)]


def test_the_ghost_of_a_shape_in_progress_takes_the_chosen_layer_colour(preview):
    from PyQt6.QtWidgets import QGraphicsPathItem
    window, _saves, _generations = preview
    window.chk_grid.setChecked(False)
    window.combo_fp_draw_layer.setCurrentIndex(window.combo_fp_draw_layer.findData("Cmts.User"))
    window._toggle_footprint_draw_tool("rectangle")
    window._on_footprint_draw_clicked(0, 0)
    window._on_footprint_draw_hovered(3 * 20, 2 * 20)
    ghosts = [i for i in window.preview_scene.items()
              if isinstance(i, QGraphicsPathItem) and i.data(0x44524D) is not None]
    assert ghosts and ghosts[0].pen().color().name() == "#5994dc"
    window._cancel_footprint_drawing()


def test_a_right_click_on_a_handle_opens_the_menu_and_changes_the_layer(preview, monkeypatch):
    """User: «aggiungere la possibilità di modificare il layer dal tasto
    destro». The handles cover the stroke (and a text's handle covers the
    text) while hovered: a right click on them must still open the menu.
    Negative: a right click on empty background opens no drawing menu."""
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtWidgets import QMenu
    window, saves, _generations = preview
    menus = []

    def layer_action(menu):
        for action in menu.actions():
            if action.menu() is not None:
                found = layer_action(action.menu())
                if found is not None:
                    return found
            elif action.text().startswith("Fabbricazione"):
                return action
        return None

    def fake_exec(menu, *_a):
        menus.append(menu)
        return layer_action(menu)

    # Harness changed with phase C (docs/canvas-menus-plan.md): the menu is
    # built by the shared `canvas_menus` module, so `QMenu.exec` (used by
    # both) is replaced instead of the window's `QMenu` name; same contract.
    monkeypatch.setattr(QMenu, "exec", fake_exec)
    window.chk_grid.setChecked(False)
    window.footprint_drawings = validate_footprint_drawings([
        record("text", ((10, 10),), text="NOTE")])
    window._draw_preview()
    _zoomed_out(window, 1.0)
    view = window.preview_view
    _send(view, QEvent.Type.MouseMove, 10 * 20, 10 * 20)
    assert len(_handles(window)) == 1, "hovered: the text shows its handle over itself"
    saves.clear()
    _send(view, QEvent.Type.MouseButtonPress, 10 * 20, 10 * 20, Qt.MouseButton.RightButton)
    assert menus, "the right click on the handle opened the drawing menu"
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): saved on «Applica».
    assert window.footprint_drawings[0]["layer"] == "F.Fab" and saves == []
    window.apply_footprint_draft()
    assert saves == [True]
    menus.clear()
    _send(view, QEvent.Type.MouseButtonPress, -5000, -5000, Qt.MouseButton.RightButton)
    assert menus == [], "no drawing there: no drawing menu"
    view.camera.pan_cancel()


def test_a_drawing_moves_by_its_body_without_a_component_body(preview):
    """Positive without a documented body (no invented outline, audit item 4):
    the drawing still moves by its body. Drawn clear of the pad-matrix
    dimension labels, which lie on top of drawings by design."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    window, saves, generations = preview
    assert not window._body_size_declared
    view = window.preview_view
    view.resize(800, 600)
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    view.set_grid(20.0, (0.0, 0.0), True)
    _main_grid_snap(window)
    window.footprint_drawings = validate_footprint_drawings([
        record("rectangle", ((10, 8), (14, 10)))])
    window._draw_preview()

    def send(kind, scene_x, scene_y, button=Qt.MouseButton.NoButton):
        pos = view.mapFromScene(QPointF(scene_x, scene_y))
        event = QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                            button, button, Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.MouseButtonPress: view.mousePressEvent,
         QEvent.Type.MouseMove: view.mouseMoveEvent,
         QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)

    send(QEvent.Type.MouseMove, 14 * 20, 9 * 20)
    send(QEvent.Type.MouseButtonPress, 14 * 20, 9 * 20, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseMove, 15 * 20, 9 * 20, Qt.MouseButton.LeftButton)
    assert window._fp_handle_drag == (0, -1, (1.0, 0.0))
    send(QEvent.Type.MouseButtonRelease, 16.2 * 20, 10.1 * 20, Qt.MouseButton.LeftButton)
    assert window.footprint_drawings[0]["points"] == [[12.0, 9.0], [16.0, 11.0]]
