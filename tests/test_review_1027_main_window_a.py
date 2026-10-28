"""@brief Review of 2026-10-27, block A: main-window regressions fixed in that round.

Each finding has a positive case (the fixed behaviour) and a negative case
(the behaviour that must stay as it was), so that a rule is verified for a
structural reason and not for the one scenario that was observed.
"""
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog, QMessageBox

from kicadgen.gui import main_window as gui
from kicadgen.gui import project_dialogs
from kicadgen.services import recent_projects
from kicadgen.services.datasheet_extractor import ExtractedParams
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES


def _project(root, name):
    """Create a project folder with its docs/ subfolder and return it."""
    folder = root / name
    (folder / "docs").mkdir(parents=True)
    return folder


def _set_language(window, language):
    window.language_combo.setCurrentIndex(window.language_combo.findData(language))


def _rename_through_the_dialog(window, monkeypatch, name):
    """Rename as the user does, through File > «Rinomina progetto…»."""
    def accept(self):
        self.field.setText(name)
        ok = self.buttons.button(self.buttons.StandardButton.Ok)
        return QDialog.DialogCode.Accepted if ok.isEnabled() else QDialog.DialogCode.Rejected
    monkeypatch.setattr(project_dialogs.ProjectNameDialog, "exec", accept)
    window._rename_project()


def _flush_deferred_deletes():
    """Deliver the DeferredDelete events that `deleteLater` posted."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def bare(window):
    """The window without a project and without recent projects."""
    window._switch_project("", window._general_project_state)
    window._config.pop(recent_projects.CONFIG_KEY, None)
    window._refresh_start_page()
    return window


# ── A1: the committed working folder follows every program-set text ──────

def test_recent_project_of_another_root_commits_the_new_root(bare, tmp_path):
    """@brief Positive: opening a recent project elsewhere commits its working folder.

    Afterwards, with no project open, typing in the field keeps the new
    folder in use and a focus-out puts the new folder back, not the old one.
    """
    window = bare
    root_a, root_b = tmp_path / "A", tmp_path / "B"
    root_a.mkdir()
    folder = _project(root_b, "Modulo")
    window.field_library_root.setText(str(root_b))
    window._open_project_at(folder)
    window._switch_project("", window._general_project_state)
    window.field_library_root.setText(str(root_a))
    assert window._library_root_committed == str(root_a)

    window._open_recent_project(str(folder))
    assert window._component_name() == "Modulo"
    assert window._library_root() == root_b
    assert window._library_root_committed == str(root_b)

    window._switch_project("", window._general_project_state)
    window.field_library_root.end(False)
    QTest.keyClicks(window.field_library_root, "x")
    assert window.field_library_root.isModified()
    assert window._library_root() == root_b
    window._on_library_root_focus_left()
    assert window.field_library_root.text() == str(root_b)
    assert window._library_root() == root_b


def test_typed_text_still_waits_for_enter(bare, tmp_path):
    """@brief Negative: a keystroke commits nothing; the folder in use is the committed one."""
    window = bare
    root_a = tmp_path / "A"
    root_a.mkdir()
    window.field_library_root.setText(str(root_a))
    assert window._library_root_committed == str(root_a)
    window.field_library_root.end(False)
    QTest.keyClicks(window.field_library_root, "zz")
    assert window.field_library_root.text() == str(root_a) + "zz"
    assert window._library_root_committed == str(root_a)
    assert window._library_root() == root_a


def test_folder_put_back_during_a_running_job_is_committed(window, tmp_path, monkeypatch):
    """@brief Positive: the folder restored while a job runs is also the committed one.

    The same helper serves every program-set text of the field: a later
    focus-out finds the restored folder, not an older one.
    """
    folder = _project(tmp_path, "InCorso")
    window._open_project_at(folder)
    other = tmp_path / "Altrove"
    other.mkdir()
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window.field_library_root.setText(str(other))
    assert window.field_library_root.text() == str(tmp_path)
    assert window._library_root_committed == str(tmp_path)
    assert window._component_name() == "InCorso"


# ── A9: the kind question comes before any state changes ─────────────────

def test_cancelling_the_kind_question_of_a_recent_project_changes_nothing(bare, tmp_path, monkeypatch):
    """@brief Positive: cancel leaves working folder and open project as they were.

    Negative: accepting the question opens the project in its folder.
    """
    from kicadgen.gui.project_state import write_project_config
    window = bare
    root_a, root_b = tmp_path / "A", tmp_path / "B"
    current = _project(root_a, "Attuale")
    other = _project(root_b, "Altro")
    # A project file without kind and without component type: the kind is
    # asked on open, and the file makes the folder a reopenable project.
    write_project_config(other, {"widgets": {}})
    assert recent_projects.is_project(other)
    window.field_library_root.setText(str(root_a))
    window._open_project_at(current)
    assert window._component_name() == "Attuale"

    asked = []
    monkeypatch.setattr(project_dialogs.ProjectKindDialog, "exec",
                        lambda self: asked.append(True) or QDialog.DialogCode.Rejected)
    window._open_recent_project(str(other))
    assert asked == [True]
    assert window._component_name() == "Attuale"
    assert window._open_project_folder == current
    assert window.field_library_root.text() == str(root_a)
    assert window._library_root_committed == str(root_a)
    assert window._library_root() == root_a

    monkeypatch.setattr(project_dialogs.ProjectKindDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    window._open_recent_project(str(other))
    assert window._component_name() == "Altro"
    assert window._library_root() == root_b
    assert window._open_project_folder == other


def test_cancelling_the_kind_question_restores_the_refused_config_mark(window, tmp_path, monkeypatch):
    """@brief A refused project_config.json noted during a cancelled open is forgotten again."""
    folder = _project(tmp_path, "Rotto")
    (folder / "project_config.json").write_text("{not json", encoding="utf-8")
    window._project_config_refused = None
    monkeypatch.setattr(project_dialogs.ProjectKindDialog, "exec",
                        lambda self: QDialog.DialogCode.Rejected)
    assert window._prepare_project_open(folder) is None
    assert window._project_config_refused is None


# ── A2: the stale-preview cleanup logs under the NEW name ─────────────────

def test_rename_with_a_locked_preview_does_not_recreate_the_old_folder(window, tmp_path, monkeypatch):
    """@brief Positive: the «left in KiCad» line lands in the renamed project's diary."""
    from kicadgen.services.kicad_library import PREVIEW_DIR_NAME, kicad_lock_file
    old = _project(tmp_path, "Vecchio")
    window._open_project_at(old)
    preview = old / PREVIEW_DIR_NAME
    preview.mkdir()
    board = preview / "Vecchio-preview.kicad_pcb"
    board.write_text("(kicad_pcb)")
    kicad_lock_file(board).write_text("lock")

    _rename_through_the_dialog(window, monkeypatch, "Nuovo")
    assert window._component_name() == "Nuovo"
    assert (tmp_path / "Nuovo").is_dir()
    assert not old.exists(), sorted(p.name for p in old.rglob("*"))
    diary = (tmp_path / "Nuovo" / window.LOG_FILENAME).read_text(encoding="utf-8")
    assert "Anteprima KiCad con il nome vecchio lasciata in" in diary
    assert board.name in {p.name for p in (tmp_path / "Nuovo" / PREVIEW_DIR_NAME).iterdir()}


