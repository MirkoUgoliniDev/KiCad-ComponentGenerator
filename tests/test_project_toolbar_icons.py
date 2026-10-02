"""@brief Icone «Nuovo progetto» e «Apri progetto» nella barra della scheda Progetto (utente, 2026-09-25)."""


def test_the_icons_lead_the_project_bar(window):  # noqa: F811
    bar = window.project_action_bar
    order = [bar.indexOf(widget) for widget in (window.btn_new_project_top,
                                                window.btn_open_project_top, window.btn_ai_log)]
    assert -1 not in order and order == sorted(order)
    assert window.btn_new_project_top.toolTip().startswith("Nuovo progetto")
    assert window.btn_open_project_top.toolTip().startswith("Apri progetto")
    assert not window.btn_new_project_top.text()  # solo icona, nome nel tooltip


def test_the_icons_run_the_same_commands_as_the_file_menu(window, monkeypatch):  # noqa: F811
    """@brief Collegamento vero: le icone passano dalla stessa guardia del menu File.

    `_new_project` e `_open_project` chiedono per prima cosa
    `_project_change_blocked`; qui risponde «bloccato», cosi' si vede che il
    clic arriva al comando senza aprire nessuna finestra.
    """
    asked = []
    # The open-project icon is disabled while there are no project folders.
    # Seed one so this test exercises its command wiring, not availability.
    (window._library_root() / "Existing" / "docs").mkdir(parents=True)
    window._refresh_open_project_commands()
    monkeypatch.setattr(window, "_project_change_blocked", lambda: asked.append(True) or True)
    window.btn_new_project_top.click()
    window.btn_open_project_top.click()
    assert len(asked) == 2
    window.action_new_project.trigger()  # il menu, per confronto: stessa guardia
    assert len(asked) == 3


def test_the_icons_follow_the_language(window):  # noqa: F811
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    assert window.btn_new_project_top.toolTip().startswith("New project")
    assert window.btn_open_project_top.toolTip().startswith("Open project")
