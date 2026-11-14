"""@brief The pin-name space rewrites: ONE summary line on the symbol status, the per-pin detail in the log.

User decision 2026-11-13 (6): the line «Pin N: nome «…» scritto come «…»…»
repeated for every pin filled the status; the status now shows one line
(count, pin numbers as ranges, one example) and the per-pin lines reach the
log through `SymbolTab.log_message`. Also: the «Solo nome principale»
switch of the pinout dialog.
"""
import pytest

from kicadgen.generators import symbol as sym
from kicadgen.generators.symbol import (LocalizedText, PinNameRewriteNote, SymbolPin, normalize_pins,
                                        pin_number_ranges, summarize_pin_notes)


@pytest.fixture(autouse=True)
def _restore_preference():
    """Every test starts from the default (ON) and leaves it as found."""
    before = sym.primary_pin_names_enabled()
    sym.set_primary_pin_names(True)
    yield
    sym.set_primary_pin_names(before)


def test_ranges_join_consecutive_decimals_and_list_the_rest():
    """«1–4, 7, A1»: only canonical decimals join, «01» stays apart."""
    assert pin_number_ranges(['3', '1', '2', '4', '7', 'A1', '01', '3']) == '1–4, 7, 01, A1'
    assert pin_number_ranges([]) == ''


def test_summary_is_one_line_with_count_ranges_and_example():
    """Positive: five rewritten pins give one visible line and five details."""
    notes = []
    normalize_pins([SymbolPin(str(n), f'A {n}') for n in (1, 2, 3, 4, 9)], notes)
    visible, detail = summarize_pin_notes(notes)
    assert len(visible) == 1 and len(detail) == 5
    line = str(visible[0])
    assert '5 pins' in line and '1–4, 9' in line and '«A 1» → «A_1»' in line
    assert all(isinstance(note, PinNameRewriteNote) for note in detail)
    assert [note.number for note in detail] == ['1', '2', '3', '4', '9']


def test_no_spaces_no_line():
    """Negative: clean names give no visible line and no detail."""
    notes = []
    normalize_pins([SymbolPin('1', 'GPIO4'), SymbolPin('2', 'GND')], notes)
    assert summarize_pin_notes(notes) == ([], [])


def test_other_notes_pass_through_in_place():
    """A note of another kind is kept as it is, the summary takes the first rewrite's place."""
    other = LocalizedText('altro', 'other')
    first = PinNameRewriteNote('2', 'A B', 'A_B')
    visible, detail = summarize_pin_notes([other, first, PinNameRewriteNote('3', 'C D', 'C_D')])
    assert visible[0] is other and len(visible) == 2 and detail == [first, detail[1]]


def test_symbol_tab_status_has_one_line_and_the_log_gets_each_pin_once(qapp):
    """The status shows the summary once; every per-pin line reaches `log_message`, once per text."""
    from kicadgen.gui.symbol_tab import SymbolTab
    tab = SymbolTab()
    logged = []
    tab.log_message.connect(logged.append)
    rows = [{'pin': str(n), 'name': f'IO {n}'} for n in range(1, 7)]
    tab.set_data('Board', rows, 'KiCadGen:Board', [str(n) for n in range(1, 7)])
    status = tab.status.text()
    assert status.count('\n') == 1, status
    assert '1–6' in status and 'Pin 1:' not in status
    assert len(logged) == 6 and all(line.startswith('Pin ') for line in logged)
    tab._changed()
    assert len(logged) == 6
    tab.deleteLater()


def test_symbol_tab_without_spaces_has_no_extra_line_and_logs_nothing(qapp):
    """Negative: clean names leave the status on one line and the log empty."""
    from kicadgen.gui.symbol_tab import SymbolTab
    tab = SymbolTab()
    logged = []
    tab.log_message.connect(logged.append)
    tab.set_data('Board', [{'pin': '1', 'name': 'GPIO4'}, {'pin': '2', 'name': 'GND'}], 'KiCadGen:Board', ['1', '2'])
    assert '\n' not in tab.status.text() and logged == []
    tab.deleteLater()


def test_symbol_tab_switch_redraws_with_the_primary_name(qapp):
    """The tab's switch changes the drawn pin names and bumps the source revision; rows keep the full text."""
    from kicadgen.gui.symbol_tab import SymbolTab
    tab = SymbolTab()
    tab.set_data('Board', [{'pin': '1', 'name': 'GPIO4 (ADC1_3, TOUCH4)'}], 'KiCadGen:Board', ['1'])
    assert tab._symbol.pins[0].pin.name == 'GPIO4'
    revision = tab.source_revision()
    tab.set_primary_pin_names(False)
    assert not tab.primary_pin_names()
    assert tab._symbol.pins[0].pin.name == 'GPIO4_(ADC1_3,_TOUCH4)'
    assert tab.source_revision() == revision + 1
    assert tab.rows()[0]['name'] == 'GPIO4 (ADC1_3, TOUCH4)'
    tab.set_primary_pin_names(False)
    assert tab.source_revision() == revision + 1
    tab.deleteLater()


def test_pinout_dialog_switch_defaults_on_and_shows_the_full_text(qapp):
    """The switch starts from the caller's preference; the table keeps the full name."""
    from kicadgen.gui.pinout_dialog import PinoutDialog
    rows = [{'pin': 1, 'name': 'GPIO4 (ADC1_3, TOUCH4, RTC)'}]
    dialog = PinoutDialog(rows)
    assert dialog.primary_names() is True
    assert dialog.table.item(0, 1).text() == 'GPIO4 (ADC1_3, TOUCH4, RTC)'
    dialog.primary_names_check.setChecked(False)
    assert dialog.primary_names() is False
    dialog.deleteLater()
    dialog = PinoutDialog(rows, primary_names=False)
    assert dialog.primary_names() is False
    dialog.deleteLater()
