"""@brief Default Reference/Value positions clear the pins drawn above the body (2026-10-15).

Defect: the defaults were fixed at h + 2.54 (Value) and h + 5.08 (Reference),
h being the half body height, so a pin on the top side, which sticks out of
the body by its own length, had the Value text drawn over its tip, in the
editor and in KiCad alike. The rule now lifts the base line from the body
top to the highest point a pin draws (its line and its number), rounded up
to the 1.27 mm grid. Symbols with no pin above the body keep exactly the
positions, and the file, of before.
"""
import math

import pytest

from kicadgen.generators.symbol import (
    PIN_NAME_OFFSET, PROPERTY_GRID_MM, SymbolPin, TEXT_SIZE, default_property_positions, layout_symbol,
    serialize_symbol,
)

#: Value is drawn centred on its anchor at TEXT_SIZE: its lower edge is half
#: a text height below the anchor. Minimum gap required between that edge
#: and anything a pin draws, mm (2.54 - 1.27 / 2 with the default text).
CLEARANCE_MM = 2.54 - TEXT_SIZE / 2


def _old_defaults(layout):
    """The positions every symbol had before the rule: fixed above the body top."""
    h = layout.height / 2
    return {'Reference': (0.0, round(h + 5.08, 4)), 'Value': (0.0, round(h + 2.54, 4))}


def _on_grid(value):
    return math.isclose(value / PROPERTY_GRID_MM, round(value / PROPERTY_GRID_MM), abs_tol=1e-7)


def _highest_top_tip(layout):
    """Outer end of the highest top pin, mm (its connection point, angle 270)."""
    return max(p.y for p in layout.pins if p.angle == 270)


def _property_line(text, key):
    return next(line for line in text.splitlines() if line.strip().startswith(f'(property "{key}"'))


SIDE_PINS = [SymbolPin('1', 'VCC', 'power_in', 'left'), SymbolPin('2', 'GND', 'power_in', 'left'),
             SymbolPin('3', 'TX', 'output', 'right'), SymbolPin('4', 'RX', 'input', 'right')]


# -- Positive: pins above the body lift the properties ---------------------

@pytest.mark.parametrize('length', [None, 5.08, 7.62, 10.16, 12.7])
def test_top_pin_lifts_value_and_reference_above_its_tip(length):
    pins = SIDE_PINS + [SymbolPin('5', 'EN', 'input', 'top', length=length)]
    layout = layout_symbol(pins)
    tip = _highest_top_tip(layout)
    assert tip > layout.height / 2, 'precondition: the top pin sticks out of the body'
    positions = default_property_positions(layout)
    value_y, reference_y = positions['Value'][1], positions['Reference'][1]
    assert value_y - TEXT_SIZE / 2 >= tip + CLEARANCE_MM - 1e-7, 'Value clears the pin tip'
    assert reference_y - value_y == pytest.approx(2.54), 'Reference keeps its line above Value'
    assert positions['Value'][0] == positions['Reference'][0] == 0.0
    assert _on_grid(value_y) and _on_grid(reference_y)
    # With a short number the tip itself is the base line: exactly tip + 2.54.
    assert value_y == pytest.approx(tip + 2.54)


def test_the_highest_of_several_top_pins_decides():
    pins = SIDE_PINS + [SymbolPin('5', 'A', 'input', 'top', length=2.54),
                        SymbolPin('6', 'B', 'input', 'top', length=10.16),
                        SymbolPin('7', 'C', 'input', 'top', length=5.08)]
    layout = layout_symbol(pins)
    assert default_property_positions(layout)['Value'][1] == pytest.approx(layout.height / 2 + 10.16 + 2.54)


def test_a_pin_number_longer_than_its_pin_is_cleared_too():
    # A vertical pin's number is rotated and centred on the line: when it is
    # longer than the pin, half the excess pokes above the tip.
    short = layout_symbol(SIDE_PINS + [SymbolPin('5', 'EN', 'input', 'top')])
    long_number = layout_symbol(SIDE_PINS + [SymbolPin('12345', 'EN', 'input', 'top', text_size=2.54)])
    tip = _highest_top_tip(long_number)
    text_top = tip - 2.54 / 2 + 5 * 2.54 / 2  # middle of the pin + half the text length bound
    value_y = default_property_positions(long_number)['Value'][1]
    assert value_y - TEXT_SIZE / 2 >= text_top + CLEARANCE_MM - 1e-7
    assert _on_grid(value_y)
    assert value_y > default_property_positions(short)['Value'][1]


