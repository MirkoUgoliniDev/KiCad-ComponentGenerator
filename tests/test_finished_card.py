"""@brief The finished card of a generation: finish flag, costs, total processing time (user, 2026-11-14).

«in alto un'icona a scacchi tipo traguardo, poi i passi compiuti e sotto la
lista dei costi AI completi per ogni LLM, il totale costi ed il tempo totale
elaborazione». User decision: the total time covers the whole analysis, the
same arc as its cost (from the last «Estrai parametri con AI», re-reads
included), and leaves out the time the program waited for the user. Only the
program's own questions are waits (`waiting_for_user`): a window the user opens
from the menu while the work runs is not (final review 2026-11-14).
"""
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.gui import analysis_overlay as overlay_module  # noqa: E402
from test_photo_inventory_gui import gui  # noqa: E402,F401 - the window fixture's module


class Clock:
    """A hand-driven monotonic clock for the card."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(overlay_module, "monotonic", fake)
    return fake


def _layout_index(overlay, widget):
    return overlay.card.layout().indexOf(widget)


def _ai_run(window, overlay, clock, seconds):
    """One AI run of the analysis on the card, marked as `_begin_ai_run` marks it."""
    window._next_operation_counts = True
    overlay.start("Analisi AI in corso")
    clock.now += seconds
    overlay.stop()


def _automatic_generation(window, overlay, clock, seconds):
    """The automatic generation on the card, marked as `_generate_applied` marks it."""
    window._next_operation_counts = True
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    overlay.set_steps(["Footprint KiCadGen", "Corpo e terminali FreeCAD"])
    clock.now += seconds


# ── layout ───────────────────────────────────────────────────────────────────

def test_the_finished_card_shows_the_flag_and_the_next_operation_the_ring(window):  # noqa: F811
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    assert not overlay.spinner.finished
    overlay.set_steps(["Footprint KiCadGen"])
    overlay.show_completed("Completato: footprint generato.")
    assert overlay.spinner.finished
    overlay.close_button.click()
    overlay.start("Analisi AI in corso")
    # Negative: a running operation never shows the finish flag.
    assert not overlay.spinner.finished
    overlay.stop()


def test_model_lines_come_before_the_total_and_the_time_last(window):  # noqa: F811
    overlay = window.analysis_overlay
    assert (_layout_index(overlay, overlay.status) < _layout_index(overlay, overlay.cost_models)
            < _layout_index(overlay, overlay.cost) < _layout_index(overlay, overlay.elapsed))


def test_long_analyses_show_hours():
    assert overlay_module._clock(62) == "01:02"
    assert overlay_module._clock(3600 + 125) == "1:02:05"


# ── the real path ────────────────────────────────────────────────────────────

def test_the_real_extraction_and_its_generation_add_up_without_the_question(window, monkeypatch, clock):  # noqa: F811
    """@brief «Estrai parametri» 30 s with a 60 s question inside, then its generation 12 s: 00:42.

    Driven through the window's own path: `_extract_with_ai`, `_on_extracted`,
    `_on_extract_finished` (which stops the analysis card and starts the
    automatic generation), the worker's signals, the held card.
    """
    from test_automatic_generation import acquired, begin
    begin(window, monkeypatch)
    clock.now += 10
    # A question of the program during the analysis: not processing time.
    with window.analysis_overlay.waiting_for_user():
        clock.now += 60
    clock.now += 20
    window._on_extracted(acquired('castellated'))
    window._on_extract_finished()
    worker = window._worker
    clock.now += 12
    monkeypatch.setattr(window, '_draw_preview', lambda: None)
    monkeypatch.setattr(window, '_draw_preview_3d', lambda: None)
    monkeypatch.setattr(window, '_show_generated_mesh', lambda p: None)
    worker.done.emit('footprint.kicad_mod')
    worker.cad3d_done.emit('model.step')
    worker.finished.emit()
    overlay = window.analysis_overlay
    try:
        assert overlay.is_holding_completed() and overlay.spinner.finished
        assert overlay.elapsed.text() == window.i18n.tr("analysis.elapsed_total", time="00:42")
    finally:
        overlay.close_button.click()


def test_a_window_opened_from_the_menu_is_not_a_wait(window, clock, monkeypatch):  # noqa: F811
    """@brief Final review 1, negative: «Account AI» open for 60 s while the AI works -- the time counts."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QDialog
    overlay = window.analysis_overlay
    overlay.start("Analisi AI in corso")
    dialog = QDialog(window)
    dialog.setModal(True)
    seen = []

    def inside():
        # A real modal window is up, as «Account AI» would be.
        seen.append(QApplication.activeModalWidget() is dialog)
        clock.now += 60
        overlay._tick()
        dialog.reject()
    QTimer.singleShot(0, inside)
    dialog.exec()
    dialog.deleteLater()
    assert seen == [True]
    assert overlay.work_seconds() == pytest.approx(60)
    overlay.stop()


