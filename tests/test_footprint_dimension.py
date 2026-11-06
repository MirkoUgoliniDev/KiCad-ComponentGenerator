"""@brief Exportable dimensions of the footprint preview (2026-11-06, priority 3 B).

A «dimension» drawing record (two measured points plus the signed offset of
its line) is drawn by the shared dimension drawer, written as KiCad's aligned
`dimension` (allowed in footprints since KiCad 7), placed by a three-click
tool or exported from a «Misura» measure, edited through «Scostamento» in the
properties dialog, mirrored with its side kept, and measured by the geometry
checks as ink.
"""
import pytest
from PyQt6.QtWidgets import QGraphicsSimpleTextItem

from kicadgen.core.footprint import (FpDimension, Footprint, dimension_crossbar, dimension_normal,
                                     drawing_graphics, validate_footprint_drawings)
from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
from kicadgen.services import footprint_checks as fc
from tests.test_package_project_gui import acquire
from tests.test_photo_inventory_gui import gui  # noqa: F401 - window fixture

REC = {"kind": "dimension", "layer": "F.Fab", "width": 0.1, "points": [[-30.0, -30.0], [-20.0, -30.0]],
       "offset": 2.0}


# ── Record and writer ──────────────────────────────────────────────────────

def test_a_dimension_record_needs_two_distinct_points_and_a_finite_offset():
    assert validate_footprint_drawings([REC]) == [REC]
    assert validate_footprint_drawings([{**REC, "points": [[0, 0], [0, 0]]}]) == []
    assert validate_footprint_drawings([{**REC, "offset": "x"}]) == []
    assert validate_footprint_drawings([{**REC, "offset": float("nan")}]) == []
    assert validate_footprint_drawings([{k: v for k, v in REC.items() if k != "offset"}])[0]["offset"] == 0.0


def test_kicad_side_convention_and_the_written_dimension():
    # KiCad: the crossbar lies at the points plus (-dy, dx)/len times height.
    assert dimension_normal((0, 0), (10, 0)) == (-0.0, 1.0)
    assert dimension_crossbar((0, 0), (10, 0), 2.0) == ((0.0, 2.0), (10.0, 2.0))
    assert dimension_crossbar((0, 0), (10, 0), -2.0) == ((0.0, -2.0), (10.0, -2.0))
    fp = Footprint(name="T", attr="smd")
    for item in drawing_graphics(validate_footprint_drawings([REC])):
        fp.add_graphic(item)
    text = fp.render()
    assert "(dimension" in text and "(type aligned)" in text and "(height 2)" in text
    assert '(gr_text "10.00 mm"' in text and "(format" in text and "(units 2)" in text
    assert "(style" in text and "keep_text_aligned" in text and '(layer "F.Fab")' in text
    with pytest.raises(ValueError):
        FpDimension(start=(0, 0), end=(0, 0), height=1.0, layer="F.Fab").to_sexpr()


def test_turn_and_mirror_carry_the_dimension_line_with_the_points():
    """The line is carried as a point: a turn keeps it where it was seen, a mirror puts it on the mirrored side."""
    from kicadgen.core.footprint import drawing_bounds, mirror_drawing, turn_drawing
    from kicadgen.core.drawing_transform import mirror_points_x, rotate_points_90
    for record in (REC, {**REC, "points": [[-30.0, -30.0], [-24.0, -22.0]], "offset": -1.5}):
        old_c1 = dimension_crossbar(record["points"][0], record["points"][1], record["offset"])[0]
        turned = turn_drawing(record, True)
        centre = ((record["points"][0][0] + record["points"][1][0]) / 2, 0)  # only the X matters for the mirror
        expected = rotate_points_90([record["points"][0], list(old_c1)],
                                    __import__("kicadgen.core.footprint", fromlist=["drawing_centre"]).drawing_centre(record), True)[1]
        got = dimension_crossbar(turned["points"][0], turned["points"][1], turned["offset"])[0]
        assert got == pytest.approx(expected, abs=1e-3), "turned crossbar == rotated crossbar"
        mirrored = mirror_drawing(record)
        axis_x = __import__("kicadgen.core.footprint", fromlist=["drawing_centre"]).drawing_centre(record)[0]
        expected = mirror_points_x([list(old_c1)], axis_x)[0]
        got = dimension_crossbar(mirrored["points"][0], mirrored["points"][1], mirrored["offset"])[0]
        assert got == pytest.approx(expected, abs=1e-3), "mirrored crossbar == mirror of the crossbar"
    assert mirror_drawing(REC)["offset"] == -2.0
    assert drawing_bounds(REC) == (-30.0, -30.0, -20.0, -28.0), "the box includes the dimension line"