def test_a_freely_placed_pin_above_the_body_is_cleared_by_its_geometry_not_its_side():
    # The side field says «left», but the pin was dragged above the body and
    # points down at it: what counts is where it is drawn.
    layout = layout_symbol(SIDE_PINS)
    h = layout.height / 2
    moved = SymbolPin('9', 'X', 'passive', 'left', position=(2.54, h + 5.08), angle=270)
    lifted = layout_symbol(SIDE_PINS + [moved])
    assert default_property_positions(lifted)['Value'][1] == pytest.approx(h + 5.08 + 2.54)


def test_the_written_file_uses_the_lifted_positions():
    pins = SIDE_PINS + [SymbolPin('5', 'EN', 'input', 'top', length=5.08)]
    layout = layout_symbol(pins)
    positions = default_property_positions(layout)
    text = serialize_symbol('ESP_Module', pins)
    assert f'(at 0 {positions["Value"][1]:g} 0)' in _property_line(text, 'Value')
    assert f'(at 0 {positions["Reference"][1]:g} 0)' in _property_line(text, 'Reference')
    assert positions != _old_defaults(layout)


# -- Negative: nothing above the body, nothing moves ------------------------

@pytest.mark.parametrize('pins', [
    SIDE_PINS,
    SIDE_PINS + [SymbolPin('5', 'GND', 'power_in', 'bottom', length=12.7)],
    [SymbolPin('1', 'A', 'passive', 'left', length=12.7), SymbolPin('2', 'B', 'passive', 'right', length=7.62)],
    [SymbolPin(str(n), f'P{n}', 'passive', 'left') for n in range(1, 21)]
    + [SymbolPin(str(n), f'P{n}', 'passive', 'right') for n in range(21, 41)],
    [SymbolPin('100', 'LONGNUMBER', 'passive', 'bottom')] + SIDE_PINS,
], ids=['sides', 'long-bottom-pin', 'long-side-pins', 'many-side-pins', 'bottom-long-number'])
def test_without_pins_above_the_body_the_positions_are_those_of_before(pins):
    layout = layout_symbol(pins)
    assert default_property_positions(layout) == _old_defaults(layout)


@pytest.mark.parametrize('body', [None, (20.32, 20.32), (30.48, 50.8)])
def test_without_pins_above_the_body_the_file_is_byte_identical(body):
    # The old file wrote the fixed defaults; writing them as an explicit
    # choice reproduces it exactly, so equality proves byte identity.
    pins = SIDE_PINS + [SymbolPin('5', 'GND', 'power_in', 'bottom')]
    layout = layout_symbol(pins, body)
    assert serialize_symbol('Board', pins, body=body) == serialize_symbol(
        'Board', pins, body=body, properties=_old_defaults(layout))


def test_the_known_default_lines_are_unchanged():
    # Golden lines of a two-pin symbol (body 10.16 x 10.16 mm, h = 5.08).
    text = serialize_symbol('Board', [SymbolPin('1', 'A', 'passive', 'left'), SymbolPin('2', 'B', 'passive', 'right')])
    assert '(property "Reference" "U" (at 0 10.16 0)' in _property_line(text, 'Reference')
    assert '(property "Value" "Board" (at 0 7.62 0)' in _property_line(text, 'Value')


def test_explicit_positions_are_never_touched_by_the_rule():
    pins = SIDE_PINS + [SymbolPin('5', 'EN', 'input', 'top', length=12.7)]
    text = serialize_symbol('Board', pins, properties={'Value': (0.0, 2.54), 'Reference': (-7.62, -12.7)})
    assert '(at 0 2.54 0)' in _property_line(text, 'Value')
    assert '(at -7.62 -12.7 0)' in _property_line(text, 'Reference')


