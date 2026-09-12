"""Test sulla mappa esplicita dei pin e sulla specchiatura delle bottom view.

Due errori distinti che questi test presidiano:
1. numerare i pin colonna per colonna (convenzione dei pin header) su un package
   DIP-style, che invece numera a U -> pad con numeri sbagliati;
2. usare le coordinate di una BOTTOM VIEW senza specchiarle -> footprint
   speculare, con i fori che non coincidono col componente reale.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.generators.pin_header import PinHeaderParams, pin_positions
from kicadgen.services.datasheet_extractor import _pin_map_seen_from_top

# Layout reale del TRACO TEL8WI letto dalla sua bottom view.
TEL8WI_BOTTOM_VIEW = [
    {"pin": 1, "row": 0, "col": 0}, {"pin": 7, "row": 0, "col": 6},
    {"pin": 8, "row": 0, "col": 7}, {"pin": 9, "row": 1, "col": 7},
    {"pin": 10, "row": 1, "col": 6}, {"pin": 16, "row": 1, "col": 0},
]


def test_bottom_view_is_mirrored():
    seen_from_top = {e["pin"]: (e["row"], e["col"])
                     for e in _pin_map_seen_from_top(TEL8WI_BOTTOM_VIEW, 8, "bottom")}
    assert seen_from_top[1] == (0, 7)
    assert seen_from_top[8] == (0, 0)
    assert seen_from_top[16] == (1, 7)


def test_top_view_is_left_untouched():
    seen_from_top = {e["pin"]: (e["row"], e["col"])
                     for e in _pin_map_seen_from_top(TEL8WI_BOTTOM_VIEW, 8, "top")}
    assert seen_from_top[1] == (0, 0)
    assert seen_from_top[8] == (0, 7)


def test_missing_pin_map_stays_none():
    assert _pin_map_seen_from_top(None, 8, "bottom") is None


def test_pin_map_overrides_column_major_numbering():
    pin_map = {1: (0, 7), 7: (0, 1), 8: (0, 0), 9: (1, 0), 10: (1, 1), 16: (1, 7)}
    params = PinHeaderParams(
        name="TEL8WI", rows=2, cols=8, pitch=2.54, row_spacing=10.16,
        pad_diameter=1.0, drill=0.5, populated_pins=set(pin_map), pin_map=pin_map,
    )
    positions = {pin: (round(x, 2), round(y, 2)) for pin, x, y in pin_positions(params)}

    # Pin 7 e 8 adiacenti sulla stessa fila, come nel datasheet.
    assert positions[7][1] == positions[8][1] == 0.0
    assert abs(positions[7][0] - positions[8][0]) == 2.54
    # Pin 1 e 16 agli estremi opposti, uno per fila.
    assert positions[1][0] == positions[16][0] == 17.78
    assert positions[1][1] == 0.0 and positions[16][1] == 10.16


def test_pin_map_rejects_position_outside_matrix():
    params = PinHeaderParams(
        name="X", rows=2, cols=4, pitch=2.54, pad_diameter=1.0, drill=0.5,
        pin_map={1: (0, 9)},
    )
    with pytest.raises(ValueError, match="fuori dalla matrice"):
        params.validate()


def test_pin_map_rejects_two_pins_in_same_position():
    params = PinHeaderParams(
        name="X", rows=2, cols=4, pitch=2.54, pad_diameter=1.0, drill=0.5,
        pin_map={1: (0, 0), 2: (0, 0)},
    )
    with pytest.raises(ValueError, match="stessa"):
        params.validate()
