"""@brief Texts drawn in the symbol editor are anchored at their bottom-left corner (user, 2026-10-14).

Positive: a new text has its anchor, handle square and grid snap at the
bottom-left corner, the file writes `(justify left bottom)` there and KiCad
draws the text starting at that point (0 and 90 degrees). Migration: texts
drawn before (centre/left/right, vertically centred) keep the box the editor
drew within a pixel and are migrated once. Negative: glyph texts imported
from KiCad libraries are unchanged; the property texts follow the same anchor
since 2026-10-16 (tests/test_symbol_property_anchor.py).
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QGraphicsSimpleTextItem

from kicadgen.core.kicad_text import kicad_text_cells
from kicadgen.generators import symbol_glyphs as g
from kicadgen.generators.symbol import SymbolPin, export_symbol, serialize_symbol
from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM, SymbolTab

S = SCENE_UNITS_PER_MM


def _kicad_cli():
    cli = shutil.which("kicad-cli")
    if cli:
        return cli
    for candidate in sorted(Path("C:/Program Files/KiCad").glob("*/bin/kicad-cli.exe"), reverse=True):
        return str(candidate)
    return None


def _rows(graphics=None):
    rows = [{"pin": str(i), "name": f"P{i}"} for i in range(1, 5)]
    if graphics is not None:
        for row in rows:
            row["symbol_graphics"] = deepcopy(graphics)
    return rows


def _tab(graphics=None, name="Board"):
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data(name, _rows(graphics), pad_ids=["1", "2", "3", "4"])
    return tab


def _text_items(tab, text):
    return [item for item in tab.view.scene().items()
            if isinstance(item, QGraphicsSimpleTextItem) and item.text() == text]


def _legacy_text(text, x, y, justify, rotation=0, size=1.27):
    """A text record as the Text tool wrote it before 2026-10-14 (no anchor marker)."""
    return {"id": f"legacy-{text}", "key": "shape:text", "label": "Testo", "source": "shape",
            "x": x, "y": y, "rotation": rotation, "mirror": False,
            "primitives": [{"kind": "text", "points": [[0.0, 0.0]], "radius": 0.0, "fill": "none",
                            "width": 0.0, "style": "default", "text": text, "size": size,
                            "angle": 0, "justify": justify}],
            "terminals": []}


def _old_drawn_rect(tab, record):
    """The scene rect the editor drew for a legacy record (the drawing code before 2026-10-14)."""
    primitive = record["primitives"][0]
    font = QFont(tab.font())
    font.setPixelSize(max(1, int(round(primitive["size"] * S))))
    item = QGraphicsSimpleTextItem(primitive["text"])
    item.setFont(font)
    bounds = item.boundingRect()
    justify = primitive["justify"]
    dx = bounds.width() if justify == "right" else bounds.width() / 2 if justify == "center" else 0
    x, y = record["x"], record["y"]
    if (primitive["angle"] + record["rotation"]) % 180 == 90:
        item.setRotation(-90)
        item.setPos(x * S - bounds.height() / 2, -y * S + dx)
    else:
        item.setPos(x * S - dx, -y * S - bounds.height() / 2)
    return item.sceneBoundingRect()


# ── New texts ────────────────────────────────────────────────────────────

def test_a_new_text_record_is_anchored_bottom_left_and_written_so():
    record = g.shape_instance("text", [(5.08, 2.54)], text="CLK", size=1.27)
    primitive = record["primitives"][0]
    assert (primitive["justify"], primitive["vjustify"]) == ("left", "bottom")
    assert g.is_bottom_left_text(record) and (record["x"], record["y"]) == (5.08, 2.54)
    assert g.shape_handles(record) == [(5.08, 2.54)], "the handle is the anchor"
    lines = g.serialize_graphics([record])
    assert lines == ['      (text "CLK" (at 5.08 2.54 0) (effects (font (size 1.27 1.27)) (justify left bottom)))']
    record["rotation"] = 90
    assert g.serialize_graphics([record])[0].endswith(
        '(text "CLK" (at 5.08 2.54 900) (effects (font (size 1.27 1.27)) (justify left bottom)))')
    assert not g.migrate_text_anchor(record, lambda text, size: (9.0, 9.0, 9.0)), "a new text is never migrated"


def test_editing_a_bottom_left_text_keeps_its_anchor_and_justification():
    record = g.shape_instance("text", [(0.0, 0.0)], text="A")
    assert g.set_text(record, "LONGER", 2.54, "right")
    primitive = record["primitives"][0]
    assert (primitive["text"], primitive["size"], primitive["justify"], primitive.get("vjustify")) == \
        ("LONGER", 2.54, "left", "bottom")
    assert (record["x"], record["y"]) == (0.0, 0.0)
    assert not g.set_text(record, "x", 1.0, "middle"), "an invalid justification is still refused"


def test_the_text_tool_snaps_the_bottom_left_corner_and_draws_the_handle_there(qapp):
    tab = _tab()
    try:
        tab._text_prompt = lambda *args: ("IN+", 1.27)
        assert tab.start_shape("text")
        tab.add_shape_point(2.6, 5.1)
        (record,) = tab.graphics()
        assert (record["x"], record["y"]) == (2.54, 5.08), "the clicked point snapped on the drawing grid"
        assert g.is_bottom_left_text(record)
        tab._draw(tab._symbol)
        (item,) = _text_items(tab, "IN+")
        ascent = tab.drawn_text_metrics("IN+", 1.27)[2] * S
        rect = item.sceneBoundingRect()
        # CONTRACT CHANGED 2026-10-16 (user: KiCad's width and box,
        # gui/kicad_text_item.py): drawn as KiCad draws from the anchor, cells
        # half the pen margin along and the baseline 0.125 x size above it.
        along, up = (v * S for v in kicad_text_cells("IN+", 1.27))
        assert rect.left() == pytest.approx(2.54 * S + along, abs=0.05), "the text starts at the anchor"
        assert rect.top() + ascent == pytest.approx(-5.08 * S - up, abs=0.05), "its baseline is KiCad's"
        assert rect.bottom() >= -5.08 * S - up - 0.05 and rect.top() < -5.08 * S,             "it grows upwards (descenders only below its baseline)"
        handles = [i for i in tab.view.scene().items() if i.data(0) == ("shape_point", 0, 0)]
        assert len(handles) == 1
        assert (handles[0].pos().x(), handles[0].pos().y()) == pytest.approx((2.54 * S, -5.08 * S))
        # Turned a quarter: it reads upwards from the same corner.
        assert tab._edit_graphic(0, "rotate", 90)
        (item,) = _text_items(tab, "IN+")
        rect = item.sceneBoundingRect()
        assert item.rotation() == -90
        assert rect.bottom() == pytest.approx(-5.08 * S - along, abs=0.05), "the text starts at the anchor"
        assert rect.left() + ascent == pytest.approx(2.54 * S - up, abs=0.05), "its baseline is KiCad's"
        assert rect.right() >= 2.54 * S - up - 0.05 and rect.left() < 2.54 * S,             "it grows to the left (descenders only right of its baseline)"
    finally:
        tab.deleteLater()


@pytest.mark.parametrize("size", [1.27, 2.54, 5.08])
def test_a_drawn_capital_is_as_tall_as_the_kicad_text_size(qapp, size):
    """User decision 2026-10-14: KiCad's text size is the cap height; the editor matches it."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter
    tab = _tab([g.shape_instance("text", [(0.0, 0.0)], text="H", size=size)])
    try:
        tab._draw(tab._symbol)
        (item,) = _text_items(tab, "H")
        zoom = 8
        source = item.sceneBoundingRect().adjusted(-2, -2, 2, 2)
        image = QImage(int(source.width() * zoom), int(source.height() * zoom), QImage.Format.Format_ARGB32)
        image.fill(QColor("white"))
        scene = tab.view.scene()
        for other in scene.items():
            other.setVisible(other is item)
        item.setBrush(QColor("black"))
        painter = QPainter(image)
        scene.render(painter, QRectF(0, 0, image.width(), image.height()), source)
        painter.end()
        rows = [y for y in range(image.height())
                if any(image.pixelColor(x, y).red() < 128 for x in range(0, image.width(), 2))]
        cap = (rows[-1] - rows[0] + 1) / zoom / S
        assert cap == pytest.approx(size, rel=0.03)
        assert tab.drawn_text_metrics("H", size)[3] == size
    finally:
        tab.deleteLater()


