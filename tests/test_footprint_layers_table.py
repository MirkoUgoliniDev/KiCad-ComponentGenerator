"""@brief The per-layer defaults table of the footprint drawing layers (plan D5, B6).

One Qt-free table gives every footprint layer its KiCad colour, stroke width
and text size; the user drawing layers, the per-layer default width and the
courtyard width of the generators are read from it.
"""
import re

import pytest

from kicadgen.core import footprint as fpmod
from kicadgen.core.footprint_layers import LAYERS, default_text, default_width, drawable_layers, layer


def test_the_table_holds_the_klc_and_kicad_defaults_in_order():
    assert [entry.name for entry in LAYERS] == ["F.SilkS", "F.Fab", "Cmts.User", "Dwgs.User", "F.CrtYd"]
    assert (layer("F.SilkS").width_mm, layer("F.SilkS").text_mm, layer("F.SilkS").text_thickness_mm) == (0.12, 1.0, 0.15)
    assert layer("F.Fab").width_mm == 0.10 and layer("Cmts.User").width_mm == 0.10
    assert layer("Dwgs.User").width_mm == 0.10 and layer("F.CrtYd").width_mm == 0.05
    for entry in LAYERS:
        assert re.fullmatch(r"#[0-9a-f]{6}", entry.colour), entry
        assert entry.side == "front" and entry.label.strip()
    # KiCad 10 default theme (builtin_color_themes.h).
    assert layer("Dwgs.User").colour == "#c2c2c2" and layer("F.CrtYd").colour == "#ff26e2"


def test_only_the_courtyard_is_not_drawable_and_unknown_layers_have_no_entry():
    assert [entry.name for entry in drawable_layers()] == ["F.SilkS", "F.Fab", "Cmts.User", "Dwgs.User"]
    assert layer("F.CrtYd").drawable is False
    assert layer("F.Cu") is None and layer("") is None


def test_default_width_and_text_follow_the_layer_and_fall_back_to_the_silkscreen():
    assert default_width("F.Fab") == 0.10 and default_width("F.SilkS") == 0.12
    assert default_width("B.Cu") == 0.12, "unknown layer -> silkscreen default"
    assert default_text("Dwgs.User") == (1.0, 0.15) and default_text("nope") == (1.0, 0.15)


def test_the_entries_are_frozen():
    with pytest.raises(Exception):
        layer("F.Fab").width_mm = 0.2


def test_footprint_reads_the_table():
    assert fpmod.USER_DRAW_LAYERS == ("F.SilkS", "F.Fab", "Cmts.User", "Dwgs.User")
    assert fpmod.COURTYARD_WIDTH_MM == 0.05
    assert fpmod.USER_DRAW_WIDTH_MM == 0.12, "the name of records saved without width is kept"
    assert fpmod.user_draw_width("F.Fab") == 0.10 and fpmod.user_draw_width("F.SilkS") == 0.12
    assert fpmod.user_draw_width("Cmts.User") == 0.10


def test_a_dwgs_user_drawing_is_accepted_and_a_courtyard_drawing_is_not():
    rec = {"kind": "line", "layer": "Dwgs.User", "width": 0.1, "points": [[0, 0], [1, 0]]}
    assert fpmod.validate_footprint_drawings([rec]) == [rec]
    assert fpmod.validate_footprint_drawings([{**rec, "layer": "F.CrtYd"}]) == []


def test_the_generators_write_the_table_courtyard_width():
    from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
    fp = build_pin_header(PinHeaderParams(rows=1, cols=3))
    courtyards = [g for g in fp.graphics if getattr(g, "layer", "") == "F.CrtYd"]
    assert courtyards and all(g.width == fpmod.COURTYARD_WIDTH_MM for g in courtyards)
