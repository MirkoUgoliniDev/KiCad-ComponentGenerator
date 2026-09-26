"""@brief Audit 25/09 (B1-B11): pretest «Solo YOLO locale», «Interrompi» and settings robustness.

Each behaviour has a positive case and a structurally different negative
case. The run-level tests go through real QThread workers and their queued
signals (review request, done, error, finished), not through direct calls of
the slots, because the defects lived in the ordering of those signals.
"""
import json
import threading
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from kicadgen.gui import main_window as gui
from kicadgen.services.photo_inventory import InventoryResult

from test_photo_inventory_gui import window  # noqa: F401 - fixture


def _pump(condition, timeout=5.0):
    """Process Qt events until `condition()` is true or the timeout expires."""
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def _png():
    import cv2
    import numpy as np
    ok, data = cv2.imencode(".png", np.zeros((20, 40, 3), np.uint8))
    assert ok
    return data.tobytes()


_ROW = {"_id": 1, "_box": [0.1, 0.1, 0.4, 0.4], "name": "YOLO candidate 1", "kind": "generic"}


class _FakeReviewDialog:
    """Stand-in for YoloReviewDialog: records the opening and accepts everything."""

    opened = []

    def __init__(self, photo_png, proposals, *_args, **_kwargs):
        self.proposals = proposals
        _FakeReviewDialog.opened.append(len(proposals))

    def exec(self):
        return QDialog.DialogCode.Accepted

    def kept(self):
        return [dict(p) for p in self.proposals]


@pytest.fixture
def review_dialog(monkeypatch):
    import kicadgen.gui.yolo_review_dialog as review_module
    import kicadgen.services.photo_yolo as photo_yolo
    _FakeReviewDialog.opened = []
    monkeypatch.setattr(review_module, "YoloReviewDialog", _FakeReviewDialog)
    monkeypatch.setattr(photo_yolo, "detector_classes", lambda: ())
    return _FakeReviewDialog


def _pretest_ready(window, monkeypatch):
    window.field_body_length.setValue(20)
    window.field_body_width.setValue(10)
    monkeypatch.setattr(window, "_is_castellated", lambda: False)


def _start_pretest(window, roles=None, paths=(Path("top.png"),)):
    window._start_yolo_pretest(list(paths), roles, None)
    return window._extract_worker


# ── B1 / B11: abort, then the abandoned run asks for its review ───────────

