"""@brief Glyph palette of the symbol editor: KiCad library reading, placement, export.

The KiCad library test runs only when KiCad is installed (its symbols folder
is found); the parser and placement tests use text fixtures.
"""
import pytest
from pathlib import Path

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


def test_symbol_block_cuts_exactly_one_balanced_symbol(tmp_path):
    """Only the wanted block is parsed now (user, 2026-10-12: 18 s to the window)."""
    text = RELAY_FIXTURE
    block = g.symbol_block(text, "Relay_X")
    assert block is not None and block.startswith('(symbol "Relay_X"') and block.endswith(")")
    (form,) = g.parse_sexpr(block)
    assert form[1] == "Relay_X" and [u[1] for u in g._children(form, "symbol")] == ["Relay_X_0_1", "Relay_X_1_1"]
    # A longer name sharing the prefix is not the symbol, and a unit is not
    # either: only forms at depth 1 qualify (review, 2026-10-12).
    assert g.symbol_block(text, "Relay_X_1_1") is None
    assert g.symbol_block(text, "Relay") is None
    assert g.symbol_block(text, "Missing") is None
    # A top-level symbol named like an earlier symbol's unit is found past
    # that unit, not confused with it.
    clash = ('(kicad_symbol_lib (symbol "A" (property "Value" "v") (symbol "A_1_1" (pin)))'
             ' (symbol "A_1_1" (property "Value" "w") (symbol "A_1_1_1_1")))')
    assert g.symbol_block(clash, "A_1_1") == '(symbol "A_1_1" (property "Value" "w") (symbol "A_1_1_1_1"))'
    # A name with a quote is matched in its escaped file spelling.
    quoted = '(kicad_symbol_lib (symbol "A\\"B" (property "Value" "q")))'
    assert g.symbol_block(quoted, 'A"B') == '(symbol "A\\"B" (property "Value" "q"))'
    # A library truncated after a symbol still serves the intact symbols.
    assert g.symbol_block('(kicad_symbol_lib (symbol "A" (pin)) (((', "A") == '(symbol "A" (pin))'
    assert g.symbol_block('(kicad_symbol_lib (symbol "A" (pin)) ) ) (symbol "B")', "B") is None, \
        "past an unbalanced close nothing is indexed"
    spans = g.top_level_symbol_spans(clash)
    assert list(spans) == ["A", "A_1_1"] and all(clash[a:b].startswith("(symbol") for a, b in spans.values())
    # Parentheses and escaped quotes inside strings do not unbalance the scan.
    tricky = '(kicad_symbol_lib (symbol "A" (property "Datasheet" "x(y\\"z)" ) (symbol "A_1_1")) (symbol "B"))'
    assert g.symbol_block(tricky, "A") == '(symbol "A" (property "Datasheet" "x(y\\"z)" ) (symbol "A_1_1"))'
    assert g.symbol_block(tricky, "B") == '(symbol "B")'
    assert g.symbol_block('(symbol "A" (open', "A") is None, "unbalanced: None, no exception"
    # Not a symbol library: nothing, even if a block would match.
    other = tmp_path / "other.kicad_sym"
    other.write_text('(kicad_pcb (symbol "Relay_X"))', encoding="utf-8")
    assert g.load_library_glyph(other, "Relay_X") is None


def test_each_library_file_is_read_once_for_all_its_glyphs(tmp_path, monkeypatch):
    """Structural: the catalogue asks many glyphs per file; the file is read once."""
    library = tmp_path / "Relay.kicad_sym"
    library.write_text(RELAY_FIXTURE, encoding="utf-8")
    g._library_text.cache_clear()
    reads = []
    original = Path.read_text

    def counting(self, *args, **kwargs):
        reads.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counting)
    try:
        for _ in range(5):
            assert g.load_library_glyph(library, "Relay_X") is not None
        assert len(reads) == 1
        # A changed file (new stamp) is read again, not served stale.
        library.write_text(RELAY_FIXTURE.replace("Relay_X", "Relay_Y"), encoding="utf-8")
        import os
        os.utime(library, ns=(library.stat().st_atime_ns, library.stat().st_mtime_ns + 10_000_000))
        assert g.load_library_glyph(library, "Relay_X") is None and len(reads) == 2
    finally:
        g._library_text.cache_clear()


