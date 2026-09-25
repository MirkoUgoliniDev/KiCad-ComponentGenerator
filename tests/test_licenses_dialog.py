"""@brief Menu «?» > «Licenze»: licenza del programma e avvisi di terze parti (utente, 2026-09-25)."""
from kicadgen.gui import main_window as gui

from test_photo_inventory_gui import window  # noqa: F401 - fixture


def test_the_help_menu_has_the_licenses_entry(window):  # noqa: F811
    help_actions = [a for a in window.menuBar().actions()][-1].menu().actions()
    assert window.action_licenses in help_actions
    assert help_actions.index(window.action_licenses) < help_actions.index(window.action_about)


def test_the_dialog_shows_the_agpl_text_and_the_notices(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui.QDialog, "exec", lambda self: 0)
    window._show_licenses()
    tabs = window._licenses_dialog.findChild(gui.QTabWidget)
    assert tabs.count() == 2
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in tabs.widget(0).toPlainText()
    assert "CompDetect" in tabs.widget(1).toPlainText()


def test_a_missing_file_is_said_not_raised(window, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(gui, "resource_path", lambda *parts: tmp_path.joinpath(*parts))
    license_text, notices = window._license_texts()
    assert "LICENSE" in license_text and "THIRD_PARTY_NOTICES.md" in notices