# -- Review M2 (2026-10-15): a pin name drawn above the body counts ---------
#
# The name is drawn PIN_NAME_OFFSET beyond the inner end and runs on along
# the pin direction. `layout_symbol` always points pins at the body, so the
# name lands inside; a pin rotated or placed to point away from the body
# draws it outside, and above the body it can collide with Value.

def _name_top(placed, length=2.54, size=TEXT_SIZE):
    """Upper bound of a pin name on an upward vertical pin, mm."""
    return placed.y + length + PIN_NAME_OFFSET + len(placed.pin.name) * size


def test_a_pin_pointing_up_from_the_body_top_clears_its_name():
    # Repro of the review: connection on the body top edge, angle 90 (pointing
    # up), long name drawn from 8.128 to ~23.37 mm with h = 5.08.
    layout = layout_symbol(SIDE_PINS)
    h = layout.height / 2
    up = SymbolPin('5', 'VERYLONGNAME', 'input', 'top', position=(0, h), angle=90)
    lifted = layout_symbol(SIDE_PINS + [up])
    placed = next(p for p in lifted.pins if p.pin.number == '5')
    value_y = default_property_positions(lifted)['Value'][1]
    assert value_y - TEXT_SIZE / 2 >= _name_top(placed) + CLEARANCE_MM - 1e-7, 'Value clears the name'
    assert _on_grid(value_y)


def test_a_short_name_on_an_upward_pin_still_counts_beyond_the_tip():
    # The line alone ends at h + 2.54; a one-character name reaches above it.
    layout = layout_symbol(SIDE_PINS)
    h = layout.height / 2
    up = layout_symbol(SIDE_PINS + [SymbolPin('5', 'E', 'input', 'top', position=(0, h), angle=90)])
    nameless = layout_symbol(SIDE_PINS + [SymbolPin('5', '', 'input', 'top', position=(0, h), angle=90)])
    assert default_property_positions(nameless)['Value'][1] == pytest.approx(h + 2.54 + 2.54), \
        'without a name (written as a tilde, not drawn) only the line counts'
    assert default_property_positions(up)['Value'][1] > default_property_positions(nameless)['Value'][1]


def test_a_rotated_top_pin_counts_its_name_and_the_unrotated_one_does_not():
    # Same pin, same name: pointing down at the body (as laid out) the name is
    # inside; turned around its connection point to point up, the name is
    # drawn above the body. Only the geometry differs.
    long_name = 'ABCDEFGHIJ'
    base = layout_symbol(SIDE_PINS + [SymbolPin('5', long_name, 'input', 'top')])
    placed = next(p for p in base.pins if p.pin.number == '5')
    rotated = layout_symbol(SIDE_PINS + [SymbolPin('5', long_name, 'input', 'top',
                                                   position=(placed.x, placed.y), angle=90)])
    assert default_property_positions(base)['Value'][1] == pytest.approx(placed.y + 2.54)
    upward = next(p for p in rotated.pins if p.pin.number == '5')
    assert default_property_positions(rotated)['Value'][1] - TEXT_SIZE / 2 \
        >= _name_top(upward) + CLEARANCE_MM - 1e-7


def test_a_side_pin_dragged_beside_the_body_and_rotated_up_counts_its_name():
    # The side field still says «left»: the rule reads the geometry only.
    layout = layout_symbol(SIDE_PINS)
    w, h = layout.width / 2, layout.height / 2
    # Two grid steps left of the body (one step would be the outer end of
    # the laid-out left pin 1), pointing up: the name starts above the body.
    moved = SymbolPin('9', 'RESET', 'input', 'left', position=(-w - 5.08, h - 2.54), angle=90)
    lifted = layout_symbol(SIDE_PINS + [moved])
    placed = next(p for p in lifted.pins if p.pin.number == '9')
    assert default_property_positions(lifted)['Value'][1] - TEXT_SIZE / 2 \
        >= _name_top(placed) + CLEARANCE_MM - 1e-7


