"""@brief Regression tests for the 2026-11-03 generator review fixes.

Each fix has a positive case and a negative case that the rule must tell
apart for a structural reason: the copper KiCad draws for a pad shape, the
three collinear points of an arc, the vertex count of a polyline, the
evidence behind a row spacing, the text of a pad number, the key of a pad
override, the length of a vertical pin name, a malformed library, the KLC
layer widths and grids, and the pin-1 pad of a header not numbered «1».
"""

from __future__ import annotations

import math

import pytest

from kicadgen.core.footprint import (FAB_WIDTH_MM, SILK_OUTSIDE_FAB_MM, Pad, courtyard_round_out,
                                     validate_pad_number)
from kicadgen.core.package_geometry import (PackageGeometry, PinSpec, copper_separation,
                                            package_from_mapping, pad_copper_shape)
from kicadgen.generators import symbol_glyphs as glyphs
from kicadgen.generators.castellated_module import (CastellatedModuleParams, CastellatedSide,
                                                    build_castellated_module)
from kicadgen.generators.package import build_package
from kicadgen.generators.pin_header import (COURTYARD_MARGIN_MM, ROW_SPACING_ASSUMED_NOTE,
                                            PinHeaderParams, build_pin_header)
from kicadgen.generators.symbol import SymbolPin, layout_symbol


def _graphics(fp, layer):
    return [g for g in fp.graphics if getattr(g, "layer", None) == layer]


def _pad(fp, number):
    return next(p for p in fp.pads if p.number == number)


# ── 1. Pad shapes: what KiCad draws ───────────────────────────────────────

def test_a_circle_with_two_sides_becomes_the_oval_kicad_draws():
    # KiCad draws `(pad circle (size 2 1))` as a 2 mm disc: the measured
    # 2 x 1 mm land is an oval, kept with both sides.
    pin = PinSpec("1", 0, 0, 2.0, 1.0, pad_type="thru_hole", shape="circle", drill_mm=0.6)
    assert pin.shape == "oval"
    assert (pin.pad_length_mm, pin.pad_width_mm) == (2.0, 1.0)
    same = PinSpec("1", 0, 0, 1.5, 1.5, pad_type="thru_hole", shape="circle", drill_mm=0.6)
    assert same.shape == "circle"


def test_the_package_file_writes_the_oval_with_both_sides():
    geometry = PackageGeometry(name="Ovals", pins=[
        PinSpec("1", 0, 0, 2.0, 1.0, pad_type="thru_hole", shape="circle", drill_mm=0.6),
        # 1.2 mm apart across the short side: two 2 mm discs would overlap,
        # the two measured 2 x 1 mm ovals do not.
        PinSpec("2", 0, 1.2, 2.0, 1.0, pad_type="thru_hole", shape="circle", drill_mm=0.6),
    ])
    text = build_package(geometry).render()
    assert text.count("thru_hole oval") == 2
    assert "thru_hole circle" not in text
    assert "(size 2 1)" in text


def test_the_core_pad_writes_an_unequal_circle_as_an_oval():
    assert Pad("1", "thru_hole", "circle", (0, 0), (2.0, 1.0), ["*.Cu"], drill=0.5).shape == "oval"
    assert Pad("1", "thru_hole", "circle", (0, 0), (1.0, 1.0), ["*.Cu"], drill=0.5).shape == "circle"


def test_a_disc_beside_a_rectangle_corner_is_judged_on_the_real_copper():
    # Diagonal: the disc's bounding square would touch the rectangle, the
    # disc does not (0.566 mm centre-to-corner minus 0.5 mm radius).
    clear = PackageGeometry(name="Diag", pins=[
        PinSpec("1", 0, 0, 1.0, 1.0, shape="circle"),
        PinSpec("2", 0.9, 0.9, 1.0, 1.0, shape="rect")])
    clear.validate()
    # Same distance along X: the rectangle edge enters the disc.
    touching = PackageGeometry(name="Side", pins=[
        PinSpec("1", 0, 0, 1.0, 1.0, shape="circle"),
        PinSpec("2", 0.9, 0.0, 1.0, 1.0, shape="rect")])
    with pytest.raises(ValueError):
        touching.validate()


