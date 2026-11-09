"""@brief Footprint preview: unfilled drawings hit on their outline (H1) and one closing handle (H2), real mouse events.

User decisions of 2026-11-09 («Sì, come KiCad», «Sì, una maniglia sola»):
(H1) an unfilled rectangle, polygon, circle and an open line, arc or curve
take a click only on their stroke, as KiCad's `EDA_SHAPE::hitTest`
(common/eda_shape.cpp); a filled one also inside. What lies inside an empty
outline (a pad, the background) gets the press. (H2) a line closed on its
first point shows one handle there, and dragging it moves both ends. The
symbol editor's twin is tests/test_symbol_outline_hit.py.
"""
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

from kicadgen.core.footprint import validate_footprint_drawings  # noqa: E402

MM = 20.0
#: Four pads at (±3, ±2) mm (tests/test_footprint_pad_drag.py), body 15 x 10 mm.
PAD = (-3.0, -2.0)
#: Inside every shape below, off the pads, off the body outline and its dimensions.
INSIDE = (0.0, 0.5)


def _record(kind, points, **extra):
    base = {"kind": kind, "layer": "F.SilkS", "width": 0.12, "points": [list(p) for p in points]}
    base.update(extra)
    return base


SHAPES = {
    "rectangle": (_record("rectangle", ((-5, -4), (5, 4))), (0.0, -4.0)),
    "polygon": (_record("polygon", ((-5, -4), (5, -4), (5, 4), (-5, 4))), (0.0, -4.0)),
    "circle": (_record("circle", ((0, 0), (5, 0))), (5.0, 0.0)),
}


def _shown(window, qapp, monkeypatch, records):
    from test_footprint_pad_drag import PINS, _show
    from kicadgen.core.package_geometry import PackageGeometry
    _show(window, qapp, monkeypatch, PackageGeometry(family="soic", body_length_mm=15.0, body_width_mm=10.0,
                                                     pins=list(PINS)))
    window.footprint_drawings = validate_footprint_drawings(records)
    window._draw_preview()
    qapp.processEvents()
    return window


def _pos(window, x_mm, y_mm):
    return window.preview_view.mapFromScene(QPointF(x_mm * MM, y_mm * MM))


def pointer_on_drawing(window, qapp, index):
    """Move the REAL pointer onto a viewport pixel where a plain left press takes drawing `index`.

    Scans the drawing's on-screen box for a pixel whose press target is the
    drawing (its stroke, a handle, or the inside of a filled shape), never an
    assumed coordinate, then sends real mouse moves there. Shared by the tests
    whose keyboard commands act on "the drawing under the pointer"
    (`MainWindow._fp_targets`, `FootprintView.drawing_under_pointer`).
    Returns the viewport point.
    """
    from PyQt6.QtCore import QPoint, QRectF
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    view = window.preview_view
    items = [item for item in window.preview_scene.items() if item.data(DRAWING_KEY_ROLE) == index]
    assert items, f"drawing {index} is drawn"
    box = QRectF(items[0].sceneBoundingRect())
    for item in items[1:]:
        box = box.united(item.sceneBoundingRect())
    area = view.mapFromScene(box).boundingRect().intersected(view.viewport().rect())
    for y in range(area.top(), area.bottom() + 1):
        for x in range(area.left(), area.right() + 1):
            pos = QPoint(x, y)
            target = view.press_target(pos)
            if target is None or target[0] not in ("drawing", "handle") or target[1] != index:
                continue
            # A button-less QTest.mouseMove only places the platform cursor, whose event can
            # arrive late or not at all on a window that is not the active one: the move
            # events are sent to the viewport directly (the path Qt's own delivery takes).
            for point in (pos + QPoint(0, 40), pos):
                _send_move(view, point)
            qapp.processEvents()
            if view.drawing_under_pointer() == index:
                return pos
    raise AssertionError(f"no pixel where a press takes drawing {index}")


def _send_move(view, pos):
    """Deliver a button-less mouse move at viewport point `pos` to the preview's viewport."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), event)


def pointer_away(window, qapp):
    """The pointer leaves the preview (Qt's leave event): no drawing is under it."""
    from PyQt6.QtCore import QEvent
    window.preview_view.leaveEvent(QEvent(QEvent.Type.Leave))
    qapp.processEvents()
    assert window.preview_view.drawing_under_pointer() == -1


def _click(window, qapp, x_mm, y_mm):
    view, pos = window.preview_view, _pos(window, x_mm, y_mm)
    QTest.mouseMove(view.viewport(), pos)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    qapp.processEvents()


def _drag(window, qapp, start_mm, end_mm):
    view = window.preview_view
    start, end = _pos(window, *start_mm), _pos(window, *end_mm)
    QTest.mouseMove(view.viewport(), start)
    qapp.processEvents()
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view.viewport(), (start + end) / 2)
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    qapp.processEvents()


# ── H1: unfilled outlines are hit on their stroke ──────────────────────

@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_a_click_inside_an_unfilled_shape_reaches_the_background_and_its_outline_selects_it(
        window, qapp, monkeypatch, kind):
    record, outline = SHAPES[kind]
    _shown(window, qapp, monkeypatch, [record])
    _click(window, qapp, *outline)
    assert window._fp_selected_indices() == [0], "positive: the outline selects the drawing"
    _click(window, qapp, *INSIDE)
    assert window._fp_selected_indices() == [], "negative: inside an empty outline is the background"


@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_a_click_on_a_pad_inside_an_unfilled_shape_selects_the_pad(window, qapp, monkeypatch, kind):
    _shown(window, qapp, monkeypatch, [SHAPES[kind][0]])
    _click(window, qapp, *PAD)
    assert window._fp_selected_pads() == [0] and window._fp_selected_indices() == []


@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_a_filled_shape_is_selected_inside(window, qapp, monkeypatch, kind):
    record = dict(SHAPES[kind][0], fill=True)
    _shown(window, qapp, monkeypatch, [record])
    assert window.footprint_drawings[0].get("fill") is True
    _click(window, qapp, *INSIDE)
    assert window._fp_selected_indices() == [0], "a filled shape answers inside, as KiCad's filled shapes"


