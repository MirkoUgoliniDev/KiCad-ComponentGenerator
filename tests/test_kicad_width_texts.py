"""@brief Texts of the symbol editor and the footprint preview drawn at KiCad's width (user, 2026-10-16).

The desktop font is narrower or wider than KiCad's stroke font depending on
the text: before, a long Value differed by up to 2-4 mm at 1.0 mm. Every text
of both tabs is now built by `gui.kicad_text_item.kicad_text_item`: capitals
`size` tall, the layout stretched to KiCad's advance cells, cells and
baseline where KiCad lays them out from the anchor.

Contracts checked here:

- geometry (any font): the drawn layout box is KiCad's advance width and
  starts/ends/centres where KiCad's cells do, baseline as KiCad's, for every
  justification and for a quarter turn; hit areas follow the stretch;
- against kicad-cli (read-only `sym export svg` / `fp export svg`): the
  visible ends of every text in the measured set lie within
  `INK_TOLERANCE` x size of KiCad's visible strokes, with the test font and
  with Segoe UI when installed; the unstretched drawing of before fails the
  same check (negative case).
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import re
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QFont, QPainterPath
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsSimpleTextItem

from kicadgen.core.kicad_text import (KICAD_BOTTOM_TO_BASELINE, KICAD_CENTRE_TO_BOTTOM, SCHEMATIC_DEFAULT_PEN_MM,
                                      kicad_advance_width, kicad_text_cells, kicad_text_margin,
                                      kicad_text_width)
from kicadgen.gui import kicad_text_item as kti
from kicadgen.services.system_dependencies import find_kicad_cli

from desktop_fonts import FONTS_DIR, desktop_font, in_desktop_font_process, run_with_desktop_fonts

#: The measured set (user request, 2026-10-16): properties, a long Value,
#: a footprint name, extreme narrow/wide strings and a pin name.
TEXTS = ("REF**", "U?", "RS485", "ESP32-WROOM-32E", "SOIC-8_3.9x4.9mm_P1.27mm", "iiii", "MMMM", "GPIO0")
SIZES = (1.0, 1.27)
#: Largest distance, as a fraction of the size, between an end of the drawn
#: text and the same end of KiCad's visible strokes. What remains after the
#: stretch is the side bearing of the first and last glyph, which differs
#: between the desktop font and KiCad's stroke font: KiCad's bearings are
#: 2-5/21 of the size (left/right of «H» 5/21, of «1» 4/21). Measured
#: (kicad-cli 10, 2026-10-16) at most 0.065 x size with Segoe UI and
#: 0.168 x size with Courier New (MMMM, SOIC-8_... at 1.27 mm); 0.2 x size
#: is about one KiCad bearing, so a failure means a wrong width or anchor,
#: not a different glyph shape. Before the stretch, the far end of
#: SOIC-8_3.9x4.9mm_P1.27mm lay 2.8-4.1 x size away (3.6-5.2 mm at 1.27).
INK_TOLERANCE = 0.2
#: Largest distance, as a fraction of the size, between the drawn baseline
#: and KiCad's (`KICAD_BOTTOM_TO_BASELINE`, itself checked against the
#: exported strokes within 0.011 mm by
#: `test_kicad_baseline_rule_matches_the_strokes`): the drawing puts it there
#: exactly; before, it sat on the anchor, 0.125 x size too low.
BASELINE_TOLERANCE = 0.02
#: Pen of the footprint user texts (the FpText default the writer keeps).
FP_PEN = 0.15


def _qt_ink(item):
    """Scene rect of the glyph outlines an item paints (its layout ink, through every transform)."""
    path = QPainterPath()
    path.addText(0.0, kti.text_baseline_px(item.text(), item.font()), item.font(), item.text())
    return item.sceneTransform().map(path).boundingRect()


def _qt_baseline_point(item):
    """Scene point of the start of the item's baseline."""
    return item.mapToScene(QPointF(0.0, kti.text_baseline_px(item.text(), item.font())))


def _fonts(qapp):
    """The fonts the drawing is checked with: the session's own and, in the
    desktop-font child process only, Segoe UI (the Windows UI font users see)
    and Courier New (monospaced: «iiii» as wide as «MMMM», the farthest from
    KiCad's proportional advances)."""
    fonts = [("test font", QFont(qapp.font()))]
    if not in_desktop_font_process():
        return fonts
    for file_name, family in (("segoeui.ttf", "Segoe UI"), ("cour.ttf", "Courier New")):
        font = desktop_font(file_name, family)
        if font is not None:
            fonts.append((family, font))
    return fonts


