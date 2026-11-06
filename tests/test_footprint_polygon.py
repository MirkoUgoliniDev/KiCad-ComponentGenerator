"""@brief Polygons in the footprint preview (2026-11-06, priority 3 A).

The line tool closes into a polygon on a click equal to its first point once
it has four points, as the symbol editor's line tool does; the closed shape
is a «polygon» record (first point stored once), written as `fp_poly`,
drawn as a polygon item, optionally filled (the «Riempimento» box of the
properties dialog, shared with rectangles and circles), measured by the
geometry checks on every side including the closing one.
"""
from PyQt6.QtWidgets import QGraphicsPolygonItem

from kicadgen.core.footprint import (FpPoly, Footprint, USER_DRAW_FILLABLE, drawing_graphics,
                                     validate_footprint_drawings)
from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
from kicadgen.services import footprint_checks as fc
from tests.test_package_project_gui import acquire
from tests.test_photo_inventory_gui import gui  # noqa: F401 - window fixture

SQUARE = [[-30.0, -30.0], [-28.0, -30.0], [-28.0, -28.0], [-30.0, -28.0]]


# ── Records and the writer ─────────────────────────────────────────────────

def test_a_polygon_record_needs_three_distinct_corners_and_keeps_fill_only_when_set():
    base = {"kind": "polygon", "layer": "F.SilkS", "width": 0.12}
    assert validate_footprint_drawings([{**base, "points": SQUARE}]) == [{**base, "points": SQUARE}]
    assert validate_footprint_drawings([{**base, "points": SQUARE[:3]}])[0]["points"] == SQUARE[:3]
    assert validate_footprint_drawings([{**base, "points": SQUARE[:2]}]) == []
    assert validate_footprint_drawings([{**base, "points": [SQUARE[0], SQUARE[0], SQUARE[1]]}]) == []
    filled = validate_footprint_drawings([{**base, "points": SQUARE, "fill": True}])
    assert filled[0]["fill"] is True
    assert "fill" not in validate_footprint_drawings([{**base, "points": SQUARE, "fill": False}])[0]
    assert validate_footprint_drawings([{**base, "points": SQUARE, "fill": "yes"}]) == []
    # A line never carries a fill; a rectangle and a circle may.
    line = {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": SQUARE[:2], "fill": True}
    assert "fill" not in validate_footprint_drawings([line])[0]
    assert USER_DRAW_FILLABLE == ("polygon", "rectangle", "circle")


def test_a_polygon_is_written_as_fp_poly_with_its_fill():
    fp = Footprint(name="T", attr="smd")
    for item in drawing_graphics(validate_footprint_drawings([
            {"kind": "polygon", "layer": "F.SilkS", "width": 0.12, "points": SQUARE, "fill": True},
            {"kind": "rectangle", "layer": "F.Fab", "width": 0.1, "points": SQUARE[:2:1][:1] + [SQUARE[2]], "fill": True}])):
        fp.add_graphic(item)
    text = fp.render()
    assert "(fp_poly" in text and "(pts" in text and text.count("(xy ") == 4
    assert text.count("(fill yes)") == 2 and '(layer "F.SilkS")' in text
    assert isinstance(fp.graphics[0], FpPoly) and fp.graphics[0].points == [tuple(p) for p in SQUARE]
    # Fewer than three corners cannot be written.
    import pytest
    with pytest.raises(ValueError):
        FpPoly(points=SQUARE[:2], layer="F.SilkS").to_sexpr()


def test_the_geometry_checks_measure_the_closing_side_too():
    from kicadgen.core.footprint import Pad
    fp = Footprint(name="T", attr="smd")
    fp.add_pad(Pad(number="1", pad_type="smd", shape="rect", at=(0, 0), size=(1.0, 1.0), layers=["F.Cu"]))
    # Three corners far away; the closing side (from the last corner back to
    # the first) runs along x = 0.6, inside the 0.2 mm clearance of the pad.
    fp.add_graphic(FpPoly(points=[(0.6, -3.0), (5.0, -3.0), (5.0, 3.0), (0.6, 3.0)], layer="F.SilkS", width=0.12))
    assert [f.code for f in fc.check_silk_clearance(fp)] == ["silk_clearance"]


# ── The line tool closing, the preview and the dialog ─────────────────────

def test_the_line_tool_closed_on_its_first_point_stores_a_polygon(window):  # noqa: F811
    acquire(window)
    window.combo_fp_draw_layer.setCurrentIndex(window.combo_fp_draw_layer.findData("F.SilkS"))
    window._finish_footprint_drawing("line", [tuple(p) for p in SQUARE] + [tuple(SQUARE[0])])
    assert window.footprint_drawings[-1] == {"kind": "polygon", "layer": "F.SilkS", "width": 0.12,
                                             "points": SQUARE}
    # Three clicks back on the first point stay a line: the close rule needs four.
    window._finish_footprint_drawing("line", [tuple(p) for p in SQUARE[:2]] + [tuple(SQUARE[0])])
    assert window.footprint_drawings[-1]["kind"] == "line"
    # The spec the palette arms carries the close rule, as the symbol editor's.
    window._toggle_footprint_draw_tool("line")
    spec = window.preview_view.click_collector.spec
    assert spec.key == "line" and spec.close_after == 4 and spec.count == 0
    window._cancel_footprint_drawing()


def test_the_polygon_is_drawn_as_a_polygon_item_and_fill_comes_from_the_dialog(window):  # noqa: F811
    acquire(window)
    window.footprint_drawings = [{"kind": "polygon", "layer": "F.SilkS", "width": 0.12, "points": SQUARE}]
    window._draw_preview()
    items = [i for i in window.preview_scene.items() if i.data(DRAWING_KEY_ROLE) == 0]
    assert len(items) == 1 and isinstance(items[0], QGraphicsPolygonItem)
    from PyQt6.QtCore import Qt
    assert items[0].brush().style() == Qt.BrushStyle.NoBrush, "an outline has no fill"
    assert window._drawing_properties_values(window.footprint_drawings[0])["fill"] is False
    assert window.set_drawing_properties(0, {"fill": True})
    assert window.footprint_drawings[0]["fill"] is True
    items = [i for i in window.preview_scene.items() if i.data(DRAWING_KEY_ROLE) == 0]
    assert 0 < items[0].brush().color().alpha() < 255, "filled: translucent layer colour"
    assert window.set_drawing_properties(0, {"fill": False})
    assert "fill" not in window.footprint_drawings[0]
    # Turn, mirror and move keep the four corners (generic point transforms).
    from kicadgen.core.footprint import mirror_drawing, translate_drawing, turn_drawing
    turned = turn_drawing(window.footprint_drawings[0], True)
    assert turned["kind"] == "polygon" and len(turned["points"]) == 4
    assert len(mirror_drawing(turned)["points"]) == 4
    assert translate_drawing(turned, 1.0, 0.0)["points"][0][0] == turned["points"][0][0] + 1.0
    assert window.FOOTPRINT_DRAWING_KIND_NAMES["polygon"] == "Poligono"