def test_a_right_click_inside_an_unfilled_rectangle_opens_the_pad_or_background_menu(window, qapp, monkeypatch):
    from test_footprint_back_anchor_zone_gui import _right_press
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    assert _right_press(window, *INSIDE, monkeypatch) == [("background", None)]
    heard = _right_press(window, *PAD, monkeypatch)
    assert heard and heard[0][0] == "pad", heard
    assert _right_press(window, 0.0, -4.0, monkeypatch)[:1] == [("drawing", 0)], "the outline keeps its menu"


def test_hover_inside_an_unfilled_shape_reports_no_drawing(window, qapp, monkeypatch):
    """The hover (handles, cursor, tooltip) follows the same hit: nothing inside, the drawing on its stroke."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    assert view._drawing_at(_pos(window, *INSIDE)) == -1
    assert view._drawing_at(_pos(window, 0.0, -4.0)) == 0
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    under = [item for item in view.items(_pos(window, *INSIDE)) if item.data(DRAWING_KEY_ROLE) is not None]
    assert under == [], "the item's Qt shape (cursor, tooltip) holds no inside"


#: Open drawings: (record, a point inside the area their chord closes, a point on the stroke), mm.
OPEN_SHAPES = {
    "l_line": (_record("line", ((-6, -6), (6, -6), (6, 6))), (3.0, -3.5), (0.0, -6.0)),
    "arc": (_record("arc", ((-6, 3), (0, -3), (6, 3))), (0.0, 1.0), (0.0, -3.0)),
    "curve": (_record("curve", ((-6, -4), (-6, 6), (6, 6), (6, -4))), (0.0, 0.0), (0.0, 3.5)),
}


@pytest.mark.parametrize("kind", sorted(OPEN_SHAPES))
def test_an_open_drawing_is_not_hit_across_its_chord(window, qapp, monkeypatch, kind):
    """Negative: the area closed by the chord of an «L», an arc or a curve is not the drawing; positive: its stroke is."""
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    record, chord_side, stroke = OPEN_SHAPES[kind]
    _shown(window, qapp, monkeypatch, [record])
    view = window.preview_view
    assert view._drawing_at(_pos(window, *chord_side)) == -1
    assert all(item.data(DRAWING_KEY_ROLE) is None for item in view.items(_pos(window, *chord_side)))
    assert view._drawing_at(_pos(window, *stroke)) == 0


def test_a_textbox_is_still_hit_inside_as_kicad_s(window, qapp, monkeypatch):
    """PCB_TEXTBOX::HitTest is its bounding box: a text box stays clickable inside (not an outline shape)."""
    _shown(window, qapp, monkeypatch, [_record("textbox", ((-5, -4), (5, 4)), text="AB", size=1.0)])
    assert window.footprint_drawings and window.footprint_drawings[0]["kind"] == "textbox"
    assert window.preview_view._drawing_at(_pos(window, *INSIDE)) == 0


# ── H2: one handle at the closing point of a closed line ───────────────

CLOSED_LINE = ((-6, -1), (-1, -1), (-1, 1), (-6, -1))
OPEN_LINE = ((-6, -1), (-1, -1), (-1, 1), (-6, 1))


def _handles(window, index):
    from kicadgen.gui.footprint_view import HANDLE_KEY_ROLE
    window._fp_hovered_drawing = index
    window._refresh_drawing_handles()
    keys = [str(item.data(HANDLE_KEY_ROLE)) for item in window.preview_scene.items()
            if item.data(HANDLE_KEY_ROLE) is not None]
    return sorted(keys)


def test_a_closed_line_shows_one_handle_at_its_closing_point(window, qapp, monkeypatch):
    _shown(window, qapp, monkeypatch, [_record("line", CLOSED_LINE), _record("line", OPEN_LINE)])
    assert _handles(window, 0) == ["0:0", "0:1", "0:2"], "three corners, one handle each"
    assert _handles(window, 1) == ["1:0", "1:1", "1:2", "1:3"], "negative: an open line keeps both ends"


def test_dragging_the_closing_handle_moves_both_ends_as_one_undo_step(window, qapp, monkeypatch):
    _shown(window, qapp, monkeypatch, [_record("line", CLOSED_LINE)])
    window._fp_hovered_drawing = 0
    window._refresh_drawing_handles()
    steps = len(window._fp_drawings_undo)
    before = deepcopy(window.footprint_drawings)
    _drag(window, qapp, (-6.0, -1.0), (-7.0, -2.0))
    points = window.footprint_drawings[0]["points"]
    assert points[0] == [-7.0, -2.0] and points[-1] == [-7.0, -2.0], points
    assert points[1:3] == before[0]["points"][1:3], "the other corners stay"
    assert _handles(window, 0) == ["0:0", "0:1", "0:2"], "still closed: still one handle"
    assert len(window._fp_drawings_undo) == steps + 1, "one drag, one undo step"
    window._undo_footprint_drawing()
    assert window.footprint_drawings == before, "one undo restores both ends"


def test_dragging_an_end_of_an_open_line_moves_that_end_alone(window, qapp, monkeypatch):
    _shown(window, qapp, monkeypatch, [_record("line", OPEN_LINE)])
    window._fp_hovered_drawing = 0
    window._refresh_drawing_handles()
    _drag(window, qapp, (-6.0, -1.0), (-7.0, -2.0))
    points = window.footprint_drawings[0]["points"]
    assert points[0] == [-7.0, -2.0] and points[-1] == [-6.0, 1.0], points


def test_a_closed_polygon_shows_a_handle_per_corner_and_its_drag_keeps_it_closed(window, qapp, monkeypatch):
    """Twin of the symbol's polygon: a footprint polygon stores no closing repeat (closed by its
    kind), so each corner has one handle and dragging the first corner moves it alone; the
    closing side follows because the polygon is closed by definition."""
    corners = ((-6, -1), (-1, -1), (-1, 1), (-6, 1))
    _shown(window, qapp, monkeypatch, [_record("polygon", corners)])
    assert _handles(window, 0) == ["0:0", "0:1", "0:2", "0:3"], "one handle per corner, no repeat"
    window._fp_hovered_drawing = 0
    window._refresh_drawing_handles()
    _drag(window, qapp, (-6.0, -1.0), (-7.0, -2.0))
    points = window.footprint_drawings[0]["points"]
    assert points[0] == [-7.0, -2.0] and points[1:] == [list(p) for p in corners[1:]], points
    assert _handles(window, 0) == ["0:0", "0:1", "0:2", "0:3"]


# ── Ctrl+click and double click inside, as the symbol editor ───────────

def test_a_ctrl_click_inside_an_unfilled_rectangle_toggles_nothing(window, qapp, monkeypatch):
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    for point, expected in ((INSIDE, []), (SHAPES["rectangle"][1], [0])):
        pos = _pos(window, *point)
        QTest.mouseMove(view.viewport(), pos)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, pos)
        qapp.processEvents()
        assert window._fp_selected_indices() == expected, point


def test_a_double_click_inside_an_unfilled_rectangle_reaches_the_dimension_beneath(window, qapp, monkeypatch):
    """The double click edits an editable dimension only when no drawing is under the pointer:
    inside an empty rectangle drawn around a body dimension it still edits it (positive); a
    FILLED rectangle there takes the double click (negative)."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent
    from kicadgen.gui.canvas_measure import DIMENSION_EDIT_ROLE
    for fill, expected in ((False, True), (True, False)):
        _shown(window, qapp, monkeypatch, [])
        view = window.preview_view
        labels = [item for item in window.preview_scene.items() if item.data(DIMENSION_EDIT_ROLE)]
        assert labels, "the package has an editable body dimension"
        centre = labels[0].sceneBoundingRect().center()
        cx, cy = centre.x() / MM, centre.y() / MM
        record = _record("rectangle", ((cx - 1.5, cy - 1.0), (cx + 1.5, cy + 1.0)), **({"fill": True} if fill else {}))
        window.footprint_drawings = validate_footprint_drawings([record])
        window._draw_preview()
        qapp.processEvents()
        asked = []
        # The window's slot opens the modal dimension dialog: only the view's choice is under test.
        view.dimension_edit_requested.disconnect()
        view.dimension_edit_requested.connect(asked.append)
        pos = view.mapFromScene(centre)
        event = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        view.mouseDoubleClickEvent(event)
        assert bool(asked) is expected, (fill, asked)