def test_desktop_fonts_in_a_separate_process(tmp_path):
    """@brief Run every font-dependent check of this module again with the desktop fonts, isolated.

    The child process registers Segoe UI and Courier New (when installed) and
    reruns this module; the shared test session never sees them.
    """
    if in_desktop_font_process():
        pytest.skip("already the desktop-font child process")
    if not any((FONTS_DIR / name).exists() for name in ("segoeui.ttf", "cour.ttf")):
        pytest.skip("no desktop font installed")
    run_with_desktop_fonts(tmp_path, __file__)


def _old_item(text, size, x, y, font, mm_to_px, vertical=False):
    """The drawing of before 2026-10-16: no stretch, layout left edge and baseline on the anchor."""
    font = kti.reference_font(font)
    item = QGraphicsSimpleTextItem(text)
    item.setFont(font)
    factor = size * mm_to_px / kti.cap_height_px(font)
    item.setScale(factor)
    down = kti.text_baseline_px(text, font) * factor
    if vertical:
        item.setRotation(-90)
        item.setPos(x - down, y)
    else:
        item.setPos(x, y - down)
    return item


# ── Geometry: width, cells and baseline as KiCad lays them out ───────

@pytest.mark.parametrize("justify", ["left", "center", "right"])
@pytest.mark.parametrize("vjustify", ["bottom", "center", "top"])
@pytest.mark.parametrize("vertical", [False, True])
def test_the_layout_box_is_kicads_cells_from_the_anchor(qapp, justify, vjustify, vertical):
    mm = 10.0
    for name, font in _fonts(qapp):
        for text in ("REF**", "SOIC-8_3.9x4.9mm_P1.27mm", "iiii", "MMMM"):
            size = 1.27
            item = kti.kicad_text_item(text, size, 50.0, 70.0, base_font=font, mm_to_px=mm,
                                       justify=justify, vjustify=vjustify, vertical=vertical)
            box = item.sceneBoundingRect()
            along = box.height() if vertical else box.width()
            assert along == pytest.approx(kicad_advance_width(text, size) * mm, abs=0.01), (name, text)
            left, baseline = kicad_text_cells(text, size, SCHEMATIC_DEFAULT_PEN_MM, justify, vjustify)
            start = _qt_baseline_point(item)
            if vertical:
                # Reading up the page: cells start `left` above the anchor,
                # the baseline lies `baseline` towards -x (the glyph tops).
                assert (start.x(), start.y()) == pytest.approx((50.0 - baseline * mm, 70.0 - left * mm), abs=0.01)
                assert box.bottom() == pytest.approx(70.0 - left * mm, abs=0.01)
            else:
                assert (start.x(), start.y()) == pytest.approx((50.0 + left * mm, 70.0 - baseline * mm), abs=0.01)
                assert box.left() == pytest.approx(50.0 + left * mm, abs=0.01)
            assert kti.kicad_text_stretch(item) > 0


def test_the_cells_follow_kicads_justification_rules():
    size, pen = 1.0, 0.15
    width, margin = kicad_text_width("RS485", size, pen), kicad_text_margin(size, pen)
    left, bottom = kicad_text_cells("RS485", size, pen, "left", "bottom")
    right, centre = kicad_text_cells("RS485", size, pen, "right", "center")
    middle, top = kicad_text_cells("RS485", size, pen, "center", "top")
    assert left == pytest.approx(margin / 2) and right == pytest.approx(-width + margin / 2)
    assert middle == pytest.approx(-kicad_advance_width("RS485", size) / 2)
    assert bottom == pytest.approx(KICAD_BOTTOM_TO_BASELINE * size)
    assert bottom - centre == pytest.approx(KICAD_CENTRE_TO_BOTTOM * size)
    assert centre - top == pytest.approx(KICAD_CENTRE_TO_BOTTOM * size)
    # Negative: the pen changes where the cells start, the size where the baseline is.
    assert kicad_text_cells("RS485", size, 0.3, "left", "bottom")[0] > left
    assert kicad_text_cells("RS485", 2 * size, pen, "left", "bottom")[1] == pytest.approx(2 * bottom)


