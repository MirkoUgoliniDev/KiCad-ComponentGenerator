"""@brief Property and pin texts drawn at the size the KiCad file gives them (user, 2026-10-15).

KiCad's text size is the cap height. Before, the symbol editor drew Reference,
Value and pin names with a fixed 12 px font (capitals ~0.66 of KiCad's) and pin
numbers with 10 px (~0.55); the footprint preview drew REF**/Value with a 1 mm
em (capitals ~0.7 mm for a 1.0 mm text). Positive: every such text has capitals
as tall as its size in the file, within 3 %. Negative: a pin with its own text
size is drawn at that size, not at the default, so the rule follows the data.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QPainterPath
from PyQt6.QtWidgets import QGraphicsSimpleTextItem

from kicadgen.core.footprint import (PROPERTY_TEXT_SIZE, PROPERTY_TEXT_THICKNESS, USER_TEXT_ANCHOR,
                                     validate_footprint_drawings)
from kicadgen.core.kicad_text import (KICAD_BOTTOM_TO_BASELINE, KICAD_CENTRE_TO_BOTTOM, kicad_text_cells,
                                      kicad_text_margin)
from kicadgen.generators.symbol import PIN_NAME_OFFSET, TEXT_SIZE
from kicadgen.gui.symbol_tab import PIN_NUMBER_GAP_MM, SCENE_UNITS_PER_MM, SymbolTab

S = SCENE_UNITS_PER_MM


def _cap(item):
    """Scene height of the outline of «H» in the item's font, scaled with the item."""
    path = QPainterPath()
    path.addText(0.0, 0.0, item.font(), "H")
    return path.boundingRect().height() * item.scale()


def _baseline_y(item):
    """Scene y of an unrotated text item's baseline."""
    return item.mapToScene(0, SymbolTab._text_baseline(item.text(), item.font())).y()


def _symbol_tab():
    rows = [{"pin": str(i), "name": f"P{i}"} for i in range(1, 5)]
    rows[1]["symbol_pin_text_size"] = 2.54
    tab = SymbolTab()
    tab.resize(1200, 800)
    tab.set_data("Board", rows, pad_ids=["1", "2", "3", "4"])
    tab._draw(tab._symbol)
    return tab


def _items(tab, text):
    return [item for item in tab.view.scene().items()
            if isinstance(item, QGraphicsSimpleTextItem) and item.text() == text]


def test_symbol_property_and_pin_texts_have_capitals_as_tall_as_the_file_size(qapp):
    tab = _symbol_tab()
    try:
        for text in ("U?", "Board", "P1", "1", "P3", "3"):
            (item,) = _items(tab, text)
            assert _cap(item) == pytest.approx(TEXT_SIZE * S, rel=0.03), text
        # Negative: pin 2 has its own size; its texts follow it, not the default.
        for text in ("P2", "2"):
            (item,) = _items(tab, text)
            assert _cap(item) == pytest.approx(2.54 * S, rel=0.03), text
            assert _cap(item) > 1.5 * TEXT_SIZE * S
    finally:
        tab.deleteLater()


def test_symbol_pin_number_sits_above_the_line_and_the_name_is_centred_on_it(qapp):
    tab = _symbol_tab()
    try:
        horizontal = [p for p in tab._symbol.pins if p.angle in (0, 180)]
        assert horizontal, "the default layout has pins on the left/right sides"
        for placed in horizontal:
            line_y = -placed.y * S
            (number,) = _items(tab, placed.pin.number)
            (name,) = _items(tab, placed.pin.name)
            # CONTRACT CHANGED 2026-10-16 (user: KiCad's width and box,
            # gui/kicad_text_item.py): the number is a bottom-justified text
            # anchored PIN_NUMBER_GAP_MM above the line, so its baseline lies
            # KiCad's 0.125 x size higher; the name is vertically centred on
            # the line as KiCad centres it (baseline 0.46 x size below).
            from kicadgen.generators.symbol import pin_text_size_of
            size = pin_text_size_of(placed.pin)
            assert _baseline_y(number) == pytest.approx(
                line_y - (PIN_NUMBER_GAP_MM + KICAD_BOTTOM_TO_BASELINE * size) * S, abs=0.05)
            assert number.sceneBoundingRect().center().y() < line_y, "the number is above the pin"
            assert _baseline_y(name) == pytest.approx(
                line_y + (KICAD_CENTRE_TO_BOTTOM - KICAD_BOTTOM_TO_BASELINE) * size * S, abs=0.05)
            # The name's cells start PIN_NAME_OFFSET past the inner end of the
            # pin plus KiCad's half pen margin.
            from kicadgen.generators.symbol import pin_length_of
            inner = (placed.x + (1 if placed.angle == 0 else -1)
                     * pin_length_of(placed.pin, tab._symbol.pin_length)) * S
            edge = name.sceneBoundingRect().left() if placed.angle == 0 else name.sceneBoundingRect().right()
            half_margin = kicad_text_margin(size) / 2
            assert abs(edge - inner) == pytest.approx((PIN_NAME_OFFSET + half_margin) * S, abs=0.05)
    finally:
        tab.deleteLater()


@pytest.fixture
def preview(window, monkeypatch):
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    window._draw_preview()
    return window


def _property_text(window, key):
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    return next(i for i in window.preview_scene.items()
                if isinstance(i, QGraphicsSimpleTextItem) and i.data(TEXT_KEY_ROLE) == key)


def _drawn_text(window):
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    return next(i for i in window.preview_scene.items()
                if isinstance(i, QGraphicsSimpleTextItem) and i.data(DRAWING_KEY_ROLE) == 0)


def test_footprint_reference_and_value_have_capitals_as_tall_as_the_file_size(preview):
    """Measured against a user text of the same size in the same scene (same mm-to-px)."""
    from kicadgen.gui.main_window import _drawing_text_baseline
    window = preview
    window.footprint_drawings = validate_footprint_drawings([
        {"kind": "text", "points": [[1, 5]], "layer": "F.SilkS", "text": "H",
         "size": PROPERTY_TEXT_SIZE, "anchor": USER_TEXT_ANCHOR}])
    window._draw_preview()
    reference = _drawn_text(window)
    for key in ("Reference", "Value"):
        item = _property_text(window, key)
        assert _cap(item) == pytest.approx(_cap(reference), rel=0.03), key
        # CONTRACT CHANGED 2026-10-16 (twice): drawn from the anchor (the
        # group's origin) as KiCad draws (justify left bottom): cells from
        # half the pen margin to the right, baseline 0.125 x size above.
        origin = item.parentItem().mapToScene(0, 0)
        baseline = item.mapToScene(0, _drawing_text_baseline(item)).y()
        left, up = kicad_text_cells(item.text(), PROPERTY_TEXT_SIZE, PROPERTY_TEXT_THICKNESS)
        assert baseline == pytest.approx(origin.y() - up * 20, abs=0.05), key
        assert item.sceneBoundingRect().left() == pytest.approx(origin.x() + left * 20, abs=0.05), key
    # Negative: a user text of another size is not drawn like the properties.
    window.footprint_drawings = validate_footprint_drawings([
        {"kind": "text", "points": [[1, 5]], "layer": "F.SilkS", "text": "H",
         "size": 2 * PROPERTY_TEXT_SIZE, "anchor": USER_TEXT_ANCHOR}])
    window._draw_preview()
    assert _cap(_drawn_text(window)) == pytest.approx(2 * _cap(_property_text(window, "Reference")), rel=0.03)
