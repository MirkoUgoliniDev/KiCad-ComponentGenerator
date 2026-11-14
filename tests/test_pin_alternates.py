"""@brief «Solo nome principale»: a pinout name «PRIMARY (ALT1, ALT2)» becomes a pin named PRIMARY with KiCad alternates.

User decision 2026-11-13 (5): default ON, remembered as a preference; the
alternates take the pin's type and the «line» style, empty and repeated
items are dropped, a name without parentheses is unchanged (a top-level
«/» too, 2026-11-14: it can be one combined signal), and with the
switch off the full text stays the pin name. The rule is structural (the
written shape), never keyed on part or pin names.
"""
import subprocess

import pytest

from kicadgen.generators import symbol as sym
from kicadgen.generators.symbol import (SymbolPin, normalize_pins, serialize_symbol, split_pin_name,
                                        symbol_inputs)
from kicadgen.generators.symbol_glyphs import parse_sexpr
from kicadgen.services.system_dependencies import find_kicad_cli


@pytest.fixture(autouse=True)
def _restore_preference():
    """Every test starts from the default (ON) and leaves it as found."""
    before = sym.primary_pin_names_enabled()
    sym.set_primary_pin_names(True)
    yield
    sym.set_primary_pin_names(before)


def _rows():
    """A GPIO with listed functions, a plain power pin, a name with spaces and repeats."""
    return [{'pin': '1', 'name': 'GPIO4 (ADC1_3, TOUCH4, RTC)', 'electrical_type': 'bidirectional'},
            {'pin': '2', 'name': 'GND', 'electrical_type': 'power_in', 'symbol_side': 'right'},
            {'pin': '3', 'name': 'IO5 (RX, , RX, IO5, TX)', 'electrical_type': 'input'}]


def _pins_of(text):
    """{number: (name, [(alternate, type, style)])} of a symbol library text."""
    tree = parse_sexpr(text)

    def walk(node):
        """Yield every `pin` form at any depth."""
        if isinstance(node, list):
            if node and node[0] == 'pin':
                yield node
            for item in node:
                yield from walk(item)
    result = {}
    for pin in walk(tree):
        name = next(item[1] for item in pin if isinstance(item, list) and item[0] == 'name')
        number = next(item[1] for item in pin if isinstance(item, list) and item[0] == 'number')
        alternates = [tuple(item[1:4]) for item in pin if isinstance(item, list) and item[0] == 'alternate']
        result[str(number)] = (str(name), [tuple(str(v) for v in a) for a in alternates])
    return result


def test_split_takes_the_text_before_the_parenthesis_and_the_listed_items():
    """Positive: «GPIO4 (ADC1_3, TOUCH4, RTC)» -> GPIO4 + three alternates in written order."""
    assert split_pin_name('GPIO4 (ADC1_3, TOUCH4, RTC)') == ('GPIO4', ('ADC1_3', 'TOUCH4', 'RTC'))


@pytest.mark.parametrize('name', ['GND', 'VDD_3V3', '~{RESET}', ''])
def test_split_leaves_a_name_without_parentheses_unchanged(name):
    """Negative: no «(» means no list, the name comes back whole."""
    assert split_pin_name(name) == (name, ())


@pytest.mark.parametrize('name', ['(NC)', 'EN (active high) pull-up'])
def test_split_leaves_a_name_that_is_not_primary_plus_list_unchanged(name):
    """Negative: nothing before «(», or text after «)», is not the «PRIMARY (list)» shape."""
    assert split_pin_name(name) == (name, ())


def test_split_drops_empty_repeated_and_primary_items():
    """Positive: «IO5 (RX, , RX, IO5, TX)» keeps RX and TX once."""
    assert split_pin_name('IO5 (RX, , RX, IO5, TX)') == ('IO5', ('RX', 'TX'))


@pytest.mark.parametrize('name, expected', [
    ('GPIO4 (ADC1_3, TOUCH4, RTC)', ('GPIO4', ('ADC1_3', 'TOUCH4', 'RTC'))),
    ('D0 (RX)', ('D0', ('RX',))),
    ('USB_DP(D+, U0RXD)', ('USB_DP', ('D+', 'U0RXD'))),
    ('IO9 (SPI.CLK, FSPI-HD)', ('IO9', ('SPI.CLK', 'FSPI-HD'))),
    # Declared limit: an identifier-like remark has the shape of a function
    # and IS split (same structure as «D0 (RX)»).
    ('GND (EP)', ('GND', ('EP',))),
])
def test_split_accepts_a_list_of_signal_identifiers(name, expected):
    """Positive: one closing group whose every item is identifier-like is split."""
    assert split_pin_name(name) == expected


