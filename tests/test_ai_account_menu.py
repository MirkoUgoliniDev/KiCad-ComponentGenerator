"""@brief «Account AI...» in the main window and the key check before paid runs (PIANO-FIX 2, 2026-09-26)."""
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui import ai_account_dialog
from kicadgen.services.photo_inventory import ReviewBundle
from tests.test_photo_inventory_gui import gui


@pytest.fixture
def no_key(window, monkeypatch):  # noqa: F811
    """The window of a user who never configured a key (no source has one)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return window


@pytest.fixture
def questions(monkeypatch):
    """Questions asked, as (title, text); the answer is set in `questions.answer`."""
    class Asked(list):
        answer = QMessageBox.StandardButton.No
    asked = Asked()
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args, **kwargs: asked.append((args[1], args[2])) or asked.answer)
    return asked


def _ready_for_extraction(window, monkeypatch):  # noqa: F811
    """Everything an AI extraction needs except the key; records whether it started."""
    started = []
    monkeypatch.setattr(window, "_photo_detector_options", lambda: ("vision", None))
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("doc.pdf")], None))
    monkeypatch.setattr(window, "_begin_ai_run", lambda title: started.append(title))
    monkeypatch.setattr(window, "_start_extract_worker", lambda *args, **kwargs: None)
    return started


def _saved_review_bundle(tmp_path):
    """A nonempty reviewed extraction bundle eligible for paid reconciliation."""
    return ReviewBundle(
        json_path=tmp_path / "review.json", body_length_mm=40, body_width_mm=20,
        proposals=[{"_id": 1, "_box": [.2, .2, .4, .4], "name": "R1",
                    "kind": "resistor", "x_mm": -8, "y_mm": -4,
                    "length_mm": 8, "width_mm": 4, "height_mm": 1}])


def test_the_configuration_menu_opens_the_ai_account_window(window, monkeypatch):  # noqa: F811
    config_menu = window.menuBar().actions()[1].menu()
    assert window.action_ai_account in config_menu.actions()
    refreshed = []
    monkeypatch.setattr(window.ai_account, "refresh_balance", lambda: refreshed.append(True))
    monkeypatch.setattr(ai_account_dialog.AiAccountDialog, "exec", lambda self: 0)
    # La finestra vera legge i saldi in background: nel test nessuna rete
    # (prima chiamava davvero openrouter.ai con la chiave finta).
    read = []
    monkeypatch.setattr(ai_account_dialog.AiAccountDialog, "refresh_balances",
                        lambda self: read.append(True))
    window.action_ai_account.trigger()
    dialog = window._ai_account_dialog
    assert isinstance(dialog, ai_account_dialog.AiAccountDialog)
    dialog.key_changed.emit()
    assert refreshed == [True]
    assert read == [True]           # the application asks for the balances


def test_with_a_key_the_extraction_starts_without_questions(window, monkeypatch,
                                                            questions):  # noqa: F811
    started = _ready_for_extraction(window, monkeypatch)
    window._extract_with_ai()
    assert started == ["Analisi AI"] and questions == []


def test_without_a_key_nothing_paid_starts_and_the_account_window_is_offered(
        no_key, monkeypatch, questions):  # noqa: F811
    window = no_key
    started = _ready_for_extraction(window, monkeypatch)
    opened = []
    monkeypatch.setattr(window, "_open_ai_account", lambda: opened.append(True))
    window._extract_with_ai()
    assert started == [] and opened == [] and len(questions) == 1
    assert "Account AI..." in questions[0][1]
    questions.answer = QMessageBox.StandardButton.Yes
    window._extract_with_ai()
    assert started == [] and opened == [True]


def test_the_yolo_only_pretest_needs_no_key(no_key, monkeypatch, questions):  # noqa: F811
    """@brief Negativo: il pretest solo YOLO e' locale e gratuito, niente domanda sulla chiave."""
    window = no_key
    pretests = []
    monkeypatch.setattr(window, "_photo_detector_options", lambda: ("yolo_only", "model.pt"))
    monkeypatch.setattr("kicadgen.services.photo_yolo.validate_yolo_setup", lambda path: None)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("doc.pdf")], None))
    monkeypatch.setattr(window, "_start_yolo_pretest", lambda *args: pretests.append(args))
    window._extract_with_ai()
    assert len(pretests) == 1 and questions == []


def test_the_inventory_relaunch_checks_the_key_before_paying(no_key, monkeypatch,
                                                              questions, tmp_path):  # noqa: F811
    window = no_key
    started = []
    bundle = _saved_review_bundle(tmp_path)
    monkeypatch.setattr(window, "_ensure_review_bundle", lambda: bundle)
    monkeypatch.setattr(window, "_review_photo", lambda bundle: b"png")
    monkeypatch.setattr(window, "_begin_ai_run", lambda title: started.append(title))
    workers = []
    monkeypatch.setattr(window, "_start_reconcile_worker", lambda *args: workers.append(args))
    window._relaunch_inventory()
    assert started == [] and workers == [] and len(questions) == 1
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    window._relaunch_inventory()
    assert started == ["Rilancio inventario"]
    assert len(workers) == 1 and workers[0][0] is bundle
    assert workers[0][2].body_length_mm == 40
    assert len(questions) == 1


def test_the_terminal_detail_checks_the_key_before_paying(no_key, monkeypatch,
                                                           questions):  # noqa: F811
    window = no_key
    window._photo_png = b"png"
    monkeypatch.setattr(window, "_photo_components_from_table", lambda: [{"id": "U1"}])
    workers = []
    monkeypatch.setattr(gui, "TerminalDetailWorker", lambda *args, **kwargs: workers.append(args))
    window._start_terminal_refinement()
    assert workers == [] and len(questions) == 1
    assert "Account AI..." in questions[0][1]