def test_an_orthogonal_dimension_measures_one_axis_and_swaps_it_under_a_turn():
    from kicadgen.core.footprint import dimension_text, turn_drawing
    rec = {**REC, "points": [[0.0, 0.0], [10.0, 3.0]], "axis": "x", "offset": -5.0}
    assert validate_footprint_drawings([rec]) == [rec]
    assert validate_footprint_drawings([{**rec, "points": [[0.0, 0.0], [0.0, 3.0]]}]) == [], "no X span"
    assert validate_footprint_drawings([{**rec, "axis": "z"}]) == []
    assert dimension_crossbar((0, 0), (10, 3), -5.0, "x") == ((0.0, -5.0), (10.0, -5.0))
    assert dimension_text((0, 0), (10, 3), "x") == "10.00 mm" and dimension_text((0, 0), (10, 3), "y") == "3.00 mm"
    turned = turn_drawing(rec, True)
    assert turned["axis"] == "y"
    fp = Footprint(name="T", attr="smd")
    for item in drawing_graphics([rec]):
        fp.add_graphic(item)
    text = fp.render()
    assert "(type orthogonal)" in text and "(orientation 0)" in text and '(gr_text "10.00 mm"' in text


def test_a_dimension_on_the_silkscreen_is_ink_including_the_overshoot_and_the_arrows():
    from kicadgen.core.footprint import DIMENSION_EXTENSION_HEIGHT_MM, Pad, dimension_ink_segments
    segments = dimension_ink_segments((0, 0), (10, 0), 2.0)
    assert len(segments) == 7, "two extension lines, the crossbar, four arrow strokes"
    assert segments[0][1][1] == pytest.approx(2.0 + DIMENSION_EXTENSION_HEIGHT_MM), "overshoot beyond the crossbar"
    fp = Footprint(name="T", attr="smd")
    fp.add_pad(Pad(number="1", pad_type="smd", shape="rect", at=(0, 0), size=(1.0, 1.0), layers=["F.Cu"]))
    # Crossbar 0.4 mm from the copper (clear), but the extension overshoot reaches 0.014 mm from it.
    fp.add_graphic(FpDimension(start=(-5, -3.4), end=(5, -3.4), height=2.8, layer="F.SilkS", width=0.12))
    assert [f.code for f in fc.check_silk_clearance(fp)] == ["silk_clearance"]
    fp.graphics[0] = FpDimension(start=(-5, -3.4), end=(5, -3.4), height=2.8, layer="F.Fab", width=0.1)
    assert fc.check_silk_clearance(fp) == []
    # A dimension on F.Fab outside the courtyard is documentation, not the part.
    from kicadgen.core.footprint import FpRect
    fp.add_graphic(FpRect(start=(-1, -1), end=(1, 1), layer="F.CrtYd", width=0.05))
    assert fc.check_courtyard_enclosure(fp) == []


def test_the_label_sits_beyond_the_crossbar_on_the_height_side_turned_along_the_line():
    from kicadgen.core.footprint import dimension_label_pose
    assert dimension_label_pose((0, 0), (10, 0), 2.0, 1.0) == ((5.0, 3.0), 0.0)
    assert dimension_label_pose((0, 0), (10, 0), -2.0, 1.0) == ((5.0, -3.0), 0.0)
    (centre, angle) = dimension_label_pose((0, 0), (0, 10), 2.0, 1.0)
    assert centre == pytest.approx((-3.0, 5.0)) and angle == 90.0, "vertical: read bottom to top"
    (_centre, angle) = dimension_label_pose((10, 0), (0, 0), 2.0, 1.0)
    assert angle == 0.0, "right to left: kept upright"
    fp = Footprint(name="T", attr="smd")
    fp.add_graphic(FpDimension(start=(0, 0), end=(10, 0), height=-2.0, layer="F.Fab"))
    assert "(at 5 -3)" in fp.render(), "the written text follows the height side"


# ── Tool, preview, dialog, export ─────────────────────────────────────────

def test_three_clicks_make_a_dimension_the_third_placing_its_line(window):  # noqa: F811
    acquire(window)
    window.combo_fp_draw_layer.setCurrentIndex(window.combo_fp_draw_layer.findData("F.Fab"))
    window._toggle_footprint_draw_tool("dimension")
    spec = window.preview_view.click_collector.spec
    assert spec.key == "dimension" and spec.count == 3
    window._cancel_footprint_drawing()
    window._finish_footprint_drawing("dimension", [(-30.0, -30.0), (-20.0, -30.0), (-25.0, -27.0)])
    assert window.footprint_drawings[-1] == {"kind": "dimension", "layer": "F.Fab", "width": 0.1,
                                             "points": [[-30.0, -30.0], [-20.0, -30.0]], "offset": 3.0}
    # The other side gives a negative offset; a third click on the line gives 0 and a toast.
    window._finish_footprint_drawing("dimension", [(-30.0, -30.0), (-20.0, -30.0), (-25.0, -33.0)])
    assert window.footprint_drawings[-1]["offset"] == -3.0
    toasts = []
    monkeypatch_toast = window._draw_feedback_toast
    window._draw_feedback_toast = lambda title, body: toasts.append(title)
    try:
        window._finish_footprint_drawing("dimension", [(-30.0, -30.0), (-20.0, -30.0), (-25.0, -30.0)])
    finally:
        window._draw_feedback_toast = monkeypatch_toast
    assert window.footprint_drawings[-1]["offset"] == 0.0 and toasts == ["Quota sulla linea misurata."]
    # Two points only: nothing is stored.
    before = len(window.footprint_drawings)
    window._finish_footprint_drawing("dimension", [(-30.0, -30.0), (-20.0, -30.0)])
    assert len(window.footprint_drawings) == before
    assert "dimension" in window._fp_draw_buttons and window.FOOTPRINT_DRAWING_KIND_NAMES["dimension"] == "Quota"