# ── Hover feedback follows the press reach (coordinator decision) ──────

def test_the_hover_cursor_and_tooltip_follow_the_press_reach(window, qapp, monkeypatch):
    """3 px off a thin stroke the press takes it, so the move cursor and the tooltip show there;
    beyond the margin the arrow comes back and no tooltip is offered (real mouse moves)."""
    from PyQt6.QtCore import QPoint
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, HIT_MARGIN_PX
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    viewport = view.viewport()
    edge = _pos(window, -5.0, 0.5)
    exact = [item for item in view.items(edge) if item.data(DRAWING_KEY_ROLE) == 0]
    assert exact, "the left side is under the edge point"
    half = 0
    while [item for item in view.items(edge - QPoint(half + 1, 0)) if item.data(DRAWING_KEY_ROLE) == 0]:
        half += 1                       # the stroke is a few pixels wide: walk to its outer pixel
    outer = edge - QPoint(half, 0)
    near = outer - QPoint(HIT_MARGIN_PX - 1, 0)
    far = outer - QPoint(HIT_MARGIN_PX + 4, 0)
    hint = exact[0].toolTip()
    assert hint
    assert not [item for item in view.items(near) if item.data(DRAWING_KEY_ROLE) == 0], "not exactly under"
    for _ in range(2):
        QTest.mouseMove(viewport, near)
        qapp.processEvents()
        assert view.press_target(near) == ("drawing", 0)
        assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, "the press reach shows the move cursor"
        assert view.reach_tooltip(near) == hint
        QTest.mouseMove(viewport, edge)
        qapp.processEvents()
        assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, "exactly on the stroke"
        QTest.mouseMove(viewport, far)
        qapp.processEvents()
        assert view.press_target(far) is None
        assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor, "negative: beyond the margin, the arrow"
        assert view.reach_tooltip(far) == "", "negative: no tooltip beyond the margin"


