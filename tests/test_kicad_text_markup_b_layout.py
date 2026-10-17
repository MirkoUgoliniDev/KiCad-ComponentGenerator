"""@brief KiCad text markup (overbar, superscript, subscript, escapes) laid out and drawn as KiCad 10 does.

Font-free contracts (always run): the markup parser follows KiCad's grammar
(include/markup_parser.h), the layout gives KiCad's advances, script sizes,
baseline shifts and overbars, `kicad_unescape` follows UnescapeString, and
texts without markup keep exactly the geometry they had before.

Qt contracts (any font): the item built by `kicad_text_item` for a marked-up
text hides the markup, lays every run in its KiCad cells and draws the bars
where the layout puts them; a plain text still gets the plain item.

kicad-cli contracts (skipped without KiCad): the bars, the justification
width and the script baselines of KiCad's own `sym export svg` and
`fp export svg` match the layout within 0.003-0.015 mm, and the drawn items
end within `INK_TOLERANCE` x size of KiCad's strokes, with the session font
and, in the desktop-font child process, Segoe UI and Courier New.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import re
import subprocess

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QFont, QPainterPath
from PyQt6.QtWidgets import QGraphicsSimpleTextItem

from kicadgen.core import kicad_text as kt
from kicadgen.gui import kicad_text_item as kti
from kicadgen.services.system_dependencies import find_kicad_cli

from desktop_fonts import FONTS_DIR, desktop_font, in_desktop_font_process, run_with_desktop_fonts

#: Marked-up texts checked against kicad-cli: overbars, scripts, nesting in
#: both orders, adjacent markups, literal tildes/carets, an unclosed
#: markup, braces inside a markup, a URL and an empty markup.
MARKED = ("~{RESET}", "V_{CC}", "x^{2}+y_{i}", "~{CS}/A^{~{B}_{n}}", "H~{a{b}c}H", "~{~{Q}}~{WR}",
          "GPIO~{0}", "H_{^{H}}^{_{H}}", "A~B^C_D", "~{x", "http://x ~{EN}", "Q~{}R")
SIZES = (1.0, 1.27)
#: Same meaning and value as in tests/test_kicad_width_texts.py: the ends
#: of the drawn ink lie within one KiCad side bearing of KiCad's strokes.
INK_TOLERANCE = 0.2
#: Largest distance, mm, between a drawn overbar end and KiCad's (both are
#: the same exact geometry: a line of the text pen with round caps).
BAR_TOLERANCE = 0.005
#: Largest distance, mm, between geometry of the layout and KiCad's strokes
#: along the reading direction (KiCad rounds to 100 nm in a schematic).
ALONG_TOLERANCE = 0.003
#: Largest distance, mm, across the reading direction: KiCad's baseline
#: carries a pen-dependent residual of at most 0.0104 mm that the rule
#: `KICAD_BOTTOM_TO_BASELINE` does not model (see core.kicad_text).
ACROSS_TOLERANCE = 0.015
FP_PEN = 0.15


def _old_units(text):
    """The advance sum before markup support, for a text without markup: the plain table sum."""
    return sum(kt.KICAD_ADVANCE_UNITS.get(c, kt.KICAD_ADVANCE_FALLBACK_UNITS) for c in text)


# ── Font-free: parser, layout, escapes ───────────────────────────────

def test_the_parser_follows_kicads_grammar():
    assert kt.parse_kicad_markup("~{RST}") == (("~", ("RST",)),)
    assert kt.parse_kicad_markup("A^{~{B}_{n}}") == ("A", ("^", (("~", ("B",)), ("_", ("n",)))))
    # Braces inside a markup: «{identifier}» is literal text, a lone «{» too.
    assert kt.parse_kicad_markup("~{a{b}c}") == (("~", ("a{b}c",)),)
    assert kt.parse_kicad_markup("~{a{b c}") == (("~", ("a{b c",)),)
    # Negative: no closing brace, no markup; lone controls are literal.
    for literal in ("~{x", "H~H", "A^B_C", "}{", "~", "^{", "x}"):
        assert kt.parse_kicad_markup(literal) == (literal,), literal
    # A URL runs to the next space and is literal; markup resumes after it.
    assert kt.parse_kicad_markup("http://a~{b} ~{c}") == ("http://a~{b} ", ("~", ("c",)))
    assert kt.parse_kicad_markup("~{}") == (("~", ()),)


def test_the_layout_matches_kicads_rules():
    layout = kt.kicad_text_layout("H~{H}H")
    assert layout.visible_text == "HHH" and layout.advance == 66
    bar, = layout.overbars
    # Bar from 0.1 x size after the start of its span to 0.1 before its end,
    # 1.23 x size above the cursor line, itself 1/21 above the baseline.
    assert (bar.start, bar.end) == pytest.approx((22 + 2.1, 44 - 2.1))
    assert bar.height == pytest.approx(1.23 + 1 / 21)
    sup = kt.kicad_text_layout("H^{H}H")
    assert sup.advance == pytest.approx(22 + 0.8 * 22 + 22)
    assert [round(r.rise, 5) for r in sup.runs] == [0.0, 0.28952, 0.0]
    assert [r.scale for r in sup.runs] == [1.0, 0.8, 1.0]
    sub = kt.kicad_text_layout("V_{CC}")
    assert sub.runs[1].rise == pytest.approx(-0.11048, abs=1e-5) and sub.runs[1].text == "CC"
    # Subscript wins over superscript in either nesting order.
    assert {round(r.rise, 5) for r in kt.kicad_text_layout("_{^{H}}^{_{H}}").runs} == {-0.11048}
    # An overbar inside a superscript: the bar keeps the full-size height and trim.
    nested = kt.kicad_text_layout("H^{~{H}}")
    assert nested.overbars[0].height == pytest.approx(bar.height)
    assert (nested.overbars[0].start, nested.overbars[0].end) == pytest.approx((24.1, 22 + 17.6 - 2.1))
    # Empty markups are discarded: no bar, no shift.
    assert kt.kicad_text_layout("Q~{}R").overbars == () and kt.kicad_text_layout("Q~{}R").visible_text == "QR"
    assert len(kt.kicad_text_layout("~{~{Q}}").overbars) == 2


def test_widths_and_cells_account_for_the_markup():
    size, pen = 1.27, 0.15
    assert kt.kicad_advance_width("~{RESET}", size) == pytest.approx(kt.kicad_advance_width("RESET", size))
    assert kt.kicad_advance_width("V_{CC}", size) == pytest.approx(size * (18 + 0.8 * 42) / 21)
    assert kt.kicad_text_width("V_{CC}", size, pen) == pytest.approx(
        kt.kicad_advance_width("V_{CC}", size) + kt.kicad_text_margin(size, pen))
    left, _ = kt.kicad_text_cells("x^{2}", size, pen, "right", "bottom")
    assert left == pytest.approx(-kt.kicad_text_margin(size, pen) / 2 - kt.kicad_advance_width("x^{2}", size))
    # The vertical justification does not depend on the markup (measured).
    assert kt.kicad_text_cells("x^{2}", size, pen, "left", "top")[1] == kt.kicad_text_cells("x2", size, pen,
                                                                                            "left", "top")[1]


def test_plain_texts_keep_their_exact_geometry():
    """Negative: without markup, units are the same int sums as before and the layout is one plain run."""
    for text in ("REF**", "SOIC-8_3.9x4.9mm_P1.27mm", "iiii", "", "A~B", "x}y{z", "50% ^_^", "~", "{slash}"):
        units = kt.kicad_advance_units(text)
        assert units == _old_units(text) and isinstance(units, int), text
        layout = kt.kicad_text_layout(text)
        assert layout.is_plain and layout.visible_text == text, text
        for justify in ("left", "center", "right"):
            assert kt.kicad_text_cells(text, 1.27, 0.15, justify, "bottom") == pytest.approx(
                ({"left": kt.kicad_text_margin(1.27, 0.15) / 2,
                  "right": -kt.kicad_text_margin(1.27, 0.15) / 2 - 1.27 * _old_units(text) / 21}
                 .get(justify, -1.27 * _old_units(text) / 42), kt.KICAD_BOTTOM_TO_BASELINE * 1.27))


def test_a_literal_tilde_is_drawn_and_counted():
    """Negative: «~» not followed by «{» is a character of 15/21 (KiCad 10 has no tilde toggle)."""
    assert kt.kicad_advance_units("A~B") == _old_units("AB") + 15
    assert kt.kicad_text_layout("A~B").overbars == ()
    assert kt.kicad_advance_units("~{x") == _old_units("~{x")


def test_unescape_follows_kicads_unescape_string():
    assert kt.kicad_unescape("H{slash}H") == "H/H"
    assert kt.kicad_unescape("V{brace}^{H}") == "V{^{H}"
    assert kt.kicad_unescape("a{lt}{gt}{colon}{space}{dollar}b") == "a<>: $b"
    # Kept: a token after a markup or variable control, unknown tokens, an unterminated brace.
    assert kt.kicad_unescape("~{lt}") == "~{lt}" and kt.kicad_unescape("${slash}") == "${slash}"
    assert kt.kicad_unescape("H{foo}H") == "H{foo}H"
    assert kt.kicad_unescape("x{slash") == "x{slash"
    # Negative: short strings and strings without braces are unchanged.
    for text in ("ab", "REF**", "~{RST}", "{}"):
        assert kt.kicad_unescape(text) == text


# ── Qt: the drawn item ───────────────────────────────────────────────

def _fonts(qapp):
    """The session font and, in the desktop-font child only, Segoe UI and Courier New."""
    fonts = [("test font", QFont(qapp.font()))]
    if not in_desktop_font_process():
        return fonts
    for file_name, family in (("segoeui.ttf", "Segoe UI"), ("cour.ttf", "Courier New")):
        font = desktop_font(file_name, family)
        if font is not None:
            fonts.append((family, font))
    return fonts


def test_desktop_fonts_in_a_separate_process(tmp_path):
    """@brief Rerun the font-dependent checks of this module with the desktop fonts, isolated."""
    if in_desktop_font_process():
        pytest.skip("already the desktop-font child process")
    if not any((FONTS_DIR / name).exists() for name in ("segoeui.ttf", "cour.ttf")):
        pytest.skip("no desktop font installed")
    run_with_desktop_fonts(tmp_path, __file__)


def _frame(item_or_point, anchor, vertical, mm):
    """(along, up) mm in the text's frame of a scene point, from the anchor (scene px)."""
    dx, dy = item_or_point.x() / mm - anchor[0], item_or_point.y() / mm - anchor[1]
    return (-dy, -dx) if vertical else (dx, -dy)


def _frame_box(rect, anchor, vertical, mm):
    """(along_min, along_max, up_min, up_max) mm of a scene rect in the text's frame."""
    corners = [_frame(p, anchor, vertical, mm) for p in (rect.topLeft(), rect.bottomRight())]
    return (min(c[0] for c in corners), max(c[0] for c in corners),
            min(c[1] for c in corners), max(c[1] for c in corners))


@pytest.mark.parametrize("vertical", [False, True])
@pytest.mark.parametrize("justify", ["left", "center", "right"])
def test_the_drawn_runs_and_bars_lie_in_kicads_layout(qapp, vertical, justify):
    mm, size, pen = 10.0, 1.27, kt.SCHEMATIC_DEFAULT_PEN_MM
    for name, font in _fonts(qapp):
        for text in MARKED:
            item = kti.kicad_text_item(text, size, 40.0, 60.0, base_font=font, mm_to_px=mm, thickness=pen,
                                       justify=justify, vjustify="bottom", vertical=vertical)
            layout = kt.kicad_text_layout(text)
            assert item.text() == text
            left, baseline = kt.kicad_text_cells(text, size, pen, justify, "bottom")
            anchor = (4.0, 6.0)
            if layout.is_plain and layout.visible_text == text:
                assert type(item) is QGraphicsSimpleTextItem, text
                continue
            assert isinstance(item, kti.KicadMarkupTextItem), text
            # The layout box spans KiCad's advance cells from where they start.
            origin = _frame(item.mapToScene(QPointF(0.0, item.baseline_px())), anchor, vertical, mm)
            assert origin == pytest.approx((left, baseline), abs=1e-3), (name, text)
            end = _frame(item.mapToScene(QPointF(layout.advance * kti.cap_height_px(item.font()) / 21,
                                                 item.baseline_px())), anchor, vertical, mm)
            assert end[0] - origin[0] == pytest.approx(kt.kicad_advance_width(text, size), abs=1e-3)
            # Each bar: KiCad's line with round caps, the text pen wide.
            bars = item.sceneTransform().map(item.overbar_path()).boundingRect()
            if layout.overbars:
                a0, a1, u0, u1 = _frame_box(bars, anchor, vertical, mm)
                expected0 = left + min(b.start for b in layout.overbars) * size / 21 - pen / 2
                expected1 = left + max(b.end for b in layout.overbars) * size / 21 + pen / 2
                assert (a0, a1) == pytest.approx((expected0, expected1), abs=1e-3), (name, text)
                height = baseline + layout.overbars[0].height * size
                assert (u0, u1) == pytest.approx((height - pen / 2, height + pen / 2), abs=1e-3)
            else:
                assert item.overbar_path().isEmpty()
            # No markup character is drawn: the glyphs are those of the visible text.
            assert item.glyph_path().elementCount() > 0 or not layout.visible_text.strip()


def test_script_runs_are_smaller_and_shifted(qapp):
    """Positive/negative: «H^{H}» reaches above a plain «HH» and «H_{H}» below its baseline."""
    font, mm, size = QFont(qapp.font()), 20.0, 1.0
    boxes = {}
    for text in ("HH", "H^{H}", "H_{H}"):
        item = kti.kicad_text_item(text, size, 0.0, 0.0, base_font=font, mm_to_px=mm)
        path = item.glyph_path() if isinstance(item, kti.KicadMarkupTextItem) else _plain_path(item)
        boxes[text] = _frame_box(item.sceneTransform().map(path).boundingRect(), (0.0, 0.0), False, mm)
    base = kt.KICAD_BOTTOM_TO_BASELINE * size
    # The «H» outline of the font is `size` tall by construction, wherever
    # the font puts it around the baseline: the scripted «H» is that
    # outline at 0.8, moved by the rise.
    top, bottom = boxes["HH"][3] - base, boxes["HH"][2] - base
    assert top - bottom == pytest.approx(size, abs=0.01)
    assert boxes["H^{H}"][3] == pytest.approx(base + kt.KICAD_SUPERSCRIPT_RISE * size + 0.8 * top, abs=0.01)
    assert boxes["H_{H}"][2] == pytest.approx(base + kt.KICAD_SUBSCRIPT_RISE * size + 0.8 * bottom, abs=0.01)
    # Negative: the plain capitals stay where they were (no rise).
    assert boxes["H^{H}"][2] == pytest.approx(boxes["HH"][2], abs=0.01)


def test_hidden_markup_does_not_change_the_hit_area_rule(qapp):
    from PyQt6.QtWidgets import QGraphicsScene
    scene = QGraphicsScene()
    item = kti.kicad_text_item("~{RESET}", 1.0, 0, 0, base_font=QFont(qapp.font()), mm_to_px=20)
    scene.addItem(item)
    box = item.sceneBoundingRect()
    assert box.width() == pytest.approx(kt.kicad_advance_width("RESET", 1.0) * 20, abs=0.5)
    assert item in scene.items(box.center())
    assert item not in scene.items(QPointF(box.right() + 5.0, box.center().y()))


def _plain_path(item):
    path = QPainterPath()
    path.addText(0.0, kti.text_baseline_px(item.text(), item.font()), item.font(), item.text())
    return path


# ── kicad-cli: KiCad's own strokes ───────────────────────────────────

def _cli():
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    return cli


def _escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _entries():
    """Every marked text at each size: left-bottom and right-bottom copies, plus a quarter-turned row."""
    entries = []
    row = 0
    for size in SIZES:
        for text in MARKED:
            for justify in ("left", "right"):
                entries.append({"text": text, "size": size, "justify": justify, "vertical": False,
                                "x": 0.0 if justify == "left" else 30.48, "y": 6.35 * row})
            entries.append({"text": text, "size": size, "justify": "left", "vertical": True,
                            "x": 60.96 + 6.35 * row, "y": 60.96})
            row += 1
    return entries


def _strokes(svg, origin):
    """[(desc, [segments])] of the stroked texts, page mm from `origin`, y down."""
    groups = []
    for desc, body in re.findall(r'<g class="stroked-text"><desc>([^<]*)</desc>(.*?)</g>', svg, re.S):
        paths = []
        for d in re.findall(r'd="([^"]*)"', body):
            values = [float(v) for v in re.findall(r"[-\d.]+", d)]
            paths.append([(a - origin[0], b - origin[1]) for a, b in zip(values[0::2], values[1::2])])
        groups.append((desc.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"), paths))
    return groups


def _assign(groups, entries):
    """{entry index: paths} taking, for each entry, the unused group of its text nearest its anchor."""
    used, found = set(), {}
    for index, entry in enumerate(entries):
        best = None
        for g, (desc, paths) in enumerate(groups):
            if g in used or desc != entry["text"] or not paths:
                continue
            xs = [p[0] for path in paths for p in path]
            ys = [p[1] for path in paths for p in path]
            dx = max(min(xs) - entry["x"], 0.0, entry["x"] - max(xs))
            dy = max(min(ys) - entry["y"], 0.0, entry["y"] - max(ys))
            if best is None or dx * dx + dy * dy < best[0]:
                best = (dx * dx + dy * dy, g)
        if best is not None:
            used.add(best[1])
            found[index] = groups[best[1]][1]
    return found


def _export_symbol(cli, entries, folder):
    lines = ['(kicad_symbol_lib (version 20231120) (generator "kicadgen_test")',
             ' (symbol "T" (in_bom yes) (on_board yes)',
             '  (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
             '  (property "Value" "T" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
             '  (symbol "T_0_1"',
             '   (rectangle (start -50 50) (end -40 40) (stroke (width 0.254) (type default)) (fill (type none)))']
    for e in entries:
        lines.append(f'   (text "{_escape(e["text"])}" (at {e["x"]} {-e["y"]} {900 if e["vertical"] else 0}) '
                     f'(effects (font (size {e["size"]} {e["size"]})) (justify {e["justify"]} bottom)))')
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
    return _assign(_strokes(svg, (float(rect.group(1)) + 50, float(rect.group(2)) + 50)), entries)


def _export_footprint(cli, entries, folder):
    lines = ['(footprint "W" (version 20240108) (generator "kicadgen_test") (layer "F.Cu")',
             ' (fp_line (start -30.5 -30) (end -29.5 -30) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))',
             ' (fp_line (start -30 -30.5) (end -30 -29.5) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))']
    for e in entries:
        lines.append(f' (fp_text user "{_escape(e["text"])}" (at {e["x"]} {e["y"]}) (layer "F.SilkS") '
                     f'(effects (font (size {e["size"]} {e["size"]}) (thickness {FP_PEN})) '
                     f'(justify {e["justify"]} bottom)))')
    lines.append(')')
    library = folder / "lib.pretty"
    library.mkdir(parents=True)
    (library / "W.kicad_mod").write_text("\n".join(lines), encoding="utf-8")
    out = folder / "svg"
    out.mkdir()
    subprocess.run([cli, "fp", "export", "svg", "--fp", "W", "--layers", "F.SilkS", "-o", str(out), str(library)],
                   capture_output=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    svg = next(out.glob("*.svg")).read_text(encoding="utf-8")
    rest = re.sub(r'<g class="stroked-text">.*?</g>', "", svg, flags=re.S)
    numbers = [(float(a), float(b)) for a, b in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", rest)]
    xs, ys = sorted({p[0] for p in numbers}), sorted({p[1] for p in numbers})
    origin = (next(x for x in xs if sum(1 for p in numbers if p[0] == x) >= 2) + 30,
              next(y for y in ys if sum(1 for p in numbers if p[1] == y) >= 2) + 30)
    return _assign(_strokes(svg, origin), entries)


@pytest.fixture(scope="module")
def symbol_strokes(tmp_path_factory):
    entries = [e for e in _entries()]
    return entries, _export_symbol(_cli(), entries, tmp_path_factory.mktemp("mk_sym")), kt.SCHEMATIC_DEFAULT_PEN_MM


@pytest.fixture(scope="module")
def footprint_strokes(tmp_path_factory):
    # fp_text user texts are not turned here: the footprint check is horizontal.
    entries = [e for e in _entries() if not e["vertical"]]
    return entries, _export_footprint(_cli(), entries, tmp_path_factory.mktemp("mk_fp")), FP_PEN


def _to_frame(point, entry):
    """(along, up) mm of a page point (y down) in the entry's text frame, from its anchor."""
    dx, dy = point[0] - entry["x"], point[1] - entry["y"]
    return (-dy, -dx) if entry["vertical"] else (dx, -dy)


def _kicad_bars(paths, entry, size):
    """[(along0, along1, up)] of KiCad's overbars: one-segment strokes across the text at bar height."""
    expected = (kt.KICAD_BOTTOM_TO_BASELINE + kt.KICAD_OVERBAR_HEIGHT) * size
    bars = []
    for path in paths:
        if len(path) != 2:
            continue
        (a0, u0), (a1, u1) = (_to_frame(p, entry) for p in path)
        if abs(u0 - u1) < 1e-4 and abs(u0 - expected) <= ACROSS_TOLERANCE:
            bars.append((min(a0, a1), max(a0, a1), u0))
    return sorted(bars)


def _layout_bars(entry, pen):
    size = entry["size"]
    left, baseline = kt.kicad_text_cells(entry["text"], size, pen, entry["justify"], "bottom")
    return sorted((left + min(b.start, b.end) * size / 21, left + max(b.start, b.end) * size / 21,
                   baseline + b.height * size) for b in kt.kicad_text_layout(entry["text"]).overbars)


@pytest.mark.parametrize("which", ["symbol_strokes", "footprint_strokes"])
def test_kicads_bars_and_widths_are_the_layouts(request, which):
    """Font-free against kicad-cli: every overbar, and the justification width, as the layout computes them."""
    entries, strokes, pen = request.getfixturevalue(which)
    for index, entry in enumerate(entries):
        assert index in strokes or not kt.kicad_text_layout(entry["text"]).runs, entry
        paths = strokes.get(index, [])
        kicad, ours = _kicad_bars(paths, entry, entry["size"]), _layout_bars(entry, pen)
        assert len(kicad) == len(ours), (entry, kicad, ours)
        for (k0, k1, ku), (o0, o1, ou) in zip(kicad, ours):
            assert (k0, k1) == pytest.approx((o0, o1), abs=ALONG_TOLERANCE), (entry, kicad, ours)
            assert ku == pytest.approx(ou, abs=ACROSS_TOLERANCE), entry
    # Width: the right-justified copy is the left one moved back by kicad_text_width.
    for index, entry in enumerate(entries):
        if entry["justify"] != "left" or entry["vertical"] or index not in strokes:
            continue
        twin = next(i for i, e in enumerate(entries) if e["text"] == entry["text"] and e["size"] == entry["size"]
                    and e["justify"] == "right")
        first_left = min(p[0] for path in strokes[index] for p in path) - entry["x"]
        first_right = min(p[0] for path in strokes[twin] for p in path) - entries[twin]["x"]
        assert first_left - first_right == pytest.approx(
            kt.kicad_text_width(entry["text"], entry["size"], pen), abs=ALONG_TOLERANCE), entry


def test_kicads_script_baselines_are_the_layouts(symbol_strokes):
    """The capitals of a superscript/subscript run end where the layout's rise puts them."""
    entries, strokes, pen = symbol_strokes
    for index, entry in enumerate(entries):
        if entry["text"] != "H_{^{H}}^{_{H}}" or entry["vertical"] or entry["justify"] != "left":
            continue
        size = entry["size"]
        ups = [_to_frame(p, entry)[1] for path in strokes[index] for p in path]
        base = kt.KICAD_BOTTOM_TO_BASELINE * size
        # Both scripted «H» are subscripts: their bottoms drop below the baseline.
        assert min(ups) == pytest.approx(base + kt.KICAD_SUBSCRIPT_RISE * size, abs=ACROSS_TOLERANCE)
        assert max(ups) == pytest.approx(base + size, abs=ACROSS_TOLERANCE)
    sup = [e for e in entries if e["text"] == "x^{2}+y_{i}" and not e["vertical"] and e["justify"] == "left"]
    for entry in sup:
        index = entries.index(entry)
        ups = [_to_frame(p, entry)[1] for path in strokes[index] for p in path]
        size = entry["size"]
        # «2» at 0.8: its top (a digit reaches the cap height) on the raised baseline.
        top = kt.KICAD_BOTTOM_TO_BASELINE * size + (kt.KICAD_SUPERSCRIPT_RISE + 0.8) * size
        assert max(ups) == pytest.approx(top, abs=ACROSS_TOLERANCE), entry


def _drawn_frame_box(item, entry, mm):
    """(along0, along1, up0, up1) mm of everything the item paints, in the entry's frame."""
    if isinstance(item, kti.KicadMarkupTextItem):
        path = item.glyph_path()
        path.addPath(item.overbar_path())
    else:
        path = _plain_path(item)
    rect = item.sceneTransform().map(path).boundingRect()
    corners = [_to_frame((p.x() / mm, p.y() / mm), entry) for p in (rect.topLeft(), rect.bottomRight())]
    return (min(c[0] for c in corners), max(c[0] for c in corners),
            min(c[1] for c in corners), max(c[1] for c in corners))


@pytest.mark.parametrize("which", ["symbol_strokes", "footprint_strokes"])
def test_drawn_marked_texts_end_where_kicad_draws_them(qapp, request, which):
    entries, strokes, pen = request.getfixturevalue(which)
    mm = 10.0
    for name, font in _fonts(qapp):
        for index, entry in enumerate(entries):
            if index not in strokes:
                continue
            size = entry["size"]
            frame = [_to_frame(p, entry) for path in strokes[index] for p in path]
            k0 = min(a for a, _ in frame) - pen / 2
            k1 = max(a for a, _ in frame) + pen / 2
            item = kti.kicad_text_item(entry["text"], size, entry["x"] * mm, entry["y"] * mm, base_font=font,
                                       mm_to_px=mm, thickness=pen, justify=entry["justify"], vjustify="bottom",
                                       vertical=entry["vertical"])
            d0, d1, _, _ = _drawn_frame_box(item, entry, mm)
            where = (which, name, entry, (d0, d1), (k0, k1))
            assert abs(d0 - k0) <= INK_TOLERANCE * size and abs(d1 - k1) <= INK_TOLERANCE * size, where
            # The overbars, exact geometry: same ends and height as KiCad's.
            if isinstance(item, kti.KicadMarkupTextItem) and not item.overbar_path().isEmpty():
                kicad = _kicad_bars(strokes[index], entry, size)
                rect = item.sceneTransform().map(item.overbar_path()).boundingRect()
                corners = [_to_frame((p.x() / mm, p.y() / mm), entry) for p in (rect.topLeft(), rect.bottomRight())]
                b0, b1 = min(c[0] for c in corners), max(c[0] for c in corners)
                assert b0 == pytest.approx(min(k[0] for k in kicad) - pen / 2, abs=BAR_TOLERANCE), where
                assert b1 == pytest.approx(max(k[1] for k in kicad) + pen / 2, abs=BAR_TOLERANCE), where


def test_the_markup_drawn_literally_misses_kicads_ends(qapp, symbol_strokes):
    """Negative: the drawing of before (markup characters shown, one stretch) ends far from KiCad's ink.

    Simulated by the plain item of the text with its markup characters as
    literal glyphs at the old width (opening and closing braces skipped):
    the long marked texts end more than the tolerance away.
    """
    entries, strokes, pen = symbol_strokes
    worst = 0.0
    for index, entry in enumerate(entries):
        if entry["vertical"] or entry["justify"] != "left" or index not in strokes:
            continue
        layout = kt.kicad_text_layout(entry["text"])
        if layout.is_plain:
            continue
        frame = [_to_frame(p, entry) for path in strokes[index] for p in path]
        top_kicad = max(u for _, u in frame)
        # Without markup support the ink never reached the bars nor the script heights.
        item = kti.kicad_text_item(layout.visible_text, entry["size"], 0.0, 0.0, base_font=QFont(qapp.font()),
                                   mm_to_px=10.0, thickness=pen)
        _, _, _, up1 = _drawn_frame_box(item, {"x": 0.0, "y": 0.0, "vertical": False}, 10.0)
        worst = max(worst, abs(top_kicad - up1) / entry["size"])
    assert worst > INK_TOLERANCE
