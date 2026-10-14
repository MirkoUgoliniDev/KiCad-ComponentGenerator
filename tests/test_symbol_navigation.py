from copy import deepcopy
from kicadgen.gui.main_window import TAB_SYMBOL, MainWindow
from tests.test_symbol_integration import setup_board

#: The real question, captured at import: conftest answers it with «Resta» in
#: every test so an unattended window never blocks.
_REAL_ASK = MainWindow._ask_pending_symbol_choice


def test_unapplied_symbol_prevents_navigation_and_preserves_draft(window, monkeypatch):
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    tab = window.symbol_tab
    before = deepcopy(window.pinout)
    tab.place_pin('1', 50.8, 25.4)
    tab.rotate_pin('1', 90)
    draft = tab.draft_rows()
    # CONTRACT CHANGED 2026-10-14 (user): a centred modal asks instead of a
    # status-bar sentence; «Resta» keeps the draft and the project.
    asked = []
    monkeypatch.setattr(window, "_ask_pending_symbol_choice",
                        lambda can_apply: asked.append(can_apply) or window.SYMBOL_EDITS_STAY)
    assert not window._can_leave_project()
    assert asked == [True]
    assert window.tabs.currentIndex() == TAB_SYMBOL
    assert tab.draft_rows() == draft
    assert window.pinout == before
    tab.cancel_button.click()
    assert not tab.has_pending_edits()
    assert window._can_leave_project()
    assert asked == [True], "no question without pending edits"


def test_the_modal_applies_or_discards_and_then_leaves(window, monkeypatch):
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    tab = window.symbol_tab
    before = deepcopy(window.pinout)
    tab.place_pin('1', 50.8, 25.4)
    monkeypatch.setattr(window, "_ask_pending_symbol_choice",
                        lambda can_apply: window.SYMBOL_EDITS_DISCARD)
    assert window._can_leave_project()
    assert not tab.has_pending_edits() and window.pinout == before, "discarded"
    tab.place_pin('1', 50.8, 25.4)
    monkeypatch.setattr(window, "_ask_pending_symbol_choice",
                        lambda can_apply: window.SYMBOL_EDITS_APPLY)
    assert window._can_leave_project()
    assert not tab.has_pending_edits()
    assert window.pinout[0]['symbol_position'] == [50.8, 25.4], "applied"


def test_an_apply_answer_with_invalid_edits_stays(window, monkeypatch):
    """«Applica» is disabled for invalid edits; a forced answer cannot apply them."""
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    tab = window.symbol_tab
    tab.place_pin('1', 50.8, 25.4)
    monkeypatch.setattr(tab, "can_apply", lambda: False)
    asked = []
    monkeypatch.setattr(window, "_ask_pending_symbol_choice",
                        lambda can_apply: asked.append(can_apply) or window.SYMBOL_EDITS_APPLY)
    assert not window._can_leave_project()
    assert asked == [False] and tab.has_pending_edits()


def test_the_modal_has_three_choices_and_stays_on_escape(window, monkeypatch):
    """The real box: Applica / Scarta / Resta, «Resta» default and Esc."""
    from PyQt6.QtWidgets import QMessageBox
    seen = {}

    def fake_exec(box):
        seen["buttons"] = [b.text() for b in box.buttons()]
        seen["apply_enabled"] = next(b for b in box.buttons()
                                     if b.text() == window.i18n.text("Applica")).isEnabled()
        seen["default"] = box.defaultButton().text()
        seen["escape"] = box.escapeButton().text()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(MainWindow, "_ask_pending_symbol_choice", _REAL_ASK)
    assert window._ask_pending_symbol_choice(False) == window.SYMBOL_EDITS_STAY
    texts = [window.i18n.text(t) for t in ("Applica", "Scarta", "Resta")]
    assert sorted(seen["buttons"]) == sorted(texts)
    assert seen["default"] == seen["escape"] == window.i18n.text("Resta")
    assert seen["apply_enabled"] is False
    assert window._ask_pending_symbol_choice(True) == window.SYMBOL_EDITS_STAY
    assert seen["apply_enabled"] is True


def test_applied_symbol_allows_navigation(window, monkeypatch):
    setup_board(window, monkeypatch)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    tab = window.symbol_tab
    tab.place_pin('1', 50.8, 25.4)
    tab.apply()
    assert window._can_leave_project()
    assert window.pinout[0]['symbol_position'] == [50.8, 25.4]
