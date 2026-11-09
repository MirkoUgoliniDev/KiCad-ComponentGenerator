"""@brief Back layers, mirrored back texts, footprint anchor move and keepout zones (plan F1, F2, F4, 2026-11-09).

Core contracts of `core.footprint_layers` and `core.footprint`: B.SilkS and
B.Fab are drawable back layers; a text on them is written mirrored exactly as
KiCad writes it; `Footprint.shift_origin` moves every positioned item and is
an exact no-op at (0, 0); a ``zone`` record round-trips and is written in the
form of KiCad 10's own library. When kicad-cli is installed, KiCad loads the
file, keeps the zone and the mirror flags and draws the back text mirrored.
"""
import copy
import dataclasses
import inspect
import math
import re
import subprocess
from pathlib import Path

import pytest

from kicadgen.core import footprint as fpmod
from kicadgen.core.footprint import (ZONE_KEEPOUT_DEFAULTS, ZONE_KEEPOUT_FLAGS, FpArc, FpCircle, FpCurve,
                                     FpDimension, FpLine, FpPoly, FpRect, FpText, FpTextBox, FpZone, Footprint,
                                     Movable, Pad, drawing_bounds, drawing_centre, drawing_graphics,
                                     mirror_drawing, mirrored_justify, resize_drawing, translate_drawing,
                                     turn_drawing, validate_footprint_drawings)
from kicadgen.core.footprint_layers import COPPER_COLOURS, is_back_layer, layer
from kicadgen.generators.symbol_glyphs import parse_sexpr
from kicadgen.services.system_dependencies import find_kicad_cli

UUID = re.compile(r'\(uuid "[^"]*"\)')


def _masked(footprint: Footprint) -> str:
    """The rendered file with every uuid blanked (uuids are fresh on every render)."""
    return UUID.sub('(uuid "-")', footprint.render())


def _node(item) -> str:
    return UUID.sub('(uuid "-")', item.to_sexpr().render(0))


# ── F1: back layers ────────────────────────────────────────────────────────

def test_back_layers_take_the_klc_values_of_their_front_twins_and_kicad_colours():
    for back, front in (("B.SilkS", "F.SilkS"), ("B.Fab", "F.Fab")):
        b, f = layer(back), layer(front)
        assert (b.width_mm, b.text_mm, b.text_thickness_mm) == (f.width_mm, f.text_mm, f.text_thickness_mm)
        assert b.side == "back" and f.side == "front" and b.drawable
    # KiCad 10 s_defaultTheme: B_SilkS (232, 178, 167), B_Fab (88, 93, 132), F_Cu/B_Cu copper.
    assert layer("B.SilkS").colour == "#e8b2a7" and layer("B.Fab").colour == "#585d84"
    assert COPPER_COLOURS == {"F.Cu": "#c83434", "B.Cu": "#4d7fc4"}


@pytest.mark.parametrize("name, back", [("B.SilkS", True), ("B.Fab", True), ("B.Cu", True), ("B.CrtYd", True),
                                        ("F.SilkS", False), ("F.Fab", False), ("Cmts.User", False),
                                        ("Dwgs.User", False), ("F.Cu", False), ("", False)])
def test_is_back_layer_reads_the_table_side_then_kicad_naming(name, back):
    assert is_back_layer(name) is back


def test_a_back_drawing_record_is_kept_like_a_front_one():
    rec = {"kind": "line", "layer": "B.SilkS", "width": 0.12, "points": [[0.0, 0.0], [1.0, 0.0]]}
    assert validate_footprint_drawings([rec]) == [rec]
    assert validate_footprint_drawings([{**rec, "layer": "B.CrtYd"}]) == [], "the back courtyard is not drawable"


# ── F1: mirrored back texts ────────────────────────────────────────────────

def test_mirrored_justify_adds_mirror_last_on_a_back_layer_only():
    assert mirrored_justify("B.SilkS", ("left", "bottom")) == ("left", "bottom", "mirror")
    assert mirrored_justify("B.Fab", None) == ("mirror",)
    assert mirrored_justify("F.SilkS", ("left", "bottom")) == ("left", "bottom")
    assert mirrored_justify("F.SilkS", None) is None, "a front centred text keeps no justify node"


