"""@brief Bottom-left anchor of the footprint's REF** and Value texts (user, 2026-10-16).

The Reference and Value properties of the footprint preview take the anchor
the drawn texts got on 2026-10-14, replacing the centre cross of 2026-10-13:
the anchor is the bottom-left corner of the text (left edge, baseline), it
shows the drawn texts' handle square, a drag snaps it to the grid, the
stored position is that corner and the .kicad_mod writes the property with
``(justify left bottom)`` at it. The ``${REFERENCE}`` fabrication text stays
centred on the body, like every generated (not user-placed) text.

CONTRACT CHANGED 2026-10-16 (user decision «KiCad fermo», both tabs):
default positions and positions saved before (text centres) are converted
with KiCad's own stroke-font geometry (`kicadgen.core.kicad_text`, measured
with kicad-cli), so nothing moves IN KICAD; the preview, whose font is not
KiCad's, may show a converted text shifted once. Every generated footprint,
with or without the window, is written in the same bottom-left form.
"""
import re
import subprocess
from copy import deepcopy

import pytest

from kicadgen.core.footprint import (
    PROPERTY_TEXT_SIZE, PROPERTY_TEXT_THICKNESS, TEXT_KEYS, TEXT_POSITION_LIMIT_MM,
    TEXT_POSITIONS_ANCHOR_FIELD, USER_TEXT_ANCHOR, Footprint, FpLine, default_text_positions,
    drawing_graphics, fab_reference_text, migrate_centred_positions, property_corner_offset,
    text_positions_anchored, text_positions_json, validate_footprint_drawings, validate_text_positions,
)
from kicadgen.core.kicad_text import (
    KICAD_CENTRE_TO_BOTTOM, SCHEMATIC_DEFAULT_PEN_MM, kicad_text_margin, kicad_text_width,
)
from kicadgen.services.system_dependencies import find_kicad_cli


def _flat(text):
    """The writer pretty-prints: whitespace collapsed for substring checks."""
    return re.sub(r"\s+", " ", text).replace("( ", "(").replace(" )", ")")


def _property(text, key):
    """The flattened ``property`` node of `key` in a rendered footprint."""
    return _node(text, f'(property "{key}"')


def _node(text, prefix):
    """The flattened balanced node starting with `prefix` in a rendered footprint."""
    flat = _flat(text)
    start = flat.index(prefix)
    depth = 0
    for index in range(start, len(flat)):
        depth += {"(": 1, ")": -1}.get(flat[index], 0)
        if depth == 0:
            return flat[start:index + 1]
    raise AssertionError("unterminated property")


# ── Core: file output and the saved mapping ──────────────────────────

def test_a_placed_text_is_written_left_bottom_at_its_anchor():
    fp = Footprint(name="X").place_texts({"Reference": (1.5, -3.25)})
    text = fp.render()
    reference = _property(text, "Reference")
    assert "(at 1.5 -3.25)" in reference
    assert "(effects (font (size 1 1) (thickness 0.15)) (justify left bottom))" in reference
    # Negative: a text neither placed nor defaulted (a Footprint built
    # directly) keeps KiCad's centring, byte for byte.
    assert "justify" not in _property(text, "Value")
    assert "justify" not in Footprint(name="X").render()