def test_rounded_corners_and_rotated_rectangles_keep_their_shape():
    # Roundrect corners (radius 0.25 x side) leave copper free where the
    # sharp rectangles would meet at one corner point.
    PackageGeometry(name="RR", pins=[
        PinSpec("1", 0, 0, 1.0, 1.0, shape="roundrect"),
        PinSpec("2", 1.0, 1.0, 1.0, 1.0, shape="roundrect")]).validate()
    with pytest.raises(ValueError):
        PackageGeometry(name="R", pins=[
            PinSpec("1", 0, 0, 1.0, 1.0, shape="rect"),
            PinSpec("2", 1.0, 1.0, 1.0, 1.0, shape="rect")]).validate()
    # Two rotated rectangles that do not touch stay accepted.
    PackageGeometry(name="Rot", pins=[
        PinSpec("1", 0, 0, 2.0, 0.4, shape="rect", rotation_deg=45),
        PinSpec("2", 0.8, 0, 2.0, 0.4, shape="rect", rotation_deg=45)]).validate()


def test_copper_separation_is_the_exact_gap():
    disc = pad_copper_shape(PinSpec("1", 0, 0, 1.0, 1.0, shape="circle"))
    other = pad_copper_shape(PinSpec("2", 3.0, 0, 1.0, 1.0, shape="circle"))
    assert copper_separation(disc, other) == pytest.approx(2.0)
    oval = pad_copper_shape(PinSpec("3", 0, 2.0, 3.0, 1.0, shape="oval"))
    assert copper_separation(disc, oval) == pytest.approx(1.0)
    square = pad_copper_shape(PinSpec("4", 0, 0, 0.4, 0.4, shape="rect"))
    assert copper_separation(disc, square) < 0


# ── 2-3. Symbol graphics: arcs and polylines KiCad can read ───────────────

def test_a_collinear_arc_is_never_drawn_or_accepted():
    assert glyphs.shape_instance("arc", [(0, 0), (4, 0), (2, 0)]) is None
    record = glyphs.shape_instance("arc", [(0, 0), (4, 0), (2, 1)])
    assert record is not None
    # Moving the mid point onto the chord would make it collinear.
    assert glyphs.set_shape_point(record, 1, 2.0, 0.0) is False
    assert glyphs.set_shape_point(record, 1, 2.0, 1.5) is True
    glyphs.validate_instances([record])
    broken = dict(record, primitives=[dict(record["primitives"][0], points=[[0, 0], [1, 0], [2, 0]])])
    with pytest.raises(ValueError):
        glyphs.validate_instances([broken])


@pytest.mark.parametrize("count, valid", [(0, False), (1, False), (2, True), (3, True)])
def test_a_polyline_needs_two_points(count, valid):
    record = {"x": 0, "y": 0, "primitives": [
        {"kind": "polyline", "points": [[i, i] for i in range(count)], "width": 0.254}]}
    if valid:
        assert glyphs.validate_instances([record])
    else:
        with pytest.raises(ValueError):
            glyphs.validate_instances([record])


# ── 4. Row spacing: assumed only with a note ──────────────────────────────

def test_a_multi_row_header_without_row_spacing_reports_the_assumption():
    assumed = PinHeaderParams(rows=2, cols=3)
    assert assumed.row_spacing_assumed()
    assert assumed.assumption_notes()
    assert ROW_SPACING_ASSUMED_NOTE in build_pin_header(assumed).description
    declared = PinHeaderParams(rows=2, cols=3, row_spacing=7.62)
    assert not declared.row_spacing_assumed() and declared.assumption_notes() == []
    assert ROW_SPACING_ASSUMED_NOTE not in build_pin_header(declared).description
    # One row: the spacing has no geometric effect, nothing is assumed.
    assert not PinHeaderParams(rows=1, cols=3).row_spacing_assumed()


# ── 5. Pad numbers: one text rule with the symbol ─────────────────────────

@pytest.mark.parametrize("number", [" 1", "A 2", "B3\n", "1\t", "C\x004"])
def test_a_pad_number_with_blanks_or_control_characters_is_refused(number):
    with pytest.raises(ValueError):
        validate_pad_number(number)
    with pytest.raises(ValueError):
        PinSpec(number, 0, 0, 1, 1).validate()
    with pytest.raises(ValueError):
        Pad(number, "smd", "rect", (0, 0), (1, 1), ["F.Cu"])