def test_the_dimension_is_drawn_with_its_value_and_its_offset_is_edited_in_the_dialog(window):  # noqa: F811
    acquire(window)
    window.footprint_drawings = [dict(REC)]
    window._draw_preview()
    items = [i for i in window.preview_scene.items() if i.data(DRAWING_KEY_ROLE) == 0]
    labels = [i for i in items if isinstance(i, QGraphicsSimpleTextItem)]
    assert labels and labels[0].text() == "10.00 mm"
    assert len(items) == 8, "two extension lines, the dimension line, four arrow strokes and the label"
    # The label is centred where KiCad puts it: one text height beyond the crossbar (y = -28 + 1 mm).
    centre = labels[0].mapToScene(labels[0].boundingRect().center())
    assert centre.x() == pytest.approx(-25.0 * 20, abs=1.0) and centre.y() == pytest.approx(-27.0 * 20, abs=1.0)
    assert window._drawing_properties_values(REC)["offset"] == 2.0
    assert window.set_drawing_properties(0, {"offset": -1.5})
    assert window.footprint_drawings[0]["offset"] == -1.5
    # Selection restyles the lines and the label without an outline on the text.
    window._fp_selection.set_many([0])
    window._refresh_drawing_selection()
    labels = [i for i in window.preview_scene.items()
              if i.data(DRAWING_KEY_ROLE) == 0 and isinstance(i, QGraphicsSimpleTextItem)]
    assert labels[0].brush().color().name() == window._fp_selected_colour()


def test_a_measure_is_exported_as_a_dimension_away_from_the_origin(window):  # noqa: F811
    from kicadgen.gui import canvas_measure as cm
    acquire(window)
    window.combo_fp_draw_layer.setCurrentIndex(window.combo_fp_draw_layer.findData("Cmts.User"))
    px = 20.0
    measure = cm.measure(cm.MeasureAnchor(-200.0, 100.0), cm.MeasureAnchor(0.0, 100.0), px)
    window._fp_measure.overlay.add(window.preview_scene, measure)
    before = len(window.footprint_drawings)
    assert window._export_measure_as_dimension(measure)
    record = window.footprint_drawings[-1]
    assert record["kind"] == "dimension" and record["layer"] == "Cmts.User"
    assert record["points"] == [[-10.0, 5.0], [0.0, 5.0]]
    # The line goes 1 mm away from the origin: the points are below it (y = 5), so further down.
    (c1, _c2) = dimension_crossbar(record["points"][0], record["points"][1], record["offset"])
    assert abs(record["offset"]) == 1.0 and c1[1] == pytest.approx(6.0)
    assert len(window.footprint_drawings) == before + 1 and window._fp_measure.overlay.count == 1
    assert window._fp_selected_indices() == [before]
    # Points ABOVE the origin: the line goes the other way (negative side).
    above = cm.measure(cm.MeasureAnchor(-200.0, -100.0), cm.MeasureAnchor(0.0, -100.0), px)
    assert window._export_measure_as_dimension(above)
    (c1, _c2) = dimension_crossbar(*window.footprint_drawings[-1]["points"], window.footprint_drawings[-1]["offset"])
    assert c1[1] == pytest.approx(-6.0)
    # A linear |dX| measure placed by its third click: an ORTHOGONAL dimension
    # between the anchors, with the line where the measure was placed.
    linear = cm.measure_linear(cm.MeasureAnchor(0.0, 0.0), cm.MeasureAnchor(100.0, 40.0, axis="x"), px,
                               place=cm.MeasureAnchor(0.0, -60.0))
    assert window._export_measure_as_dimension(linear)
    record = window.footprint_drawings[-1]
    assert record["points"] == [[0.0, 0.0], [5.0, 2.0]] and record["axis"] == "x"
    assert record["offset"] == pytest.approx(-3.0), "the line at y = -3 mm, where the measure was placed"
