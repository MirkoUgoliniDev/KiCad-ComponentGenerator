"""@brief Add / remove a vertex of a line, polygon or keepout zone in the footprint preview (plan F3).

Parity with tests/test_symbol_vertex_edit.py (user rule «stessa UX in tutte le
schede»): a right click on a SEGMENT offers «Aggiungi vertice», on a VERTEX
«Rimuovi vertice», after «Duplica»; each edit is one undo step, never leaves
an invalid drawing and says why it was refused, with the symbol editor's
words. The menu runs through the real `_footprint_drawing_context_menu` with
`QMenu.exec` replaced by a fake that triggers the chosen entry.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPointF  # noqa: E402
from PyQt6.QtWidgets import QMenu  # noqa: E402

from kicadgen.core import vertex_edit as ve  # noqa: E402
from kicadgen.core.footprint import ZONE_KEEPOUT_DEFAULTS, validate_footprint_drawings  # noqa: E402
from kicadgen.gui import canvas_menus  # noqa: E402

ADD = "Aggiungi vertice"
REMOVE = "Rimuovi vertice"
MM = 20.0
LINE = [(-10.0, 10.0), (-6.0, 10.0), (-6.0, 14.0)]
POLYGON = [(10.0, 10.0), (14.0, 10.0), (14.0, 14.0), (10.0, 14.0)]
ZONE = [(10.0, -14.0), (14.0, -14.0), (12.0, -10.0)]


@pytest.fixture
def fp(window, monkeypatch):
    """The preview holding a line (0), a polygon (1) and a keepout zone (2) on the 1 mm grid."""
    monkeypatch.setattr(window, "_generate", lambda **kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    window.chk_fp_object_snap.setChecked(False)
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    window.combo_fine_grid.setCurrentIndex(window.combo_fine_grid.findData(1))
    window.footprint_drawings = validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [list(p) for p in LINE]},
        {"kind": "polygon", "layer": "F.Fab", "width": 0.1, "points": [list(p) for p in POLYGON]},
        {"kind": "zone", "layers": ["F.Cu"], "keepout": dict(ZONE_KEEPOUT_DEFAULTS),
         "points": [list(p) for p in ZONE]},
    ])
    window._draw_preview()
    view = window.preview_view
    view.resize(800, 600)
    view.resetTransform()
    view.centerOn(0, 0)
    toasts = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: toasts.append((title, body)))
    window.toasts = toasts
    return window


def _vertices(fp, index):
    return [tuple(p) for p in fp.footprint_drawings[index]["points"]]


def _global(fp, x, y):
    view = fp.preview_view
    return view.viewport().mapToGlobal(view.mapFromScene(QPointF(x * MM, y * MM)))


def _menu_at(fp, index, x, y, monkeypatch, choose=None):
    """Open the drawing menu of `index` with the pointer at (x, y) mm; return {text: enabled}."""
    seen = {}

    def fake_exec(menu, *args):
        seen.update({a.text(): a.isEnabled() for a in menu.actions() if not a.isSeparator()})
        seen["__order__"] = [a.text() for a in menu.actions() if not a.isSeparator()]
        if choose is None:
            return None
        action = next(a for a in menu.actions() if a.text() == choose)
        assert action.isEnabled(), choose
        return action

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    fp._footprint_drawing_context_menu(index, _global(fp, x, y))
    return seen


# ── Menu: segment vs vertex ────────────────────────────────────────────

def test_a_click_on_a_segment_enables_add_only(fp, monkeypatch):
    seen = _menu_at(fp, 0, -8.0, 10.0, monkeypatch)
    assert seen[ADD] is True and seen[REMOVE] is False


def test_a_click_on_a_vertex_enables_remove_only(fp, monkeypatch):
    seen = _menu_at(fp, 0, -6.0, 10.0, monkeypatch)
    assert seen[ADD] is False and seen[REMOVE] is True


def test_a_click_inside_a_polygon_off_its_outline_enables_neither(fp, monkeypatch):
    seen = _menu_at(fp, 1, 12.0, 12.0, monkeypatch)
    assert seen[ADD] is False and seen[REMOVE] is False


def test_the_vertex_entries_follow_duplicate_as_in_the_symbol_editor(fp, monkeypatch):
    order = _menu_at(fp, 0, -8.0, 10.0, monkeypatch)["__order__"]
    duplicate = order.index("Duplica (Ctrl+D)")
    assert order[duplicate + 1:duplicate + 3] == [ADD, REMOVE]
    assert order.index("Layer") > duplicate + 2 and order.index("Proprietà elemento…") > duplicate + 2


def test_other_drawings_and_a_multi_selection_have_no_vertex_entries(fp, monkeypatch):
    fp.footprint_drawings = fp.footprint_drawings + validate_footprint_drawings([
        {"kind": "rectangle", "layer": "F.SilkS", "width": 0.12, "points": [[-10, -10], [-6, -6]]}])
    fp._draw_preview()
    seen = _menu_at(fp, 3, -10.0, -8.0, monkeypatch)
    assert ADD not in seen and REMOVE not in seen
    fp._fp_selection.set_many([0, 1])
    seen = _menu_at(fp, 0, -8.0, 10.0, monkeypatch)
    assert ADD not in seen and REMOVE not in seen


def test_the_pick_reach_is_the_shared_screen_reach(fp):
    """@brief The UI reach is the shared screen reach; the pick adds half the line's stroke (0.12 mm here)."""
    ui = fp._fp_vertex_pick_reach_mm()
    px_per_mm = abs(fp.preview_view.transform().m11()) * MM
    assert ui == pytest.approx(canvas_menus.VERTEX_PICK_PX / px_per_mm)
    reach = fp._fp_vertex_pick_reach_mm(0.12)
    assert reach == pytest.approx(ui + 0.06)
    assert fp._footprint_vertex_pick(0, -6.0 - reach * 0.9, 10.0).kind == ve.VERTEX
    assert fp._footprint_vertex_pick(0, -6.0 - reach * 1.5, 10.0).kind == ve.SEGMENT