def test_an_armed_tool_shows_the_cross_over_a_drawing_stroke(window, qapp, monkeypatch):
    """Plan C10 in both tabs: with a click tool armed the stroke under the pointer keeps the cross;
    a middle-button pan still shows the closed hand, then the cross comes back."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    viewport = view.viewport()
    edge = _pos(window, -5.0, 0.5)
    window._arm_footprint_tool("line")
    assert view.click_collector.armed
    QTest.mouseMove(viewport, edge - type(edge)(20, 0))
    QTest.mouseMove(viewport, edge)
    qapp.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.CrossCursor
    QTest.mousePress(viewport, Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier, edge)
    QTest.mouseMove(viewport, edge + type(edge)(5, 5))
    qapp.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor, "negative: the pan keeps its hand"
    QTest.mouseRelease(viewport, Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier, edge + type(edge)(5, 5))
    qapp.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.CrossCursor, "the pan over, the cross is back"
    view.click_router.disarm()


# ── One precedence for both buttons (coordinator decision) ─────────────

def _kinds_at(view, pos):
    from kicadgen.gui.canvas_hit import items_near
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, HIT_MARGIN_PX, PAD_KEY_ROLE

    def kinds(items):
        return ({"pad" for item in items if str(item.data(PAD_KEY_ROLE) or "").startswith("package:0:")}
                | {"drawing" for item in items if item.data(DRAWING_KEY_ROLE) == 0})
    exact, near = kinds(view.items(pos)), kinds(items_near(view, pos, HIT_MARGIN_PX))
    return ("pad" in exact, "pad" in near, "drawing" in exact, "drawing" in near)


def _line_left_of_pad(window, qapp, monkeypatch, gap):
    """Pad 1 (index 0) and a vertical drawn line to its left, moved until `gap` pixels separate
    the pad's last exactly-hit pixel from the line's first, along the pad's centre row.
    Returns (pixels from the pad centre leftwards, last pad pixel, first line pixel)."""
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, PAD_KEY_ROLE
    _shown(window, qapp, monkeypatch, [])
    view = window.preview_view
    for tenth in range(0, 120):
        x_mm = -3.6 - tenth / (10 * MM)        # tenths of a scene pixel left of the pad's copper edge
        window.footprint_drawings = validate_footprint_drawings([_record("line", ((x_mm, -3.0), (x_mm, -1.0)))])
        window._draw_preview()
        qapp.processEvents()
        centre = _pos(window, -3.0, -2.0)
        pixels = [centre - type(centre)(i, 0) for i in range(60)]
        on_pad = [i for i, p in enumerate(pixels)
                  if any(str(item.data(PAD_KEY_ROLE) or "").startswith("package:0:") for item in view.items(p))]
        on_line = [i for i, p in enumerate(pixels) if any(item.data(DRAWING_KEY_ROLE) == 0 for item in view.items(p))]
        if on_pad and on_line and min(on_line) - max(on_pad) == gap:
            return pixels, max(on_pad), min(on_line)
    raise AssertionError(f"no line position gives a gap of {gap} px")


def test_a_pad_two_pixels_away_beats_a_stroke_three_pixels_away_for_both_buttons(window, qapp, monkeypatch):
    """Inside the margin pass the rank decides (pads above strokes), for the left press and the
    right click alike; twin of the symbol's pin test. CONTRACT CHANGED (2026-11-09): the right
    click finds a pad within the margin too (it was exact only)."""
    from test_footprint_back_anchor_zone_gui import _right_press
    pixels, pad_end, _line_start = _line_left_of_pad(window, qapp, monkeypatch, 5)
    view = window.preview_view
    pos = pixels[pad_end + 2]
    assert _kinds_at(view, pos) == (False, True, False, True), "both within the margin only"
    assert view.press_target(pos)[:2] == ("pad", 0)
    assert view.menu_target(pos)[:2] == ("pad", 0)
    scene = view.mapToScene(pos)
    heard = _right_press(window, scene.x() / MM, scene.y() / MM, monkeypatch)
    assert heard and heard[0][0] == "pad", heard
    QTest.mouseMove(view.viewport(), pos)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    qapp.processEvents()
    assert window._fp_selected_pads() == [0] and window._fp_selected_indices() == [], "the left press takes the pad"


def test_a_stroke_exactly_under_the_pointer_beats_a_pad_within_the_margin(window, qapp, monkeypatch):
    """Negative twin: the exact pass comes before the margin pass, whatever the rank."""
    from test_footprint_back_anchor_zone_gui import _right_press
    pixels, _pad_end, line_start = _line_left_of_pad(window, qapp, monkeypatch, 3)
    view = window.preview_view
    pos = pixels[line_start]
    assert _kinds_at(view, pos) == (False, True, True, True), "stroke exact, pad within the margin"
    assert view.press_target(pos) == ("drawing", 0)
    assert view.menu_target(pos) == ("drawing", 0)
    scene = view.mapToScene(pos)
    heard = _right_press(window, scene.x() / MM, scene.y() / MM, monkeypatch)
    assert heard[:1] == [("drawing", 0)], heard


# ── Texts above pads (coordinator decision after verification D2) ──────

#: The Value anchor of verification D2: inside the 5 x 5 mm pad drawn at the origin.
VALUE_ANCHOR = (-1.5, 0.5)
#: On that pad, 1.5 mm below the Value's baseline: far from the text's glyphs and margin.
PAD_ONLY = (1.5, 2.0)


def _big_pad(window, qapp, monkeypatch, drawings=()):
    """A 5 x 5 mm package pad at the origin (and a small one at x = 8 mm), the Value anchored over it."""
    from test_footprint_pad_drag import _show
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    _show(window, qapp, monkeypatch, PackageGeometry(
        family="soic", body_length_mm=20.0, body_width_mm=10.0,
        pins=[PinSpec("1", 0.0, 0.0, 5.0, 5.0), PinSpec("2", 8.0, 0.0, 1.0, 1.0)]))
    window.footprint_text_positions = {"Value": VALUE_ANCHOR}
    window.footprint_drawings = validate_footprint_drawings(list(drawings))
    window._draw_preview()
    qapp.processEvents()
    return window


def _on_value_glyphs(window):
    """A viewport pixel exactly under both a Value glyph item and pad 1's copper (measured, never assumed)."""
    from itertools import product
    from kicadgen.gui.footprint_view import PAD_KEY_ROLE, TEXT_ANCHOR_ROLE, TEXT_KEY_ROLE
    view = window.preview_view
    group = window._footprint_text_group("Value")
    glyphs = [child for child in group.childItems() if child.data(TEXT_ANCHOR_ROLE) is None]
    area = view.mapFromScene(glyphs[0].sceneBoundingRect()).boundingRect()
    for x, y in product(range(area.left(), area.right() + 1), range(area.top(), area.bottom() + 1)):
        pos = type(area.topLeft())(x, y)
        items = view.items(pos)
        text = any(item.data(TEXT_KEY_ROLE) == "Value" and item.data(TEXT_ANCHOR_ROLE) is None for item in items)
        pad = any(str(item.data(PAD_KEY_ROLE) or "").startswith("package:0:") for item in items)
        if text and pad:
            return pos
    raise AssertionError("no pixel lies under both the Value glyphs and the pad")


