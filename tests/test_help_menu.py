"""@brief «?» menu: user guide, log folder, problem report, update check (PIANO-FIX 5-6, 2026-09-26)."""
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from kicadgen.services import update_check
from kicadgen.services.update_check import UpdateCheckResult
from tests.test_photo_inventory_gui import gui


@pytest.fixture
def opened(monkeypatch):
    """URLs the window asked the browser or the file manager to open."""
    urls = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: urls.append(url) or True)
    return urls


class _Shown(list):
    """Shown boxes plus the answers the patched dialogs give."""


@pytest.fixture
def boxes(monkeypatch):
    """Message boxes shown, as (kind, text); `question` answers Yes by default."""
    shown = _Shown()
    answers = {"question": QMessageBox.StandardButton.Yes,
               "information": QMessageBox.StandardButton.Ok}
    for kind in ("information", "warning", "question"):
        monkeypatch.setattr(QMessageBox, kind,
                            lambda *args, kind=kind: shown.append((kind, args[2]))
                            or answers.get(kind, QMessageBox.StandardButton.Ok))
    shown.answers = answers
    return shown


@pytest.fixture
def logs(monkeypatch, tmp_path):
    folder = tmp_path / "program-logs"
    monkeypatch.setattr(gui, "logs_dir", lambda: folder)
    return folder


def test_every_help_entry_is_in_the_menu(window):  # noqa: F811
    help_menu = window.menu_help
    actions = help_menu.actions()
    for action in (window.action_user_guide, window.action_open_logs,
                   window.action_report_problem, window.action_check_updates):
        assert action in actions


def test_the_user_guide_opens_the_shipped_page(window, opened, boxes):  # noqa: F811
    window._open_user_guide()
    assert len(opened) == 1 and opened[0].endswith("/help/guida.html")
    assert Path(gui.resource_path("kicadgen", "gui", "assets", "help", "guida.html")).is_file()


def test_a_missing_user_guide_is_explained_not_opened(window, opened, boxes, monkeypatch,
                                                      tmp_path):  # noqa: F811
    monkeypatch.setattr(gui, "resource_path", lambda *parts: tmp_path / "manca.html")
    window._open_user_guide()
    assert opened == []
    assert boxes and boxes[0][0] == "warning" and "manca.html" in boxes[0][1]


def test_the_log_folder_is_the_project_one_when_a_project_is_open(window, opened, logs):  # noqa: F811
    window._project_dir().mkdir(parents=True, exist_ok=True)
    window._open_log_folder()
    assert opened == [window._project_dir().as_uri()]
    assert not logs.exists()


def test_without_a_project_the_program_log_folder_is_created_and_opened(
        window, opened, logs):  # noqa: F811
    window._switch_project("", window._general_project_state)
    window._open_log_folder()
    assert logs.is_dir() and opened == [logs.as_uri()]


def test_a_problem_report_opens_nothing_when_cancelled(window, opened, boxes, logs):  # noqa: F811
    boxes.answers["information"] = QMessageBox.StandardButton.Cancel
    window._report_problem()
    assert opened == []


def test_a_problem_report_opens_the_logs_and_a_prefilled_issue_without_paths(
        window, opened, boxes, logs):  # noqa: F811
    window._project_dir().mkdir(parents=True, exist_ok=True)
    window._report_problem()
    issue = [url for url in opened if url.startswith("https://")]
    assert len(issue) == 1 and "/issues/new?" in issue[0]
    assert window._project_dir().as_uri() in opened
    # Nessun percorso locale nella segnalazione pubblica.
    from urllib.parse import unquote_plus
    assert window._project_dir().name not in unquote_plus(issue[0])


def _crash_report(logs):
    crash_dir = logs / "crash-reports"
    crash_dir.mkdir(parents=True)
    report = crash_dir / "crash-20260926-120000-000000-1.log"
    report.write_text("x", encoding="utf-8")
    return report


def test_a_problem_report_names_and_opens_the_latest_crash_report(
        window, opened, boxes, logs):  # noqa: F811
    """@brief Con un progetto aperto i log sono i suoi: i rapporti di crash si aprono a parte."""
    report = _crash_report(logs)
    window._project_dir().mkdir(parents=True, exist_ok=True)
    window._report_problem()
    assert str(report) in boxes[0][1]
    assert report.parent.as_uri() in opened and window._project_dir().as_uri() in opened


def test_without_a_project_the_crash_folder_is_not_opened_twice(
        window, opened, boxes, logs):  # noqa: F811
    """@brief Negativo: senza progetto i rapporti stanno gia' nella cartella dei log aperta."""
    report = _crash_report(logs)
    window._switch_project("", window._general_project_state)
    window._report_problem()
    assert str(report) in boxes[0][1]
    assert logs.as_uri() in opened and report.parent.as_uri() not in opened