def _thick_line(fp, width):
    """Replace the line (drawing 0) by the same line drawn `width` mm wide."""
    drawings = list(fp.footprint_drawings)
    drawings[0] = validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": width, "points": [list(p) for p in LINE]}])[0]
    fp.footprint_drawings = drawings
    fp._draw_preview()


def test_a_click_on_the_outer_edge_of_a_thick_line_picks_it(fp, monkeypatch):
    """@brief Positive: inside width/2 + the UI reach of a 2 mm line the vertex entries are on (KiCad EDA_SHAPE::hitTest)."""
    _thick_line(fp, 2.0)
    ui = fp._fp_vertex_pick_reach_mm()
    # Below the middle of the first segment, on the outer edge of the stroke.
    hit = fp._footprint_vertex_pick(0, -8.0, 10.0 + 1.0 + ui * 0.9)
    assert hit is not None and hit.kind == ve.SEGMENT
    seen = _menu_at(fp, 0, -8.0, 10.0 + 1.0 + ui * 0.5, monkeypatch)
    assert seen[ADD] is True and seen[REMOVE] is False


def test_a_click_beyond_half_the_stroke_plus_the_reach_picks_nothing(fp, monkeypatch):
    """@brief Negative: past width/2 + the UI reach the same thick line gives no vertex entry; a zone adds no width."""
    _thick_line(fp, 2.0)
    ui = fp._fp_vertex_pick_reach_mm()
    assert fp._footprint_vertex_pick(0, -8.0, 10.0 + 1.0 + ui * 1.5) is None
    seen = _menu_at(fp, 0, -8.0, 10.0 + 1.0 + ui * 1.5, monkeypatch)
    assert seen[ADD] is False and seen[REMOVE] is False
    # The zone (drawing 2) has no stroke: only the UI reach counts.
    assert fp._footprint_vertex_pick(2, 12.0, -14.0 - ui * 1.5) is None
    assert fp._footprint_vertex_pick(2, 12.0, -14.0 - ui * 0.5).kind == ve.SEGMENT


# ── Add ────────────────────────────────────────────────────────────────

def test_add_vertex_from_the_menu_inserts_the_snapped_projection(fp, monkeypatch):
    _menu_at(fp, 0, -8.1, 10.1, monkeypatch, choose=ADD)
    assert _vertices(fp, 0) == [(-10.0, 10.0), (-8.0, 10.0), (-6.0, 10.0), (-6.0, 14.0)]