def test_a_back_text_is_written_mirrored_and_a_front_text_is_not():
    back = _node(FpText(kind="user", text="AB", at=(1, 2), layer="B.SilkS", justify=("left", "bottom")))
    front = _node(FpText(kind="user", text="AB", at=(1, 2), layer="F.SilkS", justify=("left", "bottom")))
    assert "(justify left bottom mirror)" in back
    assert "(justify left bottom)" in front and "mirror" not in front
    # A centred back text: the installed library's own form, «(justify mirror)».
    assert "(justify mirror)" in _node(FpText(kind="user", text="AB", at=(0, 0), layer="B.SilkS"))
    assert "justify" not in _node(FpText(kind="user", text="AB", at=(0, 0), layer="F.SilkS"))


def test_a_back_text_box_and_a_back_dimension_are_written_mirrored():
    box = FpTextBox(start=(0, 0), end=(5, 3), text="T", layer="B.Fab")
    assert "(justify left top mirror)" in _node(box)
    assert "mirror" not in _node(dataclasses.replace(box, layer="F.Fab"))
    for dtype in ("", "radial", "leader"):
        dim = FpDimension(start=(0, 0), end=(4, 1), height=1.0, layer="B.SilkS", dtype=dtype, text="x")
        assert "(justify mirror)" in _node(dim), dtype
        assert "mirror" not in _node(dataclasses.replace(dim, layer="F.SilkS")), dtype


def test_front_records_write_no_mirror_anywhere():
    records = validate_footprint_drawings([
        {"kind": "text", "layer": "F.SilkS", "points": [[0, 0]], "text": "A", "anchor": "left_bottom"},
        {"kind": "textbox", "layer": "F.Fab", "points": [[0, 0], [4, 2]], "text": "B"},
        {"kind": "dimension", "layer": "Cmts.User", "points": [[0, 0], [3, 0]], "offset": 1.0}])
    fp = Footprint(name="T")
    for item in drawing_graphics(records):
        fp.add_graphic(item)
    assert "mirror" not in fp.render()


# ── F2: shift_origin ───────────────────────────────────────────────────────

def _writable_classes():
    """Every class of core.footprint that writes itself into the file, except the container."""
    return [cls for _name, cls in inspect.getmembers(fpmod, inspect.isclass)
            if cls.__module__ == fpmod.__name__ and hasattr(cls, "to_sexpr") and cls is not Footprint]


def test_every_written_item_class_is_movable_and_lists_each_point_field():
    """Structural guard: a new graphic kind, or a new point field, that shift_origin would not move fails here."""
    classes = _writable_classes()
    assert {FpZone, FpText, FpTextBox, FpDimension, FpCurve, FpPoly, FpArc, FpCircle, FpRect, FpLine, Pad} <= set(classes)
    for cls in classes:
        assert issubclass(cls, Movable), cls.__name__
        listed = set(cls._POINT_FIELDS) | set(cls._POINT_LIST_FIELDS)
        annotated = {f.name for f in dataclasses.fields(cls) if "Point" in str(f.type)}
        assert annotated == listed, (cls.__name__, annotated, listed)


def test_footprint_point_fields_are_the_two_property_texts():
    """A new positioned field on Footprint itself must be added to shift_origin: this fails first."""
    annotated = {f.name for f in dataclasses.fields(Footprint) if "Point" in str(f.type)}
    assert annotated == {"reference_at", "value_at"}