def _check(window, monkeypatch, result):  # noqa: F811
    monkeypatch.setattr(update_check, "check_latest_release", lambda *args, **kwargs: result)
    window._check_for_updates()
    assert not window.action_check_updates.isEnabled()
    window._update_worker.wait(5000)
    for _ in range(20):
        QApplication.processEvents()
        if window._update_worker is None:
            break
    assert window._update_worker is None and window.action_check_updates.isEnabled()


def test_an_available_update_offers_its_page(window, opened, boxes, monkeypatch):  # noqa: F811
    result = UpdateCheckResult(current_version="1.0.0", latest_version="1.1.0",
                               update_available=True,
                               release_url="https://github.com/x/y/releases/tag/v1.1.0")
    _check(window, monkeypatch, result)
    assert boxes[0][0] == "question" and "1.1.0" in boxes[0][1]
    assert opened == ["https://github.com/x/y/releases/tag/v1.1.0"]


def test_declining_an_update_opens_nothing(window, opened, boxes, monkeypatch):  # noqa: F811
    boxes.answers["question"] = QMessageBox.StandardButton.No
    _check(window, monkeypatch, UpdateCheckResult(current_version="1.0.0", latest_version="1.1.0",
                                                  update_available=True))
    assert opened == []


@pytest.mark.parametrize("result, kind, fragment", [
    (UpdateCheckResult(current_version="1.0.0", latest_version="1.0.0"), "information", "1.0.0"),
    (UpdateCheckResult(current_version="1.0.0", error_kind="no_release", error="404"),
     "information", "nessuna versione"),
    (UpdateCheckResult(current_version="1.0.0", error_kind="timeout", error="timed out"),
     "warning", "connessione"),
    (UpdateCheckResult(current_version="1.0.0", error_kind="invalid_response", error="bad json"),
     "warning", "bad json"),
])
def test_every_update_outcome_is_explained_and_nothing_is_opened(
        window, opened, boxes, monkeypatch, result, kind, fragment):  # noqa: F811
    _check(window, monkeypatch, result)
    assert boxes[0][0] == kind and fragment in boxes[0][1]
    assert opened == []


def test_the_update_worker_belongs_to_the_application_not_the_window(
        window, opened, boxes, monkeypatch):  # noqa: F811
    """@brief G2: the check survives a window close and its late answer shows nothing.

    Closing the window while GitHub is still being asked must not destroy the
    running QThread (the process would abort): the worker is owned by
    QApplication, and the answer that arrives after the close is dropped.
    """
    import threading
    release = threading.Event()

    def slow_check(*args, **kwargs):
        release.wait(5)
        return UpdateCheckResult(current_version="1.0.0", latest_version="1.1.0",
                                 update_available=True)

    monkeypatch.setattr(update_check, "check_latest_release", slow_check)
    window._check_for_updates()
    worker = window._update_worker
    assert worker.parent() is QApplication.instance()
    assert worker.isRunning()
    window.close()
    release.set()
    assert worker.wait(5000)
    for _ in range(20):
        QApplication.processEvents()
    assert boxes == [] and opened == []


def test_an_update_answer_before_the_close_is_still_shown(
        window, opened, boxes, monkeypatch):  # noqa: F811
    """@brief G2 negative: with the window open the same late answer is shown."""
    _check(window, monkeypatch, UpdateCheckResult(current_version="1.0.0",
                                                  latest_version="1.0.0"))
    assert boxes and boxes[0][0] == "information"


@pytest.mark.parametrize("language, name", [("en", "guida.en.html"), ("fr", "guida.fr.html"),
                                            ("de", "guida.de.html"), ("it", "guida.html")])
def test_the_user_guide_opens_in_the_interface_language(window, opened, boxes, monkeypatch,
                                                        language, name):  # noqa: F811
    """@brief Positive (2026-10-30): the guide of the chosen language, not always the Italian one."""
    monkeypatch.setattr(window.i18n, "language", language)
    window._open_user_guide()
    assert len(opened) == 1 and opened[0].endswith(f"/help/{name}") and boxes == []


def test_a_missing_translation_falls_back_to_the_italian_guide(window, opened, boxes, monkeypatch,
                                                               tmp_path):  # noqa: F811
    """@brief Negative: without guida.en.html the Italian guide opens, no warning."""
    (tmp_path / "guida.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr(gui, "resource_path", lambda *parts: tmp_path / parts[-1])
    monkeypatch.setattr(window.i18n, "language", "en")
    window._open_user_guide()
    assert opened == [(tmp_path / "guida.html").as_uri()] and boxes == []