def test_plain_pad_numbers_and_the_mechanical_hole_pass():
    for number in ("1", "A1", "GND", "EP"):
        validate_pad_number(number)
        PinSpec(number, 0, 0, 1, 1).validate()
    Pad("", "np_thru_hole", "circle", (0, 0), (1, 1), ["*.Cu"], drill=1.0)


def test_the_extraction_boundary_strips_the_ends_of_a_number():
    geometry = package_from_mapping({"name": "P", "pins": [
        {"number": " 1 ", "x_mm": 0, "y_mm": 0, "pad_length_mm": 1, "pad_width_mm": 1}]})
    assert geometry.pins[0].number == "1"
    with pytest.raises(ValueError):
        package_from_mapping({"name": "P", "pins": [
            {"number": "A 1", "x_mm": 0, "y_mm": 0, "pad_length_mm": 1, "pad_width_mm": 1}]})


# ── 6. Pad overrides and pin map keys ─────────────────────────────────────

def test_an_int_keyed_pad_override_is_applied():
    params = PinHeaderParams(rows=1, cols=3, pad_overrides={2: {"shape": "rect", "size": 2.0}})
    fp = build_pin_header(params)
    assert _pad(fp, "2").size == (2.0, 2.0) and _pad(fp, "2").shape == "rect"
    assert set(params.pad_overrides) == {"2"}


def test_two_override_keys_for_one_pin_are_refused():
    with pytest.raises(ValueError):
        PinHeaderParams(rows=1, cols=3, pad_overrides={
            2: {"shape": "rect", "size": 2.0}, "2": {"shape": "circle", "size": 1.8}}).validate()


def test_a_json_pin_map_with_list_positions_works():
    params = PinHeaderParams(rows=2, cols=2, row_spacing=2.54,
                             pin_map={"1": [0, 0], "2": [1, 0], "3": [1, 1], "4": [0, 1]})
    fp = build_pin_header(params)
    assert _pad(fp, "3").at == (2.54, 2.54)
    with pytest.raises(ValueError):
        PinHeaderParams(rows=2, cols=2, row_spacing=2.54, pin_map={"1": [0]}).validate()


# ── 7. Symbol body: vertical pin names need height ────────────────────────

def test_long_top_and_bottom_pin_names_make_the_body_taller():
    short = layout_symbol([SymbolPin("1", "A", side="top"), SymbolPin("2", "B", side="bottom")])
    long = layout_symbol([SymbolPin("1", "VERY_LONG_TOP_NAME", side="top"),
                          SymbolPin("2", "VERY_LONG_BOTTOM_NAME", side="bottom")])
    assert long.height > short.height
    # Left/right names keep sizing the width only.
    side = layout_symbol([SymbolPin("1", "VERY_LONG_LEFT_NAME", side="left")])
    assert side.height == short.height


# ── 8. Malformed library: None, as documented ─────────────────────────────

def test_a_malformed_library_glyph_is_none(tmp_path):
    library = tmp_path / "broken.kicad_sym"
    library.write_text('(kicad_symbol_lib (version 20211014) (generator test)\n'
                       '  (symbol "X" (symbol "X_0_1" (polyline (pts (xy 1)) '
                       '(stroke (width 0) (type default)) (fill (type none))))))\n', encoding="utf-8")
    assert glyphs.load_library_glyph(library, "X") is None
    good = tmp_path / "good.kicad_sym"
    good.write_text('(kicad_symbol_lib (version 20211014) (generator test)\n'
                    '  (symbol "X" (symbol "X_0_1" (polyline (pts (xy 0 0) (xy 1 1)) '
                    '(stroke (width 0) (type default)) (fill (type none))))))\n', encoding="utf-8")
    assert glyphs.load_library_glyph(good, "X") is not None


# ── 9. KLC layers ─────────────────────────────────────────────────────────

def _on_grid(value):
    return math.isclose(value * 100, round(value * 100), abs_tol=1e-6)


def test_courtyard_round_out_never_reduces_the_clearance():
    assert courtyard_round_out(-1.103, 2.101) == (-1.11, 2.11)
    assert courtyard_round_out(-1.1, 2.1) == (-1.1, 2.1)