@pytest.mark.skipif(g.kicad_symbols_dir() is None, reason="KiCad not installed")
def test_the_block_read_matches_a_full_parse_on_the_installed_libraries():
    """Equivalence with the old whole-file parse, on EVERY top-level symbol of
    the catalogue's libraries (each library parsed once: the whole-file
    parse is the slow path this change removed)."""
    folder = g.kicad_symbols_dir()
    libraries = sorted({library for _k, _l, _g, library, _s in g.KICAD_GLYPHS})
    checked = 0
    for library in libraries:
        path = folder / f"{library}.kicad_sym"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        root = g.parse_sexpr(text)[0]
        for full in g._children(root, "symbol"):
            (fast,) = g.parse_sexpr(g.symbol_block(text, full[1]))
            assert fast == full, (library, full[1])
            checked += 1
    assert checked >= len(g.KICAD_GLYPHS)


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


# ── Anchored wires (user, 2026-10-10) ───────────────────────────────────────

def _two_terminal_glyph():
    """A bar with a terminal on each end: pin outer end 3 mm out, 2 mm long, pointing inwards."""
    return g.Glyph("t", "t", (g.Primitive("polyline", ((-1, 0), (1, 0))),),
                   (g.Terminal(-3, 0, 0, 2.0, "A", "1"), g.Terminal(3, 0, 180, 2.0, "B", "2")), "builtin")


def _wire(points):
    return {"key": "wire", "label": "Filo", "source": "wire", "x": 0.0, "y": 0.0, "rotation": 0,
            "mirror": False, "terminals": [],
            "primitives": [{"kind": "polyline", "points": points, "radius": 0.0, "fill": "none", "width": 0.254}]}


def _layout_with_pins():
    from kicadgen.generators.symbol import layout_symbol
    return layout_symbol([SymbolPin("1", "A"), SymbolPin("2", "B", side="right"), SymbolPin("3", "C", side="top")])


def _legacy_record():
    return {"x": 0, "y": 0, "primitives": [{"kind": "polyline", "points": [[0, 0], [1, 0]]}]}


def test_placed_records_get_a_stable_id_and_legacy_records_are_given_one():
    glyph = _two_terminal_glyph()
    first, second = g.instance_from_glyph(glyph, 0, 0), g.instance_from_glyph(glyph, 5, 0)
    assert first["id"] and second["id"] and first["id"] != second["id"]
    legacy = _legacy_record()
    duplicate = dict(first)
    records = [first, legacy, duplicate]
    assert g.ensure_instance_ids(records) is records
    assert first["id"] == records[0]["id"], "an existing id is kept"
    assert legacy["id"] and duplicate["id"] != first["id"], "a missing or repeated id gets a fresh one"
    # Rows carry the same list: ids are assigned positionally so the rows keep agreeing.
    rows = [{"pin": "1", "symbol_graphics": [_legacy_record()]}, {"pin": "2", "symbol_graphics": [_legacy_record()]}]
    ident = g.symbol_graphics(rows)[0]["id"]
    assert rows[0]["symbol_graphics"][0]["id"] == ident == rows[1]["symbol_graphics"][0]["id"]
    assert g.symbol_graphics(rows)[0]["id"] == ident, "a second reading keeps the id"
    with pytest.raises(ValueError):
        g.validate_instances([{"id": 7, "x": 0, "y": 0, "primitives": [{}]}])


def test_pin_and_terminal_inner_ends_follow_the_pin_direction():
    layout = _layout_with_pins()
    ends = g.pin_inner_ends(layout)
    left, right, top = ([p for p in layout.pins if p.pin.number == n][0] for n in ("1", "2", "3"))
    assert ends["1"] == (round(left.x + layout.pin_length, 4), left.y), "angle 0 points right"
    assert ends["2"] == (round(right.x - layout.pin_length, 4), right.y), "angle 180 points left"
    assert ends["3"] == (top.x, round(top.y - layout.pin_length, 4)), "angle 270 points down"
    assert g.terminal_inner_end(g.Terminal(0, 5, 270, 2.54)) == (0, 2.46)
    assert g.terminal_inner_end(g.Terminal(0, -5, 90, 2.54)) == (0, -2.46)


def test_anchor_for_point_picks_the_nearest_within_tolerance_and_none_beyond():
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)   # inner ends at (-1, 0) and (1, 0)
    pin_ends = {"7": (1.5, 0.0)}
    anchor, point = g.anchor_for_point([element], pin_ends, 0.9, 0.2)
    assert anchor == {"kind": "terminal", "element": element["id"], "index": 1, "end": "inner"} and point == (1, 0)
    anchor, point = g.anchor_for_point([element], pin_ends, 1.45, 0.0)
    assert anchor == {"kind": "pin", "number": "7"} and point == (1.5, 0.0), "the pin is nearer"
    assert g.anchor_for_point([element], pin_ends, 0.0, 3.0) is None, "beyond the tolerance"
    assert g.anchor_for_point([element], None, -1.0, 1.0, tolerance=0.5) is None
    unidentified = {k: v for k, v in element.items() if k != "id"}
    assert g.anchor_for_point([unidentified], None, 1.0, 0.0) is None, "no id: nothing to anchor to"