def test_the_defaults_are_written_centred_at_the_d1_centre_and_a_placed_text_is_anchored():
    """CONTRACT CHANGED 2026-10-17 (user, «Predefinito centrato»).

    Positive: an unmoved Reference/Value is a centred KiCad text at its D1
    centre, so it stays centred whatever KiCad shows in it (the board shows
    the schematic Value; a rename changes the name's width). The preview
    still draws it from the corner KiCad draws it from (`text_anchors`).
    Negative: a text the user places is written bottom-left anchored.
    """
    from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
    fp = build_pin_header(PinHeaderParams(name="Hdr", rows=2, cols=5))
    centres = default_text_positions(fp)
    text = fp.render()
    for key, label in (("Reference", "REF**"), ("Value", "Hdr")):
        assert f"(at {centres[key][0]:g} {centres[key][1]:g})" in _property(text, key)
        assert "justify" not in _property(text, key)
        assert fp.text_centres()[key] == pytest.approx(centres[key], abs=1e-12)
        dx, dy = property_corner_offset(label)
        expected = (round(centres[key][0] + dx, 4), round(centres[key][1] + dy, 4))
        assert fp.text_anchors()[key] == pytest.approx(expected, abs=1e-9)
    fp.place_texts({"Value": (1.0, 9.0)})
    placed = fp.render()
    assert "(at 1 9)" in _property(placed, "Value") and "(justify left bottom)" in _property(placed, "Value")
    assert "justify" not in _property(placed, "Reference"), "the unmoved one stays centred"


def test_the_corner_offset_is_kicads_geometry_and_depends_on_the_text():
    """Half the KiCad width left, 0.585 x size down (y down); wide and narrow texts differ."""
    dx, dy = property_corner_offset("REF**")
    width = kicad_text_width("REF**", PROPERTY_TEXT_SIZE, PROPERTY_TEXT_THICKNESS)
    assert (dx, dy) == pytest.approx((-width / 2, KICAD_CENTRE_TO_BOTTOM * PROPERTY_TEXT_SIZE))
    # Measured with kicad-cli fp export svg: REF** is 4.483 mm wide at 1.0 / 0.15 mm.
    assert width == pytest.approx(4.483, abs=0.002)
    # Same character count, different advances: a font-free length rule would not separate them.
    assert property_corner_offset("MMMM")[0] < property_corner_offset("iiii")[0] - 1.0
    assert property_corner_offset("MMMM")[1] == property_corner_offset("iiii")[1]


def test_the_margin_follows_the_pen_of_each_tab():
    """Footprint texts carry their own thickness; symbol properties the schematic default pen."""
    # kicad-cli measurements (mm): footprint 0.15 -> 0.1974, 0.1 -> 0.1316,
    # 0.3 at size 1.0 clamped to 0.25 -> 0.3289; symbol default pen -> 0.2005.
    assert kicad_text_margin(1.0, 0.15) == pytest.approx(0.1974, abs=1e-4)
    assert kicad_text_margin(1.0, 0.1) == pytest.approx(0.1316, abs=1e-4)
    assert kicad_text_margin(1.0, 0.3) == pytest.approx(0.3289, abs=1e-4)
    assert kicad_text_margin(2.0, 0.3) == pytest.approx(0.3947, abs=1e-4)
    assert kicad_text_margin(1.27, SCHEMATIC_DEFAULT_PEN_MM) == pytest.approx(0.2005, abs=1e-4)
    # Negative: the symbol's margin is not the footprint's.
    assert abs(kicad_text_margin(1.0, PROPERTY_TEXT_THICKNESS)
               - kicad_text_margin(1.27, SCHEMATIC_DEFAULT_PEN_MM)) > 0.002


def test_anchors_and_centres_are_inverse_and_a_direct_footprint_is_converted():
    fp = Footprint(name="Board", reference_at=(2.0, -3.0), value_at=(2.0, 3.0))
    anchors = fp.text_anchors()
    assert anchors["Reference"] == migrate_centred_positions(
        {"Reference": (2.0, -3.0)}, {"Reference": property_corner_offset("REF**")})["Reference"]
    placed = Footprint(name="Board").place_texts(anchors)
    assert placed.text_centres()["Reference"] == pytest.approx((2.0, -3.0), abs=1e-4)
    assert placed.text_centres()["Value"] == pytest.approx((2.0, 3.0), abs=1e-4)
    # The Value conversion uses the footprint name KiCad shows.
    assert Footprint(name="A_much_longer_name", value_at=(2.0, 3.0)).text_anchors()["Value"][0] < anchors["Value"][0]