@pytest.mark.parametrize('name', [
    'VCC(3.3V)',                              # item starts with a digit: a value
    'RESET (active low)',                     # item with a space: prose
    'VIN (5-12V, 1A max)',                    # value and prose items
    'GPIO4 (ADC1_3, TOUCH4) (strapping)',     # two top-level groups
    'A0 (ADC0, DAC',                          # unclosed group
    'A0 (ADC0 (alt), DAC)',                   # nested group
    'A0 ADC0), DAC (X)',                      # stray «)» before the group
    'A0 (, )',                                # no item survives
    'IO0 (~{BOOT})',                          # KiCad overbar markup is not an identifier
])
def test_split_keeps_the_whole_name_when_the_group_is_not_a_function_list(name):
    """Negative: unbalanced or repeated groups, or any non-identifier item, keep the name whole."""
    assert split_pin_name(name) == (name, ())


@pytest.mark.parametrize('name, expected', [
    ('GPIO4 (ADC1_3/RTC/TOUCH4)', ('GPIO4', ('ADC1_3', 'RTC', 'TOUCH4'))),
    ('GPIO15 (ADC2_4/U0RTS/RTC/XTAL_32K_P)', ('GPIO15', ('ADC2_4', 'U0RTS', 'RTC', 'XTAL_32K_P'))),
    # Both separators in one group.
    ('IO5 (RX, TX/CTS)', ('IO5', ('RX', 'TX', 'CTS'))),
    ('IO5 (RX//TX/IO5)', ('IO5', ('RX', 'TX'))),
])
def test_split_accepts_the_slash_inside_the_group(name, expected):
    """Positive: «/» separates the items of the one group like «,» (2026-11-13 devkit pinout)."""
    assert split_pin_name(name) == expected


@pytest.mark.parametrize('name', [
    'VIN (5/12V)',                            # items that are values, not identifiers
    'RESET (active/low level)',               # an item with a space is prose
    'A0 (ADC0/DAC) (alt)',                    # two top-level groups
    'GPIO1 / IO1 (labelled "1")',             # a quoted remark is no identifier
])
def test_split_keeps_slash_names_whole_with_a_group_that_is_not_a_list(name):
    """Negative: a bracket that is not one closing group of identifiers keeps the whole name."""
    assert split_pin_name(name) == (name, ())


# CONTRACT CHANGED 2026-11-14 (coordinator decision after a generality
# audit): alternatives come from the parentheses the extraction writes
# («PRIMARY (ALT1, ALT2)»); a top-level slash can be ONE combined signal
# («DE/RE» tied enables, half-duplex «RX/TX», «IN/OUT»), which the text
# cannot tell from two alternatives, so it is never split. A short-lived
# version of this file split «USB_D-/GPIO19» at the top level.
@pytest.mark.parametrize('name', [
    'IO0/BOOT', 'USB_D-/GPIO19', 'GPIO19 / USB_D-',   # alternatives written without parentheses
    'DE/RE', 'RX/TX', 'D+/D-', 'IN/OUT',              # one combined signal of the same shape
    'I/O', 'R/W', 'N/C', 'ON/OFF',
    'USB_D-', 'GPIO_0', 'U0RXD', '+3V3', '~{RESET}',   # single names
    'USB_D- GPIO19', 'LINE IN',                       # a space never splits
])
def test_split_keeps_a_top_level_slash_or_space_name_whole(name):
    """Negative: without the closing parenthesised list nothing is split, whatever the slash or space joins."""
    assert split_pin_name(name) == (name, ())


@pytest.mark.parametrize('name, expected', [
    ('U0RXD (GPIO44, CLK_OUT2)', ('U0RXD', ('GPIO44', 'CLK_OUT2'))),
    ('GPIO19 (USB_D-, U1RTS, ADC2_8)', ('GPIO19', ('USB_D-', 'U1RTS', 'ADC2_8'))),
])
def test_split_accepts_the_form_the_extraction_is_asked_for(name, expected):
    """Positive against the negatives above: the same names in «PRIMARY (ALT1, ALT2)» are split."""
    assert split_pin_name(name) == expected


