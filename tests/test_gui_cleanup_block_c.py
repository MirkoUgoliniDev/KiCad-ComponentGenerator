"""@brief GUI cleanup of findings G3, G4, G6 and the preview-colour preference (HANDOFF 131, block C).

Each behaviour has a positive and a negative case:

* G3: messages set after the one-time retranslation go through the catalog
  when they are written, and the model list names the chosen provider;
* G4: a 3D result made stale by a data change is still discarded, but one log
  line says so and that the files on disk are out of date;
* G6: the hidden terminal pass charges its calls to the project it started
  from, like every other paid run;
* the «Colori» choice of the STEP chooser goes through the window's
  in-memory preferences, so memory and config.json never diverge.
"""
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox, QWidget

from kicadgen.services import messages

from test_photo_inventory_gui import gui


@pytest.fixture(autouse=True)
def app(qapp):
    """Every test builds widgets: the session QApplication must exist first."""
    return qapp


@pytest.fixture
def english(monkeypatch):
    """Register a translator that knows only the given catalog entries."""
    catalog = {}

    def translate(key, default=None):
        return catalog.get(key, default)

    monkeypatch.setattr(messages, "_translator", translate)
    return catalog


@pytest.fixture
def italian(monkeypatch):
    """No translator: the Italian sources are used as they are."""
    monkeypatch.setattr(messages, "_translator", None)


# ── G3: texts translated when they are set ──────────────────────────────

def test_the_kicad_search_failure_is_translated_when_set(english):
    """@brief Positive: the failure text of the model search follows the language."""
    from kicadgen.gui.photo_review_dialog import PhotoReviewDialog
    english["ui:Ricerca KiCad fallita: {message}"] = "KiCad search failed: {message}"
    stub = SimpleNamespace(_closing=False, model_status=QLabel())
    PhotoReviewDialog._match_failed(stub, "boom")
    assert stub.model_status.text() == "KiCad search failed: boom"


def test_the_kicad_search_failure_stays_italian_without_a_translator(italian):
    """@brief Negative: no translator, the Italian template with the value filled in."""
    from kicadgen.gui.photo_review_dialog import PhotoReviewDialog
    stub = SimpleNamespace(_closing=False, model_status=QLabel())
    PhotoReviewDialog._match_failed(stub, "boom")
    assert stub.model_status.text() == "Ricerca KiCad fallita: boom"


def test_an_interrupted_3d_run_is_logged_in_the_active_language(window, english):  # noqa: F811
    """@brief Positive: the «INTERROTTO 3D» line goes through the catalog."""
    english["ui:INTERROTTO 3D: {message}"] = "3D INTERRUPTED: {message}"
    window._on_cad3d_error(gui.CAD_ABORT_MESSAGE)
    log = window._log_plain_text()
    assert "3D INTERRUPTED: " in log and "INTERROTTO 3D" not in log


def test_an_interrupted_3d_run_keeps_the_italian_marker(window, italian):  # noqa: F811
    """@brief Negative: in Italian the marker other code and tests read is unchanged."""
    window._on_cad3d_error(gui.CAD_ABORT_MESSAGE)
    assert "INTERROTTO 3D: " in window._log_plain_text()


class _IdleWorker(QThread):
    """Stand-in for _ModelsWorker: records the provider, never starts, never downloads."""
    done = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, force_refresh=False, parent=None, provider="openrouter"):
        super().__init__()
        self.provider = provider

    def start(self):
        """Never run: the test only reads the loading message."""


@pytest.mark.parametrize("provider, named, other", [("deepseek", "DeepSeek", "OpenRouter"),
                                                     ("openrouter", "OpenRouter", "DeepSeek")])
def test_the_model_list_message_names_the_chosen_provider(qapp, monkeypatch, provider, named, other):
    """@brief Positive and negative: DeepSeek is named for DeepSeek, OpenRouter only for OpenRouter."""
    from kicadgen.gui import model_dialog
    monkeypatch.setattr(model_dialog, "_ModelsWorker", _IdleWorker)
    dialog = model_dialog.ModelPickerDialog("anthropic/claude-sonnet-5")
    try:
        combo = dialog.combo_provider
        combo.setCurrentIndex(combo.findData(provider))
        dialog._worker = None
        dialog._load_models()
        text = dialog.label_selected.text()
        assert named in text and other not in text
    finally:
        dialog._closed = True
        dialog.close()


