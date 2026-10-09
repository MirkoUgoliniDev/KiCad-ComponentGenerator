from copy import deepcopy
from kicadgen.gui.main_window import TAB_SYMBOL
from tests.test_symbol_integration import setup_board


def test_unapplied_symbol_prevents_navigation_and_preserves_draft(window, monkeypatch):
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    tab = window.symbol_tab
    before = deepcopy(window.pinout)
    tab.place_pin('1', 50.8, 25.4)
    tab.rotate_pin('1', 90)
    draft = tab.draft_rows()
    assert not window._can_leave_project()
    assert window.tabs.currentIndex() == TAB_SYMBOL
    assert tab.draft_rows() == draft
    assert window.pinout == before
    assert 'Applica' in tab.status.text()
    tab.cancel_button.click()
    assert not tab.has_pending_edits()
    assert window._can_leave_project()


def test_applied_symbol_allows_navigation(window, monkeypatch):
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    tab = window.symbol_tab
    tab.place_pin('1', 50.8, 25.4)
    tab.apply()
    assert window._can_leave_project()
    assert window.pinout[0]['symbol_position'] == [50.8, 25.4]