def test_the_fabrication_reference_stays_centred_on_the_body():
    """Negative: ${REFERENCE} is generated geometry centred on the body, not a user anchor."""
    fp = Footprint(name="X").place_texts({"Reference": (0, -3), "Value": (0, 3)})
    fp.add_graphic(fab_reference_text((0.0, 0.0), (3.0, 2.0)))
    text = fp.render()
    assert "justify left bottom" in _property(text, "Reference")
    assert "justify" not in _node(text, '(fp_text user "${REFERENCE}"')


def test_footprint_texts_have_no_rotation_like_the_drawn_texts():
    """The footprint texts are horizontal (no angle in `at`), drawn ones as well: rotation does not apply."""
    fp = Footprint(name="X").place_texts({"Reference": (2, -3)})
    for item in drawing_graphics(validate_footprint_drawings([
            {"kind": "text", "layer": "F.SilkS", "points": [[2, 3]], "text": "T", "size": 1.0,
             "anchor": USER_TEXT_ANCHOR}])):
        fp.add_graphic(item)
    flat = _flat(fp.render())
    assert "(at 2 -3)" in _property(fp.render(), "Reference")
    assert re.search(r'\(fp_text user "T" \(at 2 3\)', flat)


def test_the_saved_mapping_is_marked_only_when_it_holds_a_position():
    assert text_positions_json({}) == {}
    data = text_positions_json({"Reference": (1.0, 2.0)})
    assert data == {"Reference": [1.0, 2.0], TEXT_POSITIONS_ANCHOR_FIELD: USER_TEXT_ANCHOR}
    assert text_positions_anchored(data)
    # The mark never becomes a position, so an older program still reads it.
    assert validate_text_positions(data) == {"Reference": (1.0, 2.0)}
    for unmarked in ({"Reference": [1, 2]}, {"anchor": "centre"}, None, [1, 2]):
        assert not text_positions_anchored(unmarked)


def test_migrating_centred_positions_moves_by_the_offset_and_clamps_at_the_bound():
    moved = migrate_centred_positions({"Reference": (1.0, -2.0), "Value": (0.0, 3.0)},
                                      {"Reference": (-1.25, 0.5), "Value": (-2.0, 0.5)})
    assert moved == {"Reference": (-0.25, -1.5), "Value": (-2.0, 3.5)}
    # A key without a measured offset is left out (back to its default).
    assert migrate_centred_positions({"Value": (0.0, 3.0)}, {}) == {}
    edge = migrate_centred_positions({"Value": (-499.9, 0.0)}, {"Value": (-1.0, 0.5)})
    assert edge == {"Value": (-TEXT_POSITION_LIMIT_MM, 0.5)}


# ── Window ───────────────────────────────────────────────────────────

pytest.importorskip("PyQt6")


@pytest.fixture
def preview(window, monkeypatch):
    """The window with its preview drawn; saves and regenerations recorded instead of run."""
    generations = []
    monkeypatch.setattr(window, "_generate", lambda **kwargs: generations.append(kwargs) or True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite",
                        lambda **_kwargs: window._generate(footprint_only=True))
    window._draw_preview()
    return window


def _groups(window):
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    return {item.data(TEXT_KEY_ROLE): item for item in window.preview_scene.items()
            if item.data(TEXT_KEY_ROLE) is not None and item.parentItem() is None}


def _glyphs(group):
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    return next(c for c in group.childItems() if isinstance(c, QGraphicsSimpleTextItem))


def _left_and_baseline(item):
    """The bottom-left anchor (scene px) KiCad draws this property text item from.

    CONTRACT CHANGED 2026-10-16 (user: KiCad's width and box,
    gui/kicad_text_item.py): the preview no longer puts the layout's left
    edge and baseline on the anchor but KiCad's cells and baseline, half the
    pen margin right of it and 0.125 x size above; this undoes that offset.
    """
    from kicadgen.core.footprint import PROPERTY_TEXT_SIZE, PROPERTY_TEXT_THICKNESS
    from kicadgen.core.kicad_text import kicad_text_cells
    from kicadgen.gui.main_window import _drawing_text_baseline
    along, up = kicad_text_cells(item.text(), PROPERTY_TEXT_SIZE, PROPERTY_TEXT_THICKNESS)
    box = item.sceneBoundingRect()
    return (box.left() - along * 20,
            item.scenePos().y() + _drawing_text_baseline(item) * item.scale() + up * 20)


