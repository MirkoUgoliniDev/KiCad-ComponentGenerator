"""@brief Minimal drawing palette of the footprint preview (user, 2026-10-13).

Free drawings (line, rectangle, circle, arc, text) on a chosen layer
(F.SilkS printed, F.Fab documentation, Cmts.User comments) live beside the
generated footprint, like the pad overrides: saved in the project under
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
    window._draw_preview()
    return window, saves, generations


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
    window._on_footprint_draw_clicked(1.2 * 20, 1.2 * 20)
    window._on_footprint_draw_clicked(4.9 * 20, 2.8 * 20)
    assert window._fp_draw_tool is None, "the tool disarms itself when the shape closes"
    assert window.footprint_drawings == [{"kind": "rectangle", "layer": "F.SilkS",
                                          "width": 0.12, "points": [[1.0, 1.0], [5.0, 3.0]]}]
    assert saves == [True] and generations == [{"footprint_only": True}]
    assert _drawing_items(window), "the preview draws the stored rectangle"
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

    class FakeAction:
        def __init__(self, label):
            self.label = label

        def setCheckable(self, _value):
            pass

        def setChecked(self, _value):
            pass

    class FakeMenu:
        def __init__(self, *_a, **_k):
            self.actions = []

        def addAction(self, label):
            self.actions.append(FakeAction(label))
            return self.actions[-1]

        def addMenu(self, _label):
            return FakeMenu()

        def addSeparator(self):
            pass

        def exec(self, *_a):
            return next(a for a in self.actions if "Elimina" in a.label)

    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(gui, "QMenu", FakeMenu)
    window._footprint_drawing_context_menu(0)
    assert window.footprint_drawings == []
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
    window._toggle_footprint_draw_tool("circle")
    window._on_footprint_draw_clicked(0, 0)
    window._on_footprint_draw_clicked(0.9 * 20, 0.9 * 20)  # same 2.54 node
    assert len(shown) == 1 and window._fp_draw_tool == "circle", \
        "the click is refused with a message, the tool stays armed"
    window._cancel_footprint_drawing()
    # Degenerate at finish time (the line tool allows repeated clicks).
    window._toggle_footprint_draw_tool("rectangle")
    window._fp_draw_clicks = [(0.0, 0.0), (0.0, 5.0)]  # flat rectangle
    window._on_footprint_draw_finish()
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
    window._toggle_footprint_draw_tool("rectangle")
    window._on_footprint_draw_clicked(0.9 * 20, -0.1 * 20)
    window._on_footprint_draw_clicked((0.7 + 2.54 * 2 + 0.4) * 20, (-0.3 + 2.54 - 0.5) * 20)
    points = window.footprint_drawings[0]["points"]
    assert points == [[0.7, -0.3], [pytest.approx(5.78), pytest.approx(2.24)]]
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
    assert len(_handles(window)) == 3, "one handle per stored point, plus the centre anchor"
    send(QEvent.Type.MouseMove, 10 * 20, 10 * 20)        # away from it
    assert _handles(window) == []
    send(QEvent.Type.MouseMove, 4 * 20, 1.5 * 20)
    # Drag the second corner from (4, 3) to the node nearest (6.3, 4.8).
    send(QEvent.Type.MouseButtonPress, 4 * 20, 3 * 20, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseMove, 5 * 20, 4 * 20, Qt.MouseButton.LeftButton)
    assert window._fp_handle_drag == (0, 1, (5.0, 4.0))
    send(QEvent.Type.MouseButtonRelease, 6.3 * 20, 4.8 * 20, Qt.MouseButton.LeftButton)
    assert window.footprint_drawings[0]["points"] == [[0, 0], [6.0, 5.0]]
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


def test_the_anchor_or_the_body_moves_the_whole_drawing(preview):
    """User, 2026-10-13: «se seleziono il punto di ancoraggio deve essere
    possibile muovere l'elemento», like an element of the symbol editor."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QGraphicsEllipseItem
    window, saves, generations = preview
    view = window.preview_view
    view.resize(800, 600)
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    view.set_grid(20.0, (0.0, 0.0), True)
    window.footprint_drawings = validate_footprint_drawings([
        record("rectangle", ((0, 0), (4, 2)))])
    window._draw_preview()
    saves.clear(), generations.clear()

    def send(kind, scene_x, scene_y, button=Qt.MouseButton.NoButton):
        pos = view.mapFromScene(QPointF(scene_x, scene_y))
        event = QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                            button, button, Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.MouseButtonPress: view.mousePressEvent,
         QEvent.Type.MouseMove: view.mouseMoveEvent,
         QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)

    send(QEvent.Type.MouseMove, 4 * 20, 1 * 20)           # hover the right edge
    from kicadgen.gui.footprint_view import HANDLE_KEY_ROLE
    anchors = [h for h in _handles(window) if str(h.data(HANDLE_KEY_ROLE)).endswith(":-1")]
    vertices = [h for h in _handles(window) if not str(h.data(HANDLE_KEY_ROLE)).endswith(":-1")]
    assert len(anchors) == 1 and anchors[0].pos() == QPointF(2 * 20, 1 * 20), \
        "one anchor at the centre of the drawing"
    # Same style as the symbol editor's handles: yellow outline, green fill
    # (translucent on a vertex, solid on the anchor), constant screen size.
    from PyQt6.QtWidgets import QGraphicsItem
    for handle in anchors + vertices:
        assert handle.pen().color().name() == "#f9e2af"
        assert handle.brush().color().name() == "#a6e3a1"
        assert handle.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
    assert anchors[0].brush().color().alpha() == 255
    assert all(v.brush().color().alpha() == 150 for v in vertices)
    # Grab the anchor and drop it two nodes right, one down: everything moves.
    send(QEvent.Type.MouseButtonPress, 2 * 20, 1 * 20, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseMove, 3 * 20, 1 * 20, Qt.MouseButton.LeftButton)
    assert window._fp_handle_drag == (0, -1, (1.0, 0.0))
    send(QEvent.Type.MouseButtonRelease, 4.2 * 20, 2.1 * 20, Qt.MouseButton.LeftButton)
    assert window.footprint_drawings[0]["points"] == [[2.0, 1.0], [6.0, 3.0]]
    assert saves == [True] and generations == [{"footprint_only": True}]
    # Grabbing the body (the drawn edge) moves it too; a drop on the grab node changes nothing.
    send(QEvent.Type.MouseButtonPress, 6 * 20, 2 * 20, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseButtonRelease, 6 * 20, 2 * 20, Qt.MouseButton.LeftButton)
    assert window.footprint_drawings[0]["points"] == [[2.0, 1.0], [6.0, 3.0]] and len(saves) == 1
    send(QEvent.Type.MouseButtonPress, 6 * 20, 2 * 20, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseButtonRelease, 5 * 20, 2 * 20, Qt.MouseButton.LeftButton)
    assert window.footprint_drawings[0]["points"] == [[1.0, 1.0], [5.0, 3.0]]
    # A text has one point only: no extra anchor.
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
    window._fp_draw_clicks = [(1.0, 1.0)]
    window.footprint_drawings = window.footprint_drawings  # unchanged
    window._apply_footprint_drawings(window.footprint_drawings + validate_footprint_drawings(
        [record("text", ((1, 1),), text="X")]))
    assert not enabled(window.btn_fp_redo)
    assert len(saves) == 6, "every step, undo and redo included, is saved"


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
    assert window.footprint_drawings[0]["layer"] == "F.Fab" and len(saves) == 2
    state = deepcopy(window._project_state())
    window._apply_project_state(state)
    assert not window.btn_fp_undo.isEnabledTo(window.btn_fp_undo.parentWidget()),         "a project load starts a fresh history"
