"""@brief Pin numbers where KiCad draws them (user decision, 2026-10-16).

KiCad 10 anchors a pin number, bottom-justified, 14 mil (0.3556 mm) from its
pin line (text offset 4 mil + PIN_TEXT_MARGIN 4 mil + pin pen 6 mil), so its
baseline lies 0.3556 + 0.125 x size from the line, on horizontal and
vertical pins alike (`kicadgen.core.kicad_text.kicad_pin_number_baseline`).
The editor used 0.178 mm before, about 0.18 mm too low, and the symbol
generator reserved «0.508 mm + size» above a horizontal pin for its number,
which KiCad's numbers exceed from size 1.27 upwards.

Positive: the editor's number baseline lies within 0.02 mm of kicad-cli's
at several sizes and all four pin angles; the reserved height covers the
number KiCad draws at 2.54 mm. Negative: the old 0.178 mm anchor and the old
reservation fail the same checks; a default-size (1.27 mm) symbol keeps its
property positions, because the rule's larger reservation still fits below
the body's top edge.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import re
import subprocess

import pytest
from PyQt6.QtWidgets import QGraphicsSimpleTextItem

from kicadgen.core.kicad_text import (KICAD_BOTTOM_TO_BASELINE, KICAD_PIN_NUMBER_ANCHOR_MM, KICAD_PIN_PEN_MM,
                                      kicad_pin_number_baseline, kicad_pin_number_extent)
from kicadgen.generators.symbol import (PROPERTY_GRID_MM, TEXT_SIZE, SymbolPin, default_property_positions,
                                        layout_symbol, pin_length_of, pin_text_size_of)
from kicadgen.services.system_dependencies import find_kicad_cli

#: Pin number baselines above the line measured with kicad-cli 10.0
#: `sym export svg` on «H» strokes (2026-10-16), mm, {size: height}; the
#: same at angles 0, 90, 180 and 270.
MEASURED_BASELINE = {0.635: 0.4413, 1.0: 0.4860, 1.27: 0.5191, 1.5: 0.5472, 2.0: 0.6084, 2.54: 0.6745}
#: The editor's anchor before 2026-10-16: 4 mil margin + half the 6 mil pen.
OLD_ANCHOR_MM = 0.1016 + 0.0762
#: The generator's reservation before 2026-10-16: fixed gap + one text size.
OLD_GAP_BOUND_MM = 0.508
#: Tolerance of the editor against KiCad (user request): 0.02 mm.
TOLERANCE_MM = 0.02
SIZES = (0.635, 1.27, 2.54)
SIDES = ("left", "right", "top", "bottom")


def test_the_rule_reproduces_the_measurements():
    assert KICAD_PIN_NUMBER_ANCHOR_MM == pytest.approx(0.3556, abs=1e-9), "14 mil"
    for size, measured in MEASURED_BASELINE.items():
        assert kicad_pin_number_baseline(size) == pytest.approx(measured, abs=0.007), size
        # Negative: the old anchor is about 0.18 mm too low at every size.
        old = OLD_ANCHOR_MM + KICAD_BOTTOM_TO_BASELINE * size
        assert measured - old > 0.17, size


def test_the_reservation_covers_kicads_number_and_the_old_one_did_not():
    for size in (1.27, 1.5, 2.0, 2.54, 5.08):
        # Ink top of a capital/digit: baseline + cap (centreline) + half pen.
        ink = kicad_pin_number_baseline(size) + size + KICAD_PIN_PEN_MM / 2
        assert kicad_pin_number_extent(size) >= ink
        assert OLD_GAP_BOUND_MM + size < ink, f"the old bound was already enough at {size}"
    # Below about 0.5 mm the old bound was larger: the rule depends on the size.
    assert kicad_pin_number_extent(0.5) < OLD_GAP_BOUND_MM + 0.5 + 0.1


def _side_pins(size):
    """Four pins per side, all with the number text size `size`."""
    return [SymbolPin(str(i), f"P{i}", "input", "left" if i <= 4 else "right", text_size=size)
            for i in range(1, 9)]


def test_default_size_symbols_keep_their_property_positions():
    layout = layout_symbol(_side_pins(TEXT_SIZE))
    h = layout.height / 2
    assert default_property_positions(layout) == {"Reference": (0.0, round(h + 5.08, 4)),
                                                  "Value": (0.0, round(h + 2.54, 4))}


def test_a_large_number_in_the_top_row_lifts_the_properties_above_it():
    # 1.905 mm: between the new threshold (1.798 mm) and the old one (2.032 mm).
    layout = layout_symbol(_side_pins(1.905))
    h = layout.height / 2
    top_row = max(p.y for p in layout.pins)
    number_top = top_row + kicad_pin_number_extent(1.905)
    assert number_top > h, "the number reaches above the body"
    assert top_row + OLD_GAP_BOUND_MM + 1.905 < h, "the old estimate did not see it"
    value = default_property_positions(layout)["Value"][1]
    assert value == pytest.approx(h + PROPERTY_GRID_MM + 2.54)
    assert value - TEXT_SIZE / 2 > number_top


# -- kicad-cli: the editor's baseline against KiCad's strokes ------------

def _cli():
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("KiCad CLI not installed")
    return cli


def _rows():
    """Rows with every side x every size; numbers start with «H» so the lowest stroke is the baseline."""
    rows, n = [], 0
    for side in SIDES:
        for size in SIZES:
            n += 1
            rows.append({"pin": f"H{n}", "name": f"N{n}", "symbol_side": side, "symbol_pin_text_size": size})
    return rows


def _symbol_tab():
    from kicadgen.gui.symbol_tab import SymbolTab
    rows = _rows()
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data("T", rows, pad_ids=[row["pin"] for row in rows])
    tab._draw(tab._symbol)
    return tab


def _kicad_number_baselines(cli, symbol, folder):
    """{number: baseline height from the pin line, mm} of the pins of `symbol` exported by kicad-cli."""
    lines = ['(kicad_symbol_lib (version 20241209) (generator "kicadgen_test")',
             ' (symbol "T" (pin_names (offset 0.508)) (in_bom yes) (on_board yes)',
             '  (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))',
             '  (property "Value" "T" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))',
             '  (symbol "T_0_1"',
             '   (rectangle (start -100 100) (end -90 90) (stroke (width 0.254) (type default)) (fill (type none)))',
             '  )', '  (symbol "T_1_1"']
    for placed in symbol.pins:
        size = pin_text_size_of(placed.pin)
        length = pin_length_of(placed.pin, symbol.pin_length)
        lines.append(f'   (pin passive line (at {placed.x} {placed.y} {placed.angle}) (length {length}) '
                     f'(name "{placed.pin.name}" (effects (font (size {size} {size})))) '
                     f'(number "{placed.pin.number}" (effects (font (size {size} {size})))))')
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
    ox, oy = float(rect.group(1)) + 100, float(rect.group(2)) + 100
    strokes = {}
    for desc, body in re.findall(r'<g class="stroked-text"><desc>([^<]*)</desc>(.*?)</g>', svg, re.S):
        values = [float(v) for v in re.findall(r"[-\d.]+", " ".join(re.findall(r'd="([^"]*)"', body)))]
        strokes[desc] = [(x - ox, y - oy) for x, y in zip(values[0::2], values[1::2])]
    found = {}
    for placed in symbol.pins:
        points = strokes[placed.pin.number]
        if placed.angle in (0, 180):
            # Page y grows down; the line is at -placed.y, the number above it.
            found[placed.pin.number] = -placed.y - max(y for _, y in points)
        else:
            # Reading up the page, glyph tops towards -x: left of the line.
            found[placed.pin.number] = placed.x - max(x for x, _ in points)
    return found


def _editor_number_baselines(tab, anchor_shift_mm=0.0):
    """{number: baseline height from the pin line, mm} of the numbers the editor draws."""
    from kicadgen.gui.kicad_text_item import text_baseline_px
    from kicadgen.gui.symbol_tab import SCENE_UNITS_PER_MM as S
    items = {item.text(): item for item in tab.view.scene().items()
             if isinstance(item, QGraphicsSimpleTextItem) and isinstance(item.data(0), tuple)
             and item.data(0)[0] == "pin" and item.data(0)[1] == item.text()}
    found = {}
    for placed in tab._symbol.pins:
        item = items[placed.pin.number]
        point = item.mapToScene(0, text_baseline_px(item.text(), item.font()))
        if placed.angle in (0, 180):
            # Scene y grows down: symbol y of the baseline is -scene y / S.
            found[placed.pin.number] = -point.y() / S - placed.y
        else:
            found[placed.pin.number] = placed.x - point.x() / S
        found[placed.pin.number] += anchor_shift_mm
    return found


@pytest.fixture(scope="module")
def kicad_and_editor(qapp, tmp_path_factory):
    cli = _cli()
    tab = _symbol_tab()
    try:
        angles = {placed.angle for placed in tab._symbol.pins}
        assert angles == {0, 90, 180, 270}, "every pin orientation is exercised"
        kicad = _kicad_number_baselines(cli, tab._symbol, tmp_path_factory.mktemp("pinnum"))
        editor = _editor_number_baselines(tab)
        sizes = {placed.pin.number: pin_text_size_of(placed.pin) for placed in tab._symbol.pins}
        angle_of = {placed.pin.number: placed.angle for placed in tab._symbol.pins}
    finally:
        tab.deleteLater()
    return kicad, editor, sizes, angle_of


def test_editor_pin_numbers_sit_on_kicads_baseline(kicad_and_editor):
    kicad, editor, sizes, angle_of = kicad_and_editor
    for number, height in kicad.items():
        assert editor[number] == pytest.approx(height, abs=TOLERANCE_MM), (number, sizes[number], angle_of[number])
        # The rule itself, against KiCad, at every size and orientation.
        assert kicad_pin_number_baseline(sizes[number]) == pytest.approx(height, abs=0.007)


def test_the_old_anchor_fails_the_same_comparison(kicad_and_editor):
    kicad, editor, sizes, _ = kicad_and_editor
    shift = OLD_ANCHOR_MM - KICAD_PIN_NUMBER_ANCHOR_MM
    for number, height in kicad.items():
        assert abs(editor[number] + shift - height) > 5 * TOLERANCE_MM, number


def test_kicads_drawn_number_fits_the_reserved_height(kicad_and_editor):
    """Ink top of KiCad's horizontal-pin numbers against the reservation, at every size."""
    kicad, _, sizes, angle_of = kicad_and_editor
    for number, baseline in kicad.items():
        if angle_of[number] not in (0, 180):
            continue
        size = sizes[number]
        ink_top = baseline + size + KICAD_PIN_PEN_MM / 2
        assert ink_top <= kicad_pin_number_extent(size) + 1e-6, number
        if size >= 1.27:
            assert ink_top > OLD_GAP_BOUND_MM + size, f"{number}: the old bound covered size {size}"