def test_a_wire_anchored_to_a_terminal_follows_the_element_while_a_fixed_point_stays():
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)
    wire = _wire([{"x": 1.0, "y": 0.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 1}},
                  [4.0, 0.0]])
    graphics = [element, wire]
    assert g.resolve_wire_points(wire, graphics, None) == [(1, 0), (4, 0)]
    element["x"], element["y"] = 10.0, 5.0                      # move: the anchored end follows
    assert g.resolve_wire_points(wire, graphics, None) == [(11, 5), (4, 0)]
    element["rotation"] = 90                                      # turn: terminal B goes up
    assert g.resolve_wire_points(wire, graphics, None) == [(10, 6), (4, 0)]
    element["rotation"], element["mirror"] = 0, True              # mirror: terminal B goes left
    assert g.resolve_wire_points(wire, graphics, None) == [(9, 5), (4, 0)]
    # placed_primitives with the graphics resolves; without them it keeps the stored x/y.
    assert g.placed_primitives(wire, graphics=graphics)[0].points == ((9, 5), (4, 0))
    assert g.placed_primitives(wire)[0].points == ((1, 0), (4, 0))
    assert wire["primitives"][0]["points"][0]["x"] == 1.0, "resolution never rewrites the record"


def test_a_wire_anchored_to_a_pin_follows_the_pin_ends_and_falls_back_when_gone():
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)
    wire = _wire([{"x": -5.0, "y": 0.0, "anchor": {"kind": "pin", "number": "3"}},
                  {"x": 1.0, "y": 0.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 1}}])
    assert g.resolve_wire_points(wire, [element, wire], {"3": (-7.62, 2.54)}) == [(-7.62, 2.54), (1, 0)]
    assert g.resolve_wire_points(wire, [element, wire], {"3": (-7.62, 5.08)})[0] == (-7.62, 5.08)
    # Pin removed, element removed, terminal index out of range: the stored x/y stand in.
    assert g.resolve_wire_points(wire, [element, wire], {"4": (0, 0)})[0] == (-5, 0)
    assert g.resolve_wire_points(wire, [wire], None) == [(-5, 0), (1, 0)]
    wire["primitives"][0]["points"][1]["anchor"]["index"] = 9
    assert g.resolve_wire_points(wire, [element, wire], None)[1] == (1, 0)


def test_deleting_an_element_or_a_pin_freezes_the_anchored_wire_points_in_place():
    element = g.instance_from_glyph(_two_terminal_glyph(), 10, 5)
    other = g.instance_from_glyph(_two_terminal_glyph(), 0, -10)
    wire = _wire([{"x": 0.0, "y": 0.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 0}},
                  {"x": 0.0, "y": 0.0, "anchor": {"kind": "pin", "number": "2"}},
                  {"x": 0.0, "y": 0.0, "anchor": {"kind": "terminal", "element": other["id"], "index": 1}}])
    graphics = [element, other, wire]
    pin_ends = {"2": (20.0, 0.0)}
    g.freeze_wire_anchors(graphics, element["id"], pin_ends)
    points = wire["primitives"][0]["points"]
    assert points[0] == [9, 5], "fixed where the terminal was"
    assert isinstance(points[1], dict) and isinstance(points[2], dict), "other anchors untouched"
    del graphics[0]
    assert g.resolve_wire_points(wire, graphics, pin_ends) == [(9, 5), (20, 0), (1, -10)]
    g.freeze_pin_anchors(graphics, "2", pin_ends)
    assert points[1] == [20, 0] and isinstance(points[2], dict)
    assert g.resolve_wire_points(wire, graphics, None) == [(9, 5), (20, 0), (1, -10)]


def test_the_export_carries_the_resolved_wire_coordinates():
    layout = _layout_with_pins()
    ends = g.pin_inner_ends(layout)
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)
    element["x"] = 2.54                                           # moved after the wire was drawn
    wire = _wire([{"x": -99.0, "y": -99.0, "anchor": {"kind": "pin", "number": "1"}},
                  {"x": -99.0, "y": -99.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 0}},
                  [0.0, 2.54]])
    text = serialize_symbol("Part", [SymbolPin("1", "A"), SymbolPin("2", "B", side="right"),
                                     SymbolPin("3", "C", side="top")], graphics=[element, wire])
    unit = text.split('(symbol "Part_0_1"')[1].split('(symbol "Part_1_1"')[0]
    x1, y1 = ends["1"]
    assert f"(polyline (pts (xy {g._num(x1)} {g._num(y1)}) (xy 1.54 0) (xy 0 2.54))" in unit
    assert "-99" not in unit
    assert "(xy 1.54 0) (xy 3.54 0)" in unit, "the element's own bar is where the element is"


