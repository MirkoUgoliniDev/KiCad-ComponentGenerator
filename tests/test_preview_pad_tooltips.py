"""@brief Hovering a pad of the 2D preview shows its pin from the pinout table (user, 27/09)."""
import pytest

pytest.importorskip("pyvistaqt")
from PyQt6.QtWidgets import QGraphicsSimpleTextItem

from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, pad_positions,
)
from test_contact_style import dev_kit


def _tips(window):
    """Tooltip of every copper item and pad number of the scene, by kind."""
    copper, numbers = [], []
    for item in window.preview_scene.items():
        if not item.toolTip():
            continue
        (numbers if isinstance(item, QGraphicsSimpleTextItem) else copper).append(item.toolTip())
    return copper, numbers


@pytest.mark.parametrize("style", [CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE,
                                   CONTACT_HALF_AND_THROUGH])
def test_every_castellated_pad_and_its_number_carry_the_pin_function(window, style):
    params = dev_kit(style)
    # Pins given in both forms the extraction can produce: int and text.
    window.pinout = [{"pin": 1, "name": "3V3 (supply)"}, {"pin": "23", "name": "GND"}]
    window._draw_preview_castellated(params)
    copper, numbers = _tips(window)
    count = len(list(pad_positions(params)))
    assert len(copper) == len(numbers) == count
    assert "Pin 1: 3V3 (supply)" in copper and "Pin 1: 3V3 (supply)" in numbers
    assert "Pin 23: GND" in copper


def test_a_pin_missing_from_the_table_shows_only_its_number(window):
    """Negative: nothing is invented for a pin the documentation does not describe."""
    window.pinout = [{"pin": 1, "name": "3V3"}]
    window._draw_preview_castellated(dev_kit(CONTACT_HALF_HOLE))
    copper, _ = _tips(window)
    assert "Pin 2" in copper
    assert not any(tip.startswith("Pin 2:") for tip in copper)


def test_the_tooltip_follows_the_table_not_the_drawing_order(window):
    window.pinout = [{"pin": "A2", "name": "SDA"}, {"pin": 10, "name": "RESET"}]
    assert window._pad_tooltip("A2") == "Pin A2: SDA"
    assert window._pad_tooltip(10) == window._pad_tooltip("10") == "Pin 10: RESET"
    assert window._pad_tooltip(" 10 ") == "Pin 10: RESET"
    window.pinout = [{"pin": 3, "name": "  "}]
    assert window._pad_tooltip(3) == "Pin 3"


def test_matrix_pads_carry_the_tooltip_too(window):
    from kicadgen.generators.pin_header import PinHeaderParams
    window.pinout = [{"pin": 2, "name": "TX"}]
    window._last_params = PinHeaderParams(rows=1, cols=3, pitch=2.54)
    window._draw_preview()
    copper, numbers = _tips(window)
    assert "Pin 2: TX" in copper and "Pin 2: TX" in numbers and "Pin 1" in copper