def test_the_stretch_is_per_text_not_one_factor(qapp):
    """Positive/negative: «iiii» is widened and «MMMM» narrowed by the same font, or vice versa."""
    for name, font in _fonts(qapp):
        stretches = {text: kti.kicad_text_stretch(kti.kicad_text_item(text, 1.0, 0, 0, base_font=font, mm_to_px=20))
                     for text in ("iiii", "MMMM", "REF**")}
        assert len({round(v, 3) for v in stretches.values()}) == 3, (name, stretches)
        empty = kti.kicad_text_item("", 1.0, 0, 0, base_font=font, mm_to_px=20)
        assert kti.kicad_text_stretch(empty) == 1.0


def test_hit_areas_follow_the_stretched_item(qapp):
    """The shape a click and a hover test is the stretched text, not the font's own box."""
    scene = QGraphicsScene()
    font = QFont(qapp.font())
    item = kti.kicad_text_item("SOIC-8_3.9x4.9mm_P1.27mm", 1.0, 0, 0, base_font=font, mm_to_px=20)
    scene.addItem(item)
    box = item.sceneBoundingRect()
    assert box.width() == pytest.approx(kicad_advance_width("SOIC-8_3.9x4.9mm_P1.27mm", 1.0) * 20, abs=0.01)
    inside = QPointF(box.right() - 1.0, box.center().y())
    outside = QPointF(box.right() + 2.0, box.center().y())
    assert item in scene.items(inside) and item not in scene.items(outside)


def test_measurements_are_cached(qapp):
    font = kti.reference_font(QFont(qapp.font()))
    kti._cap_cache.clear()
    kti._line_cache.clear()
    for _ in range(50):
        kti.kicad_text_item("GPIO0", 1.27, 0, 0, base_font=font, mm_to_px=10)
    assert len(kti._cap_cache) == 1 and len(kti._line_cache) == 1
    # A change of the available fonts may resolve the same key to another
    # face: the caches are emptied (negative: a redraw alone keeps them).
    kti.kicad_text_item("GPIO0", 1.27, 0, 0, base_font=font, mm_to_px=10)
    assert len(kti._cap_cache) == 1
    qapp.fontDatabaseChanged.emit()
    assert not kti._cap_cache and not kti._line_cache


# ── kicad-cli: the drawn extents against KiCad's strokes ─────────────

def _cli():
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    return cli


def _stroke_groups(svg):
    """[(text, [(x, y), ...])] of every stroked text of a kicad-cli SVG, SVG units (mm, y down)."""
    groups = []
    for desc, body in re.findall(r'<g class="stroked-text"><desc>([^<]*)</desc>(.*?)</g>', svg, re.S):
        numbers = [float(v) for v in re.findall(r"[-\d.]+", " ".join(re.findall(r'd="([^"]*)"', body)))]
        groups.append((desc.replace("&amp;", "&"), list(zip(numbers[0::2], numbers[1::2]))))
    return groups


def _match(groups, entries, origin):
    """{entry index: stroke box (left, right, top, bottom), page mm, y down} of the group drawn from its anchor.

    Each entry takes, among the groups of its text, the one whose box lies
    nearest to its anchor (a bottom-left text's box starts within a stroke
    bearing of it); other texts that render the same string (a default
    property, the fabrication ${REFERENCE}) lie farther.
    """
    ox, oy = origin
    boxes = []
    for text, points in groups:
        xs, ys = [p[0] - ox for p in points], [p[1] - oy for p in points]
        boxes.append((text, (min(xs), max(xs), min(ys), max(ys))))

    def gap(entry, box):
        left, right, top, bottom = box
        dx = max(left - entry["x"], 0.0, entry["x"] - right)
        dy = max(top - entry["y"], 0.0, entry["y"] - bottom)
        return dx * dx + dy * dy

    found = {}
    for index, entry in enumerate(entries):
        candidates = [box for text, box in boxes if text == entry["text"]]
        if candidates:
            found[index] = min(candidates, key=lambda box: gap(entry, box))
    return found


def _entries(vertical_too=True):
    """Texts of `TEXTS` x `SIZES` on a 6.35 mm grid (a multiple of the symbol grid), turned ones in a row."""
    entries = []
    for s, size in enumerate(SIZES):
        for t, text in enumerate(TEXTS):
            entries.append({"text": text, "size": size, "vertical": False,
                            "x": 0.0, "y": 6.35 * (t + len(TEXTS) * s)})
            if vertical_too:
                entries.append({"text": text, "size": size, "vertical": True,
                                "x": 40.64 + 6.35 * (t + len(TEXTS) * s), "y": 40.64})
    return entries


