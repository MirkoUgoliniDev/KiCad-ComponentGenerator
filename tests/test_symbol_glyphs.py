"""@brief Glyph palette of the symbol editor: KiCad library reading, placement, export.

The KiCad library test runs only when KiCad is installed (its symbols folder
is found); the parser and placement tests use text fixtures.
"""
import pytest

from kicadgen.generators import symbol_glyphs as g
from kicadgen.generators.symbol import serialize_symbol, SymbolPin

RELAY_FIXTURE = """(kicad_symbol_lib (version 20231120) (generator kicad_symbol_editor)
  (symbol "Relay_X" (in_bom yes)
    (property "Value" "Relay_X" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (symbol "Relay_X_0_1"
      (rectangle (start -10.16 5.08) (end 10.16 -5.08) (stroke (width 0.254) (type default)) (fill (type background)))
      (rectangle (start -8.255 1.905) (end -1.905 -1.905) (stroke (width 0.254) (type default)) (fill (type none)))
      (polyline (pts (xy -7.62 -1.905) (xy -2.54 1.905)) (stroke (width 0.254) (type default)) (fill (type none)))
      (circle (center 5.08 0) (radius 0.254) (stroke (width 0) (type default)) (fill (type outline)))
      (arc (start 1 1) (mid 2 1.5) (end 3 1) (stroke (width 0.254) (type default)) (fill (type none)))
    )
    (symbol "Relay_X_1_1"
      (pin passive line (at -5.08 7.62 270) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at -5.08 -7.62 90) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""


def test_the_parser_reads_nested_forms_numbers_and_escaped_strings():
    forms = g.parse_sexpr('(a 1 "two \\"quoted\\"" (b -2.5 sym) ())')
    assert forms == [["a", 1.0, 'two "quoted"', ["b", -2.5, "sym"], []]]
    with pytest.raises(ValueError):
        g.parse_sexpr("(a (b)")
    with pytest.raises(ValueError):
        g.parse_sexpr('(a "open')


def test_a_library_symbol_becomes_a_glyph_without_its_body_outline(tmp_path):
    library = tmp_path / "Relay.kicad_sym"
    library.write_text(RELAY_FIXTURE, encoding="utf-8")
    glyph = g.load_library_glyph(library, "Relay_X", "relay_x", "Relè X")
    assert glyph is not None and glyph.source == "kicad:Relay:Relay_X"
    assert [p.kind for p in glyph.primitives] == ["rectangle", "polyline", "circle", "arc"]
    assert glyph.body_hint == (20.32, 10.16), "the outline is dropped and kept as a size hint"
    assert [(t.x, t.y, t.angle, t.number) for t in glyph.terminals] == [
        (-5.08, 7.62, 270, "1"), (-5.08, -7.62, 90, "2")]
    assert g.load_library_glyph(library, "Missing") is None
    assert g.load_library_glyph(tmp_path / "none.kicad_sym", "Relay_X") is None


def test_placement_turns_and_mirrors_primitives_and_terminals_consistently():
    glyph = g.Glyph("t", "t", (g.Primitive("polyline", ((0, 0), (2, 0))),),
                    (g.Terminal(2, 0, 180, 2.54),), "builtin")
    turned = g.instance_from_glyph(glyph, 10, 5, 90)
    assert g.placed_primitives(turned)[0].points == ((10, 5), (10, 7))
    terminal = g.placed_terminals(turned)[0]
    assert (terminal.x, terminal.y, terminal.angle) == (10, 7, 270)
    mirrored = g.instance_from_glyph(glyph, 0, 0, 0, mirror=True)
    assert g.placed_primitives(mirrored)[0].points == ((0, 0), (-2, 0))
    assert g.placed_terminals(mirrored)[0].angle == 0, "a left-pointing pin now points right"
    # The record keeps the untransformed shape: moving twice never drifts.
    assert turned["primitives"][0]["points"] == [[0, 0], [2, 0]]


def test_a_dropped_pin_snaps_to_a_near_terminal_only():
    glyph = g.Glyph("t", "t", (g.Primitive("polyline", ((0, 0), (2, 0))),),
                    (g.Terminal(2, 0, 180, 2.54, "COM"),), "builtin")
    instance = g.instance_from_glyph(glyph, 0, 0)
    assert g.snap_to_terminal([instance], 2.5, 0.5).name == "COM"
    assert g.snap_to_terminal([instance], 5.0, 0.0) is None


def test_graphics_are_exported_inside_the_graphic_unit_of_the_symbol():
    glyph = g.Glyph("t", "t", (g.Primitive("polyline", ((0, 0), (2, 0)), fill="none", width=0.254),
                               g.Primitive("circle", ((1, 1),), 0.5, "outline", 0),
                               g.Primitive("arc", ((0, 0), (1, 1), (2, 0))),
                               g.Primitive("rectangle", ((0, 0), (1, 1)), fill="background")),
                    (), "builtin")
    text = serialize_symbol("Part", [SymbolPin("1", "A"), SymbolPin("2", "B", side="right")],
                            graphics=[g.instance_from_glyph(glyph, 1, 2)])
    unit = text.split('(symbol "Part_0_1"')[1].split('(symbol "Part_1_1"')[0]
    assert "(polyline (pts (xy 1 2) (xy 3 2))" in unit
    assert "(circle (center 2 3) (radius 0.5)" in unit and "(fill (type outline))" in unit
    assert "(arc (start 1 2) (mid 2 3) (end 3 2)" in unit
    assert "(rectangle (start 1 2) (end 2 3)" in unit
    with pytest.raises(ValueError):
        serialize_symbol("Part", [SymbolPin("1", "A")],
                         graphics=[{"x": float("nan"), "primitives": [{}]}])


def test_rows_carry_one_consistent_graphics_list():
    record = {"x": 0, "y": 0, "primitives": [{"kind": "polyline", "points": [[0, 0], [1, 0]]}]}
    rows = [{"pin": "1", "symbol_graphics": [dict(record)]}, {"pin": "2", "symbol_graphics": [dict(record)]}]
    assert len(g.symbol_graphics(rows)) == 1
    assert g.symbol_graphics([{"pin": "1"}]) == []
    rows[1]["symbol_graphics"] = []
    with pytest.raises(ValueError, match="incoerenti"):
        g.symbol_graphics(rows)


def test_the_builtin_set_stands_in_without_kicad(monkeypatch):
    monkeypatch.setattr(g, "kicad_symbols_dir", lambda: None)
    g.catalogue.cache_clear()
    try:
        keys = [glyph.key for glyph in g.catalogue()]
        assert "relay_coil" in keys and "relay_contact_spdt" in keys and "resistor" in keys
        assert all(glyph.source.startswith("builtin") for glyph in g.catalogue())
        assert all(glyph.terminals for glyph in g.catalogue() if glyph.key != "relay_link")
    finally:
        g.catalogue.cache_clear()


@pytest.mark.skipif(g.kicad_symbols_dir() is None, reason="KiCad not installed")
def test_the_installed_kicad_libraries_feed_the_catalogue():
    g.catalogue.cache_clear()
    try:
        glyphs = {glyph.key: glyph for glyph in g.catalogue()}
        assert glyphs["relay_spdt"].source.startswith("kicad:Relay:")
        assert len(glyphs["relay_spdt"].terminals) == 5
        assert glyphs["relay_spdt"].body_hint is not None
        assert glyphs["resistor"].source.startswith("kicad:Device:")
    finally:
        g.catalogue.cache_clear()


def test_the_relay_parts_are_always_offered_separately():
    """Coil, dashed link and the three contacts, KiCad installed or not (user, 2026-10-10)."""
    keys = [glyph.key for glyph in g.catalogue()]
    for key in ("relay_coil", "relay_link", "relay_contact_spdt", "relay_contact_no", "relay_contact_nc"):
        assert key in keys
    parts = {glyph.key: glyph for glyph in g.relay_part_glyphs()}
    assert [t.name for t in parts["relay_contact_spdt"].terminals] == ["COM", "NC", "NO"]
    assert [t.name for t in parts["relay_contact_no"].terminals] == ["COM", "NO"]
    assert [t.name for t in parts["relay_contact_nc"].terminals] == ["COM", "NC"]
    assert parts["relay_link"].terminals == ()
    # Every terminal lies on the 100 mil grid, so pins snapped there keep the grid.
    for glyph in parts.values():
        for t in glyph.terminals:
            assert abs(t.x / 2.54 - round(t.x / 2.54)) < 1e-6 and abs(t.y / 2.54 - round(t.y / 2.54)) < 1e-6


def test_every_catalogue_entry_has_a_known_group_and_the_grouping_covers_all():
    known = {key for key, _ in g.GROUPS}
    assert all(group in known for _k, _l, group, _lib, _sym in g.KICAD_GLYPHS)
    grouped = g.grouped_catalogue()
    assert sum(len(glyphs) for _k, _l, glyphs in grouped) == len(g.catalogue())
    assert grouped[0][0] == "relay" and grouped[0][2][0].key == "relay_coil"
