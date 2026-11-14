"""@brief The status bar: what is running and which project is open.

These tests lived in tests/test_workflow_bar.py, deleted with the workflow step
bar (user, 2026-11-14: «troppa confusione»); the status bar stays, so they are
kept here without their assertions on the bar (review 2026-11-14, finding 2).
"""
from tests.test_photo_inventory_gui import gui  # noqa: F401 - the window fixture's module


def test_the_status_bar_says_what_is_running(window):  # noqa: F811
    assert window.statusBar().currentMessage() == "Pronto"
    window.analysis_overlay.start("Analisi AI in corso")
    assert window.statusBar().currentMessage().startswith("Analisi AI in corso")
    window.analysis_overlay.stop()
    assert window.statusBar().currentMessage() == "Pronto"


def _fresh_window(monkeypatch, tmp_path):
    """Construct a MainWindow as at start-up: no name typed, no refresh forced."""
    from qt_fixtures import PreviewStub
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    return gui.MainWindow()


def test_a_new_window_says_no_project_without_any_event(qapp, monkeypatch, tmp_path):
    """@brief Positive (2026-10-17, S1): the status label is right as soon as the window exists.

    The label was built after every refresh of the construction and stayed
    empty until the first language switch recomputed it.
    """
    from qt_fixtures import shutdown_main_window
    window = _fresh_window(monkeypatch, tmp_path)
    try:
        assert window.status_project.text() == "Nessun progetto aperto"
    finally:
        shutdown_main_window(window, qapp)


def test_opening_a_project_still_drives_the_status_after_start_up(qapp, monkeypatch, tmp_path):
    """@brief Negative: the start-up pass must not freeze the label; opening a project rewrites it."""
    from qt_fixtures import shutdown_main_window
    window = _fresh_window(monkeypatch, tmp_path)
    try:
        folder = window._library_root() / "APERTO"
        (folder / "APERTO.pretty").mkdir(parents=True)
        (folder / "APERTO.pretty" / "APERTO.kicad_mod").write_text("(footprint)")
        window._open_project_at(folder, save_current=False)
        assert window.status_project.text() == "Progetto: APERTO"
    finally:
        shutdown_main_window(window, qapp)