def test_a_text_over_a_pad_is_dragged_and_reset_from_the_pad(window, qapp, monkeypatch):
    """Verification D2 reproduced: with the Value anchored at (-1.5, 0.5) over a 5 x 5 mm pad the
    pad won every button, so the text could be neither dragged nor reset. Precedence handles >
    texts > pads > strokes (coordinator decision 2026-11-09): on the glyphs the left press drags
    the TEXT and the right click opens its menu, whose «Posizione predefinita» puts it back;
    elsewhere on the pad the pad keeps the press (negative). CONTRACT CHANGED: pads ranked above texts."""
    from PyQt6.QtWidgets import QMenu
    from kicadgen.gui.footprint_view import PAD_KEY_ROLE
    _big_pad(window, qapp, monkeypatch)
    view = window.preview_view
    glyph = _on_value_glyphs(window)
    assert view.press_target(glyph) == ("text", "Value")
    assert view.menu_target(glyph) == ("text", "Value")
    assert view.reach_tooltip(glyph) == window._footprint_text_group("Value").childItems()[0].toolTip()
    # The left press drags the text, never the pad.
    pins_before = [(p.x_mm, p.y_mm) for p in window.package_geometry.pins]
    end = glyph + type(glyph)(80, 40)
    QTest.mouseMove(view.viewport(), glyph)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, glyph)
    QTest.mouseMove(view.viewport(), (glyph + end) / 2)
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    qapp.processEvents()
    moved = window.footprint_text_positions["Value"]
    assert moved != pytest.approx(VALUE_ANCHOR), "the text moved"
    assert [(p.x_mm, p.y_mm) for p in window.package_geometry.pins] == pins_before, "the pad stayed"
    # Back over the pad, the right click's menu resets it.
    window.footprint_text_positions = {"Value": VALUE_ANCHOR}
    window._draw_preview()
    qapp.processEvents()
    glyph = _on_value_glyphs(window)
    offered = []

    def choose_reset(menu, *_args, **_kwargs):
        offered.extend(action.text() for action in menu.actions())
        return menu.actions()[0]
    monkeypatch.setattr(QMenu, "exec", choose_reset)
    QTest.mouseMove(view.viewport(), glyph)
    QTest.mousePress(view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, glyph)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, glyph)
    qapp.processEvents()
    assert offered == ["Posizione predefinita"], offered
    assert "Value" not in window.footprint_text_positions, "the text is back at its default position"
    # Negative: on the pad away from the text the pad keeps the press and the menu.
    window.footprint_text_positions = {"Value": VALUE_ANCHOR}
    window._draw_preview()
    qapp.processEvents()
    pad_only = _pos(window, *PAD_ONLY)
    assert any(str(item.data(PAD_KEY_ROLE) or "").startswith("package:0:") for item in view.items(pad_only))
    assert view.press_target(pad_only)[:2] == ("pad", 0)
    assert view.menu_target(pad_only)[:2] == ("pad", 0)


def test_a_ctrl_click_on_a_text_over_a_pad_toggles_the_pad(window, qapp, monkeypatch):
    """Texts are not part of the selection: the Ctrl+click toggle leaves them out and finds the pad."""
    _big_pad(window, qapp, monkeypatch)
    view = window.preview_view
    glyph = _on_value_glyphs(window)
    QTest.mouseMove(view.viewport(), glyph)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, glyph)
    qapp.processEvents()
    assert window._fp_selected_pads() == [0]


def test_a_pad_without_a_text_over_it_beats_a_stroke_exactly_under_the_pointer(window, qapp, monkeypatch):
    """Negative twin of the text rule: a pad keeps its rank over a drawn line crossing it."""
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, PAD_KEY_ROLE
    _big_pad(window, qapp, monkeypatch, [_record("line", ((-4.0, 2.0), (4.0, 2.0)))])
    view = window.preview_view
    pos = _pos(window, *PAD_ONLY)
    items = view.items(pos)
    assert any(item.data(DRAWING_KEY_ROLE) == 0 for item in items)
    assert any(str(item.data(PAD_KEY_ROLE) or "").startswith("package:0:") for item in items)
    assert view.press_target(pos)[:2] == ("pad", 0)
    assert view.menu_target(pos)[:2] == ("pad", 0)


# ── Hover of a pad whose drag is refused (verification D5) ─────────────

def test_the_hover_of_an_unmovable_pad_shows_what_the_press_takes(window, qapp, monkeypatch):
    """Verification D5: with pads that cannot move (a part on a pin header) the press skips the
    pad and takes the drawn line crossing it, but the hover showed the pad's tooltip. The hover
    now follows the press's ladder (`gesture_accepts`): the line's tooltip and move cursor.
    Negative: movable pads keep the pad's tooltip, and the press takes the pad."""
    _big_pad(window, qapp, monkeypatch, [_record("line", ((-4.0, 2.0), (4.0, 2.0)))])
    view = window.preview_view
    viewport = view.viewport()
    pos = _pos(window, *PAD_ONLY)
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, PAD_KEY_ROLE
    pad_hint = next(item.toolTip() for item in view.items(pos)
                    if str(item.data(PAD_KEY_ROLE) or "").startswith("package:0:"))
    line_hint = next(item.toolTip() for item in view.items(pos) if item.data(DRAWING_KEY_ROLE) == 0)
    assert pad_hint and line_hint and pad_hint != line_hint
    assert view.reach_tooltip(pos) == pad_hint, "negative: a movable pad shows its own tooltip"
    monkeypatch.setattr(window, "_package_pads_movable", lambda: False)
    window._draw_preview()
    qapp.processEvents()
    assert not window.gesture_accepts(("pad", 0))
    assert window.gesture_accepts(("handle", 0, -1))
    QTest.mouseMove(viewport, pos + type(pos)(0, 30))
    QTest.mouseMove(viewport, pos)
    qapp.processEvents()
    assert view.reach_tooltip(pos) == line_hint, "the hover names what the press takes"
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    qapp.processEvents()
    assert window._fp_selected_indices() == [0] and window._fp_selected_pads() == [], "the press takes the line"


# ── The E key edits what a press would take (verification D4) ─────────