def _every_kind() -> Footprint:
    from kicadgen.generators.castellated_module import OffsetDrillPad
    fp = Footprint(name="T", reference_at=(0.0, -3.0), value_at=(0.0, 3.0))
    fp.add_pad(Pad("1", "smd", "rect", (1.0, 2.0), (1.0, 0.5), ["F.Cu", "F.Mask"], rotation=30.0))
    fp.add_pad(OffsetDrillPad("2", "thru_hole", "oval", (4.0, 0.0), (2.0, 1.2), ["*.Cu", "*.Mask"],
                              drill=0.6, drill_offset=(0.3, 0.0)))
    for item in (FpLine((0, 0), (1, 1), "F.SilkS"), FpRect((0, 0), (2, 1), "F.Fab"),
                 FpCircle((1, 1), 0.5, "B.SilkS"), FpArc((0, 0), (1, 1), (2, 0), "F.SilkS"),
                 FpPoly([(0, 0), (1, 0), (0, 1)], "B.Fab"), FpCurve([(0, 0), (1, 2), (2, 2), (3, 0)], "F.SilkS"),
                 FpTextBox((0, 4), (3, 6), "T", "Cmts.User"), FpText("user", "U", (2, 2), "B.SilkS"),
                 FpDimension((0, 0), (3, 0), 1.0, "F.Fab"), FpDimension((0, 0), (0, 3), 2.0, "F.Fab", axis="y"),
                 FpDimension((1, 1), (2, 2), 0.0, "F.Fab", dtype="radial"),
                 FpDimension((1, 1), (2, 1), 0.0, "F.Fab", dtype="center"),
                 FpDimension((1, 1), (3, 3), 0.0, "F.Fab", dtype="leader", text="L"),
                 FpZone([(0, 0), (2, 0), (2, 2)], ("F.Cu", "B.Cu"))):
        fp.add_graphic(item)
    return fp


def test_shift_origin_moves_every_point_and_nothing_relative():
    fp = _every_kind()
    before = copy.deepcopy(fp)
    assert fp.shift_origin(1.5, -2.0) is fp
    for old, new in zip((*before.pads, *before.graphics), (*fp.pads, *fp.graphics)):
        assert [(x - 1.5, y + 2.0) for x, y in old.anchor_points()] == pytest.approx(new.anchor_points())
        for f in dataclasses.fields(old):
            if f.name not in set(old._POINT_FIELDS) | set(old._POINT_LIST_FIELDS):
                assert getattr(new, f.name) == getattr(old, f.name), (type(old).__name__, f.name)
    assert fp.pads[1].drill_offset == (0.3, 0.0) and fp.pads[0].rotation == 30.0
    assert fp.reference_at == pytest.approx((-1.5, -1.0)) and fp.value_at == pytest.approx((-1.5, 5.0))
    # The dimension values are translation invariant: the written texts are the same.
    texts = lambda f: re.findall(r'\(gr_text "([^"]*)"', f.render())  # noqa: E731
    assert texts(fp) == texts(before)


def test_shift_origin_at_zero_is_an_exact_no_op():
    fp = _every_kind()
    pads, graphics, rendered = fp.pads, fp.graphics, _masked(fp)
    fp.shift_origin(0, 0)
    fp.shift_origin(-0.0, 0.0)
    assert fp.pads is pads and fp.graphics is graphics, "nothing replaced"
    assert _masked(fp) == rendered


def test_shift_origin_then_back_restores_the_file():
    fp = _every_kind()
    rendered = _masked(fp)
    fp.shift_origin(2.54, 1.27).shift_origin(-2.54, -1.27)
    assert _masked(fp) == rendered, "every coordinate is formatted to 1 nm, so float noise vanishes"


@pytest.mark.parametrize("ax, ay", [(math.nan, 0), (0, math.inf), (True, 0), ("1", 0), (None, 0)])
def test_shift_origin_refuses_a_non_number_and_leaves_the_footprint(ax, ay):
    fp = _every_kind()
    rendered = _masked(fp)
    with pytest.raises(ValueError):
        fp.shift_origin(ax, ay)
    assert _masked(fp) == rendered


def test_shift_origin_refuses_a_footprint_kicad_could_not_hold():
    fp = _every_kind()
    rendered = _masked(fp)
    with pytest.raises(ValueError):
        fp.shift_origin(2200.0, 0.0)
    assert _masked(fp) == rendered, "refused before anything moved"
    fp.shift_origin(2000.0, 0.0)  # within the +-2147 mm reach: accepted


def test_shift_origin_refuses_an_item_it_cannot_move():
    class Opaque:
        def to_sexpr(self):
            raise AssertionError("never written")
    fp = _every_kind()
    fp.add_graphic(Opaque())
    with pytest.raises(TypeError):
        fp.shift_origin(1.0, 0.0)
    fp.shift_origin(0.0, 0.0)  # the no-op touches nothing, so it does not refuse