def test_add_vertex_on_the_zone_closing_side_keeps_it_a_zone(fp, monkeypatch):
    _menu_at(fp, 2, 11.0, -12.0, monkeypatch, choose=ADD)
    stored = fp.footprint_drawings[2]
    assert stored["kind"] == "zone" and len(stored["points"]) == 4
    assert tuple(stored["points"][-1]) == (11.0, -12.0), "the closing side appends the vertex"


def test_add_vertex_landing_on_an_end_is_refused_with_a_reason(fp):
    before = fp.footprint_drawings
    assert fp.add_footprint_vertex(0, 0, -6.3, 10.0) is False, "the 1 mm grid snaps it onto vertex 1"
    assert fp.footprint_drawings == before
    assert "coinciderebbe" in fp.toasts[-1][1]


def test_add_then_undo_restores_the_drawing_in_one_step(fp):
    before = [dict(r) for r in fp.footprint_drawings]
    assert fp.add_footprint_vertex(0, 0, -8.0, 10.0)
    assert fp.footprint_drawings != before
    fp._undo_footprint_drawing()
    assert fp.footprint_drawings == before


# ── Remove ─────────────────────────────────────────────────────────────

def test_remove_vertex_from_the_menu(fp, monkeypatch):
    _menu_at(fp, 1, 14.0, 14.0, monkeypatch, choose=REMOVE)
    assert _vertices(fp, 1) == [POLYGON[0], POLYGON[1], POLYGON[3]]


def test_a_line_of_two_vertices_refuses_a_removal(fp):
    assert fp.remove_footprint_vertex(0, 1)
    before = fp.footprint_drawings
    assert fp.remove_footprint_vertex(0, 0) is False
    assert fp.footprint_drawings == before
    assert "almeno 2" in fp.toasts[-1][1]


def test_a_triangular_zone_refuses_a_removal(fp):
    before = fp.footprint_drawings
    assert fp.remove_footprint_vertex(2, 0) is False
    assert fp.footprint_drawings == before
    assert "almeno 3" in fp.toasts[-1][1]


def test_a_polygon_that_would_lose_its_area_refuses_the_removal(fp):
    fp.footprint_drawings = fp.footprint_drawings + validate_footprint_drawings([
        {"kind": "polygon", "layer": "F.SilkS", "width": 0.12,
         "points": [[-10, -10], [-8, -10], [-6, -10], [-8, -6]]}])
    before = fp.footprint_drawings
    assert fp.remove_footprint_vertex(3, 3) is False
    assert fp.footprint_drawings == before
    assert "senza area" in fp.toasts[-1][1]
    assert fp.remove_footprint_vertex(3, 1), "positive: removing a middle collinear vertex is fine"


def test_remove_then_undo_and_redo(fp):
    before = [dict(r) for r in fp.footprint_drawings]
    assert fp.remove_footprint_vertex(1, 2)
    fp._undo_footprint_drawing()
    assert fp.footprint_drawings == before
    fp._redo_footprint_drawing()
    assert len(_vertices(fp, 1)) == 3


def test_vertex_edits_refuse_other_kinds(fp):
    fp.footprint_drawings = fp.footprint_drawings + validate_footprint_drawings([
        {"kind": "text", "layer": "F.SilkS", "points": [[-10, -10]], "text": "X", "size": 1.0,
         "anchor": "left_bottom"}])
    before = fp.footprint_drawings
    assert fp._footprint_vertex_pick(3, -10.0, -10.0, reach=1.0) is None
    assert fp.remove_footprint_vertex(3, 0) is False
    assert fp.add_footprint_vertex(3, 0, -9.0, -10.0) is False
    assert fp.footprint_drawings == before


# ── Review 2026-11-09, D2: the pick reads the PRESS position, not QCursor ──