def test_an_abandoned_pretest_never_opens_its_review(window, monkeypatch, review_dialog):
    """@brief B1: after «Interrompi» the late review request gets None, nothing opens or is saved."""
    _pretest_ready(window, monkeypatch)
    release, answers = threading.Event(), []

    def run(self):
        release.wait(5)
        answers.append(self._review_proposals(_png(), [dict(_ROW)], 20.0, 10.0))
        self.done.emit(None)
    monkeypatch.setattr(gui.YoloPretestWorker, "run", run)
    monkeypatch.setattr(gui.QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    worker = _start_pretest(window)
    assert _pump(worker.isRunning)
    window._request_abort()
    assert window.analysis_overlay.isHidden()
    release.set()
    assert _pump(worker.isFinished) and _pump(lambda: window._extract_worker is None)
    assert answers == [None]
    assert review_dialog.opened == []
    project = window._project_dir()
    assert not (project / "photo-review").exists()
    assert not (project / gui.MainWindow.PRETEST_SUBDIR / "photo-review").exists()
    # La modale non torna su: nessuna operazione la possiederebbe piu'.
    assert window.analysis_overlay.isHidden()
    assert window.btn_extract_ai.isEnabled()


def test_an_abandoned_paid_run_stops_before_the_reconciliation(window, monkeypatch, review_dialog):
    """@brief B1 on the paid path: the abandoned ExtractWorker gets None, i.e. no reconciliation."""
    answers = []

    def run(self):
        answers.append(self._review_proposals(_png(), [dict(_ROW)], 20.0, 10.0))
    monkeypatch.setattr(gui.ExtractWorker, "run", run)
    worker = gui.ExtractWorker([], photo_detector="yolo")
    worker._analysis_revision = window._analysis_revision - 1  # corsa scaduta
    worker.review_requested.connect(window._on_review_requested)
    worker.start()
    assert _pump(worker.isFinished)
    assert answers == [None] and review_dialog.opened == []
    assert not (window._project_dir() / "photo-review").exists()


def test_the_current_pretest_review_opens_and_saves_apart(window, monkeypatch, review_dialog):
    """@brief Negative of B1 and positive of B2: a current run opens the review, and the kept
    boxes go to the pretest folder, never to the extraction bundle."""
    _pretest_ready(window, monkeypatch)
    answers = []

    def run(self):
        kept = self._review_proposals(_png(), [dict(_ROW)], 20.0, 10.0)
        answers.append(kept)
        self.done.emit(InventoryResult(components=[dict(_ROW)]) if kept is not None else None)
    monkeypatch.setattr(gui.YoloPretestWorker, "run", run)
    worker = _start_pretest(window)
    assert _pump(worker.isFinished) and _pump(lambda: not window._extraction_busy)
    assert review_dialog.opened == [1] and answers[0][0]["_id"] == 1
    project = window._project_dir()
    assert (project / "yolo-pretest" / "photo-review" / "proposals.json").is_file()
    assert not (project / "photo-review").exists()
    assert window._review_bundle() is None
    # Il pretest non e' un'estrazione: non la segna come rivista.
    assert window._reviewed_this_extraction is False
    # B11: la vera via di fine ha riportato la finestra in quiete.
    assert window.analysis_overlay.isHidden()
    assert window.btn_extract_ai.isEnabled() and window.tabs.isEnabled()
    assert "Pretest YOLO concluso: 1 riquadri tenuti" in window.ai_progress_text.toPlainText()


# ── B2: pretest bundle and evidence apart from the extraction ─────────────

def test_pretest_evidence_goes_to_its_own_cache(window, monkeypatch):
    """@brief B2: the pretest detector JSON never counts as extraction evidence."""
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    _pretest_ready(window, monkeypatch)
    worker = _start_pretest(window)
    project = window._project_dir()
    assert Path(worker.photo_cache_dir) == project / "yolo-pretest" / "photo-cache"
    window._hide_analysis_overlay()
    window._end_extraction_busy()
    # Un detector JSON del pretest, piu' recente di qualunque estrazione.
    cache = Path(worker.photo_cache_dir)
    cache.mkdir(parents=True)
    (cache / "detector-29990101-000000.json").write_text(
        json.dumps({"components": [dict(_ROW)]}), encoding="utf-8")
    assert window._saved_detector_proposals() is None
    # Negativo: lo stesso file nella cache dell'estrazione e' evidenza valida.
    extraction_cache = window._photo_cache_dir()
    extraction_cache.mkdir(parents=True)
    (extraction_cache / "detector-29990101-000000.json").write_text(
        json.dumps({"components": [dict(_ROW)]}), encoding="utf-8")
    assert window._saved_detector_proposals() is not None


def test_reopen_and_relaunch_follow_the_detector(window):
    """@brief B2: in pretest mode «Riapri revisione» opens the pretest bundle and the paid
    relaunch is not offered; with the paid detector only the extraction bundle counts."""
    window._write_review_bundle([dict(_ROW)], _png(), 20, 10, detector="yolo_only",
                                root=window._pretest_root())
    combo = window.combo_photo_detector
    combo.setCurrentIndex(combo.findData("yolo_only"))
    window._refresh_reopen_review_button()
    parent = window.btn_reopen_review.parentWidget()
    assert window.btn_reopen_review.isVisibleTo(parent)
    assert not window.btn_relaunch_inventory.isVisibleTo(parent)
    # Negativo: con «YOLO locale + verifica AI» il pacchetto del pretest non
    # esiste per il rilancio ne' per la revisione dell'estrazione.
    combo.setCurrentIndex(combo.findData("yolo"))
    window._refresh_reopen_review_button()
    assert not window.btn_reopen_review.isVisibleTo(parent)
    assert not window.btn_relaunch_inventory.isVisibleTo(parent)
    window._write_review_bundle([dict(_ROW)], _png(), 20, 10, detector="yolo")
    window._refresh_reopen_review_button()
    assert window.btn_relaunch_inventory.isVisibleTo(parent)
    assert window._review_bundle().detector == "yolo"


def test_a_legacy_pretest_bundle_is_not_a_relaunch_base(window):
    """@brief B2: a `photo-review/` bundle recorded as yolo_only (written before the fix)
    reopens as a pretest but is never reconciled for money."""
    window._write_review_bundle([dict(_ROW)], _png(), 20, 10, detector="yolo_only")
    assert window._review_bundle() is None
    assert window._pretest_bundle().detector == "yolo_only"


def test_reopening_in_pretest_mode_saves_into_the_pretest_bundle(window, review_dialog):
    """@brief B2: the free reopening writes back where it read, not into the extraction bundle."""
    window._write_review_bundle([dict(_ROW)], _png(), 20, 10, detector="yolo_only",
                                root=window._pretest_root())
    combo = window.combo_photo_detector
    combo.setCurrentIndex(combo.findData("yolo_only"))
    window._reopen_detector_review()
    assert review_dialog.opened == [1]
    assert not (window._project_dir() / "photo-review").exists()
    assert "Revisione del pretest YOLO salvata offline" in window._log_plain_text()


# ── B3: the real abort label ──────────────────────────────────────────────

def test_the_abort_names_the_pretest_through_the_real_path(window, monkeypatch):
    """@brief B3: confirmation and log say «Pretest YOLO locale», not «Analisi AI dei documenti»."""
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    monkeypatch.setattr(gui.QThread, "isRunning", lambda self: True)
    asked = []
    monkeypatch.setattr(gui.QMessageBox, "question",
                        lambda _p, _t, text, *a: asked.append(text) or QMessageBox.StandardButton.Yes)
    _pretest_ready(window, monkeypatch)
    _start_pretest(window)
    window._request_abort()
    assert asked[0].splitlines()[0].startswith("Pretest YOLO locale")
    assert "Analisi AI dei documenti" not in window._log_plain_text()
    # Negativo: nello stesso slot l'estrazione vera resta «Analisi AI dei documenti».
    window._extract_worker = gui.ExtractWorker([])
    assert window._abortable_operation()[2] == "Analisi AI dei documenti"
    window._extract_worker = None
    window._abandoned_workers = []


# ── B4: no stale cost in the pretest modal ────────────────────────────────

def test_the_pretest_modal_does_not_show_the_last_paid_cost(window, monkeypatch):
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    window.analysis_overlay.set_cost("Costo analisi: 0.1234 USD")
    # Negativo: senza quote il pretest non parte e il consuntivo resta.
    monkeypatch.setattr(window, "_body_size", lambda: None)
    _start_pretest(window)
    assert not window.analysis_overlay.cost.isHidden()
    monkeypatch.undo()
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    _pretest_ready(window, monkeypatch)
    _start_pretest(window)
    assert window.analysis_overlay.cost.isHidden()
    window._on_yolo_pretest_finished()


# ── B5: pretest wording for pretest errors ────────────────────────────────

def test_a_pretest_error_is_reported_as_such(window, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a: shown.append(a[1]))

    def run(self):
        self.error.emit("boom")
    monkeypatch.setattr(gui.YoloPretestWorker, "run", run)
    _pretest_ready(window, monkeypatch)
    worker = _start_pretest(window)
    assert _pump(worker.isFinished) and _pump(lambda: not window._extraction_busy)
    assert shown == ["Pretest YOLO non riuscito"]
    assert window.analysis_overlay.isHidden() and window.btn_extract_ai.isEnabled()
    # Negativo: l'errore dell'estrazione vera resta «Estrazione fallita».
    window._on_extract_error("boom")
    assert shown[-1] == "Estrazione fallita"


# ── B6: busy text and YOLO controls during a run ──────────────────────────

def test_a_settings_change_during_a_run_keeps_the_busy_state(window):
    window._begin_ai_run("Analisi AI")
    window.btn_yolo_settings.setEnabled(False)
    window._refresh_model_combo()  # cio' che fa «Default generali AI» dopo OK
    assert window.btn_extract_ai.text() == "Analisi in corso..."
    assert not window.btn_yolo_settings.isEnabled()
    window._on_extract_finished()
    assert window.btn_extract_ai.text() == "Estrai parametri con AI"
    assert window.btn_yolo_settings.isEnabled()
    # Negativo: a riposo lo stesso ricalcolo rimette il testo di riposo.
    window.btn_extract_ai.setText("x")
    window._refresh_model_combo()
    assert window.btn_extract_ai.text() == "Estrai parametri con AI"


# ── B7: licenses menu and dialog ──────────────────────────────────────────

def test_the_licenses_entry_and_close_button_follow_the_language(window, monkeypatch):
    monkeypatch.setattr(gui.QDialog, "exec", lambda self: 0)
    from PyQt6 import sip
    window._show_licenses()
    dialog = window._licenses_dialog
    close = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close)
    assert close.text() == "Chiudi" and close.toolTip()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert sip.isdeleted(dialog)
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    assert window.action_licenses.text() == "Licenses"
    window._show_licenses()
    close = window._licenses_dialog.findChild(QDialogButtonBox).button(
        QDialogButtonBox.StandardButton.Close)
    assert close.text() == "Close"
    window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
    assert window.action_licenses.text() == "Licenze"