def test_a_name_starting_just_beyond_an_inner_end_on_the_body_edge_counts():
    # Inner end exactly on the top edge, pointing up: the name starts 0.508
    # mm above the edge, outside the body, and is drawn there.
    layout = layout_symbol(SIDE_PINS)
    h = layout.height / 2
    pin = SymbolPin('5', 'LONGNAME', 'input', 'top', position=(0, h - 2.54), angle=90)
    lifted = layout_symbol(SIDE_PINS + [pin])
    name_top = h + PIN_NAME_OFFSET + len('LONGNAME') * TEXT_SIZE
    assert default_property_positions(lifted)['Value'][1] - TEXT_SIZE / 2 >= name_top + CLEARANCE_MM - 1e-7


@pytest.mark.parametrize('pins', [
    [SymbolPin('1', 'A_VERY_LONG_LEFT_NAME', 'passive', 'left'),
     SymbolPin('2', 'A_VERY_LONG_RIGHT_NAME', 'passive', 'right')],
    SIDE_PINS + [SymbolPin('5', 'A_VERY_LONG_TOP_NAME', 'input', 'top')],
    SIDE_PINS + [SymbolPin('5', 'BOTTOM_NAME_LONGER_THAN_THE_BODY', 'input', 'bottom')],
    SIDE_PINS + [SymbolPin('5', 'TOPNAME', 'input', 'top', length=12.7, text_size=5.08),
                 SymbolPin('6', 'BOTTOMNAME', 'input', 'bottom', text_size=5.08)],
], ids=['long-side-names', 'long-top-name', 'bottom-name-past-the-body', 'large-texts'])
def test_layout_symbol_names_inside_the_body_change_nothing(pins):
    # Negative: `layout_symbol` points every pin at the body, so each name
    # starts inside it. The positions equal those of the same symbol without
    # names (line and number only), and the file is byte-identical to one
    # written with them. The bottom name longer than the body is a body-size
    # problem and is deliberately not counted (see `_pin_drawing_top`).
    layout = layout_symbol(pins)
    nameless = layout_symbol([SymbolPin(p.number, '', p.electrical_type, p.side, length=p.length,
                                        text_size=p.text_size) for p in pins])
    expected = default_property_positions(nameless)
    assert default_property_positions(layout) == expected
    if not any(p.side == 'top' for p in pins):
        assert expected == _old_defaults(layout)
    assert serialize_symbol('Board', pins) == serialize_symbol('Board', pins, properties=expected)


# -- Review L4 (2026-10-15): hidden pins are not drawn by KiCad -------------

def test_a_hidden_top_pin_does_not_lift_the_properties():
    # KiCad draws neither the line nor the texts of a `(hide yes)` pin in the
    # schematic: the properties keep the place of a symbol without it.
    hidden = SIDE_PINS + [SymbolPin('5', 'EN', 'input', 'top', length=12.7, hidden=True)]
    layout = layout_symbol(hidden)
    assert default_property_positions(layout) == _old_defaults(layout)
    text = serialize_symbol('Board', hidden)
    assert '(hide yes)' in text
    assert f'(at 0 {_old_defaults(layout)["Value"][1]:g} 0)' in _property_line(text, 'Value')


def test_the_same_top_pin_visible_lifts_the_properties():
    visible = SIDE_PINS + [SymbolPin('5', 'EN', 'input', 'top', length=12.7, hidden=False)]
    layout = layout_symbol(visible)
    assert default_property_positions(layout)['Value'][1] == pytest.approx(_highest_top_tip(layout) + 2.54)
    assert default_property_positions(layout) != _old_defaults(layout)


def test_a_hidden_upward_pin_with_a_long_name_does_not_lift_and_a_visible_one_does():
    layout = layout_symbol(SIDE_PINS)
    h = layout.height / 2
    hidden = layout_symbol(SIDE_PINS + [SymbolPin('5', 'VERYLONGNAME', 'input', 'top',
                                                  position=(0, h), angle=90, hidden=True)])
    visible = layout_symbol(SIDE_PINS + [SymbolPin('5', 'VERYLONGNAME', 'input', 'top',
                                                   position=(0, h), angle=90)])
    assert default_property_positions(hidden) == _old_defaults(hidden)
    assert default_property_positions(visible)['Value'][1] > _old_defaults(visible)['Value'][1]