def test_e_opens_the_drawing_on_its_stroke_and_nothing_inside_its_empty_outline(window, qapp, monkeypatch):
    """Verification D4: E read the hovered drawing that reveals the handles, kept through the
    drawing's bounding rectangle, so inside an empty rectangle E opened it although a press there
    is the background (H1). E now takes the press's drawing at the pointer, as the symbol editor's
    `_hovered_graphic`. Positive: on the stroke it opens; negative: inside it opens nothing,
    while the handles still show (the reveal hysteresis is unchanged)."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    opened = []
    monkeypatch.setattr(window, "_edit_drawing_properties", lambda index: opened.append(index))
    QTest.mouseMove(view.viewport(), _pos(window, *SHAPES["rectangle"][1]))
    qapp.processEvents()
    assert view.drawing_under_pointer() == 0
    assert window._edit_footprint_target_properties() and opened == [0], "on the stroke, E opens it"
    opened.clear()
    QTest.mouseMove(view.viewport(), _pos(window, 0.0, -3.5))
    QTest.mouseMove(view.viewport(), _pos(window, *INSIDE))
    qapp.processEvents()
    assert window._fp_hovered_drawing == 0, "the handles stay revealed inside (hysteresis kept)"
    assert view.drawing_under_pointer() == -1
    assert not window._edit_footprint_target_properties() and opened == [], "inside the empty outline, nothing"
    from PyQt6.QtCore import QEvent
    view.leaveEvent(QEvent(QEvent.Type.Leave))
    assert view.drawing_under_pointer() == -1, "the pointer outside the view: nothing"


# ── Delete, R, Ctrl+D, Ctrl+C act on what a press would take (D4 class) ─

def _key_effect(window, key):
    """Press `key` in the preview; return what it changed: (drawings, undo depth, clipboard size)."""
    view = window.preview_view
    view.setFocus()
    QTest.keyClick(view, *key)
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()
    return deepcopy(window.footprint_drawings), len(window._fp_drawings_undo), len(window._fp_clipboard)


KEYS = {
    "delete": (Qt.Key.Key_Delete,),
    "turn": (Qt.Key.Key_R,),
    "duplicate": (Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier),
    "copy": (Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier),
}


@pytest.mark.parametrize("where", ["stroke", "inside_empty", "inside_filled"])
@pytest.mark.parametrize("command", sorted(KEYS))
def test_keyboard_commands_take_the_drawing_a_press_would_take(window, qapp, monkeypatch, command, where):
    """Same class as verification D4 (the E key): with nothing selected Delete, R, Ctrl+D and
    Ctrl+C read the hovered drawing that reveals the handles, kept through the drawing's bounding
    rectangle, so inside an EMPTY rectangle Delete deleted it although a press there is the
    background (H1, «Sì, come KiCad»). They now take `FootprintView.drawing_under_pointer`.
    Positive: on the stroke, and inside a FILLED rectangle, the command acts; negative: inside the
    empty outline it does nothing, while the handles stay revealed (the hysteresis the old rule
    read is still there, so the negative fails for a structural reason, not by chance)."""
    record = dict(SHAPES["rectangle"][0], **({"fill": True} if where == "inside_filled" else {}))
    _shown(window, qapp, monkeypatch, [record])
    assert len(window._fp_clipboard) == 0
    view = window.preview_view
    if where == "stroke":
        pointer_on_drawing(window, qapp, 0)
    else:
        for point in (SHAPES["rectangle"][1], (0.0, -3.5), INSIDE):
            _send_move(view, _pos(window, *point))
        qapp.processEvents()
        assert window._fp_hovered_drawing == 0, "the handles stay revealed inside (hysteresis kept)"
    before = deepcopy(window.footprint_drawings)
    depth = len(window._fp_drawings_undo)
    drawings, undo, clipboard = _key_effect(window, KEYS[command])
    if where == "inside_empty":
        assert view.drawing_under_pointer() == -1
        assert (drawings, undo, clipboard) == (before, depth, 0), "inside an empty outline: nothing"
        return
    if command == "delete":
        assert drawings == [] and undo == depth + 1
    elif command == "turn":
        assert len(drawings) == 1 and drawings[0]["points"] != before[0]["points"] and undo == depth + 1
    elif command == "duplicate":
        assert len(drawings) == 2 and undo == depth + 1
    else:
        assert drawings == before and clipboard == 1, "the drawing is copied"


# ── The hover cursor is undone when an interaction begins (D3) ─────────

def test_cancel_interaction_undoes_the_hover_move_cursor(window, qapp, monkeypatch):
    """Twin of the symbol's placement test: the footprint's interactions (a tool armed, a drag)
    begin with `cancel_interaction` or `_press_gesture`, which undo the reach cursor; negative:
    an idle hover keeps it."""
    from PyQt6.QtCore import QPoint
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE, HIT_MARGIN_PX
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    viewport = view.viewport()
    edge = _pos(window, -5.0, 0.5)
    half = 0
    while [item for item in view.items(edge - QPoint(half + 1, 0)) if item.data(DRAWING_KEY_ROLE) == 0]:
        half += 1
    near = edge - QPoint(half + HIT_MARGIN_PX - 1, 0)
    QTest.mouseMove(viewport, near)
    qapp.processEvents()
    assert viewport.cursor().shape() == Qt.CursorShape.SizeAllCursor, "negative: idle, the reach cursor shows"
    view.cancel_interaction()
    assert viewport.cursor().shape() == Qt.CursorShape.ArrowCursor


# ── The pointer target follows the camera (verification 2026-11-09) ────

def test_a_second_delete_after_the_auto_fit_takes_what_is_under_the_cursor_now(window, qapp, monkeypatch):
    """The pointer used to be stored in SCENE units: after the first Delete the auto-fit moved the
    camera without a mouse move, and the second Delete deleted a drawing the refit had carried away
    from the cursor. The pointer is now a VIEWPORT point mapped at query time. Positive: a line on
    the left side of a large rectangle; the pointer on that shared side takes the rectangle; Delete
    removes it, the refit zooms onto the line, which leaves the cursor; the second Delete does
    nothing and the answer equals the press target at the physical pointer."""
    from PyQt6.QtCore import QPoint
    _shown(window, qapp, monkeypatch, [_record("line", ((-30, -1), (-30, 1))),
                                       _record("rectangle", ((-30, -20), (30, 20)))])
    view = window.preview_view
    view.auto_fit = True
    window._draw_preview()
    qapp.processEvents()
    pos = _pos(window, -30, 0)
    for point in (pos + QPoint(0, 40), pos):
        _send_move(view, point)
    qapp.processEvents()
    assert view.drawing_under_pointer() == 1, "the rectangle is on top on the shared side"
    scale = view.transform().m11()
    _key_effect(window, KEYS["delete"])
    assert [record["kind"] for record in window.footprint_drawings] == ["line"]
    assert view.transform().m11() != scale, "the auto-fit changed the camera without a mouse move"
    physical = view.press_target(pos)
    assert physical is None, "the refit carried the line away from the cursor"
    assert view.drawing_under_pointer() == -1
    drawings, undo, _clipboard = _key_effect(window, KEYS["delete"])
    assert [record["kind"] for record in drawings] == ["line"], "the second Delete deletes nothing"


def test_a_wheel_zoom_under_the_pointer_keeps_the_drawing_under_it(window, qapp, monkeypatch):
    """Negative of the camera-follow rule: a wheel zoom anchored under the mouse keeps the same
    scene point under the same viewport point, so the drawing under the pointer stays the target
    (on the stroke of an empty rectangle, zoomed in by a real wheel event)."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pos = pointer_on_drawing(window, qapp, 0)
    scale = view.transform().m11()
    event = QWheelEvent(QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)), QPoint(0, 0), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                        False)
    QApplication.sendEvent(view.viewport(), event)
    qapp.processEvents()
    assert view.transform().m11() > scale, "the wheel zoomed in"
    assert view.drawing_under_pointer() == 0