def test_a_program_question_is_cut_from_its_opening_to_its_answer(window, clock):  # noqa: F811
    """@brief Final review 2: the cut starts when the question opens, not at the last clock tick."""
    overlay = window.analysis_overlay
    overlay.start("Analisi AI in corso")
    overlay._tick()
    clock.now += 3          # work on the GUI thread after the last tick
    with overlay.waiting_for_user():
        clock.now += 40
        assert overlay.work_seconds() == pytest.approx(3)
        with overlay.waiting_for_user():   # a nested question counts once
            clock.now += 5
    clock.now += 2
    assert overlay.work_seconds() == pytest.approx(5)
    overlay.stop()


def test_the_total_time_adds_every_operation_of_the_analysis(window, clock):  # noqa: F811
    """@brief Extraction 30 s + re-read 20 s + automatic generation 12 s = 01:02."""
    overlay = window.analysis_overlay
    window._analysis_work_seconds = 0.0
    window._analysis_time_partial = False
    _ai_run(window, overlay, clock, 30)
    _ai_run(window, overlay, clock, 20)
    _automatic_generation(window, overlay, clock, 12)
    window._hold_completed_overlay("Completato: footprint e modello STEP generati.", analysis_cost=True)
    assert overlay.elapsed.text() == window.i18n.tr("analysis.elapsed_total", time="01:02")
    overlay.close_button.click()
    # Closing the card does not count the generation twice.
    assert window._analysis_work_seconds == pytest.approx(62)


# ── what does not count ──────────────────────────────────────────────────────

def test_a_card_without_analysis_keeps_its_own_elapsed_time(window, clock):  # noqa: F811
    """@brief Negative: a STEP regeneration shows its own time, not the analysis total, and adds nothing."""
    overlay = window.analysis_overlay
    window._analysis_work_seconds = 500.0
    overlay.start("Generazione modello STEP", abortable=True)
    overlay.set_steps(["Solido 3D con FreeCAD"])
    clock.now += 7
    overlay._tick()
    window._hold_completed_overlay("Completato: modello STEP rigenerato.")
    try:
        assert overlay.elapsed.text() == window.i18n.tr("analysis.elapsed", time="00:07")
        # Review 6: its own time follows a language switch too.
        window.show()
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        assert overlay.elapsed.text() == "Elapsed time  00:07"
    finally:
        window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
        overlay.close_button.click()
    assert window._analysis_work_seconds == pytest.approx(500.0)


def test_the_local_yolo_pretest_is_not_analysis_time(window, clock):  # noqa: F811
    """@brief Final review 3: the free pretest runs with the extraction flag but is no AI run."""
    overlay = window.analysis_overlay
    window._analysis_work_seconds = 0.0
    window._extraction_busy = True
    try:
        overlay.start("Pretest YOLO in corso")
        clock.now += 45
        overlay.stop()
    finally:
        window._extraction_busy = False
    assert window._analysis_work_seconds == pytest.approx(0.0)