# ── B8: a failure while starting the pretest ──────────────────────────────

def test_a_pretest_that_cannot_start_gives_the_window_back(window, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a: shown.append(a[1:3]))

    def broken(self):
        raise RuntimeError("thread refused")
    monkeypatch.setattr(gui.QThread, "start", broken)
    _pretest_ready(window, monkeypatch)
    tabs_before = window.tabs.isEnabled()
    _start_pretest(window)  # non deve propagare: da uno slot abbatterebbe PyQt6
    assert shown == [("Pretest YOLO non riuscito", "thread refused")]
    assert window.btn_extract_ai.isEnabled()
    assert window.tabs.isEnabled() == tabs_before
    assert window.analysis_overlay.isHidden() and not window._extraction_busy


# ── B9: missing top view ──────────────────────────────────────────────────

def test_without_a_top_view_the_pretest_asks_for_the_role(window, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: shown.append(a[2]))
    monkeypatch.setattr(gui.QThread, "start", lambda self: shown.append("started"))
    _pretest_ready(window, monkeypatch)
    _start_pretest(window, roles={"top.png": "pinout"})
    assert shown and "Immagine con quote" in shown[0] and "started" not in shown
    assert "Lunghezza corpo" not in shown[0]
    # Negativo: la stessa foto con il ruolo di vista dall'alto parte.
    shown.clear()
    _start_pretest(window, roles={"top.png": "top"})
    assert shown == ["started"]
    window._on_yolo_pretest_finished()