def test_pin_header_courtyard_is_the_klc_margin_on_the_grid():
    fp = build_pin_header(PinHeaderParams(rows=1, cols=2, pitch=2.54, pad_diameter=1.7, drill=1.0))
    courtyard, = _graphics(fp, "F.CrtYd")
    for value in (*courtyard.start, *courtyard.end):
        assert _on_grid(value)
    # User decision 2026-11-03: KLC connector clearance, 0.5 mm beyond the
    # 1.7 mm pad at x = 0 (pad edge -0.85), never less.
    assert COURTYARD_MARGIN_MM == 0.5
    assert min(courtyard.start[0], courtyard.end[0]) <= -0.85 - 0.5 + 1e-9
    silk = _graphics(fp, "F.SilkS")
    # Without a body the silk frames the pads uncut and no F.Fab outline is invented.
    assert not [g for g in _graphics(fp, "F.Fab") if type(g).__name__ == "FpRect"]
    frame = [g for g in silk if type(g).__name__ == "FpRect"]
    assert len(frame) == 1 and frame[0].start[0] < -0.85


def test_silk_lies_outside_the_fab_outline():
    fp = build_pin_header(PinHeaderParams(rows=1, cols=2, body_length=6.0, body_width=3.0,
                                          body_offset_x=-1.73, body_offset_y=-1.5))
    fab, = [g for g in _graphics(fp, "F.Fab") if type(g).__name__ == "FpRect"]
    assert fab.width == FAB_WIDTH_MM
    silk_x = [x for g in _graphics(fp, "F.SilkS") if type(g).__name__ in ("FpRect", "FpLine")
              for x in (g.start[0], g.end[0])]
    assert min(silk_x) == pytest.approx(fab.start[0] - SILK_OUTSIDE_FAB_MM)


def test_castellated_fab_outline_has_the_klc_width_and_silk_outside():
    fp = build_castellated_module(CastellatedModuleParams(
        name="M", body_length=20.0, body_width=15.0, pad_length=1.8, pad_width=1.2, pad_overhang=0.5,
        sides=[CastellatedSide(edge="left", count=3, pitch=2.54, first_pin=1)]))
    fab = [g for g in _graphics(fp, "F.Fab") if type(g).__name__ == "FpRect"]
    assert fab and all(g.width == FAB_WIDTH_MM for g in fab)
    xs = [x for g in _graphics(fp, "F.SilkS") if type(g).__name__ in ("FpRect", "FpLine")
          for x in (g.start[0], g.end[0])]
    assert min(xs) == pytest.approx(-10.0 - SILK_OUTSIDE_FAB_MM)
    courtyard, = _graphics(fp, "F.CrtYd")
    assert all(_on_grid(v) for v in (*courtyard.start, *courtyard.end))


def test_package_courtyard_is_rounded_outwards():
    geometry = PackageGeometry(name="P", body_length_mm=3.003, body_width_mm=2.0, pins=[
        PinSpec("1", -1.0, 0, 0.6, 0.6), PinSpec("2", 1.0, 0, 0.6, 0.6)])
    courtyard, = _graphics(build_package(geometry), "F.CrtYd")
    assert courtyard.start[0] <= -3.003 / 2 - 0.25 and _on_grid(courtyard.start[0])
    assert courtyard.end[0] >= 3.003 / 2 + 0.25 and _on_grid(courtyard.end[0])


# ── 10. The pin-1 pad of a header numbered A1, A2... ──────────────────────

def test_the_square_pad_follows_the_pin_one_marker_rule():
    # CONTRACT CHANGED (generality audit 2026-11-03, GA8): A1 is pin 1 only
    # on a real grid (two row letters, two columns); one lettered row is not.
    grid = build_pin_header(PinHeaderParams(rows=2, cols=2, pin_map={
        "A1": (0, 0), "A2": (0, 1), "B1": (1, 0), "B2": (1, 1)}))
    assert _pad(grid, "A1").shape == "rect"
    assert all(_pad(grid, n).shape == "circle" for n in ("A2", "B1", "B2"))
    row = build_pin_header(PinHeaderParams(rows=1, cols=3, pin_map={"A1": (0, 0), "A2": (0, 1), "A10": (0, 2)}))
    assert all(_pad(row, n).shape == "circle" for n in ("A1", "A2", "A10"))
    numbered = build_pin_header(PinHeaderParams(rows=1, cols=2))
    assert _pad(numbered, "1").shape == "rect" and _pad(numbered, "2").shape == "circle"