def _built(window):
    return window._build_footprint(window._preview_params())


def test_the_default_texts_sit_on_the_file_anchor_and_show_the_square_on_it(preview):
    from PyQt6.QtWidgets import QGraphicsLineItem, QGraphicsRectItem
    window = preview
    built = _built(window)
    anchors = built.text_anchors()
    for key, group in _groups(window).items():
        text = _glyphs(group)
        left, baseline = _left_and_baseline(text)
        # The preview draws from the anchor the .kicad_mod holds.
        assert (group.pos().x(), group.pos().y()) == pytest.approx(
            (anchors[key][0] * 20, anchors[key][1] * 20), abs=0.01)
        assert (left, baseline) == pytest.approx((group.pos().x(), group.pos().y()), abs=0.01)
        (square,) = [c for c in group.childItems() if isinstance(c, QGraphicsRectItem)]
        # CONTRACT CHANGED 2026-10-16 (user): the square shows only near the
        # text or while dragging, as in the symbol tab (hover tests below).
        assert square.scenePos() == group.pos() and not square.isVisible()
        assert not [c for c in group.childItems() if isinstance(c, QGraphicsLineItem)]
        # The text grows right and up from the corner.
        box = text.sceneBoundingRect()
        assert box.right() > left and box.top() < baseline
    assert window.footprint_text_positions == {}, "a default is not stored"


def test_the_square_survives_the_drawing_handles_refresh(preview):
    """Negative: refreshing the drawings' hover handles must not remove the text anchors."""
    from PyQt6.QtWidgets import QGraphicsRectItem
    window = preview
    window._refresh_drawing_handles()
    for group in _groups(window).values():
        assert [c for c in group.childItems() if isinstance(c, QGraphicsRectItem)]


def test_a_legacy_centred_position_is_converted_once_on_load(preview):
    window = preview
    state = deepcopy(window._project_state())
    state["footprint_texts"] = {"Reference": [2.0, -5.0]}          # saved before: a centre
    window._apply_project_state(state)
    window._draw_preview()
    converted = window.footprint_text_positions["Reference"]
    dx, dy = property_corner_offset("REF**")
    assert converted == pytest.approx((2.0 + dx, -5.0 + dy), abs=1e-4), "KiCad's corner of that centre"
    left, baseline = _left_and_baseline(_glyphs(_groups(window)["Reference"]))
    assert (left, baseline) == pytest.approx((converted[0] * 20, converted[1] * 20), abs=0.01)
    # Saved again it is marked; loading it again converts nothing.
    saved = window._project_state()["footprint_texts"]
    assert saved[TEXT_POSITIONS_ANCHOR_FIELD] == USER_TEXT_ANCHOR
    window._apply_project_state(deepcopy(window._project_state()))
    assert window.footprint_text_positions["Reference"] == converted


def test_a_marked_position_is_read_as_it_is(preview):
    """Negative: a mapping saved with the mark already holds anchors."""
    window = preview
    state = deepcopy(window._project_state())
    state["footprint_texts"] = {"Reference": [2.0, -5.0], "anchor": USER_TEXT_ANCHOR}
    window._apply_project_state(state)
    assert window.footprint_text_positions == {"Reference": (2.0, -5.0)}


def test_a_legacy_recovery_draft_is_converted_and_a_marked_one_is_not(preview):
    window = preview
    legacy = window._footprint_state_from_json({"texts": {"Value": [1.0, 4.0]}})
    dx, dy = property_corner_offset(window._footprint_property_texts()["Value"])
    assert legacy["texts"]["Value"] == pytest.approx((1.0 + dx, 4.0 + dy), abs=1e-4)
    marked = window._footprint_state_from_json(
        {"texts": window._footprint_state_json(window._footprint_edit_state())["texts"]
         | {"Value": [1.0, 4.0], "anchor": USER_TEXT_ANCHOR}})
    assert marked["texts"]["Value"] == (1.0, 4.0)