def _flat(points):
    """Points [[x, y], ...] as one flat list, for `pytest.approx`."""
    return [value for point in points for value in point]


# ── Arrows with nothing selected select the drawing they nudge ─────────

def test_held_arrows_on_the_drawing_under_the_pointer_select_it_and_move_it_every_step(window, qapp, monkeypatch):
    """With nothing selected, an arrow nudged the drawing under the pointer until it slid out from
    under the stored point (a filled 10 x 8 mm rectangle moved 6 steps, a stroke 1): a shape-
    dependent stop. The first nudge now SELECTS the drawing (KiCad acts on the item under the
    cursor and keeps it), so 40 Right presses move it 40 steps, one undo step each."""
    _shown(window, qapp, monkeypatch, [_record("rectangle", ((-5, -4), (5, 4)), fill=True)])
    view = window.preview_view
    for point in (SHAPES["rectangle"][1], INSIDE):
        _send_move(view, _pos(window, *point))
    qapp.processEvents()
    assert view.drawing_under_pointer() == 0 and window._fp_selected_indices() == []
    step_x, _step_y = window._footprint_paste_step()
    depth = len(window._fp_drawings_undo)
    view.setFocus()
    for _ in range(40):
        QTest.keyClick(view, Qt.Key.Key_Right)
    qapp.processEvents()
    assert _flat(window.footprint_drawings[0]["points"]) == pytest.approx(
        [-5 + 40 * step_x, -4, 5 + 40 * step_x, 4])
    assert window._fp_selected_indices() == [0], "the nudged drawing is selected"
    assert len(window._fp_drawings_undo) == depth + 40, "one undo step per nudge"


def test_arrows_with_a_selection_move_the_selection_and_ignore_the_pointer(window, qapp, monkeypatch):
    """Negative: with a drawing selected, the arrows move the selection, never the drawing under
    the pointer, and the selection is not replaced by it."""
    _shown(window, qapp, monkeypatch, [_record("rectangle", ((-5, -4), (5, 4)), fill=True),
                                       _record("line", ((10, -4), (20, -4)))])
    view = window.preview_view
    window._select_footprint_drawing(1)
    for point in (SHAPES["rectangle"][1], INSIDE):
        _send_move(view, _pos(window, *point))
    qapp.processEvents()
    assert view.drawing_under_pointer() == 0
    step_x, _step_y = window._footprint_paste_step()
    before = deepcopy(window.footprint_drawings)
    view.setFocus()
    for _ in range(3):
        QTest.keyClick(view, Qt.Key.Key_Right)
    qapp.processEvents()
    assert window.footprint_drawings[0] == before[0], "the drawing under the pointer stays"
    assert _flat(window.footprint_drawings[1]["points"]) == pytest.approx([10 + 3 * step_x, -4, 20 + 3 * step_x, -4])
    assert window._fp_selected_indices() == [1]



def test_a_refused_nudge_does_not_select_the_drawing_under_the_pointer(window, qapp, monkeypatch):
    """Review 2026-11-09, point 2: the pointer's drawing was selected BEFORE the move was tried, so
    an arrow the validation refused («Spostamento scartato.») still turned the hover into a
    selection. Negative twin of the held-arrows test: the translated record is degenerate, the move
    is refused, and nothing changes: no selection, same drawings, no undo step."""
    import kicadgen.core.footprint as footprint
    _shown(window, qapp, monkeypatch, [_record("rectangle", ((-5, -4), (5, 4)), fill=True)])
    view = window.preview_view
    for point in (SHAPES["rectangle"][1], INSIDE):
        _send_move(view, _pos(window, *point))
    qapp.processEvents()
    assert view.drawing_under_pointer() == 0 and window._fp_selected_indices() == []
    toasts = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: toasts.append(title))
    monkeypatch.setattr(footprint, "translate_drawing",
                        lambda record, dx, dy: dict(record, points=[[1.0, 1.0], [1.0, 1.0]]))
    before = deepcopy(window.footprint_drawings)
    depth = len(window._fp_drawings_undo)
    assert window._nudge_footprint_drawings(1, 0) is False
    assert toasts, "the validation refused the move"
    assert window.footprint_drawings == before and len(window._fp_drawings_undo) == depth
    assert window._fp_selected_indices() == [], "a refused nudge selects nothing"


def _zoom_away(view, qapp, pos):
    """Zoom 4x about the viewport's top-left corner: the scene slides away from the still pointer at `pos`."""
    from PyQt6.QtCore import QPoint
    view.auto_fit = False
    assert view.camera.zoom(4.0, QPoint(0, 0)), "the camera changed"
    qapp.processEvents()
    target = view.press_target(pos)
    assert target is None or target[0] not in ("drawing", "handle"), "the drawing left the cursor"


def test_the_readout_follows_a_camera_change_without_a_move(window, qapp, monkeypatch):
    """Review 2026-11-09, point 4: on camera_changed the coordinate line kept the scene point of the
    last move, which the zoom had moved away from the still cursor. It now shows the point under the
    cursor NOW. Negative: with the pointer outside the view the line keeps its last point."""
    from PyQt6.QtCore import QEvent
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pos = pointer_on_drawing(window, qapp, 0)
    before = QPointF(window._fp_cursor_position)
    _zoom_away(view, qapp, pos)
    now = view.mapToScene(pos)
    assert window._fp_cursor_position != before
    assert (window._fp_cursor_position.x(), window._fp_cursor_position.y()) == pytest.approx((now.x(), now.y()))
    view.leaveEvent(QEvent(QEvent.Type.Leave))
    kept = QPointF(window._fp_cursor_position)
    view.camera.zoom(0.5)
    qapp.processEvents()
    assert window._fp_cursor_position == kept, "negative: outside the view, the last point stays"