def _kicad_read_back(cli, fp, name, tmp_path):
    """Write `fp`, let kicad-cli re-save it (`fp upgrade`) and parse KiCad's own file: what KiCad READ."""
    library = tmp_path / f"{name}.pretty"
    library.mkdir()
    (library / f"{name}.kicad_mod").write_text(fp.render(), encoding="utf-8")
    upgraded = tmp_path / f"up{name}"
    done = subprocess.run([cli, "fp", "upgrade", "--force", "-o", str(upgraded), str(library)],
                          capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    return library, parse_sexpr(next(upgraded.rglob("*.kicad_mod")).read_text(encoding="utf-8"))[0]


def test_kicad_reads_the_shifted_footprint_at_the_moved_coordinates(tmp_path):
    """shift_origin(3, -2) moves every point by (-3, +2) in the file KiCad reads; the drawing keeps its extent.

    Contract (review 2026-11-09): the coordinates KiCad re-saves are asserted,
    so a shift_origin that moved nothing fails here; the svg extent over the
    front AND back silkscreen and fabrication layers and the copper stays
    the same, so nothing was moved relative to the rest.
    """
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    roots, sizes = {}, []
    for shift, name in (((0.0, 0.0), "A"), ((3.0, -2.0), "B")):
        fp = _every_kind()
        fp.name = name
        fp.shift_origin(*shift)
        library, roots[name] = _kicad_read_back(cli, fp, name, tmp_path)
        out = tmp_path / f"svg{name}"
        out.mkdir()
        done = subprocess.run([cli, "fp", "export", "svg", "--fp", name, "--layers",
                               "F.SilkS,B.SilkS,F.Fab,B.Fab,F.Cu,B.Cu", "-o", str(out), str(library)],
                              capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert done.returncode == 0, done.stderr
        svg = next(out.glob("*.svg")).read_text(encoding="utf-8")
        sizes.append(re.search(r'width="([\d.]+)mm" height="([\d.]+)mm"', svg).groups())
    assert sizes[0] == sizes[1]

    def pad_one_at(root):
        pad = next(n for n in root if isinstance(n, list) and n[0] == "pad" and n[1] == "1")
        return next(c for c in pad if isinstance(c, list) and c[0] == "at")[1:]

    def first_zone_xy(root):
        zone = next(n for n in root if isinstance(n, list) and n[0] == "zone")
        polygon = next(c for c in zone if isinstance(c, list) and c[0] == "polygon")
        return polygon[1][1][1:]

    # _every_kind: pad 1 at (1, 2) turned 30 degrees, zone's first corner at (0, 0).
    assert pad_one_at(roots["A"]) == pytest.approx([1.0, 2.0, 30.0])
    assert pad_one_at(roots["B"]) == pytest.approx([-2.0, 4.0, 30.0]), "the pad moved by (-3, +2), rotation kept"
    assert first_zone_xy(roots["A"]) == pytest.approx([0.0, 0.0])
    assert first_zone_xy(roots["B"]) == pytest.approx([-3.0, 2.0])


# ── F4: zone records ───────────────────────────────────────────────────────

ZONE = {"kind": "zone", "layers": ["F.Cu", "B.Cu"], "points": [[0, 0], [4, 0], [4, 3], [0, 3]],
        "keepout": {"tracks": True, "vias": True, "pads": False, "copperpour": True, "footprints": False}}


def test_a_zone_record_round_trips_with_every_flag_stored():
    clean = validate_footprint_drawings([ZONE])
    assert clean == [{**ZONE, "points": [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0]]}]
    assert validate_footprint_drawings(clean) == clean


def test_a_zone_without_flags_takes_kicad_rule_area_defaults():
    clean = validate_footprint_drawings([{**ZONE, "keepout": {}}])[0]
    assert clean["keepout"] == ZONE_KEEPOUT_DEFAULTS
    # KiCad 10 ZONE_SETTINGS(): tracks, vias, pads not allowed; zone fills and footprints allowed.
    assert ZONE_KEEPOUT_DEFAULTS == {"tracks": True, "vias": True, "pads": True,
                                     "copperpour": False, "footprints": False}
    assert tuple(clean["keepout"]) == ZONE_KEEPOUT_FLAGS


def test_zone_layers_are_normalised_to_kicad_order_and_unknown_sets_refused():
    assert validate_footprint_drawings([{**ZONE, "layers": ["B.Cu", "F.Cu"]}])[0]["layers"] == ["F.Cu", "B.Cu"]
    assert validate_footprint_drawings([{**ZONE, "layers": ["B.Cu"]}])[0]["layers"] == ["B.Cu"]
    for bad in (["F.SilkS"], ["F.Cu", "F.Cu"], [], ["In1.Cu"], "F.Cu", ["F.Cu", "B.Cu", "In1.Cu"], [1]):
        assert validate_footprint_drawings([{**ZONE, "layers": bad}]) == [], bad


@pytest.mark.parametrize("change", [
    {"points": [[0, 0], [4, 0]]},                         # two points: no polygon
    {"points": [[0, 0], [2, 0], [4, 0]]},                 # collinear: no area
    {"points": [[0, 0], [4, 0], [4, math.nan]]},          # not finite
    {"keepout": {"holes": True}},                         # unknown flag
    {"keepout": {"pads": 1}},                             # not a bool
    {"keepout": ["pads"]},                                # not a mapping
], ids=["two-points", "collinear", "nan", "unknown-flag", "int-flag", "list-flags"])
def test_a_malformed_zone_is_dropped(change):
    assert validate_footprint_drawings([{**ZONE, **change}]) == []


@pytest.mark.parametrize("points", [
    [[0, 0], [4, 4], [4, 0], [0, 4]],              # bow tie: two sides cross
    [[0, 0], [4, 0], [4, 4], [2, 0]],              # a corner lying on another side
    [[0, 0], [4, 0], [2, 0], [2, 3]],              # a side folding back over the previous one
    [[0, 0], [4, 0], [4, 3], [6, 3], [6, -1], [0, -1], [0, 1]],  # the closing side runs back over the previous one
], ids=["bow-tie", "corner-on-side", "fold-back", "closing-side-overlaps"])
def test_a_zone_whose_outline_crosses_itself_is_dropped(points):
    assert validate_footprint_drawings([{**ZONE, "points": points}]) == []
    assert not fpmod.polygon_is_simple(points)


@pytest.mark.parametrize("points", [
    [[0, 0], [6, 0], [6, 2], [2, 2], [2, 6], [0, 6]],   # concave L: no side meets a non-adjacent one
    [[0, 0], [4, 0], [2, 1], [4, 2], [0, 2]],           # concave notch reaching inside
    [[0, 0], [1, 0], [0, 1]],                           # the smallest polygon
], ids=["concave-l", "notch", "triangle"])
def test_a_simple_zone_outline_is_kept_even_when_concave(points):
    assert len(validate_footprint_drawings([{**ZONE, "points": points}])) == 1
    assert fpmod.polygon_is_simple(points)


def test_a_bow_tie_polygon_drawing_is_still_kept():
    """Only a ZONE must be simple: a crossed graphic polygon is a drawing KiCad shows as drawn."""
    bow = {"kind": "polygon", "layer": "F.SilkS", "width": 0.12, "points": [[0, 0], [4, 4], [4, 0], [0, 4]]}
    assert len(validate_footprint_drawings([bow])) == 1


# ── repeated vertices (review 2026-11-09) ──────────────────────────────────

@pytest.mark.parametrize("record, kept", [
    ({"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[0, 0], [0, 0], [1, 0], [1, 0.00002], [1, 1]]},
     [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]),
    # An open polyline may end where it starts: that is a closed outline drawn as a line, not a repeat.
    ({"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[0, 0], [1, 0], [1, 1], [0, 0]]},
     [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]),
    ({"kind": "polygon", "layer": "F.Fab", "width": 0.1, "points": [[0, 0], [2, 0], [2, 0], [2, 2], [0, 0]]},
     [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]]),
    ({**ZONE, "points": [[0, 0], [4, 0], [4, 0], [4, 3], [0, 3], [0, 0]]},
     [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0]]),
], ids=["line", "closed-line", "polygon", "zone"])
def test_a_repeated_vertex_is_dropped_and_the_record_kept(record, kept):
    clean = validate_footprint_drawings([record])
    assert len(clean) == 1 and clean[0]["points"] == kept


@pytest.mark.parametrize("record", [
    {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[1, 1], [1.00001, 1]]},
    {"kind": "polygon", "layer": "F.Fab", "width": 0.1, "points": [[0, 0], [2, 0], [2, 0], [0, 0]]},
    {**ZONE, "points": [[0, 0], [4, 0], [4, 0], [0, 0]]},
], ids=["line", "polygon", "zone"])
def test_a_record_left_with_too_few_vertices_is_dropped(record):
    assert validate_footprint_drawings([record]) == []