def test_a_drop_snaps_the_corner_to_the_grid(preview):
    window = preview
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    window._on_footprint_text_moved("Reference", 3.43 * 20, -4.58 * 20)
    assert window.footprint_text_positions == {"Reference": (3.4, -4.6)}
    group = _groups(window)["Reference"]
    left, baseline = _left_and_baseline(_glyphs(group))
    assert (left, baseline) == pytest.approx((3.4 * 20, -4.6 * 20), abs=0.01), "the corner is the node"
    assert "3.4 mm, -4.6 mm" in group.toolTip()


def test_the_written_file_anchors_the_placed_text_and_centres_the_unmoved_one(preview, tmp_path):
    """CONTRACT CHANGED 2026-10-17 (user, «Predefinito centrato»).

    The placed Value is written bottom-left at the preview anchor; the
    unmoved Reference is written centred at its D1 centre, and the preview
    draws it from the corner KiCad draws that centred text from.
    """
    from kicadgen.gui.workers import GenerateWorker
    window = preview
    window.footprint_text_positions = {"Value": (0.5, 6.0)}
    params = window._preview_params()
    anchors = window._footprint_text_anchors(lambda: window._build_footprint(params))
    worker = GenerateWorker(params, tmp_path)
    worker.text_positions = dict(window.footprint_text_positions)
    written = []
    worker.done.connect(written.append)
    worker.run()
    assert written, "the footprint was written"
    text = open(written[0], encoding="utf-8").read()
    value = _property(text, "Value")
    assert "(at 0.5 6)" in value and "(justify left bottom)" in value
    built = window._build_footprint(params)
    centre = built.text_centres()["Reference"]
    reference = _property(text, "Reference")
    assert f"(at {centre[0]:g} {centre[1]:g})" in reference and "justify" not in reference
    assert anchors["Reference"] == built.text_anchors()["Reference"], "drawn where KiCad draws it"


def test_reference_and_a_drawn_text_behave_the_same(preview):
    """REF** and a drawn «REF**» at the same anchor: same box on screen, same justify in the file."""
    window = preview
    window.footprint_drawings = validate_footprint_drawings([
        {"kind": "text", "layer": "F.SilkS", "points": [[2.0, 7.0]], "text": "REF**", "size": 1.0,
         "anchor": USER_TEXT_ANCHOR}])
    window.footprint_text_positions = {"Reference": (2.0, 7.0)}
    window._draw_preview()
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    drawn = next(i for i in window.preview_scene.items()
                 if isinstance(i, QGraphicsSimpleTextItem) and i.data(DRAWING_KEY_ROLE) == 0)
    reference = _glyphs(_groups(window)["Reference"])
    a, b = drawn.sceneBoundingRect(), reference.sceneBoundingRect()
    assert (a.left(), a.top(), a.width(), a.height()) == pytest.approx(
        (b.left(), b.top(), b.width(), b.height()), abs=0.01)
    fp = Footprint(name="X").place_texts({"Reference": (2.0, 7.0)})
    for item in drawing_graphics(window.footprint_drawings):
        fp.add_graphic(item)
    flat = _flat(fp.render())
    effects = "(effects (font (size 1 1) (thickness 0.15)) (justify left bottom))"
    assert "(at 2 7)" in _property(fp.render(), "Reference") and effects in _property(fp.render(), "Reference")
    drawn_node = flat[flat.index('(fp_text user "REF**"'):]
    assert drawn_node.startswith('(fp_text user "REF**" (at 2 7)') and effects in drawn_node


# ── KiCad: the converted text lands where the centred one was ────────

#: Reference cross drawn on F.SilkS, far from every text, so the SVG origin
#: is found without confusing it with a glyph stroke.
_CROSS = (-30.0, -30.0)