def test_rename_without_previews_still_logs_in_the_new_diary(window, tmp_path, monkeypatch):
    """@brief Negative: a plain rename keeps its log line, in the new folder only."""
    old = _project(tmp_path, "Prima")
    window._open_project_at(old)
    _rename_through_the_dialog(window, monkeypatch, "Dopo")
    assert window._component_name() == "Dopo"
    assert not old.exists()
    diary = (tmp_path / "Dopo" / window.LOG_FILENAME).read_text(encoding="utf-8")
    assert "Progetto rinominato: Prima diventa Dopo." in diary


# ── A3: an unreadable docs/ folder costs one log line, not the process ──

def test_unreadable_docs_folder_leaves_the_list_empty_and_logs_once(window, monkeypatch):
    """@brief Positive: PermissionError on docs/ is caught; said once per folder."""
    docs = window._docs_dir()
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "datasheet.pdf").write_bytes(b"%PDF")
    real_iterdir = Path.iterdir

    def denied(self):
        if self == docs:
            raise PermissionError(13, "Access is denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", denied)
    window._refresh_docs_list()
    window._refresh_docs_list()
    assert window.docs_list.count() == 0
    log = window.ai_progress_text.toPlainText()
    assert log.count("Cartella dei documenti non leggibile:") == 1
    assert str(docs) in log


def test_readable_docs_folder_is_listed_again_after_the_error(window, monkeypatch):
    """@brief Negative: a folder that can be read is listed; the line returns only after a recovery."""
    docs = window._docs_dir()
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "datasheet.pdf").write_bytes(b"%PDF")
    real_iterdir = Path.iterdir
    denied = {"on": True}

    def maybe_denied(self):
        if self == docs and denied["on"]:
            raise PermissionError(13, "Access is denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", maybe_denied)
    window._refresh_docs_list()
    denied["on"] = False
    window._refresh_docs_list()
    assert [window.docs_list.item(i).text() for i in range(window.docs_list.count())] == ["datasheet.pdf"]
    denied["on"] = True
    window._refresh_docs_list()
    assert window.docs_list.count() == 0
    assert window.ai_progress_text.toPlainText().count("Cartella dei documenti non leggibile:") == 2


# ── A4 / A5: visible texts go through the catalogue ──────────────────────

def test_photo_call_line_is_one_catalogue_template(window):
    """@brief The photo-call line has a {model} placeholder in every language and translates whole."""
    entry = DEFAULT_TRANSLATIONS["ui:Chiamata sulla fotografia affidata a {model}"]
    for language in LANGUAGES:
        assert "{model}" in entry[language]
    _set_language(window, "en")
    assert gui.tx("Chiamata sulla fotografia affidata a {model}", model="x/y") == "Photo call entrusted to x/y"
    _set_language(window, "it")
    assert gui.tx("Chiamata sulla fotografia affidata a {model}", model="x/y") == \
        "Chiamata sulla fotografia affidata a x/y"