def test_a_held_record_with_a_repeat_never_writes_a_zero_length_line():
    """drawing_graphics collapses repeats itself: a record edited after validation writes no empty fp_line."""
    line = {"kind": "line", "layer": "F.SilkS", "width": 0.12, "points": [[0, 0], [1, 0], [1, 0], [1, 1]]}
    items = drawing_graphics([line])
    assert [(i.start, i.end) for i in items] == [((0, 0), (1, 0)), ((1, 0), (1, 1))]
    zone = drawing_graphics([{**validate_footprint_drawings([ZONE])[0],
                              "points": [[0, 0], [4, 0], [4, 0], [4, 3], [0, 3]]}])[0]
    assert len(zone.points) == 4


def test_a_zone_keeps_no_drawing_layer_or_width():
    clean = validate_footprint_drawings([{**ZONE, "layer": "F.SilkS", "width": 0.2, "fill": True}])[0]
    assert set(clean) == {"kind", "layers", "points", "keepout"}


def test_zone_transforms_keep_layers_and_flags_and_stay_valid():
    zone = validate_footprint_drawings([ZONE])[0]
    assert drawing_bounds(zone) == (0.0, 0.0, 4.0, 3.0) and drawing_centre(zone) == (2.0, 1.5)
    for moved in (turn_drawing(zone, True), turn_drawing(zone, False), mirror_drawing(zone),
                  translate_drawing(zone, 1.0, -1.0)):
        assert moved["layers"] == zone["layers"] and moved["keepout"] == zone["keepout"]
        assert validate_footprint_drawings([moved]) == [moved]
    assert turn_drawing(zone, True)["points"] != zone["points"]
    assert resize_drawing(zone, width=10.0) == zone, "a zone has no size to set: unchanged"