def test_the_hover_handles_follow_a_camera_change_without_a_move(window, qapp, monkeypatch):
    """Review 2026-11-09, point 5: the handles revealed for the drawing under the OLD scene point
    survived an auto-fit, a scroll or a zoom, and a handle is the first rank of the press pick. The
    camera change now re-runs the hover at the pointer: the drawing left the cursor, its handles go.
    The negative is the wheel zoom under the pointer below, which keeps them."""
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pos = pointer_on_drawing(window, qapp, 0)
    assert view.hovered_drawing() == 0 and window._fp_hovered_drawing == 0
    _zoom_away(view, qapp, pos)
    assert view.hovered_drawing() == -1 and window._fp_hovered_drawing == -1
    assert view.drawing_under_pointer() == -1


def test_a_wheel_zoom_under_the_pointer_keeps_the_hover_handles(window, qapp, monkeypatch):
    """Negative of the point-5 test: a wheel zoom anchored under the mouse keeps the drawing under
    the pointer, so its handles stay revealed after the hover is re-run."""
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication
    _shown(window, qapp, monkeypatch, [SHAPES["rectangle"][0]])
    view = window.preview_view
    pos = pointer_on_drawing(window, qapp, 0)
    event = QWheelEvent(QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)), QPoint(0, 0), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                        False)
    QApplication.sendEvent(view.viewport(), event)
    qapp.processEvents()
    assert view.hovered_drawing() == 0 and window._fp_hovered_drawing == 0


# ── A body resize handle over pad copper gives way (FootprintView._pick) ─

def _corner_pad_package(window, qapp, monkeypatch):
    """A 15 x 10 mm body whose top-left corner (the 'nw' resize handle) lies on pad 1's copper;
    the 'ne' corner has no pad. Returns the viewport points of both corners, handles revealed."""
    from PyQt6.QtCore import QPoint
    from test_footprint_pad_drag import _show
    from kicadgen.core.package_geometry import PackageGeometry, PinSpec
    _show(window, qapp, monkeypatch, PackageGeometry(
        family="soic", body_length_mm=15.0, body_width_mm=10.0,
        pins=[PinSpec("1", -7.5, -5.0, 1.2, 1.2), PinSpec("2", 7.5, 5.0, 1.2, 1.2)]))
    assert window._body_outline_editable(window.package_geometry)
    view = window.preview_view
    corners = _pos(window, -7.5, -5.0), _pos(window, 7.5, -5.0)
    for point in (corners[0] + QPoint(0, 30), corners[0]):
        _send_move(view, point)
    qapp.processEvents()
    return corners


def _body_handle(view, pos):
    from kicadgen.gui.footprint_view import BODY_PART_ROLE
    return [str(item.data(BODY_PART_ROLE)) for item in view.items(pos)
            if item.data(BODY_PART_ROLE) not in (None, "move") and item.isVisible()]


def test_a_body_resize_handle_over_pad_copper_gives_the_press_and_the_menu_to_the_pad(window, qapp, monkeypatch):
    """Positive: on the 'nw' handle square where the pad's copper is exactly under the pointer, the
    left press and the right click take the pad (`_pick` drops the body handles and picks again)."""
    corner, _free = _corner_pad_package(window, qapp, monkeypatch)
    view = window.preview_view
    assert _body_handle(view, corner) == ["nw"], "the visible handle is exactly under the pointer"
    assert view._pad_copper_target(corner) is not None
    assert view.press_target(corner)[0] == "pad"
    assert view.menu_target(corner)[0] == "pad"


def test_a_body_resize_handle_off_the_copper_keeps_the_press_and_the_menu(window, qapp, monkeypatch):
    """Negative: the 'ne' handle, with no pad beneath, is still the body's handle for both buttons."""
    from PyQt6.QtCore import QPoint
    _corner, free = _corner_pad_package(window, qapp, monkeypatch)
    view = window.preview_view
    for point in (free + QPoint(0, 30), free):
        _send_move(view, point)
    qapp.processEvents()
    assert _body_handle(view, free) == ["ne"]
    assert view._pad_copper_target(free) is None
    assert view.press_target(free) == ("body", "ne")
    assert view.menu_target(free) == ("body", "ne")


def test_a_drawing_handle_over_the_pad_beats_the_pad_after_the_body_handle_gives_way(window, qapp, monkeypatch):
    """The re-pick keeps the ranks: a drawing's end handle on that pad (RANK_HANDLE) wins over the
    pad (RANK_PIN), for the press and the menu."""
    from PyQt6.QtCore import QPoint
    corner, _free = _corner_pad_package(window, qapp, monkeypatch)
    window.footprint_drawings = validate_footprint_drawings([_record("line", ((-7.5, -5.0), (-7.5, 3.0)))])
    window._draw_preview()
    qapp.processEvents()
    view = window.preview_view
    for point in (corner + QPoint(0, 30), corner):
        _send_move(view, point)
    qapp.processEvents()
    assert _body_handle(view, corner) == ["nw"] and view._pad_copper_target(corner) is not None
    assert view.press_target(corner) == ("handle", 0, 0)
    assert view.menu_target(corner) == ("handle", 0, 0)


def test_a_text_over_the_pad_beats_the_pad_after_the_body_handle_gives_way(window, qapp, monkeypatch):
    """The re-pick keeps the ranks: a Reference text laid over the pad (RANK_TEXT) wins over the pad
    (RANK_PIN) on a pixel where the visible body handle, the text and the copper are all exact."""
    from PyQt6.QtCore import QPoint
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    corner, _free = _corner_pad_package(window, qapp, monkeypatch)
    window.footprint_text_positions = {"Reference": (-7.5, -5.0)}
    window._draw_preview()
    qapp.processEvents()
    view = window.preview_view
    for point in (corner + QPoint(0, 30), corner):
        _send_move(view, point)
    qapp.processEvents()
    found = None
    for dy in range(-8, 9):
        for dx in range(-8, 9):
            pos = corner + QPoint(dx, dy)
            texts = [item for item in view.items(pos) if item.data(TEXT_KEY_ROLE) is not None]
            if texts and _body_handle(view, pos) and view._pad_copper_target(pos) is not None:
                found = pos
                break
        if found is not None:
            break
    assert found is not None, "a pixel with the body handle, the text and the copper exactly under it"
    assert view.press_target(found) == ("text", "Reference")
    assert view.menu_target(found) == ("text", "Reference")
