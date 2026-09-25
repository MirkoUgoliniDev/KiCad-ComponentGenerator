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


def test_the_help_menu_holds_only_licenses_and_about(window):  # noqa: F811
    """@brief Utente, 25/09: tolta «Apri cartella progetto tool» dal menu «?»."""
    actions = [a for a in window.menuBar().actions()][-1].menu().actions()
    texts = [a.text() for a in actions if not a.isSeparator()]
    assert texts == ["Licenze", "About"]
    assert not hasattr(window, "action_open_tool_dir")


def test_the_configuration_menu_no_longer_opens_config_json(window):  # noqa: F811
    """@brief Utente, 25/09: tolta «Apri config.json» dal menu Configurazione."""
    menus = [a.menu() for a in window.menuBar().actions() if a.menu()]
    texts = [a.text() for menu in menus for a in menu.actions()]
    assert "Apri config.json" not in texts
    assert "Archivio abbinamenti accettati..." in texts  # il resto del menu c'e'
    assert not hasattr(window, "action_open_config")


def test_the_file_menu_holds_only_the_project_commands(window):  # noqa: F811
    """@brief Utente, 25/09: dal menu File tolte cartella componente e comandi KiCad."""
    file_menu = window.menuBar().actions()[0].menu()
    texts = [a.text() for a in file_menu.actions() if not a.isSeparator()]
    assert texts == ["Nuovo progetto...", "Apri progetto...", "Rinomina progetto...", "Esci"]
    for name in ("action_open_output", "action_test_kicad", "action_export",
                 "action_install_global"):
        assert not hasattr(window, name)