def test_kicad_draws_a_new_text_starting_at_its_anchor(tmp_path):
    cli = _kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    horizontal = g.shape_instance("text", [(5.08, 2.54)], text="HORZ", size=2.54)
    vertical = g.shape_instance("text", [(-5.08, -2.54)], text="VERT", size=2.54)
    vertical["rotation"] = 90
    path = export_symbol(tmp_path / "t.kicad_sym", "T", [SymbolPin("1", "A")], graphics=[horizontal, vertical])
    source = path.read_text(encoding="utf-8")
    body = re.search(r"\(rectangle \(start ([-\d.]+) ([-\d.]+)\)", source)
    result = subprocess.run([cli, "sym", "export", "svg", "-o", str(tmp_path / "svg"), str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    svg = next((tmp_path / "svg").glob("*.svg")).read_text(encoding="utf-8")
    # Independent reference: the body rectangle's start corner.
    rect = re.search(r'<rect x="([-\d.]+)" y="([-\d.]+)"', svg)
    ox, oy = float(rect.group(1)) - float(body.group(1)), float(rect.group(2)) + float(body.group(2))

    def strokes(label):
        group = re.search(r'<g class="stroked-text"><desc>' + label + r'</desc>(.*?)</g>', svg, re.S).group(1)
        numbers = [float(v) for v in re.findall(r"[-\d.]+", " ".join(re.findall(r'd="([^"]*)"', group)))]
        return numbers[0::2], numbers[1::2]

    xs, ys = strokes("HORZ")
    ax, ay = ox + 5.08, oy - 2.54
    assert min(xs) >= ax and max(ys) <= ay, "right of and above the anchor"
    assert min(xs) - ax < 1.0 and ay - max(ys) < 0.6, "starting at the anchor (stroke bearing only)"
    xs, ys = strokes("VERT")
    ax, ay = ox - 5.08, oy + 2.54
    assert max(xs) <= ax and max(ys) <= ay, "left of and above the anchor: read upwards"
    assert ay - max(ys) < 1.0 and ax - max(xs) < 0.6, "starting at the anchor (stroke bearing only)"


# ── Migration of texts drawn before ──────────────────────────────────────

@pytest.mark.parametrize("justify", ["left", "center", "right"])
@pytest.mark.parametrize("rotation", [0, 90])
def test_a_legacy_text_keeps_its_drawn_box_and_is_migrated_once(qapp, justify, rotation):
    legacy = _legacy_text("GND", 2.54, 1.27, justify, rotation)
    tab = _tab([legacy])
    try:
        (record,) = tab.graphics()
        assert g.is_bottom_left_text(record)
        assert (record["primitives"][0]["justify"], record["primitives"][0]["vjustify"]) == ("left", "bottom")
        assert not tab.has_pending_edits(), "migrated before the baseline: the project does not load modified"
        tab._draw(tab._symbol)
        (item,) = _text_items(tab, "GND")
        # The anchor is the bottom-left corner of the box the user SAW (old
        # drawing, legacy metric: font em = size), within a pixel...
        old = _old_drawn_rect(tab, legacy)
        legacy_ascent = tab.text_metrics("GND", legacy["primitives"][0]["size"])[2] * S
        if rotation:
            corner = (old.left() + legacy_ascent, old.bottom())
        else:
            corner = (old.left(), old.top() + legacy_ascent)
        assert (record["x"] * S, -record["y"] * S) == pytest.approx(corner, abs=1.0)
        # ...and the text is drawn from that corner, now at KiCad's size.
        # CONTRACT CHANGED 2026-10-16: from that corner as KiCad draws (cells
        # half the pen margin along, baseline 0.125 x size above it).
        new = item.sceneBoundingRect()
        ascent = tab.drawn_text_metrics("GND", legacy["primitives"][0]["size"])[2] * S
        along, up = (v * S for v in kicad_text_cells("GND", legacy["primitives"][0]["size"]))
        if rotation:
            assert (new.left() + ascent + up, new.bottom() + along) == pytest.approx(corner, abs=1.0)
        else:
            assert (new.left() - along, new.top() + ascent + up) == pytest.approx(corner, abs=1.0)
        if justify != "left" or rotation:
            assert (record["x"], record["y"]) != (legacy["x"], legacy["y"]), "the anchor moved to the corner"
        # Once: migrating again, or loading the migrated rows again, changes nothing.
        again = deepcopy(record)
        assert not g.migrate_text_anchor(again, tab.text_metrics) and again == record
        tab.set_data("Board", tab.rows(), pad_ids=["1", "2", "3", "4"], force=True)
        assert tab.graphics() == [record]
    finally:
        tab.deleteLater()


def test_migration_moves_the_anchor_along_the_text_frame():
    measure = lambda text, size: (4.0, 2.0, 1.6)  # width, line height, baseline depth (mm)
    centre = _legacy_text("A", 10.0, 10.0, "center")
    assert g.migrate_text_anchor(centre, measure)
    assert (centre["x"], centre["y"]) == (8.0, 9.4), "half the width left, baseline 0.6 mm below the middle"
    right = _legacy_text("A", 10.0, 10.0, "right", rotation=90)
    assert g.migrate_text_anchor(right, measure)
    assert (right["x"], right["y"]) == (10.6, 6.0), "turned: back along +Y, baseline towards +X"
    assert g.serialize_graphics([right])[0].endswith("(justify left bottom)))")
    assert not g.migrate_text_anchor(right, measure)


def test_a_recovery_draft_saved_before_the_migration_is_still_offered(qapp, tmp_path):
    from kicadgen.services.symbol_draft import save_draft
    tab = _tab([_legacy_text("GND", 2.54, 1.27, "center")])
    try:
        before = deepcopy(tab._premigration_baseline)
        assert before is not None and "text_anchor" not in before[0]["symbol_graphics"][0]
        draft = deepcopy(before)
        draft[0]["name"] = "RENAMED"
        path = tmp_path / ".symbol-draft.json"
        save_draft(path, before, draft)
        tab.set_recovery_path(path)
        assert tab._recoverable is not None, "the old draft is offered"
        assert tab._recoverable[0]["name"] == "RENAMED"
        assert tab._recoverable[0]["symbol_graphics"] == tab.graphics(), "its texts migrated like the baseline"
    finally:
        tab.deleteLater()


# ── What does not change ─────────────────────────────────────────────────

def test_imported_glyph_texts_keep_their_justification_exactly(qapp):
    unit = g.parse_sexpr(
        '(symbol "X_0_1"'
        ' (text "A" (at 1 2 0) (effects (font (size 1.27 1.27)) (justify left bottom)))'
        ' (text "B" (at 1 4 0) (effects (font (size 1.27 1.27)) (justify right)))'
        ' (text "C" (at 1 6 0) (effects (font (size 1.27 1.27)) (justify top)))'
        ' (text "D" (at 1 8 0) (effects (font (size 1.27 1.27)))))')[0]
    primitives = g.primitives_of(unit)
    assert [(p.justify, p.vjustify) for p in primitives] == [
        ("left", "bottom"), ("right", "center"), ("center", "top"), ("center", "center")]
    instance = g.instance_from_glyph(g.Glyph("x", "x", tuple(primitives), (), "kicad:Test"), 0, 0)
    lines = g.serialize_graphics([instance])
    assert [line.split("(effects ")[1] for line in lines] == [
        "(font (size 1.27 1.27)) (justify left bottom)))", "(font (size 1.27 1.27)) (justify right)))",
        "(font (size 1.27 1.27)) (justify top)))", "(font (size 1.27 1.27))))"], "written back as read"
    assert "vjustify" not in instance["primitives"][1], "a centred line keeps the record format it had"
    tab = _tab([instance])
    try:
        assert tab.graphics() == [instance], "a library glyph text is never migrated"
        assert not g.migrate_text_anchor(deepcopy(instance), tab.text_metrics)
    finally:
        tab.deleteLater()


def test_property_texts_are_unchanged_by_a_drawn_text():
    pins = [SymbolPin("1", "A")]
    plain = serialize_symbol("T", pins)
    drawn = serialize_symbol("T", pins, graphics=[g.shape_instance("text", [(0.0, 0.0)], text="NOTE")])
    properties = lambda body: [line for line in body.splitlines() if "(property " in line]
    assert properties(drawn) == properties(plain)
    # CONTRACT CHANGED 2026-10-17 (user, «Predefinito centrato»): unmoved
    # Reference and Value are centred again, like the hidden properties; only
    # a placed one carries the drawn texts' bottom-left anchor
    # (tests/test_symbol_property_anchor.py).
    assert [("justify" in line) for line in properties(drawn)] == [False, False, False, False, False]