def _symbol_kicad_boxes(cli, entries, folder):
    """KiCad's visible stroke box of each entry exported as a bottom-left symbol text (page mm, y down)."""
    lines = ['(kicad_symbol_lib (version 20231120) (generator "kicadgen_test")',
             ' (symbol "T" (in_bom yes) (on_board yes)',
             '  (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
             '  (property "Value" "T" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
             '  (symbol "T_0_1"',
             '   (rectangle (start -50 50) (end -40 40) (stroke (width 0.254) (type default)) (fill (type none)))']
    for e in entries:
        lines.append(f'   (text "{e["text"]}" (at {e["x"]} {-e["y"]} {900 if e["vertical"] else 0}) '
                     f'(effects (font (size {e["size"]} {e["size"]})) (justify left bottom)))')
    lines += ['  )', ' )', ')']
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "t.kicad_sym"
    path.write_text("\n".join(lines), encoding="utf-8")
    out = folder / "svg"
    result = subprocess.run([cli, "sym", "export", "svg", "-o", str(out), str(path)], capture_output=True,
                            text=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr
    svg = next(out.glob("*.svg")).read_text(encoding="utf-8")
    rect = re.search(r'<rect x="([-\d.]+)" y="([-\d.]+)"', svg)
    origin = (float(rect.group(1)) + 50, float(rect.group(2)) + 50)
    return _match(_stroke_groups(svg), entries, origin), SCHEMATIC_DEFAULT_PEN_MM


def _footprint_kicad_boxes(cli, entries, folder):
    """KiCad's visible stroke box of each entry exported as a bottom-left footprint user text."""
    from kicadgen.core.footprint import Footprint, FpLine, drawing_graphics, validate_footprint_drawings
    records = validate_footprint_drawings([
        {"kind": "text", "layer": "F.SilkS", "width": 0.12, "points": [[e["x"], e["y"]]], "text": e["text"],
         "size": e["size"], "anchor": "left_bottom"} for e in entries])
    fp = Footprint(name="W")
    for graphic in drawing_graphics(records):
        fp.add_graphic(graphic)
    # Origin cross far from every text (located in the SVG by its two strokes).
    fp.add_graphic(FpLine(start=(-30.5, -30.0), end=(-29.5, -30.0), layer="F.SilkS", width=0.12))
    fp.add_graphic(FpLine(start=(-30.0, -30.5), end=(-30.0, -29.5), layer="F.SilkS", width=0.12))
    library = folder / "lib.pretty"
    library.mkdir(parents=True)
    (library / "W.kicad_mod").write_text(fp.render(), encoding="utf-8")
    out = folder / "svg"
    # fp export svg does not create its output folder.
    out.mkdir()
    subprocess.run([cli, "fp", "export", "svg", "--fp", "W", "--layers", "F.SilkS", "-o", str(out), str(library)],
                   capture_output=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    svg = next(out.glob("*.svg")).read_text(encoding="utf-8")
    rest = re.sub(r'<g class="stroked-text">.*?</g>', "", svg, flags=re.S)
    numbers = [(float(a), float(b)) for a, b in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", rest)]
    xs, ys = sorted({p[0] for p in numbers}), sorted({p[1] for p in numbers})
    origin = (next(x for x in xs if sum(1 for p in numbers if p[0] == x) >= 2) + 30,
              next(y for y in ys if sum(1 for p in numbers if p[1] == y) >= 2) + 30)
    return _match(_stroke_groups(svg), entries, origin), FP_PEN


def _visible(box, pen):
    """A stroke centreline box widened by half the pen on every side: what KiCad paints."""
    left, right, top, bottom = box
    return left - pen / 2, right + pen / 2, top - pen / 2, bottom + pen / 2


def _drawn_box(item, mm_to_px):
    """(left, right, top, bottom, baseline) of an item's ink, page mm (y down); baseline in the glyph frame."""
    ink = _qt_ink(item)
    start = _qt_baseline_point(item)
    return (ink.left() / mm_to_px, ink.right() / mm_to_px, ink.top() / mm_to_px, ink.bottom() / mm_to_px,
            start.x() / mm_to_px, start.y() / mm_to_px)


def compare(entries, kicad, pen, build, mm_to_px):
    """Rows of (entry, drawn box, KiCad visible box, error of the start, of the end, of the baseline), mm.

    `build(entry)` returns the drawn item; the start/end are along the
    reading direction (bottom/top for a quarter-turned text) and the
    baseline error compares the drawn baseline with KiCad's «H»-height rule
    (`KICAD_BOTTOM_TO_BASELINE`), checked against the strokes elsewhere.
    """
    rows = []
    for index, entry in enumerate(entries):
        if index not in kicad:
            raise AssertionError(f"kicad-cli did not draw {entry}")
        left, right, top, bottom = _visible(kicad[index], pen)
        item = build(entry)
        dl, dr, dt, db, bx, by = _drawn_box(item, mm_to_px)
        base = KICAD_BOTTOM_TO_BASELINE * entry["size"]
        if entry["vertical"]:
            start, end = db - bottom, top - dt
            baseline = (entry["x"] - base) - bx
        else:
            start, end = dl - left, dr - right
            baseline = (entry["y"] - base) - by
        rows.append((entry, (dl, dr, dt, db), (left, right, top, bottom), start, end, baseline))
    return rows


def _check(rows, label):
    for entry, drawn, kicad, start, end, baseline in rows:
        size = entry["size"]
        where = (label, entry["text"], size, entry["vertical"], drawn, kicad)
        assert abs(start) <= INK_TOLERANCE * size and abs(end) <= INK_TOLERANCE * size, where
        assert abs(baseline) <= BASELINE_TOLERANCE * size, where


@pytest.fixture(scope="module")
def symbol_kicad(tmp_path_factory):
    cli = _cli()
    entries = _entries()
    boxes, pen = _symbol_kicad_boxes(cli, entries, tmp_path_factory.mktemp("kw_sym"))
    return entries, boxes, pen


@pytest.fixture(scope="module")
def footprint_kicad(tmp_path_factory):
    cli = _cli()
    entries = _entries(vertical_too=False)
    boxes, pen = _footprint_kicad_boxes(cli, entries, tmp_path_factory.mktemp("kw_fp"))
    return entries, boxes, pen


def test_kicad_baseline_rule_matches_the_strokes(symbol_kicad, footprint_kicad):
    """The constant the drawing uses, checked on the exported capitals (no descenders in these texts)."""
    for entries, boxes, pen in (symbol_kicad, footprint_kicad):
        for index, entry in enumerate(entries):
            if entry["text"] not in ("REF**", "U?", "MMMM", "GPIO0", "ESP32-WROOM-32E"):
                continue
            left, right, top, bottom = boxes[index]
            base = KICAD_BOTTOM_TO_BASELINE * entry["size"]
            if entry["vertical"]:
                assert entry["x"] - right == pytest.approx(base, abs=0.011), entry
            else:
                assert entry["y"] - bottom == pytest.approx(base, abs=0.011), entry


def _symbol_tab(qapp, entries, font=None):
    from kicadgen.generators import symbol_glyphs as g
    from kicadgen.gui.symbol_tab import SymbolTab
    records = []
    for e in entries:
        record = g.shape_instance("text", [(e["x"], -e["y"])], text=e["text"], size=e["size"])
        record["rotation"] = 90 if e["vertical"] else 0
        records.append(record)
    rows = [{"pin": str(i), "name": f"P{i}"} for i in range(1, 3)]
    for row in rows:
        row["symbol_graphics"] = deepcopy(records)
    tab = SymbolTab()
    if font is not None:
        tab.setFont(font)
    tab.resize(1200, 800)
    tab.set_data("Board", rows, pad_ids=["1", "2"])
    tab._draw(tab._symbol)
    return tab


def _symbol_rows(tab, entries, kicad, pen):
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM
    items = {item.data(0)[1]: item for item in tab.view.scene().items()
             if isinstance(item, QGraphicsSimpleTextItem) and isinstance(item.data(0), tuple)
             and item.data(0)[0] == "graphic"}
    order = iter(range(len(entries)))
    return compare(entries, kicad, pen, lambda entry: items[next(order)], SCENE_UNITS_PER_MM)


def test_symbol_editor_texts_end_where_kicad_draws_them(qapp, symbol_kicad):
    entries, kicad, pen = symbol_kicad
    for name, font in _fonts(qapp):
        tab = _symbol_tab(qapp, entries, font)
        try:
            _check(_symbol_rows(tab, entries, kicad, pen), f"symbol, {name}")
        finally:
            tab.deleteLater()


def test_footprint_preview_texts_end_where_kicad_draws_them(qapp, footprint_kicad):
    from kicadgen.gui import main_window as gui
    entries, kicad, pen = footprint_kicad
    records = [{"kind": "text", "layer": "F.SilkS", "width": 0.12, "points": [[e["x"], e["y"]]],
                "text": e["text"], "size": e["size"], "anchor": "left_bottom"} for e in entries]
    original = QFont(qapp.font())
    try:
        for name, font in _fonts(qapp):
            # The preview lays texts out in the application font (QFont()).
            qapp.setFont(font)
            rows = compare(entries, kicad, pen,
                           lambda e: gui._drawing_text_item(records[entries.index(e)], 20.0), 20.0)
            _check(rows, f"footprint, {name}")
    finally:
        qapp.setFont(original)


def test_the_unstretched_drawing_of_before_fails_the_same_check(qapp, symbol_kicad):
    """Negative: the font's own width (no stretch, baseline on the anchor) misses KiCad's ends."""
    entries, kicad, pen = symbol_kicad
    for name, font in _fonts(qapp):
        rows = compare(entries, kicad, pen,
                       lambda e: _old_item(e["text"], e["size"], e["x"] * 10, e["y"] * 10, font, 10.0, e["vertical"]),
                       10.0)
        worst = max(abs(end) / entry["size"] for entry, _, _, _, end, _ in rows)
        assert worst > 2.0, (name, worst)
        assert all(abs(baseline) > BASELINE_TOLERANCE * entry["size"] for entry, *_, baseline in rows), name


def test_footprint_properties_end_where_kicad_draws_them(qapp, window, monkeypatch, tmp_path):
    """REF** and the Value of the preview against kicad-cli of the footprint written at the same anchors."""
    from kicadgen.core.footprint import PROPERTY_TEXT_THICKNESS, Footprint, FpLine
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    cli = _cli()
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    # A long Value: what the preview draws for a component of that name.
    monkeypatch.setattr(window, "_footprint_property_texts",
                        lambda: {"Reference": "REF**", "Value": "ESP32-WROOM-32E"})
    window._draw_preview()
    items = {item.data(TEXT_KEY_ROLE): item for item in window.preview_scene.items()
             if isinstance(item, QGraphicsSimpleTextItem) and item.data(TEXT_KEY_ROLE)}
    # The anchor each text is drawn from: the position of its group (20 px/mm).
    anchors = {key: (item.parentItem().pos().x() / 20.0, item.parentItem().pos().y() / 20.0)
               for key, item in items.items()}
    texts = window._footprint_property_texts()
    fp = Footprint(name=texts["Value"]).place_texts(anchors)
    fp.add_graphic(FpLine(start=(-30.5, -30.0), end=(-29.5, -30.0), layer="F.SilkS", width=0.12))
    fp.add_graphic(FpLine(start=(-30.0, -30.5), end=(-30.0, -29.5), layer="F.SilkS", width=0.12))
    library = tmp_path / "lib.pretty"
    library.mkdir()
    (library / f"{fp.name}.kicad_mod").write_text(fp.render(), encoding="utf-8")
    out = tmp_path / "svg"
    out.mkdir()
    subprocess.run([cli, "fp", "export", "svg", "--fp", fp.name, "--layers", "F.SilkS,F.Fab", "-o", str(out),
                    str(library)], capture_output=True, timeout=180,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    svg = next(out.glob("*.svg")).read_text(encoding="utf-8")
    rest = re.sub(r'<g class="stroked-text">.*?</g>', "", svg, flags=re.S)
    numbers = [(float(a), float(b)) for a, b in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", rest)]
    xs, ys = sorted({p[0] for p in numbers}), sorted({p[1] for p in numbers})
    origin = (next(x for x in xs if sum(1 for p in numbers if p[0] == x) >= 2) + 30,
              next(y for y in ys if sum(1 for p in numbers if p[1] == y) >= 2) + 30)
    entries = [{"text": texts[key], "size": 1.0, "vertical": False, "x": anchors[key][0], "y": anchors[key][1]}
               for key in ("Reference", "Value")]
    kicad = _match(_stroke_groups(svg), entries, origin)
    order = iter(("Reference", "Value"))
    _check(compare(entries, kicad, PROPERTY_TEXT_THICKNESS, lambda e: items[next(order)], 20.0), "footprint properties")
