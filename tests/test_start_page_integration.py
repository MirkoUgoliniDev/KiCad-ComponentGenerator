"""@brief Start page and recent projects wired into the main window (PIANO-FIX item 1, 2026-09-26)."""
import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.services import recent_projects
from tests.test_photo_inventory_gui import gui


@pytest.fixture
def bare(window):  # noqa: F811
    """The window as at first start: no project open and no recent projects.

    The shared fixture opens «Componente_di_prova» for the tests that need a
    name; here it is closed and forgotten again.
    """
    window._switch_project("", window._general_project_state)
    window._config.pop(recent_projects.CONFIG_KEY, None)
    window._refresh_start_page()
    return window


def _project(root, name):
    """A project folder as «Nuovo progetto…» leaves it: the folder and docs/."""
    folder = root / name
    (folder / "docs").mkdir(parents=True)
    return folder


def _open_and_close(window, folder):
    """Open `folder` (which writes its project_config.json) and close it again."""
    window._open_project_at(folder)
    window._switch_project("", window._general_project_state)


def test_the_start_page_replaces_the_tabs_only_without_a_project(bare, tmp_path):  # noqa: F811
    window = bare
    assert window.central_stack.currentWidget() is window.start_page
    folder = _project(tmp_path, "Scheda")
    window._open_project_at(folder)
    assert window.central_stack.currentWidget() is window.tabs
    window._switch_project("", window._general_project_state)
    assert window.central_stack.currentWidget() is window.start_page
    assert window.start_page.recent_paths() == [str(folder)]


def test_opening_a_project_puts_it_first_among_the_recent_ones(bare, tmp_path):  # noqa: F811
    window = bare
    first, second = _project(tmp_path, "Primo"), _project(tmp_path, "Secondo")
    _open_and_close(window, first)
    _open_and_close(window, second)
    assert recent_projects.stored(window._config)[:2] == [str(second), str(first)]
    _open_and_close(window, first)
    assert recent_projects.stored(window._config)[:2] == [str(first), str(second)]


def test_a_recent_project_of_another_working_folder_opens_there(bare, tmp_path):  # noqa: F811
    window = bare
    folder = _project(tmp_path / "altra", "Modulo")
    window.field_library_root.setText(str(folder.parent))
    _open_and_close(window, folder)
    window.field_library_root.setText(str(tmp_path / "vuota"))
    window.start_page.open_recent_requested.emit(str(folder))
    assert window._component_name() == "Modulo"
    assert window._library_root() == folder.parent
    assert window._project_dir() == folder
    assert window.central_stack.currentWidget() is window.tabs


def test_a_recent_folder_that_is_no_longer_a_project_is_explained_not_opened(
        bare, tmp_path, monkeypatch):  # noqa: F811
    """@brief Negativo: una cartella senza project_config.json non si apre.

    Resta pero' nell'elenco salvato, come per `load`: un disco scollegato
    torna utilizzabile quando lo si ricollega.
    """
    window = bare
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(args[2]))
    folder = _project(tmp_path, "SoloDocumenti")
    recent_projects.add(window._config, folder)
    window._open_recent_project(str(folder))
    assert window._component_name() == ""
    assert shown and str(folder) in shown[0]
    assert str(folder) in recent_projects.stored(window._config)


def test_removing_from_the_list_keeps_the_folder(bare, tmp_path):  # noqa: F811
    window = bare
    folder = _project(tmp_path, "DaTogliere")
    _open_and_close(window, folder)
    window.start_page.remove_recent_requested.emit(str(folder))
    assert str(folder) not in recent_projects.stored(window._config)
    assert window.start_page.recent_paths() == []
    assert folder.is_dir()


def test_a_rename_drops_the_old_folder_but_switching_project_keeps_it(bare, tmp_path):  # noqa: F811
    window = bare
    old = _project(tmp_path, "Vecchio")
    other = _project(tmp_path, "Altro")
    window._open_project_at(old)
    # Cambio di progetto: la cartella precedente esiste ancora e resta.
    window._open_project_at(other)
    assert str(old) in recent_projects.stored(window._config)
    # Rinomina: la cartella precedente non esiste piu' e sparisce dall'elenco.
    renamed = tmp_path / "Nuovo"
    other.rename(renamed)
    window.field_name.setText("Nuovo")
    stored = recent_projects.stored(window._config)
    assert stored[0] == str(renamed)
    assert str(other) not in stored and str(old) in stored


def test_the_file_menu_lists_recent_projects_and_tells_same_names_apart(bare, tmp_path):  # noqa: F811
    window = bare
    window._fill_recent_menu()
    actions = window.menu_recent.actions()
    assert len(actions) == 1 and not actions[0].isEnabled()
    first = _project(tmp_path / "a", "Gemello")
    second = _project(tmp_path / "b", "Gemello")
    unique = _project(tmp_path / "b", "Unico")
    for folder in (first, second, unique):
        window.field_library_root.setText(str(folder.parent))
        _open_and_close(window, folder)
    window._fill_recent_menu()
    texts = {action.toolTip(): action.text() for action in window.menu_recent.actions()}
    assert texts[str(unique)] == "Unico"
    assert str(first.parent) in texts[str(first)] and str(second.parent) in texts[str(second)]


def test_the_start_page_shows_and_changes_the_working_folder(bare, tmp_path, monkeypatch):  # noqa: F811
    window = bare
    assert window.start_page.library_root_text() == str(tmp_path)
    target = tmp_path / "scelta"
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: str(target))
    window.start_page.btn_root.click()
    assert window._library_root() == target
    assert window.start_page.library_root_text() == str(target)