def test_legacy_wires_without_anchors_and_malformed_anchors():
    wire = _wire([[0.0, 0.0], [2.54, 0.0], [2.54, 2.54]])
    assert g.validate_instances([wire]) == [wire]
    assert g.resolve_wire_points(wire, [wire], {"1": (0, 0)}) == [(0, 0), (2.54, 0), (2.54, 2.54)]
    assert g.placed_primitives(wire, graphics=[wire])[0].points == ((0, 0), (2.54, 0), (2.54, 2.54))
    assert "(polyline (pts (xy 0 0) (xy 2.54 0) (xy 2.54 2.54))" in "\n".join(g.serialize_graphics([wire]))
    for bad in ({"x": 0, "y": 0, "anchor": {"kind": "nowhere"}},
                {"x": 0, "y": 0, "anchor": {"kind": "terminal", "element": 3, "index": 0}},
                {"x": 0, "y": 0, "anchor": {"kind": "terminal", "element": "a", "index": -1}},
                {"x": 0, "y": 0, "anchor": {"kind": "pin", "number": 3}},
                {"x": float("inf"), "y": 0}, [1.0], [0.0, float("nan")]):
        with pytest.raises(ValueError):
            g.validate_instances([_wire([bad, [0.0, 0.0]])])


# ── Adversarial review of the anchored wires (2026-10-10) ───────────────────

def test_refresh_writes_the_resolved_position_as_the_fallback_and_keeps_the_anchor():
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)
    wire = _wire([{"x": -99.0, "y": -99.0, "anchor": {"kind": "pin", "number": "1"}},
                  {"x": -99.0, "y": -99.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 1}},
                  {"x": 7.0, "y": 7.0, "anchor": {"kind": "pin", "number": "gone"}},
                  [4.0, 0.0]])
    graphics = [element, wire]
    g.refresh_wire_anchors(graphics, {"1": (-7.62, 2.54)})
    points = wire["primitives"][0]["points"]
    assert (points[0]["x"], points[0]["y"]) == (-7.62, 2.54) and points[0]["anchor"] == {"kind": "pin", "number": "1"}
    assert (points[1]["x"], points[1]["y"]) == (1.0, 0.0) and points[1]["anchor"]["kind"] == "terminal"
    assert (points[2]["x"], points[2]["y"]) == (7.0, 7.0), "an anchor that does not resolve keeps its fallback"
    assert points[3] == [4.0, 0.0], "free points are not touched"
    # Positive: with the pin gone, the wire end is where the pin last was.
    assert g.resolve_wire_points(wire, graphics, None)[0] == (-7.62, 2.54)
    # Negative: with the pin present the anchor still wins over the fallback.
    assert g.resolve_wire_points(wire, graphics, {"1": (-7.62, 5.08)})[0] == (-7.62, 5.08)


def test_the_fallback_lives_in_the_wire_frame_so_a_moved_wire_keeps_it_in_place():
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)
    wire = _wire([{"x": 0.0, "y": 0.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 1}},
                  [4.0, 0.0]])
    wire["x"], wire["y"], wire["rotation"] = 2.54, 5.08, 90        # the wire itself was moved and turned
    graphics = [element, wire]
    assert g.wire_local_point(wire, 1.0, 0.0) == (-5.08, 1.54)
    assert g.resolve_wire_points(wire, graphics, None)[0] == (1.0, 0.0), "the anchor is absolute"
    g.refresh_wire_anchors(graphics, None)
    point = wire["primitives"][0]["points"][0]
    assert (point["x"], point["y"]) == (-5.08, 1.54), "stored in the wire frame"
    assert g.resolve_wire_points(wire, [wire], None)[0] == (1.0, 0.0), "element gone: same absolute place"
    g.freeze_wire_anchors(graphics, element["id"], None)
    assert wire["primitives"][0]["points"][0] == [-5.08, 1.54]
    assert g.resolve_wire_points(wire, [wire], None)[0] == (1.0, 0.0), "frozen where it was, not transformed twice"