def test_a_real_right_press_picks_the_segment_under_the_press(fp, monkeypatch):
    """The view's signal carries the press's global point; the pointer may be anywhere else.

    Positive: a right press at the midpoint of the line's first segment offers
    «Aggiungi vertice» even though `QCursor.pos()` answers a far point.
    Negative twin: the same press on the line's vertex offers «Rimuovi vertice».
    """
    from PyQt6.QtCore import QEvent, QPoint, Qt
    from PyQt6.QtGui import QMouseEvent
    from kicadgen.gui import main_window as gui

    class FarCursor:
        @staticmethod
        def pos():
            return QPoint(-30000, -30000)
    monkeypatch.setattr(gui, "QCursor", FarCursor)
    view = fp.preview_view
    seen = {}

    def fake_exec(menu, *args):
        seen.clear()
        seen.update({a.text(): a.isEnabled() for a in menu.actions() if not a.isSeparator()})
        return None
    monkeypatch.setattr(QMenu, "exec", fake_exec)

    def press(x, y):
        local = QPointF(view.mapFromScene(QPointF(x * MM, y * MM)))
        event = QMouseEvent(QEvent.Type.MouseButtonPress, local, QPointF(view.viewport().mapToGlobal(local)),
                            Qt.MouseButton.RightButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(event)
        return dict(seen)

    on_segment = press(-8.0, 10.0)
    assert on_segment.get(ADD) is True and on_segment.get(REMOVE) is False, on_segment
    on_vertex = press(-6.0, 10.0)
    assert on_vertex.get(ADD) is False and on_vertex.get(REMOVE) is True, on_vertex


def test_the_drawing_signal_carries_the_press_global_position(fp):
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QMouseEvent
    view = fp.preview_view
    heard = []
    view.drawing_context_requested.disconnect()
    view.drawing_context_requested.connect(lambda index, pos: heard.append((index, pos)))
    local = QPointF(view.mapFromScene(QPointF(-8.0 * MM, 10.0 * MM)))
    global_point = QPointF(view.viewport().mapToGlobal(local))
    view.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, local, global_point, Qt.MouseButton.RightButton,
                                     Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier))
    assert heard == [(0, global_point.toPoint())]


# ── Decision A1: a line that ends on its first point is closed ─────────

SQUARE = [(-10.0, -10.0), (-6.0, -10.0), (-6.0, -6.0), (-10.0, -6.0)]


def _with_line(fp, points):
    fp.footprint_drawings = fp.footprint_drawings + validate_footprint_drawings([
        {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [list(p) for p in points]}])
    fp._draw_preview()
    return len(fp.footprint_drawings) - 1


def test_removing_a_vertex_of_a_closed_line_keeps_it_closed(fp):
    index = _with_line(fp, SQUARE + [SQUARE[0]])
    assert fp.remove_footprint_vertex(index, 0)
    points = _vertices(fp, index)
    assert points == [SQUARE[1], SQUARE[2], SQUARE[3], SQUARE[1]], "closed again on the next vertex"
    assert fp.footprint_drawings[index]["kind"] == "line"


def test_an_open_line_with_the_same_vertices_opens_on_a_removal(fp):
    """Negative twin: without the closing repeat the line is open, as before."""
    index = _with_line(fp, SQUARE)
    assert fp.remove_footprint_vertex(index, 0)
    assert _vertices(fp, index) == SQUARE[1:]


def test_a_closed_line_of_three_vertices_refuses_a_removal(fp):
    index = _with_line(fp, SQUARE[:3] + [SQUARE[0]])
    before = fp.footprint_drawings
    assert fp.remove_footprint_vertex(index, 1) is False
    assert fp.footprint_drawings == before
    assert "almeno 3" in fp.toasts[-1][1]


def test_the_closing_side_of_a_closed_line_takes_a_vertex(fp, monkeypatch):
    """The closing side (last vertex back to the first) is a segment, as a polygon's."""
    index = _with_line(fp, SQUARE + [SQUARE[0]])
    pick = fp._footprint_vertex_pick(index, -10.0, -8.0, reach=0.3)
    assert pick is not None and pick.kind == ve.SEGMENT and pick.index == 3
    _menu_at(fp, index, -10.0, -8.0, monkeypatch, choose=ADD)
    assert _vertices(fp, index) == SQUARE + [(-10.0, -8.0), SQUARE[0]]