@pytest.mark.parametrize("language", ["en", "fr"])
def test_session_model_picker_title_is_translated(window, monkeypatch, language):
    """@brief Positive: the picker receives the catalogue title of the active language."""
    seen = {}

    def fake_pick_model(current, parent, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(gui, "pick_model", fake_pick_model)
    _set_language(window, language)
    window._pick_session_model()
    assert seen["title"] == DEFAULT_TRANSLATIONS["ui:Modello solo per questa sessione"][language]


def test_session_model_picker_title_in_italian(window, monkeypatch):
    """@brief Negative: in Italian the title is the source text, unchanged."""
    seen = {}
    monkeypatch.setattr(gui, "pick_model", lambda current, parent, **kw: seen.update(kw))
    _set_language(window, "it")
    window._pick_session_model()
    assert seen["title"] == "Modello solo per questa sessione"


# ── A6: the proposal of the analysis never renames by itself ─────────────

def test_document_migration_helpers_are_gone():
    """@brief The unreachable migration helpers and their catalogue key no longer exist."""
    assert not hasattr(gui.MainWindow, "_migrate_docs_to_new_name")
    assert not hasattr(gui.MainWindow, "_rename_folder_case")
    assert "component.rename_conflict" not in DEFAULT_TRANSLATIONS


def test_extraction_keeps_the_chosen_name_and_logs_the_proposal(window):
    """@brief An extraction proposing another name leaves the project name alone."""
    params = ExtractedParams(
        name="Proposto", description="", tags="", confidence="high", notes="",
        rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=5.08, pad_diameter_mm=1.7, drill_mm=1.0)
    window._on_extracted(params)
    assert window._component_name() == "Componente_di_prova"
    assert params.name == "Componente_di_prova"
    assert "L'analisi propone il nome «Proposto»" in window.ai_progress_text.toPlainText()


# ── A7: modal dialogs are released once answered ─────────────────────────

def test_modal_dialogs_do_not_accumulate_as_window_children(window, monkeypatch):
    """@brief Positive: N openings leave the number of dialog children unchanged."""
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    _flush_deferred_deletes()
    before = len(window.findChildren(QDialog))
    for _ in range(4):
        window._show_about()
        window._show_shortcuts()
        window._ask_board_size()
        window._ask_pending_edits_choice(("symbol",), True)
        window._ask_geometry_doubt({"code": "x", "message": "m", "pins": [], "swap_clears": False})
        window._ask_user_pin_names({"1": "A"}, {"1": "B"})
    assert len(window.findChildren(QDialog)) > before     # alive until the event loop runs
    _flush_deferred_deletes()
    assert len(window.findChildren(QDialog)) == before


def test_answer_is_still_readable_right_after_exec(window, monkeypatch):
    """@brief Negative: the release is deferred, so the answer of the dialog is read first."""
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    _flush_deferred_deletes()
    result = window._ask_board_size()
    assert isinstance(result, tuple) and len(result) == 2
    assert window._ask_pending_edits_choice(("footprint",), True) == window.PENDING_EDITS_STAY
    dialog = window._board_size_dialog
    assert dialog.windowTitle()                           # readable before the flush
    _flush_deferred_deletes()
    with pytest.raises(RuntimeError):
        dialog.windowTitle()                              # released afterwards


# ── A8: the key check covers the crop model too ──────────────────────────

def _no_openrouter_key(monkeypatch):
    from kicadgen.services import openrouter, providers

    def missing():
        raise openrouter.MissingApiKeyError("no OpenRouter key in this test")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(openrouter, "api_key", missing)
    monkeypatch.setattr(providers, "deepseek_api_key", lambda: "deepseek-test-key")
    return providers


def test_openrouter_crop_model_with_deepseek_main_model_asks_for_the_key(window, monkeypatch):
    """@brief Positive: DeepSeek main and photo model, OpenRouter crop model -> the key is asked."""
    providers = _no_openrouter_key(monkeypatch)
    questions = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args, **kwargs: questions.append(args) or QMessageBox.StandardButton.No)
    values = {"model": providers.DEEPSEEK_PREFIX + "deepseek-chat", "crop_model": "openai/gpt-4o"}
    monkeypatch.setattr(window, "_profile_values", lambda: dict(values))
    window._session_model = None
    assert window._run_models() == (values["model"], values["model"], "openai/gpt-4o")
    assert window._ensure_api_key() is False
    assert len(questions) == 1


def test_all_deepseek_models_need_no_openrouter_key(window, monkeypatch):
    """@brief Negative: with every model on DeepSeek (crop model blank) nothing is asked."""
    providers = _no_openrouter_key(monkeypatch)
    questions = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args, **kwargs: questions.append(args) or QMessageBox.StandardButton.No)
    values = {"model": providers.DEEPSEEK_PREFIX + "deepseek-chat", "crop_model": ""}
    monkeypatch.setattr(window, "_profile_values", lambda: dict(values))
    window._session_model = None
    assert window._run_models() == (values["model"], values["model"])
    assert window._ensure_api_key() is True
    assert questions == []