def test_anchor_direction_reads_the_pin_or_the_turned_terminal_and_none_otherwise():
    layout = _layout_with_pins()
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)
    assert g.anchor_direction({"kind": "pin", "number": "1"}, [element], layout) == 0, "left pin points right"
    assert g.anchor_direction({"kind": "pin", "number": "2"}, [element], layout) == 180
    assert g.anchor_direction({"kind": "pin", "number": "3"}, [element], layout) == 270, "top pin points down"
    assert g.anchor_direction({"kind": "terminal", "element": element["id"], "index": 1}, [element], layout) == 180
    element["rotation"] = 90
    assert g.anchor_direction({"kind": "terminal", "element": element["id"], "index": 1}, [element], layout) == 270
    assert g.anchor_direction({"kind": "pin", "number": "9"}, [element], layout) is None
    assert g.anchor_direction({"kind": "pin", "number": "1"}, [element], None) is None
    assert g.anchor_direction({"kind": "terminal", "element": "nope", "index": 0}, [element], layout) is None
    assert g.anchor_direction({"kind": "terminal", "element": element["id"], "index": 5}, [element], layout) is None
    assert g.anchor_direction(None, [element], layout) is None


def test_a_precomputed_elements_map_gives_the_same_resolution():
    element = g.instance_from_glyph(_two_terminal_glyph(), 3, 0)
    wire = _wire([{"x": 0.0, "y": 0.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 0}},
                  [9.0, 9.0]])
    graphics = [element, wire]
    by_id = g.elements_by_id(graphics)
    assert set(by_id) == {element["id"]}, "wires are not anchorable"
    assert g.resolve_wire_points(wire, graphics, None, by_id) == g.resolve_wire_points(wire, graphics, None)
    assert g.placed_primitives(wire, graphics, None, by_id)[0].points == ((2.0, 0.0), (9.0, 9.0))


def test_the_terminal_dot_is_anchorable_and_an_anchor_without_end_means_inner():
    """User, 2026-10-10: a wire drawn to the coil's dot (outer end) must follow the coil."""
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)   # dots at (-3, 0) and (3, 0)
    anchor, point = g.anchor_for_point([element], None, 2.9, 0.1)
    assert anchor == {"kind": "terminal", "element": element["id"], "index": 1, "end": "outer"}
    assert point == (3, 0)
    outer = _wire([{"x": 3.0, "y": 0.0, "anchor": anchor}, [6.0, 0.0]])
    legacy = _wire([{"x": 1.0, "y": 0.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 1}},
                    [6.0, 0.0]])
    moved = dict(element, x=2.0)
    assert g.resolve_wire_points(outer, [moved, outer], None)[0] == (5.0, 0.0), "the dot moved with the element"
    assert g.resolve_wire_points(legacy, [moved, legacy], None)[0] == (3.0, 0.0), "no end = inner, as saved before"
    rotated = dict(element, rotation=90)
    assert g.resolve_wire_points(outer, [rotated, outer], None)[0] == (0.0, 3.0)
    assert g.anchor_direction(anchor, [rotated], None) == 270, "the terminal's own axis, turned"
    g.validate_instances([outer, legacy])
    bad = _wire([{"x": 3.0, "y": 0.0, "anchor": dict(anchor, end="middle")}, [6.0, 0.0]])
    with pytest.raises(ValueError):
        g.validate_instances([bad])


def test_free_points_on_an_attachment_are_adopted_as_anchors_and_others_stay_free():
    """Migration of wires drawn before anchors existed: exact coincidence only."""
    element = g.instance_from_glyph(_two_terminal_glyph(), 0, 0)
    pin_ends = {"1": (-6.0, 4.0)}
    wire = _wire([[-6.0, 4.0], [-6.0, 0.0], [-3.0, 0.0]])          # pin end, corner, dot
    near = _wire([[1.0, 0.3], [6.0, 0.3]])                           # 0.3 mm from the inner end: not on it
    already = _wire([{"x": 1.0, "y": 0.0, "anchor": {"kind": "terminal", "element": element["id"], "index": 1}},
                     [6.0, 0.0]])
    graphics = [element, wire, near, already]
    assert g.adopt_coincident_anchors(graphics, pin_ends) == 2
    points = wire["primitives"][0]["points"]
    assert points[0] == {"x": -6.0, "y": 4.0, "anchor": {"kind": "pin", "number": "1"}}
    assert points[1] == [-6.0, 0.0], "a corner stays free"
    assert points[2]["anchor"] == {"kind": "terminal", "element": element["id"], "index": 0, "end": "outer"}
    assert near["primitives"][0]["points"] == [[1.0, 0.3], [6.0, 0.3]]
    assert already["primitives"][0]["points"][0]["anchor"] == {"kind": "terminal", "element": element["id"], "index": 1}
    assert g.adopt_coincident_anchors(graphics, pin_ends) == 0, "idempotent"
    moved = dict(element, x=1.0)
    assert g.resolve_wire_points(wire, [moved, wire], pin_ends)[2] == (-2.0, 0.0), "the adopted end follows"