def test_the_vertex_refusal_words_are_the_shared_table(fp):
    """Parity: the footprint shows `canvas_menus`' words, the ones the symbol editor shows."""
    fp._refuse_vertex_edit(ve.NO_AREA)
    assert fp.toasts[-1] == (canvas_menus.VERTEX_REFUSED, canvas_menus.vertex_refusal_text(ve.NO_AREA))
    fp._refuse_vertex_edit("unknown reason")
    assert fp.toasts[-1][1] == canvas_menus.VERTEX_REFUSED


# ── Review 2026-11-09: crossed zones, collapsed repeats, shared margin ──

CROSSED = "si incrocia o si tocca"


def test_a_vertex_drag_that_crosses_a_zone_is_refused_with_the_crossing_reason(fp):
    """Positive: dragging a corner of a square zone past the opposite side makes a bow tie -> refused, named.

    Negative twin: a drag that keeps the outline simple is stored.
    """
    square = [(10.0, -14.0), (14.0, -14.0), (14.0, -10.0), (10.0, -10.0)]
    fp.footprint_drawings = fp.footprint_drawings[:2] + validate_footprint_drawings([
        {"kind": "zone", "layers": ["F.Cu"], "keepout": dict(ZONE_KEEPOUT_DEFAULTS),
         "points": [list(p) for p in square]}])
    fp._draw_preview()
    before = fp.footprint_drawings
    # Corner 2 (14, -10) to (10, -16): sides 1-2 and 3-0 cross.
    fp._on_footprint_handle_dropped(2, 2, 10.0 * MM, -16.0 * MM)
    assert fp.footprint_drawings == before
    assert CROSSED in fp.toasts[-1][1], fp.toasts[-1]
    fp._on_footprint_handle_dropped(2, 2, 15.0 * MM, -9.0 * MM)
    assert _vertices(fp, 2)[2] == (15.0, -9.0), "negative: a simple outline is stored"


def test_the_crossing_reason_is_only_for_a_crossed_zone(fp):
    """Negative: a zone without area keeps the area reason; a simple zone never reads as crossed."""
    flat = {"kind": "zone", "layers": ["F.Cu"], "keepout": dict(ZONE_KEEPOUT_DEFAULTS),
            "points": [[0, 0], [2, 0], [4, 0]]}
    assert "un'area" in fp._degenerate_drawing_text(flat)
    bow = dict(flat, points=[[0, 0], [4, 4], [4, 0], [0, 4]])
    assert CROSSED in fp._degenerate_drawing_text(bow)
    assert CROSSED not in fp._degenerate_drawing_text(dict(flat, points=[[0, 0], [4, 0], [4, 4]]))


def test_a_repeated_zone_vertex_is_not_reported_as_a_crossing(fp):
    """@brief The reason judges the outline as the validator does, after dropping repeated corners.

    Negative: a simple triangle with one corner repeated is not "crossed" (the
    validator itself accepts it once the repeat is collapsed). Positive: a
    bow tie with a repeated corner still is.
    """
    zone = {"kind": "zone", "layers": ["F.Cu"], "keepout": dict(ZONE_KEEPOUT_DEFAULTS),
            "points": [[0, 0], [4, 0], [4, 0], [4, 4]]}
    assert validate_footprint_drawings([zone]), "a repeated corner alone is collapsed, not refused"
    assert CROSSED not in fp._degenerate_drawing_text(zone)
    bow = dict(zone, points=[[0, 0], [4, 4], [4, 4], [4, 0], [0, 4]])
    assert not validate_footprint_drawings([bow])
    assert CROSSED in fp._degenerate_drawing_text(bow)


def test_a_vertex_dropped_on_its_neighbour_is_collapsed_in_memory(fp):
    """The drop stores the VALIDATED record: the repeated vertex is gone at once, not only after a reload."""
    assert len(_vertices(fp, 1)) == 4
    fp._on_footprint_handle_dropped(1, 1, POLYGON[0][0] * MM, POLYGON[0][1] * MM)
    stored = _vertices(fp, 1)
    assert stored == [POLYGON[0], POLYGON[2], POLYGON[3]], stored
    assert fp.footprint_drawings == validate_footprint_drawings(fp.footprint_drawings), "already in validated form"


def test_the_footprint_hit_margin_is_the_shared_context_margin():
    from kicadgen.gui import footprint_view
    assert footprint_view.HIT_MARGIN_PX == canvas_menus.CONTEXT_HIT_MARGIN_PX
