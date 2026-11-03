"""@brief Alphanumeric pin labels in the window (review 2026-11-03, after the extraction fix).

The extraction now returns pins as printed («A1», «EP»); the window keys
them as text and keeps digits-only labels as ints, so numeric grids behave as
before. Positive and negative cases differ by the label's structure.
"""
import pytest

from kicadgen.gui.main_window import _pin_map_key


def test_a_digits_only_label_stays_an_int_and_a_lettered_one_text():
    assert _pin_map_key("11") == 11 and _pin_map_key(" 3 ") == 3
    assert _pin_map_key("A1") == "A1" and _pin_map_key("EP") == "EP"


@pytest.mark.parametrize("bad", ["", " ", "A 1", "-1", True, "pin uno"])
def test_a_label_that_is_not_a_pin_is_refused(bad):
    with pytest.raises(ValueError):
        _pin_map_key(bad)


def test_populated_pins_accept_labels_and_refuse_junk(window):
    window.field_populated_pins.setText("1, A1, 14")
    assert window._parse_populated_pins() == {1, "A1", 14}
    window.field_populated_pins.setText("1, A 1")
    with pytest.raises(ValueError):
        window._parse_populated_pins()


def test_a_mixed_pin_map_is_saved_in_pin_order(window):
    window.pin_map = {"A2": (1, 1), 11: (0, 0), "A1": (0, 1), 2: (1, 0)}
    saved = window._project_state()["pin_map"]
    assert [entry[0] for entry in saved] == [2, 11, "A1", "A2"]