# ── F4: zone writer ────────────────────────────────────────────────────────

def test_zone_writer_uses_layer_for_one_side_and_layers_for_both():
    one = _node(FpZone([(0, 0), (1, 0), (0, 1)], ("B.Cu",)))
    both = _node(FpZone([(0, 0), (1, 0), (0, 1)], ("F.Cu", "B.Cu")))
    assert '(layer "B.Cu")' in one and "(layers" not in one
    assert '(layers "F.Cu" "B.Cu")' in both
    flags = _node(FpZone([(0, 0), (1, 0), (0, 1)], ("F.Cu",), {**ZONE_KEEPOUT_DEFAULTS, "footprints": True}))
    assert re.search(r"\(tracks not_allowed\).*\(vias not_allowed\).*\(pads not_allowed\)"
                     r".*\(copperpour allowed\).*\(footprints not_allowed\)", flags.replace("\n", " "))


@pytest.mark.parametrize("zone", [FpZone([(0, 0), (1, 0)], ("F.Cu",)), FpZone([(0, 0), (1, 0), (0, 1)], ("F.SilkS",)),
                                  FpZone([(0, 0), (1, 0), (0, 1)], ("F.Cu",), {"holes": True})],
                         ids=["two-points", "silk-layer", "unknown-flag"])
def test_zone_writer_refuses_what_kicad_could_not_read(zone):
    with pytest.raises(ValueError):
        zone.to_sexpr()


def test_zones_are_written_after_the_pads_and_graphics_keep_their_order():
    fp = Footprint(name="T")
    fp.add_graphic(FpZone([(0, 0), (1, 0), (0, 1)], ("F.Cu",)))
    fp.add_graphic(FpLine((0, 0), (1, 0), "F.SilkS"))
    fp.add_pad(Pad("1", "smd", "rect", (0, 0), (1, 1), ["F.Cu"]))
    heads = [node[0] for node in parse_sexpr(fp.render())[0] if isinstance(node, list)]
    assert heads.index("fp_line") < heads.index("pad") < heads.index("zone")
    assert fp.graphics[0].__class__ is FpZone, "the stored order is untouched"