# ── Shape tools and beziers (user, 2026-10-12: «tracciare disegni, linee curve») ──

BEZIER_FIXTURE = """(kicad_symbol_lib (version 20231120) (generator kicad_symbol_editor)
  (symbol "Curve_X" (in_bom yes)
    (symbol "Curve_X_0_1"
      (bezier (pts (xy 0 0) (xy 1 2) (xy 3 -1) (xy 5.08 0)) (stroke (width 0.254) (type default)) (fill (type none)))
      (bezier (pts (xy 0 0) (xy 1 2)) (stroke (width 0.254) (type default)) (fill (type none)))
    )
  )
)
"""


def test_a_bezier_is_read_from_the_library_and_written_back_the_same_way():
    (library,) = g.parse_sexpr(BEZIER_FIXTURE)
    unit = g._child(g._child(library, "symbol"), "symbol")
    primitives = g.primitives_of(unit)
    assert [p.kind for p in primitives] == ["bezier"], "a bezier needs exactly four points"
    assert primitives[0].points == ((0, 0), (1, 2), (3, -1), (5.08, 0)) and primitives[0].width == 0.254
    glyph = g.Glyph("t", "t", (primitives[0],), (), "builtin")
    text = serialize_symbol("Part", [SymbolPin("1", "A")], graphics=[g.instance_from_glyph(glyph, 1, 1)])
    assert "(bezier (pts (xy 1 1) (xy 2 3) (xy 4 0) (xy 6.08 1)) (stroke (width 0.254) (type default)) (fill (type none))" in text


def test_bezier_sampling_starts_and_ends_on_the_curve_ends_and_bulges_towards_the_controls():
    points = g.bezier_points((0, 0), (0, 1), (1, 1), (1, 0), segments=4)
    assert points[0] == (0, 0) and points[-1] == (1, 0) and len(points) == 5
    assert points[2] == (0.5, 0.75), "the midpoint of a symmetric cubic"
    assert g.primitive_outline(g.Primitive("bezier", ((0, 0), (0, 1), (1, 1), (1, 0))), 4) == points
    assert g.primitive_outline(g.Primitive("rectangle", ((0, 0), (2, 1)))) == [(0, 0), (2, 0), (2, 1), (0, 1), (0, 0)]
    assert g.primitive_outline(g.Primitive("polyline", ((0, 0), (2, 1)))) == [(0, 0), (2, 1)]


def test_a_shape_is_centred_on_its_bounds_so_turning_acts_about_itself():
    rect = g.shape_instance("rectangle", [(0, 0), (5.08, 2.54)])
    assert rect["source"] == "shape" and g.is_shape(rect) and not g.is_wire(rect)
    assert (rect["x"], rect["y"]) == (2.54, 1.27)
    assert rect["primitives"][0]["points"] == [[-2.54, -1.27], [2.54, 1.27]]
    rect["rotation"] = 90
    (p,) = g.placed_primitives(rect)
    assert sorted(p.points) == [(1.27, 3.81), (3.81, -1.27)], "turned about its own centre (2.54, 1.27)"
    circle = g.shape_instance("circle", [(2.54, 2.54), (5.08, 2.54)])
    assert circle["primitives"][0] == {"kind": "circle", "points": [[0.0, 0.0]], "radius": 2.54,
                                       "fill": "none", "width": g.SHAPE_WIDTH_MM, "style": "default"}
    arc = g.shape_instance("arc", [(0, 0), (5.08, 0), (2.54, 2.54)])
    assert arc["primitives"][0]["kind"] == "arc" and arc["primitives"][0]["points"][1] == [0.0, 1.27], \
        "clicked start, end, then the point between: stored start, mid, end"
    bezier = g.shape_instance("bezier", [(0, 0), (0, 2.54), (2.54, 2.54), (2.54, 0)], fill="outline")
    assert bezier["primitives"][0]["kind"] == "bezier" and bezier["primitives"][0]["fill"] == "outline"
    line = g.shape_instance("line", [(0, 0), (2.54, 0), (2.54, 2.54)])
    assert line["primitives"][0]["kind"] == "polyline" and len(line["primitives"][0]["points"]) == 3
    unit = serialize_symbol("Part", [SymbolPin("1", "A")], graphics=[rect, circle, arc, bezier, line])
    assert "(rectangle" in unit and "(circle" in unit and "(arc" in unit and "(bezier" in unit and "(polyline" in unit