def test_a_slash_head_stays_the_primary_of_its_list():
    """Positive and negative in one name: «DE/RE (TXEN)» keeps «DE/RE» whole as primary, TXEN alternate."""
    assert split_pin_name('DE/RE (TXEN)') == ('DE/RE', ('TXEN',))
    assert split_pin_name('I/O (GPIO4)') == ('I/O', ('GPIO4',))


@pytest.mark.parametrize('name, expected', [
    ('RST (EN)', ('RST', ('EN',))),
    ('EN (RST)', ('EN', ('RST',))),
    ('CE (DIS)', ('CE', ('DIS',))),
])
def test_enable_words_are_signal_names_not_states(name, expected):
    """Positive (audit L4): «EN» and «DIS» name signals, so «RST (EN)» splits exactly like «EN (RST)»."""
    assert split_pin_name(name) == expected


@pytest.mark.parametrize('name', ['EN (ON/OFF)', 'MODE (HIGH/LOW)', 'SW (OPEN/CLOSED)'])
def test_pure_level_words_still_describe_one_signal(name):
    """Negative: a group of level words only («ON/OFF») stays in the name."""
    assert split_pin_name(name) == (name, ())


def test_collision_guard_keeps_different_signals_apart():
    """Negative: «GPIO0 (BOOT)» and «GPIO0 (STRAP)» would both be GPIO0, so both keep their full names."""
    rows = [{'pin': '1', 'name': 'GPIO0 (BOOT)'}, {'pin': '2', 'name': 'GPIO0 (STRAP)'},
            {'pin': '3', 'name': 'GPIO19 (USB_D-)'}]
    pins, _body = symbol_inputs(rows)
    assert [(pin.name, pin.alternates) for pin in pins] == [
        ('GPIO0 (BOOT)', ()), ('GPIO0 (STRAP)', ()), ('GPIO19', ('USB_D-',))]


def test_one_list_written_in_another_order_is_split_on_both_pins():
    """Positive: «IO5 (RX, TX)» and «IO5 (TX, RX)» are one identity (same set of alternates), not a collision."""
    rows = [{'pin': '1', 'name': 'IO5 (RX, TX)'}, {'pin': '2', 'name': 'IO5 (TX, RX)'}]
    pins, _body = symbol_inputs(rows)
    assert [(pin.name, set(pin.alternates)) for pin in pins] == [
        ('IO5', {'RX', 'TX'}), ('IO5', {'RX', 'TX'})]


def test_collision_guard_compares_names_as_kicad_shows_them():
    """Negative (audit L5): «PWR EN» is shown «PWR_EN», so «PWR_EN (CE)» would look alike and stays whole."""
    pins, _body = symbol_inputs([{'pin': '1', 'name': 'PWR EN'}, {'pin': '2', 'name': 'PWR_EN (CE)'}])
    assert [(pin.name, pin.alternates) for pin in pins] == [('PWR EN', ()), ('PWR_EN (CE)', ())]
    # Positive: with a primary KiCad shows differently the split goes ahead.
    pins, _body = symbol_inputs([{'pin': '1', 'name': 'PWR EN'}, {'pin': '2', 'name': 'PWREN (CE)'}])
    assert [(pin.name, pin.alternates) for pin in pins] == [('PWR EN', ()), ('PWREN', ('CE',))]


def test_a_bare_name_still_collides_with_its_split_twin():
    """Negative: a bare «GND» and «GND (AGND)» are different identities, so «GND (AGND)» stays whole."""
    pins, _body = symbol_inputs([{'pin': '1', 'name': 'GND'}, {'pin': '2', 'name': 'GND (AGND)'}])
    assert [(pin.name, pin.alternates) for pin in pins] == [('GND', ()), ('GND (AGND)', ())]


# Real run 2026-11-14 (TEL converter): the model restated the pin name
# inside its own list, «+Vin (+Vin (Vcc))», or wrote «GND (GND)».
@pytest.mark.parametrize('name, expected', [
    ('+Vin (+Vin (Vcc))', ('+Vin', ('Vcc',))),        # one restated nesting level flattened
    ('P1 (P1 (RX, TX))', ('P1', ('RX', 'TX'))),
    ('GND (GND)', ('GND', ())),                       # the list only repeats the name
    ('+Vin (+Vin)', ('+Vin', ())),                    # a repeat may have any shape
    ('PWR_EN (PWR EN)', ('PWR_EN', ())),              # equal as KiCad shows it
    ('+Vin (+Vin (+Vin))', ('+Vin', ())),
    ('-Vin (GND)', ('-Vin', ('GND',))),               # a different name is still an alternate
])
def test_a_list_restating_the_primary_is_read_without_the_repeat(name, expected):
    """Positive: repeats of the primary are dropped and one restated nested group is flattened."""
    assert split_pin_name(name) == expected