def _with_cross(fp):
    x, y = _CROSS
    fp.add_graphic(FpLine(start=(x - 0.5, y), end=(x + 0.5, y), layer="F.SilkS", width=0.12))
    fp.add_graphic(FpLine(start=(x, y - 0.5), end=(x, y + 0.5), layer="F.SilkS", width=0.12))
    return fp


def _svg_extent(svg, text):
    """Stroke extent (mm, footprint axes) of `text` in a kicad-cli SVG, located through `_CROSS`."""
    groups = re.findall(r'<g class="stroked-text"><desc>([^<]*)</desc>(.*?)</g>', svg, re.S)
    rest = re.sub(r'<g class="stroked-text">.*?</g>', "", svg, flags=re.S)
    numbers = [(float(a), float(b)) for a, b in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", rest)]
    xs, ys = sorted({p[0] for p in numbers}), sorted({p[1] for p in numbers})
    off_x = next(x for x in xs if sum(1 for p in numbers if p[0] == x) >= 2) - _CROSS[0]
    off_y = next(y for y in ys if sum(1 for p in numbers if p[1] == y) >= 2) - _CROSS[1]
    (body,) = [body for desc, body in groups if desc == text]
    points = [(float(a) - off_x, float(b) - off_y)
              for a, b in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", body)]
    return (min(p[0] for p in points), max(p[0] for p in points),
            min(p[1] for p in points), max(p[1] for p in points))


def _export(cli, footprint, folder):
    library = folder / "lib.pretty"
    library.mkdir(parents=True)
    (library / f"{footprint.name}.kicad_mod").write_text(footprint.render(), encoding="utf-8")
    out = folder / "svg"
    out.mkdir()
    subprocess.run([cli, "fp", "export", "svg", "--fp", footprint.name, "--layers", "F.SilkS,F.Fab",
                    "-o", str(out), str(library)], capture_output=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    svgs = list(out.glob("*.svg"))
    assert svgs, "kicad-cli exported the footprint"
    return svgs[0].read_text(encoding="utf-8")


def _cli():
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    return cli


#: Tolerance of «nothing moves in KiCad», mm (user decision, 2026-10-16).
_KICAD_TOL = 0.02


@pytest.mark.parametrize("name", ["ANCHOR", "ESP32-WROOM-32E", "iiii_MMMM"])
def test_kicad_draws_the_converted_texts_where_the_centred_ones_were(tmp_path, name):
    """kicad-cli read-only export: centred at C, then left-bottom at the converted corner, same strokes."""
    cli = _cli()
    centres = {"Reference": (5.0, 5.0), "Value": (5.0, 20.0)}
    corners = migrate_centred_positions(
        centres, {key: property_corner_offset(text) for key, text in
                  Footprint(name=name).property_texts().items()})
    before = _export(cli, _with_cross(Footprint(name=name, reference_at=centres["Reference"],
                                                value_at=centres["Value"])), tmp_path / "before")
    after = _export(cli, _with_cross(Footprint(name=name).place_texts(corners)), tmp_path / "after")
    for text in ("REF**", name):
        b, a = _svg_extent(before, text), _svg_extent(after, text)
        assert all(abs(x - y) <= _KICAD_TOL for x, y in zip(a, b)), (text, before, after)


def test_kicad_draws_the_generated_defaults_where_the_d1_centres_put_them(tmp_path):
    """The preview's corners of the centred defaults are where KiCad draws them.

    CONTRACT CHANGED 2026-10-17: the generated defaults are centred; the
    preview draws (and a drag starts from) `text_anchors`, so placing both
    texts at those corners must not move them in KiCad.
    """
    from dataclasses import replace
    from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
    cli = _cli()
    centred = build_pin_header(PinHeaderParams(name="Hdr", rows=2, cols=5))
    # The fabrication ${REFERENCE} also renders as «REF**»: left out of both
    # copies, it is not a property and is not converted.
    centred.graphics = [g for g in centred.graphics if getattr(g, "text", None) != "${REFERENCE}"]
    fp = replace(centred, text_justify={}, graphics=list(centred.graphics)).place_texts(centred.text_anchors())
    assert "justify left bottom" in _property(fp.render(), "Reference")
    assert "justify" not in _property(centred.render(), "Reference")
    after = _export(cli, _with_cross(fp), tmp_path / "after")
    before = _export(cli, _with_cross(centred), tmp_path / "before")
    for text in ("REF**", "Hdr"):
        b, a = _svg_extent(before, text), _svg_extent(after, text)
        assert all(abs(x - y) <= _KICAD_TOL for x, y in zip(a, b)), (text, before, after)


def test_the_kicad_check_sees_a_conversion_without_the_pen_margin(tmp_path):
    """Negative: the export resolves the margin; dropping it (a width-only rule) moves REF** past the tolerance."""
    cli = _cli()
    centre = (5.0, 5.0)
    width_only = (property_corner_offset("REF**")[0] + kicad_text_margin(1.0, PROPERTY_TEXT_THICKNESS) / 2,
                  property_corner_offset("REF**")[1])
    corner = migrate_centred_positions({"Reference": centre}, {"Reference": width_only})["Reference"]
    before = _export(cli, _with_cross(Footprint(name="N", reference_at=centre)), tmp_path / "before")
    after = _export(cli, _with_cross(Footprint(name="N").place_texts({"Reference": corner})),
                    tmp_path / "after")
    b, a = _svg_extent(before, "REF**"), _svg_extent(after, "REF**")
    assert abs(a[0] - b[0]) > _KICAD_TOL * 3


# ── Anchor square on hover, as in the symbol tab (user, 2026-10-16) ──

def _square(group):
    from PyQt6.QtWidgets import QGraphicsRectItem
    (square,) = [c for c in group.childItems() if isinstance(c, QGraphicsRectItem)]
    return square


def _hover_view(window):
    view = window.preview_view
    view.resize(800, 600)
    window._draw_preview()
    return view


def _send(view, kind, pos, button=None):
    """Deliver a mouse event at a viewport point (QPoint) to the view's handler."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    button = Qt.MouseButton.NoButton if button is None else button
    event = QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                        button, button, Qt.KeyboardModifier.NoModifier)
    {QEvent.Type.MouseButtonPress: view.mousePressEvent,
     QEvent.Type.MouseMove: view.mouseMoveEvent,
     QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)


def _text_box(view, group):
    """Viewport rect of the glyphs of a text group."""
    return view.mapFromScene(_glyphs(group).sceneBoundingRect()).boundingRect()


def _visible_squares(window):
    return {key for key, group in _groups(window).items() if _square(group).isVisible()}


def test_the_anchor_square_is_hidden_at_rest(preview):
    window = preview
    _hover_view(window)
    assert _visible_squares(window) == set()
    # A redraw with nothing hovered keeps them hidden.
    window._draw_preview()
    assert _visible_squares(window) == set()


def test_the_anchor_square_shows_near_its_text_and_hides_when_the_pointer_leaves(preview):
    from PyQt6.QtCore import QEvent, QPoint
    window = preview
    view = _hover_view(window)
    box = _text_box(view, _groups(window)["Reference"])
    _send(view, QEvent.Type.MouseMove, box.center())
    assert _visible_squares(window) == {"Reference"}, "only the hovered text's square"
    # A redraw under the pointer keeps it shown (the square is rebuilt).
    window._draw_preview()
    assert _visible_squares(window) == {"Reference"}
    # It survives the drawing handles' refresh while visible.
    window._refresh_drawing_handles()
    assert _visible_squares(window) == {"Reference"}
    _send(view, QEvent.Type.MouseMove, QPoint(box.right() + 200, box.center().y()))
    assert _visible_squares(window) == set(), "far from the text: hidden again"
    _send(view, QEvent.Type.MouseMove, box.center())
    assert _visible_squares(window) == {"Reference"}
    view.leaveEvent(QEvent(QEvent.Type.Leave))
    assert _visible_squares(window) == set(), "the pointer left the view"


@pytest.mark.parametrize("zoom", [1.0, 3.0])
def test_the_reveal_margin_is_the_symbol_tabs_in_screen_px_at_any_zoom(preview, zoom):
    """Same margin as the symbol tab's property anchors: inside it shown, just beyond it hidden."""
    from PyQt6.QtCore import QEvent, QPoint
    from kicadgen.gui.footprint_view import HOVER_KEEP_MARGIN_PX
    from kicadgen.gui.symbol_tab import HANDLE_HOVER_MARGIN
    assert HOVER_KEEP_MARGIN_PX == HANDLE_HOVER_MARGIN
    window = preview
    view = _hover_view(window)
    view.zoom(zoom)
    box = _text_box(view, _groups(window)["Reference"])
    y = box.center().y()
    _send(view, QEvent.Type.MouseMove, QPoint(box.right() + HANDLE_HOVER_MARGIN - 1, y))
    assert "Reference" in _visible_squares(window)
    _send(view, QEvent.Type.MouseMove, QPoint(box.right() + HANDLE_HOVER_MARGIN + 3, y))
    assert "Reference" not in _visible_squares(window)
    _send(view, QEvent.Type.MouseMove, QPoint(box.center().x(), box.top() - HANDLE_HOVER_MARGIN + 1))
    assert "Reference" in _visible_squares(window)
    _send(view, QEvent.Type.MouseMove, QPoint(box.center().x(), box.top() - HANDLE_HOVER_MARGIN - 3))
    assert "Reference" not in _visible_squares(window)


def test_the_anchor_square_stays_shown_while_its_text_is_dragged(preview):
    from PyQt6.QtCore import QEvent, QPoint, Qt
    window = preview
    view = _hover_view(window)
    group = _groups(window)["Reference"]
    box = _text_box(view, group)
    start = box.center()
    _send(view, QEvent.Type.MouseMove, start)
    _send(view, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
    far = start + QPoint(250, 150)
    _send(view, QEvent.Type.MouseMove, far, Qt.MouseButton.LeftButton)
    assert _square(group).isVisible(), "dragged: shown wherever the pointer goes"
    view.leaveEvent(QEvent(QEvent.Type.Leave))
    assert _square(group).isVisible(), "leaving the view mid-drag does not hide it"
    _send(view, QEvent.Type.MouseButtonRelease, far, Qt.MouseButton.LeftButton)
    assert "Reference" in window.footprint_text_positions
    # Redrawn at the drop point, under the pointer: still shown.
    assert _visible_squares(window) == {"Reference"}
    _send(view, QEvent.Type.MouseMove, far + QPoint(300, 300))
    assert _visible_squares(window) == set()


def test_a_renamed_default_value_stays_centred_and_a_placed_one_keeps_its_corner():
    """@brief S3 (2026-10-17): the installer's rename rewrites the Value text only.

    Positive: the default Value is centred, so the longer name stays centred
    on the same point (no justify, same `(at ...)`). Negative: a Value the
    user placed keeps its bottom-left corner, as placed.
    """
    from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
    from kicadgen.services.kicad_library import name_footprint
    fp = build_pin_header(PinHeaderParams(name="Hdr", rows=2, cols=5))
    before = _property(fp.render(), "Value")
    renamed = _property(name_footprint(fp.render(), "Header_2x05_LongerName"), "Value")
    assert '"Header_2x05_LongerName"' in renamed and "justify" not in renamed
    at = re.compile(r"\(at [-\d.]+ [-\d.]+\)")
    assert at.search(renamed).group(0) == at.search(before).group(0), "same centre"
    placed = fp.place_texts({"Value": (1.0, 9.0)}).render()
    moved = _property(name_footprint(placed, "Header_2x05_LongerName"), "Value")
    assert "(at 1 9)" in moved and "(justify left bottom)" in moved