@pytest.mark.parametrize("key, clicks", [
    ("rectangle", [(0, 0), (0, 2.54)]),          # no area
    ("rectangle", [(0, 0)]),                     # too few
    ("circle", [(1, 1), (1, 1)]),                # zero radius
    ("arc", [(0, 0), (2.54, 0), (0, 0)]),        # repeated point
    ("bezier", [(0, 0), (1, 1), (2, 2)]),        # too few
    ("line", [(0, 0)]),                          # too few
    ("unknown", [(0, 0), (1, 1)]),
])
def test_clicks_that_do_not_make_the_shape_give_nothing(key, clicks):
    assert g.shape_instance(key, clicks) is None


def test_the_style_of_an_element_is_changed_for_every_primitive_and_bad_values_refused():
    glyph = g.Glyph("t", "t", (g.Primitive("polyline", ((0, 0), (2, 0))),
                               g.Primitive("circle", ((1, 1),), 0.5)), (), "builtin")
    record = g.instance_from_glyph(glyph, 0, 0)
    assert g.set_primitive_style(record, width=0.5, fill="background")
    assert all(p["width"] == 0.5 and p["fill"] == "background" for p in record["primitives"])
    assert not g.set_primitive_style(record, fill="striped")
    assert not g.set_primitive_style(record, width=-1) and not g.set_primitive_style(record, width=True)
    assert not g.set_primitive_style(record) and not g.set_primitive_style({"primitives": []}, width=1)
    assert all(p["width"] == 0.5 and p["fill"] == "background" for p in record["primitives"]), "unchanged"


def test_every_shape_tool_has_a_palette_glyph_of_its_own_kind():
    glyphs = {glyph.key: glyph for glyph in g.shape_tool_glyphs()}
    assert set(glyphs) == {key for key, _l, _k, _c in g.SHAPE_TOOLS}
    for key, _label, kind, _count in g.SHAPE_TOOLS:
        assert glyphs[key].primitives[0].kind == kind and glyphs[key].terminals == ()


# ── Texts, stroke styles, handles, clipboard copies (user, 2026-10-12: «fai tutto») ──

TEXT_FIXTURE = """(kicad_symbol_lib (version 20211014) (generator kicad_symbol_editor)
  (symbol "Text_X" (in_bom yes)
    (symbol "Text_X_0_1"
      (text "IN+" (at 1.27 2.54 900) (effects (font (size 1.27 1.27)) (justify left)))
      (text "~" (at 0 0 0) (effects (font (size 2 2))))
      (polyline (pts (xy 0 0) (xy 2 0)) (stroke (width 0.254) (type dash)) (fill (type none)))
      (polyline (pts (xy 0 1) (xy 2 1)) (stroke (width 0.254) (type zigzag)) (fill (type none)))
    )
  )
)
"""


def test_texts_and_stroke_types_are_read_and_written_back():
    (library,) = g.parse_sexpr(TEXT_FIXTURE)
    unit = g._child(g._child(library, "symbol"), "symbol")
    text, tilde, dashed, unknown = g.primitives_of(unit)
    assert (text.kind, text.text, text.points, text.size, text.justify) == ("text", "IN+", ((1.27, 2.54),), 1.27, "left")
    assert text.angle == 90, "a KiCad angle in tenths of a degree is read as degrees"
    assert (tilde.angle, tilde.justify, tilde.size) == (0, "center", 2.0)
    assert dashed.style == "dash" and unknown.style == "default", "an unknown stroke type falls back"
    glyph = g.Glyph("t", "t", (text, dashed), (), "builtin")
    body = serialize_symbol("Part", [SymbolPin("1", "A")], graphics=[g.instance_from_glyph(glyph, 1, 1, rotation=90)])
    assert '(text "IN+" (at -1.54 2.27 0) (effects (font (size 1.27 1.27)) (justify left)))' in body, \
        "turned with the element: 90 + 90 = horizontal again, anchor turned about the origin"
    assert "(stroke (width 0.254) (type dash))" in body
    quoted = g.shape_instance("text", [(0, 0)], text='say "hi"')
    assert '(text "say \\"hi\\""' in serialize_symbol("Part", [SymbolPin("1", "A")], graphics=[quoted])