@pytest.mark.parametrize('name', [
    'A (B (C))',                 # the nested head is another name: C may be a remark on B
    'P (X, P (Y))',              # the nested group does not hold the whole outer group
    'P (P (Q (R)))',             # two nesting levels
    'P (P (active low))',        # flattened, but the item is prose
    'P (P (A)) x',               # text after the groups
    '+Vin (+Vin ())',            # nothing inside the nested group
    'GND (GND, active low)',     # a repeat does not excuse a remark beside it
])
def test_an_ambiguous_nesting_keeps_the_whole_name(name):
    """Negative: nesting that is not a restatement of the primary, or a remark beside a repeat, stays whole."""
    assert split_pin_name(name) == (name, ())


def test_a_restated_name_does_not_trigger_the_collision_guard():
    """Positive: «GND (GND)» is the same signal as «GND», both pins are named GND with no alternate."""
    pins, _body = symbol_inputs([{'pin': '1', 'name': 'GND'}, {'pin': '2', 'name': 'GND (GND)'}])
    assert [(pin.name, pin.alternates) for pin in pins] == [('GND', ()), ('GND', ())]


def test_one_remark_item_keeps_the_whole_list():
    """Negative against a positive of the same shape: «X (RX, 1A max)» stays whole, «X (RX, TX)» splits."""
    assert split_pin_name('X (RX, 1A max)') == ('X (RX, 1A max)', ())
    assert split_pin_name('X (RX, TX)') == ('X', ('RX', 'TX'))


def test_switch_on_writes_the_primary_name_and_alternates_with_the_pin_type():
    """Positive (default ON): pin 1 is «GPIO4» with ADC1_3, RTC, TOUCH4 as bidirectional line alternates."""
    pins, body = symbol_inputs(_rows())
    pins_in_file = _pins_of(serialize_symbol('MOD', pins, body=body))
    assert pins_in_file['1'] == ('GPIO4', [('ADC1_3', 'bidirectional', 'line'), ('RTC', 'bidirectional', 'line'),
                                           ('TOUCH4', 'bidirectional', 'line')])
    assert pins_in_file['2'] == ('GND', [])
    assert pins_in_file['3'] == ('IO5', [('RX', 'input', 'line'), ('TX', 'input', 'line')])


def test_switch_off_keeps_the_full_text_and_no_alternates():
    """Negative: with the preference off the pin keeps today's full name."""
    sym.set_primary_pin_names(False)
    pins, body = symbol_inputs(_rows())
    pins_in_file = _pins_of(serialize_symbol('MOD', pins, body=body))
    # The full name gets KiCad's space substitution, as before this change.
    assert pins_in_file['1'] == ('GPIO4_(ADC1_3,_TOUCH4,_RTC)', [])
    assert symbol_inputs(_rows(), primary_names=True)[0][0].name == 'GPIO4'


def test_the_explicit_argument_wins_over_the_preference():
    """Positive and negative on the same rows: `primary_names` overrides the process-wide choice."""
    assert symbol_inputs(_rows(), primary_names=False)[0][0].name == 'GPIO4 (ADC1_3, TOUCH4, RTC)'
    sym.set_primary_pin_names(False)
    assert symbol_inputs(_rows(), primary_names=True)[0][0].alternates == ('ADC1_3', 'TOUCH4', 'RTC')


def test_rows_keep_the_full_text():
    """The pinout rows are never rewritten: only the pins built from them differ."""
    rows = _rows()
    symbol_inputs(rows)
    assert rows[0]['name'] == 'GPIO4 (ADC1_3, TOUCH4, RTC)'


def test_normalize_drops_repeats_and_the_name_and_sorts_as_kicad_saves():
    """Alternates given directly: duplicates, empties and the pin's name dropped, code-point order."""
    (pin,) = normalize_pins([SymbolPin('7', 'A', alternates=('Z', '', 'B', 'Z', 'A'))])
    assert pin.alternates == ('B', 'Z')


