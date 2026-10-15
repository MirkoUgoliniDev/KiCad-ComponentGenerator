"""@brief «Strumenti» menu: the six entries removed on 25/09, back in one place (user, 2026-09-26)."""
from PyQt6.QtWidgets import QMessageBox

from tests.test_photo_inventory_gui import fill_required_metadata, gui

KICAD_ACTIONS = ("action_test_kicad", "action_export", "action_install_global")


def _write_outputs(window, *, step):  # noqa: F811
    name = window._component_name()
    footprint = window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"
    footprint.parent.mkdir(parents=True, exist_ok=True)
    footprint.write_text("(footprint)", encoding="utf-8")
    if step:
        model = window._project_dir() / f"{name}.3dshapes" / f"{name}.step"
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_text("ISO-10303-21;", encoding="utf-8")
    window._refresh_output_actions()


def test_the_menu_bar_order_and_the_tools_entries(window):  # noqa: F811
    titles = [action.text() for action in window.menuBar().actions()]
    assert titles == ["File", "Configurazione", "Strumenti", "?"]
    texts = [a.text() for a in window.menu_tools.actions() if not a.isSeparator()]
    assert texts == ["Apri cartella componente", "Prova in KiCad...",
                     "Installa nel progetto KiCad...", "Installa come libreria globale...",
                     "Apri cartella progetto tool", "Apri config.json"]


def test_kicad_entries_are_off_with_the_reason_until_footprint_and_step_exist(window):  # noqa: F811
    window._refresh_output_actions()
    for attr in KICAD_ACTIONS + ("action_open_output",):
        action = getattr(window, attr)
        assert not action.isEnabled() and action.toolTip().startswith("Genera prima")
    # Solo il footprint: la cartella si apre, i comandi KiCad no (serve anche lo STEP).
    _write_outputs(window, step=False)
    assert window.action_open_output.isEnabled()
    assert not any(getattr(window, attr).isEnabled() for attr in KICAD_ACTIONS)
    # CONTRACT CHANGED 2026-10-15: the install entries also need the required
    # metadata (user); filled so this test keeps checking the file gate.
    fill_required_metadata(window)
    _write_outputs(window, step=True)
    assert all(getattr(window, attr).isEnabled() for attr in KICAD_ACTIONS)


def test_menu_entries_and_kicad_tab_buttons_stay_in_step(window):  # noqa: F811
    """@brief Voce di menu e pulsante della scheda KiCad sono lo stesso comando."""
    pairs = (("action_test_kicad", "btn_test_kicad"), ("action_export", "btn_export"),
             ("action_install_global", "btn_install_global"))
    for step in (False, True):
        if step:
            _write_outputs(window, step=True)
        else:
            window._refresh_output_actions()
        for action, button in pairs:
            assert getattr(window, action).isEnabled() == getattr(window, button).isEnabled()


def test_the_technical_entries_open_their_folder_and_file(window, monkeypatch, tmp_path):  # noqa: F811
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setenv("KICADGEN_DATA_DIR", str(tmp_path / "dati"))
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gui, "config_path", lambda: config)
    window.action_open_tool_dir.trigger()
    window.action_open_config.trigger()
    assert opened == [(tmp_path / "dati").as_uri(), config.as_uri()]


def test_the_tools_menu_follows_the_language(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    combo = window.language_combo
    combo.setCurrentIndex(combo.findData("en"))
    try:
        assert window.menu_tools.title() == "Tools"
        assert window.menu_help.title() == "Help"
        assert window.action_open_config.text() == "Open config.json"
        assert window.action_test_kicad.text() == "Test in KiCad..."
    finally:
        combo.setCurrentIndex(combo.findData("it"))