# ── G4: a stale 3D result leaves one log line ──────────────────────────

@pytest.mark.parametrize("slot", ["_on_cad3d_generated", "_on_regen_3d_done"])
def test_a_stale_3d_result_is_discarded_with_one_log_line(window, monkeypatch, italian, slot):  # noqa: F811
    """@brief Positive: data changed during the run -> discarded, and the log says so."""
    shown = []
    monkeypatch.setattr(window, "_show_generated_mesh", lambda path: shown.append(path))
    stale = window._model_revision - 1
    monkeypatch.setattr(window, "sender", lambda: SimpleNamespace(_model_revision=stale))
    getattr(window, slot)("C:/out/U1.step")
    log = window._log_plain_text()
    assert shown == []
    assert log.count("Modello 3D scartato") == 1 and "C:/out/U1.step" in log
    assert "OK (3D)" not in log


@pytest.mark.parametrize("slot", ["_on_cad3d_generated", "_on_regen_3d_done"])
def test_a_current_3d_result_is_shown_without_the_discard_line(window, monkeypatch, italian, slot):  # noqa: F811
    """@brief Negative: an up-to-date result is shown and no discard is announced."""
    shown = []
    monkeypatch.setattr(window, "_show_generated_mesh", lambda path: shown.append(path))
    current = window._model_revision
    monkeypatch.setattr(window, "sender", lambda: SimpleNamespace(_model_revision=current))
    getattr(window, slot)("C:/out/U1.step")
    log = window._log_plain_text()
    assert shown == [Path("C:/out/U1.step")]
    assert "Modello 3D scartato" not in log and "OK (3D) -> C:/out/U1.step" in log


# ── G6: the terminal pass charges the project it started from ──────────

class _FakeTerminalWorker(QThread):
    """Terminal worker that makes no call and ends at once."""
    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, photo, components, *args, **kwargs):
        super().__init__()
        self.components = components
        self.only_leads = kwargs.get("only_leads", False)

    def run(self):
        pass


def _arm_terminal_pass(window, monkeypatch):  # noqa: F811
    calls = []
    window._photo_png = b"png"
    monkeypatch.setattr(window, "_photo_components_from_table", lambda: [{"id": "U1"}])
    monkeypatch.setattr(window, "_ensure_api_key", lambda: True)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(window.profile_store, "snapshot",
                        lambda key: {"terminal_prompt": "t", "lead_prompt": "l"})
    monkeypatch.setattr(gui, "TerminalDetailWorker", _FakeTerminalWorker)
    monkeypatch.setattr(window.ai_account, "bind_run_project",
                        lambda folder: calls.append(("bind", folder)))
    monkeypatch.setattr(window.ai_account, "release_run_project",
                        lambda: calls.append(("release",)))
    return calls


def test_the_terminal_pass_is_charged_to_its_starting_project(window, monkeypatch):  # noqa: F811
    """@brief Positive: bound before the worker starts, released when it finishes."""
    calls = _arm_terminal_pass(window, monkeypatch)
    window._start_terminal_refinement()
    worker = window._terminal_worker
    assert calls == [("bind", window._project_dir_or_none())]
    assert worker.wait(5000)
    for _ in range(50):
        QApplication.processEvents()
        if len(calls) == 2:
            break
    assert calls[-1] == ("release",)
    for button in (window.btn_terminal_detail, window.btn_lead_refinement):
        assert button.isEnabledTo(button.parentWidget())


def test_the_terminal_pass_does_not_start_while_another_run_is_bound(window, monkeypatch):  # noqa: F811
    """@brief Negative: with another worker running nothing is bound and nothing starts."""
    calls = _arm_terminal_pass(window, monkeypatch)
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window._start_terminal_refinement()
    assert calls == [] and window._terminal_worker is None