def test_an_alternate_with_spaces_is_kept_as_written():
    """KiCad 10 keeps «A B» as an alternate (verified with `sym upgrade`), unlike a pin name."""
    notes = []
    (pin,) = normalize_pins([SymbolPin('7', 'A', alternates=('X Y',))], notes)
    assert pin.alternates == ('X Y',) and notes == []


def test_an_alternate_with_a_control_character_is_refused():
    """Negative: an alternate is validated like the name."""
    with pytest.raises(sym.SymbolError):
        normalize_pins([SymbolPin('7', 'A', alternates=('X\x00',))])


def test_a_pin_without_alternates_is_written_as_before():
    """Negative: no alternate form, the pin closes after its number."""
    text = serialize_symbol('MOD', [SymbolPin('1', 'GND')])
    assert '(alternate' not in text
    assert '(number "1" (effects (font (size 1.27 1.27)))))' in text


def test_kicad_loads_the_alternates(tmp_path):
    """KiCad 10 reads the written alternates and keeps them when it re-saves the library."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip('KiCad CLI not installed')
    pins, body = symbol_inputs(_rows())
    library = tmp_path / 'ALT.kicad_sym'
    library.write_text(serialize_symbol('ALT', pins, body=body), encoding='utf-8')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    upgraded = tmp_path / 'ALT_up.kicad_sym'
    done = subprocess.run([cli, 'sym', 'upgrade', '--force', '-o', str(upgraded), str(library)],
                          capture_output=True, timeout=120, stdin=subprocess.DEVNULL, creationflags=flags)
    assert done.returncode == 0, done.stderr
    assert _pins_of(upgraded.read_text(encoding='utf-8'))['1'] == _pins_of(library.read_text(encoding='utf-8'))['1']
    svg = tmp_path / 'svg'
    done = subprocess.run([cli, 'sym', 'export', 'svg', '-o', str(svg), str(library)],
                          capture_output=True, timeout=120, stdin=subprocess.DEVNULL, creationflags=flags)
    assert done.returncode == 0, done.stderr
    assert list(svg.glob('*.svg'))


# --- generality audit 2026-11-13, item 5: different names never collapse ------------------------

@pytest.mark.parametrize("first,second,primary", [
    ("SPI (MOSI)", "SPI (MISO)", "SPI"),
    ("GND (AGND)", "GND (PGND)", "GND"),
    ("NO (NO1)", "NO (NO2)", "NO"),
])
def test_different_names_with_one_primary_keep_their_full_names(first, second, primary):
    """Negative of the split: two different signals never end up with the same pin name."""
    rows = [{'pin': '1', 'name': first}, {'pin': '2', 'name': second},
            {'pin': '3', 'name': 'GPIO4 (ADC1_3, TOUCH4)'}]
    pins, _body = symbol_inputs(rows, primary_names=True)
    assert [(p.name, p.alternates) for p in pins[:2]] == [(first, ()), (second, ())]
    assert split_pin_name(first)[0] == primary, "the split alone would have made them alike"
    # A pin whose primary no other name shares is still split.
    assert (pins[2].name, pins[2].alternates) == ('GPIO4', ('ADC1_3', 'TOUCH4'))


def test_the_same_full_name_on_several_pins_stays_alike():
    """Positive: four «GND» (or stacked pins) share their name as written."""
    rows = [{'pin': str(n), 'name': 'GND'} for n in range(1, 5)] + [
        {'pin': '5', 'name': 'IO5 (RX, TX)'}, {'pin': '6', 'name': 'IO5 (RX, TX)'}]
    pins, _body = symbol_inputs(rows, primary_names=True)
    assert [p.name for p in pins[:4]] == ['GND'] * 4
    assert [(p.name, p.alternates) for p in pins[4:]] == [('IO5', ('RX', 'TX'))] * 2


def test_a_plain_name_and_a_split_one_with_the_same_primary_stay_apart():
    """«GND» and «GND (EP)»: the split one keeps its full name, the plain one is unchanged."""
    pins, _body = symbol_inputs([{'pin': '1', 'name': 'GND'}, {'pin': '2', 'name': 'GND (EP)'}],
                                primary_names=True)
    assert [(p.name, p.alternates) for p in pins] == [('GND', ()), ('GND (EP)', ())]

