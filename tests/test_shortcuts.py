"""@brief Keyboard shortcuts and their list in the «?» menu (PIANO-FIX item 7, 2026-09-26)."""
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QMessageBox

from tests.test_photo_inventory_gui import gui


def test_every_shortcut_is_unique_and_bound_to_an_existing_method(window):  # noqa: F811
    keys = [key for key, _label, _method in gui.MainWindow.SHORTCUTS]
    assert len(keys) == len(set(keys))
    for _key, _label, method in gui.MainWindow.SHORTCUTS:
        assert callable(getattr(window, method))
    bound = {shortcut.key().toString() for shortcut in window._shortcuts}
    bound |= {window.action_new_project.shortcut().toString(),
              window.action_open_project.shortcut().toString()}
    assert {QKeySequence(key).toString() for key in keys} <= bound


def test_no_shortcut_starts_the_paid_analysis():
    """@brief Negativo: un tasto premuto per errore non deve spendere credito."""
    methods = {method for _key, _label, method in gui.MainWindow.SHORTCUTS}
    assert "_extract_with_ai" not in methods and "_relaunch_inventory" not in methods


def test_a_disabled_tab_is_not_opened_by_its_key(window, monkeypatch):  # noqa: F811
    # Senza progetto la finestra rispegnerebbe la scheda appena aperta: qui si
    # prova solo la scorciatoia.
    monkeypatch.setattr(window, "_on_tab_changed", lambda index: None)
    window.tabs.currentChanged.disconnect()
    window.tabs.setCurrentIndex(gui.TAB_PROJECT)
    window.tabs.setTabEnabled(gui.TAB_MODEL_3D, False)
    window._shortcut_tab_model_3d()
    assert window.tabs.currentIndex() == gui.TAB_PROJECT
    window.tabs.setTabEnabled(gui.TAB_MODEL_3D, True)
    window._shortcut_tab_model_3d()
    assert window.tabs.currentIndex() == gui.TAB_MODEL_3D


def test_a_command_the_interface_does_not_offer_is_not_run(window, monkeypatch):  # noqa: F811
    """@brief Negativo: F5 non rigenera se il pulsante e' spento."""
    clicked = []
    window.btn_regen_3d.clicked.connect(lambda: clicked.append(True))
    window.btn_regen_3d.setEnabled(False)
    window._shortcut_regenerate_3d()
    assert clicked == []


def test_the_menu_lists_every_shortcut(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    window._show_shortcuts()
    text = window._shortcuts_box.text()
    for key, label, _method in gui.MainWindow.SHORTCUTS:
        assert key in text and label in text
