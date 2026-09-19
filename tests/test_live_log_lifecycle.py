"""@brief Verify live GUI logging survives component-name migrations."""

from pathlib import Path
import shutil

import pytest

from test_photo_inventory_gui import window
from kicadgen.services.datasheet_extractor import ExtractedParams


def _extracted(name):
    return ExtractedParams(
        name=name, description="Log lifecycle regression", tags="test",
        confidence="high", notes="", rows=2, cols=4, pitch_mm=2.54,
        row_spacing_mm=5.08, pad_diameter_mm=1.7, drill_mm=1.0,
    )


def _prepare_project(window, docs_state):
    window.field_name.setText("LogOriginal")
    docs = window._docs_dir()
    assert not docs.exists()
    if docs_state != "absent":
        docs.mkdir(parents=True)
    if docs_state == "populated":
        (docs / "datasheet.pdf").write_bytes(b"original document")
    window._append_log("before-migration-proof")
    return window._log_file()


@pytest.mark.parametrize("handler", ["_append_log", "_on_ai_progress"])
def test_warning_reaches_disk_and_ai_dialog_exactly_once(window, handler):
    message = "ATTENZIONE: <b>live-warning-proof</b> & details"
    getattr(window, handler)(message)

    assert window.ai_log_dialog.isAncestorOf(window.ai_progress_text)
    assert window._log_file().read_text(encoding="utf-8").count(message) == 1
    assert window.ai_progress_text.toPlainText().count(message) == 1
    assert window.log_text.toPlainText().count(message) == 1
    assert window.btn_copy_ai_log.isEnabled()


@pytest.mark.parametrize("docs_state", ["absent", "empty"])
def test_name_migration_without_documents_preserves_log_and_continues_it(window, docs_state):
    source = _prepare_project(window, docs_state)
    original = source.read_bytes()
    target = source.parent.parent / "LogRenamed" / window.LOG_FILENAME

    window._on_extracted(_extracted("LogRenamed"))

    assert window._component_name() == "LogRenamed"
    assert window._log_file() == target
    assert not source.exists()
    assert target.read_bytes().startswith(original)
    window._append_log("after-migration-proof")
    text = target.read_text(encoding="utf-8")
    assert text.count("before-migration-proof") == 1
    assert text.count("after-migration-proof") == 1
    assert not source.exists()


@pytest.mark.parametrize("docs_state", ["absent", "empty", "populated"])
@pytest.mark.parametrize("entry", ["move", "migrate", "extracted"])
def test_failed_log_move_refuses_rename_and_preserves_project(
    window, monkeypatch, docs_state, entry,
):
    source = _prepare_project(window, docs_state)
    old_project = window._project_dir()
    original = source.read_bytes()
    destination = old_project.parent / "LogRenamed"
    attempts = []

    def fail_move(src, dst):
        attempts.append((Path(src), Path(dst)))
        raise OSError("log-move-denied-proof")

    monkeypatch.setattr(shutil, "move", fail_move)
    if entry == "move":
        assert window._move_log_to(destination) is False
    elif entry == "migrate":
        assert window._migrate_docs_to_new_name("LogRenamed") == "LogOriginal"
    else:
        params = _extracted("LogRenamed")
        window._on_extracted(params)
        assert params.name == "LogOriginal"

    assert attempts == [(source, destination / window.LOG_FILENAME)]
    assert window._component_name() == "LogOriginal"
    assert window._project_dir() == old_project
    assert window._log_file() == source
    assert source.read_bytes().startswith(original)
    assert not (destination / window.LOG_FILENAME).exists()
    if docs_state == "populated":
        assert (window._docs_dir() / "datasheet.pdf").read_bytes() == b"original document"
        assert not (destination / "docs" / "datasheet.pdf").exists()

    warning = "Rinomina annullata: impossibile trasferire il log (log-move-denied-proof)."
    assert source.read_text(encoding="utf-8").count(warning) == 1
    assert window.ai_progress_text.toPlainText().count(warning) == 1
    assert window.log_text.toPlainText().count(warning) == 1
    window._append_log("still-original-proof")
    assert source.read_text(encoding="utf-8").count("still-original-proof") == 1


def test_same_path_move_and_name_migration_do_not_delete_or_duplicate_log(window, monkeypatch):
    source = _prepare_project(window, "populated")
    original = source.read_bytes()

    def unexpected_file_operation(*args, **kwargs):
        pytest.fail("A same-path log migration must not move or delete files")

    monkeypatch.setattr(shutil, "move", unexpected_file_operation)
    monkeypatch.setattr(Path, "unlink", unexpected_file_operation)
    assert window._move_log_to(source.parent) is True
    assert source.read_bytes() == original
    assert window._migrate_docs_to_new_name("LogOriginal") == "LogOriginal"
    assert source.read_bytes() == original
    assert (window._docs_dir() / "datasheet.pdf").read_bytes() == b"original document"