def test_drawing_graphics_builds_one_fpzone_per_record():
    items = drawing_graphics(validate_footprint_drawings([ZONE]))
    assert len(items) == 1 and isinstance(items[0], FpZone)
    assert tuple(items[0].layers) == ("F.Cu", "B.Cu") and items[0].keepout == ZONE["keepout"]


def _library_zone():
    cli = find_kicad_cli()
    if not cli:
        return None
    path = (Path(cli).parent.parent / "share" / "kicad" / "footprints" / "Connector_PCBEdge.pretty"
            / "SODIMM-200_1.8V_Card_edge.kicad_mod")
    if not path.is_file():
        return None
    root = parse_sexpr(path.read_text(encoding="utf-8"))[0]
    return next(node for node in root if isinstance(node, list) and node[0] == "zone")


def _shape(node):
    """Heads of a node's children, recursively, without values: the form, not the numbers."""
    return [(child[0], _shape(child)) for child in node[1:] if isinstance(child, list)]


def test_zone_writer_matches_the_installed_library_form():
    library = _library_zone()
    if library is None:
        pytest.skip("KiCad footprint library not installed")
    ours = parse_sexpr(_node(FpZone([(0, 0), (1, 0), (1, 1), (0, 1)], ("F.Cu", "B.Cu"),
                                    {**ZONE_KEEPOUT_DEFAULTS, "copperpour": True})))[0]
    assert _shape(ours) == _shape(library)
    keep = lambda node: [c for c in node if isinstance(c, list) and c[0] in ("layers", "hatch", "min_thickness",  # noqa: E731
                                                                           "keepout", "fill", "connect_pads")]
    assert keep(ours) == keep(library), "same values: KiCad 10 defaults, the library's flags"


def test_kicad_loads_the_zone_and_the_mirrored_back_texts(tmp_path):
    """kicad-cli: the file parses, an upgrade rewrites the zone and the mirror flags, the back text is mirrored."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    fp = Footprint(name="T")
    fp.add_pad(Pad("1", "smd", "rect", (10, 0), (1, 1), ["F.Cu", "F.Mask"]))
    for item in drawing_graphics(validate_footprint_drawings([
            ZONE,
            {"kind": "text", "layer": "F.SilkS", "points": [[0, 0]], "text": "ABCD", "anchor": "left_bottom"},
            {"kind": "text", "layer": "B.SilkS", "points": [[0, 0]], "text": "WXYZ", "anchor": "left_bottom"},
            {"kind": "textbox", "layer": "B.Fab", "points": [[0, 5], [10, 8]], "text": "BOX"}])):
        fp.add_graphic(item)
    library = tmp_path / "t.pretty"
    library.mkdir()
    (library / "T.kicad_mod").write_text(fp.render(), encoding="utf-8")
    upgraded = tmp_path / "up"
    done = subprocess.run([cli, "fp", "upgrade", "--force", "-o", str(upgraded), str(library)],
                          capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    text = next(upgraded.rglob("*.kicad_mod")).read_text(encoding="utf-8")
    assert '(layers "F.Cu" "B.Cu")' in text and "(copperpour not_allowed)" in text and "(pads allowed)" in text
    assert "(justify left bottom mirror)" in text and "(justify left top mirror)" in text
    out = tmp_path / "svg"
    out.mkdir()
    done = subprocess.run([cli, "fp", "export", "svg", "--fp", "T", "--layers", "F.SilkS,B.SilkS",
                           "-o", str(out), str(library)], capture_output=True, timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert done.returncode == 0, done.stderr
    svg = next(out.glob("*.svg")).read_text(encoding="utf-8")

    def extent(word):
        body = re.search(r"<desc>" + word + r"</desc>(.*?)</g>", svg, re.S).group(1)
        xs = [float(x) for x in re.findall(r"[ML]([-\d.]+) [-\d.]+", body)]
        return min(xs), max(xs)
    (f0, f1), (b0, b1) = extent("ABCD"), extent("WXYZ")
    # Same anchor: the front text grows right of it, the mirrored back text left of it.
    assert b1 < f0
    anchor = (f0 + b1) / 2
    assert f0 - anchor == pytest.approx(anchor - b1, abs=0.05)