def test_a_text_record_is_validated_and_edited():
    record = g.shape_instance("text", [(2.54, 1.27)], text="  CLK ", size=1.27, justify="right")
    assert record["primitives"][0]["text"] == "CLK" and record["primitives"][0]["justify"] == "right"
    assert (record["x"], record["y"]) == (2.54, 1.27) and record["primitives"][0]["points"] == [[0.0, 0.0]]
    assert g.shape_instance("text", [(0, 0)], text="   ") is None
    assert g.shape_instance("text", [(0, 0)], text="a\nb") is None
    assert g.shape_instance("text", [(0, 0)], text="a", size=0) is None
    assert g.shape_instance("text", [(0, 0)], text="a", justify="top") is None
    assert g.set_text(record, "RST", 2.54, "center") and record["primitives"][0]["size"] == 2.54
    assert not g.set_text(record, "", 1.0) and not g.set_text(record, "x", -1)
    rect = g.shape_instance("rectangle", [(0, 0), (1, 1)])
    assert not g.set_text(rect, "x", 1.0), "only a text element"
    bad = dict(record, primitives=[dict(record["primitives"][0], text="  ")])
    with pytest.raises(ValueError):
        g.validate_instances([bad])
    assert g.set_primitive_style(rect, style="dot") and rect["primitives"][0]["style"] == "dot"
    assert not g.set_primitive_style(rect, style="zigzag")


def test_shape_handles_move_one_point_and_refuse_a_degenerate_result():
    rect = g.shape_instance("rectangle", [(0, 0), (5.08, 2.54)])
    assert g.shape_handles(rect) == [(0.0, 0.0), (5.08, 2.54)]
    assert g.set_shape_point(rect, 1, 7.62, 2.54) and g.shape_handles(rect) == [(0.0, 0.0), (7.62, 2.54)]
    assert not g.set_shape_point(rect, 1, 0.0, 5.0), "a rectangle without width"
    assert not g.set_shape_point(rect, 2, 1, 1) and g.shape_handles(rect) == [(0.0, 0.0), (7.62, 2.54)]
    rect["rotation"] = 90
    assert g.set_shape_point(rect, 1, 1.27, 6.35), "an absolute point is stored in the turned frame"
    assert g.shape_handles(rect)[1] == (1.27, 6.35)
    circle = g.shape_instance("circle", [(2.54, 2.54), (5.08, 2.54)])
    assert g.shape_handles(circle) == [(2.54, 2.54), (5.08, 2.54)]
    assert g.set_shape_point(circle, 1, 7.62, 2.54) and circle["primitives"][0]["radius"] == 5.08
    assert g.set_shape_point(circle, 0, 0.0, 0.0) and (circle["x"], circle["y"]) == (0.0, 0.0)
    assert not g.set_shape_point(circle, 1, 0.0, 0.0), "zero radius"
    text = g.shape_instance("text", [(1, 1)], text="A")
    assert g.shape_handles(text) == [(1.0, 1.0)] and g.set_shape_point(text, 0, 2.54, 0) and text["x"] == 2.54
    glyph = g.instance_from_glyph(g.catalogue()[0], 0, 0)
    assert g.shape_handles(glyph) == [] and not g.set_shape_point(glyph, 0, 1, 1), "glyphs have no handles"


def test_bounds_scaling_and_detached_copies():
    rect = g.shape_instance("rectangle", [(0, 0), (5.08, 2.54)])
    assert g.instance_bounds(rect) == (0.0, 0.0, 5.08, 2.54)
    assert g.scale_shape(rect, width=2.54) and g.instance_bounds(rect) == (1.27, 0.0, 3.81, 2.54), "about its origin"
    assert not g.scale_shape(rect, width=0) and not g.scale_shape(rect, radius=1)
    circle = g.shape_instance("circle", [(0, 0), (1, 0)])
    assert g.scale_shape(circle, radius=2.54) and circle["primitives"][0]["radius"] == 2.54
    assert not g.scale_shape(circle, width=1), "a circle takes a radius only"
    assert not g.scale_shape(g.shape_instance("text", [(0, 0)], text="A"), width=1)
    # A wire anchored to a glyph terminal is copied with its anchor frozen.
    glyph = g.instance_from_glyph(g.relay_part_glyphs()[0], 0, 0)
    end = g.terminal_end(g.placed_terminals(glyph)[0], "inner")
    wire = {"id": "w", "key": g.WIRE_KEY, "label": "Filo", "source": "wire", "x": 0.0, "y": 0.0,
            "rotation": 0, "mirror": False, "terminals": [],
            "primitives": [{"kind": "polyline", "radius": 0.0, "fill": "none", "width": 0.254,
                            "points": [[0, 0], {"x": 0, "y": 0, "anchor": {"kind": "terminal", "element": glyph["id"],
                                                                                "index": 0, "end": "inner"}}]}]}
    copy = g.detached_copy(wire, [glyph, wire], {})
    assert copy["id"] != wire["id"] and copy["primitives"][0]["points"][1] == [end[0], end[1]]
    assert "anchor" in wire["primitives"][0]["points"][1], "the original keeps its anchor"