def test_the_extents_are_blamed_only_when_a_photo_did_not_calibrate(window, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: shown.append(a[2]))
    # Foto provata e non calibrata: le quote sono la causa probabile.
    window._on_yolo_pretest_done(InventoryResult(warnings=["Nessuna vista calibrabile"]))
    assert "Lunghezza corpo" in shown[-1]
    # Negativo: foto calibrata, YOLO non ha trovato nulla -- le quote non c'entrano.
    window._on_yolo_pretest_done(InventoryResult(warnings=["Nessun riquadro"], photo_png=b"png"))
    assert "Lunghezza corpo" not in shown[-1]


# ── B10: project YOLO settings, key by key ────────────────────────────────

def test_one_bad_yolo_setting_does_not_reset_the_others(window):
    window._apply_project_state({"yolo_settings": {"confidence": 0.46, "nms_iou": 7,
                                                   "tile_sizes": [640], "bogus": 1}})
    chosen = window._chosen_yolo_settings()
    assert chosen["confidence"] == pytest.approx(0.46) and chosen["tile_sizes"] == [640]
    assert chosen["nms_iou"] == gui.validate_yolo_settings({})["nms_iou"]
    log = window._log_plain_text()
    assert "yolo_settings.nms_iou" in log and "yolo_settings.bogus" in log
    assert "yolo_settings.confidence" not in log
    # Negativo: un valore che non e' un oggetto non passa piu' in silenzio.
    window._apply_project_state({"yolo_settings": [0.3]})
    assert "yolo_settings (list)" in window._log_plain_text()
    assert window._chosen_yolo_settings() == gui.validate_yolo_settings({})


def test_opening_a_project_with_a_bad_setting_does_not_rewrite_it(window):
    state = dict(window._project_state(), yolo_settings={"confidence": 0.46, "nms_iou": 7})
    window._switch_project(window._component_name(), state)
    # Lo stato applicato vale come gia' salvato: l'autosalvataggio non scrive.
    assert window._project_state_text() == window._last_saved_project_state