# ── «Colori» of the STEP chooser: the window's preferences, not a copy ──

class _Owner(QWidget):
    """Window stand-in exposing the two members the dialog looks for."""

    def __init__(self, config):
        super().__init__()
        self._config = config
        self.saved = []

    def _save_config_safely(self):
        self.saved.append(dict(self._config))
        return True


def _candidates_dialog(parent=None):
    from kicadgen.gui.model_candidates_dialog import ModelCandidatesDialog
    return ModelCandidatesDialog({"name": "U1", "length_mm": 3, "width_mm": 3, "height_mm": 1},
                                 {"candidates": []}, parent=parent)


def test_the_colour_choice_goes_through_the_windows_preferences(qapp, monkeypatch):
    """@brief Positive: read from and written to the window's in-memory config, saved by it."""
    from kicadgen.gui import app_config, model_candidates_dialog as mcd
    disk_writes = []
    monkeypatch.setattr(app_config, "save_config", lambda config: disk_writes.append(config))
    monkeypatch.setattr(app_config, "load_config", lambda: {mcd.PREVIEW_COLORS_KEY: True})
    owner = _Owner({mcd.PREVIEW_COLORS_KEY: False, "grid_unit": "mm"})
    dialog = _candidates_dialog(owner)
    try:
        assert not dialog.colors_checkbox.isChecked(), "the window's value, not the disk's"
        dialog.colors_checkbox.setChecked(True)
        assert owner._config == {mcd.PREVIEW_COLORS_KEY: True, "grid_unit": "mm"}
        assert owner.saved == [owner._config]
        assert disk_writes == [], "no private load-change-save copy of config.json"
    finally:
        dialog.close()
        owner.close()


def test_without_the_window_the_colour_choice_still_reaches_the_disk(qapp, monkeypatch):
    """@brief Negative: a dialog with no preferences owner keeps the direct file path."""
    from kicadgen.gui import app_config, model_candidates_dialog as mcd
    disk_writes = []
    monkeypatch.setattr(app_config, "save_config", lambda config: disk_writes.append(dict(config)))
    monkeypatch.setattr(app_config, "load_config", lambda: {mcd.PREVIEW_COLORS_KEY: True})
    plain_parent = QWidget()
    dialog = _candidates_dialog(plain_parent)
    try:
        assert dialog.colors_checkbox.isChecked()
        dialog.colors_checkbox.setChecked(False)
        assert disk_writes == [{mcd.PREVIEW_COLORS_KEY: False}]
    finally:
        dialog.close()
        plain_parent.close()


def test_an_estimated_package_origin_produces_a_note_for_the_log():
    """Blocco C (131): un'origine «pin1» viene stimata dal campo dei pad e lo
    dice con una nota (la finestra la scrive nel log quando acquisisce la
    geometria); negativo: body_center non aggiunge note."""
    from types import SimpleNamespace
    from kicadgen.core.package_geometry import package_from_mapping
    pins = [dict(number=str(i + 1), x_mm=i * 2.54, y_mm=0.0, pad_length_mm=1.5,
                 pad_width_mm=1.0) for i in range(4)]
    geometry = package_from_mapping(dict(name="X", family="sip", origin="pin1", pins=pins))
    assert geometry.frame_notes()
    centred = package_from_mapping(dict(name="X", family="sip", origin="body_center", pins=pins))
    assert centred.frame_notes() == []


def test_a_refused_terminal_pass_says_why(window, monkeypatch):
    """Revisione del blocco C: con un altro lavoro in corso il passo terminali
    non parte e lo dice nel log (negativo: senza lavori in corso non c'e' questa riga)."""
    logged = []
    monkeypatch.setattr(window, "_append_log", logged.append)
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    window._start_terminal_refinement()
    assert any("non avviato" in line for line in logged)
    logged.clear()
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
    monkeypatch.setattr(window, "_photo_png", None, raising=False)
    window._start_terminal_refinement()
    assert not any("non avviato" in line for line in logged)