def test_a_step_regeneration_in_between_is_not_analysis_time(window, clock):  # noqa: F811
    overlay = window.analysis_overlay
    window._analysis_work_seconds = 0.0
    _ai_run(window, overlay, clock, 30)
    overlay.start("Generazione modello STEP", abortable=True)
    overlay.set_steps(["Solido 3D con FreeCAD"])
    clock.now += 120
    window._hold_completed_overlay("Completato: modello STEP rigenerato.")
    overlay.close_button.click()
    assert window._analysis_work_seconds == pytest.approx(30)


def test_the_live_log_and_the_abort_question_do_not_stop_the_clock(window, clock, monkeypatch):  # noqa: F811
    """@brief Review 1: modal, but the work goes on under them -- no pause."""
    overlay = window.analysis_overlay
    overlay.start("Analisi AI in corso", abortable=True)
    from PyQt6.QtWidgets import QMessageBox

    def question(*_args, **_kwargs):
        clock.now += 20
        overlay._tick()
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", question)
    assert window._confirm_abort("ai", "fase") is False
    assert overlay.work_seconds() == pytest.approx(20)
    overlay.stop()


def test_opening_another_project_forgets_the_analysis_time(window, tmp_path):  # noqa: F811
    """@brief Review 2: project B's card never shows project A's minutes."""
    window._analysis_work_seconds = 240.0
    folder = tmp_path / "ALTRO"
    (folder / "ALTRO.pretty").mkdir(parents=True)
    (folder / "ALTRO.pretty" / "ALTRO.kicad_mod").write_text("(footprint)")
    window._open_project_at(folder, save_current=False)
    assert window._analysis_work_seconds is None


def test_no_analysis_no_count(window):  # noqa: F811
    """@brief Negative: before any analysis, or for an operation outside it, nothing is collected."""
    window._analysis_work_seconds = None
    window._operation_counts = True
    window._on_overlay_work_done(25.0)
    assert window._analysis_work_seconds is None
    window._analysis_work_seconds = 0.0
    window._on_overlay_work_done(25.0)
    assert window._analysis_work_seconds == pytest.approx(25.0)
    window._operation_counts = False
    window._on_overlay_work_done(25.0)
    assert window._analysis_work_seconds == pytest.approx(25.0)


def test_a_reread_without_an_extraction_in_this_session_is_plain_elapsed_time(window, clock):  # noqa: F811
    """@brief Final review 4: re-read 20 s + generation 9 s shown as «Tempo trascorso 00:29», not a total.

    The analysis began in an earlier session, whose time is unknown: the card
    counts this session's work but never calls it the analysis total.
    """
    overlay = window.analysis_overlay
    window._analysis_work_seconds = None
    window._begin_ai_run("Rilettura parziale")
    try:
        assert window._analysis_work_seconds == 0.0 and window._analysis_time_partial
        overlay.start("Rilettura parziale")
        clock.now += 20
        overlay.stop()
    finally:
        window._end_extraction_busy()
    _automatic_generation(window, overlay, clock, 9)
    window._hold_completed_overlay("Completato: footprint generato.", analysis_cost=True)
    try:
        assert overlay.elapsed.text() == window.i18n.tr("analysis.elapsed", time="00:29")
    finally:
        overlay.close_button.click()


def test_the_total_time_follows_a_language_switch(window, clock):  # noqa: F811
    overlay = window.analysis_overlay
    window._analysis_work_seconds = 0.0
    window._analysis_time_partial = False
    _automatic_generation(window, overlay, clock, 75)
    window.show()
    window._hold_completed_overlay("Completato: footprint generato.", analysis_cost=True)
    try:
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        assert overlay.elapsed.text() == "Total processing time  01:15"
    finally:
        window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
        overlay.close_button.click()
