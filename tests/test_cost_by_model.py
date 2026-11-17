"""@brief The AI cost split by model: the run tally and the cards that show it (user, 2026-11-14).

The user asked, on the finished card of the generation, for «un resoconto del
costo suddiviso per modello LLM usato» (the cost split by the LLM model used).
Positive and negative cases differ for a structural reason: the finished card
of an automatic generation follows a paid analysis of the same run and shows
it; a generation started by hand or a STEP regeneration made no call and shows
nothing; a new analysis forgets the previous one's split.
"""
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.services import openrouter
from test_automatic_generation import acquired, begin
from test_photo_inventory_gui import gui  # noqa: F401 - the window fixture's module


@pytest.fixture(autouse=True)
def clean_totals():
    """@brief Every test starts and ends with empty run totals."""
    openrouter.reset_usage()
    yield
    openrouter.reset_usage()


def _bill(cost, model):
    """@brief One paid call of `model` as the provider answers it (None: no cost reported)."""
    usage = {"prompt_tokens": 10, "completion_tokens": 0}
    if cost is not None:
        usage["cost"] = cost
    openrouter._record_usage({"usage": usage}, model=model)


# ── the run tally ────────────────────────────────────────────────────────────

def test_each_model_gets_its_cost_and_calls_the_most_expensive_first():
    _bill(0.02, "deepseek/deepseek-flash")
    _bill(0.10, "openai/gpt-5.6-sol")
    _bill(0.02, "openai/gpt-5.6-sol")
    totals = openrouter.usage_totals()
    assert {model: (pytest.approx(cost), calls, missing)
            for model, (cost, calls, missing) in totals.by_model.items()} == {
        "deepseek/deepseek-flash": (0.02, 1, 0), "openai/gpt-5.6-sol": (0.12, 2, 0)}
    lines = totals.model_lines()
    assert len(lines) == 2
    assert lines[0].startswith("openai/gpt-5.6-sol: 0.1200 USD") and "2" in lines[0]
    assert lines[1].startswith("deepseek/deepseek-flash: 0.0200 USD")
    assert sum(cost for cost, _calls, _missing in totals.by_model.values()) == pytest.approx(totals.cost_usd)


def test_a_call_without_cost_makes_its_model_a_minimum_not_a_zero():
    """@brief Negative: a missing cost is never summed as 0 and silently shown as exact."""
    _bill(0.03, "openai/gpt-5.6-sol")
    _bill(None, "openai/gpt-5.6-sol")
    (line,) = openrouter.usage_totals().model_lines()
    assert "almeno 0.0300 USD" in line and "1 senza costo" in line


def test_the_answer_name_is_used_only_without_a_requested_model():
    openrouter._record_usage({"usage": {"cost": 0.01}, "model": "vendor/served-name"}, model="vendor/asked")
    openrouter._record_usage({"usage": {"cost": 0.01}, "model": "vendor/served-name"})
    assert set(openrouter.usage_totals().by_model) == {"vendor/asked", "vendor/served-name"}


def test_a_snapshot_does_not_move_with_later_calls_and_a_reset_empties_it():
    _bill(0.01, "m/a")
    snapshot = openrouter.usage_totals()
    _bill(0.01, "m/a")
    assert snapshot.by_model == {"m/a": [0.01, 1, 0]}
    openrouter.reset_usage()
    assert openrouter.usage_totals().by_model == {} and openrouter.usage_totals().model_lines() == []


# ── the cards ────────────────────────────────────────────────────────────────

def _finish_automatic_generation(window, monkeypatch):
    """@brief Run an extraction followed by its automatic generation up to the finished card."""
    begin(window, monkeypatch)
    # The worker's calls are made inside the run's usage scope (review
    # 2026-11-16, R6-8: `ExtractWorker` runs its job in `usage_scope.active()`).
    with window._usage_scope.active():
        _bill(0.10, "openai/gpt-5.6-sol")
        _bill(0.02, "deepseek/deepseek-flash")
    window._on_extracted(acquired('castellated'))
    window._on_extract_finished()
    worker = window._worker
    overlay = window.analysis_overlay
    # While the generation runs there is still no cost on its card (decision of 2026-09-23).
    assert overlay.cost.isHidden() and overlay.cost_models.isHidden()
    monkeypatch.setattr(window, '_draw_preview', lambda: None)
    monkeypatch.setattr(window, '_draw_preview_3d', lambda: None)
    monkeypatch.setattr(window, '_show_generated_mesh', lambda p: None)
    worker.done.emit('footprint.kicad_mod')
    worker.cad3d_done.emit('model.step')
    worker.finished.emit()
    return overlay


def test_the_finished_card_of_an_automatic_run_shows_the_analysis_cost_by_model(window, monkeypatch):  # noqa: F811
    overlay = _finish_automatic_generation(window, monkeypatch)
    assert overlay.is_holding_completed()
    assert "0.1200" in overlay.cost.text() and not overlay.cost.isHidden()
    lines = overlay.cost_models.text().split("\n")
    assert [line.split(":")[0] for line in lines] == ["openai/gpt-5.6-sol", "deepseek/deepseek-flash"]
    assert not overlay.cost_models.isHidden()
    # The AI log keeps the same split under the total.
    assert "openai/gpt-5.6-sol: 0.1000 USD" in window.ai_progress_text.toPlainText()
    overlay.close_button.click()


def test_a_card_finished_without_an_analysis_shows_no_cost(window):  # noqa: F811
    """@brief Negative: a STEP regeneration (no call) holds its card with no cost, even after a past analysis."""
    window._last_analysis_cost = ("Costo ultima analisi: 0.1200 USD", ["openai/gpt-5.6-sol: 0.1200 USD (1)"])
    overlay = window.analysis_overlay
    overlay.start("Generazione modello STEP", abortable=True)
    overlay.set_steps(["Modelli STEP dalla libreria KiCad", "Solido 3D con FreeCAD"])
    window._hold_completed_overlay("Completato: modello STEP rigenerato.")
    assert overlay.is_holding_completed()
    assert overlay.cost.isHidden() and overlay.cost_models.isHidden()
    overlay.close_button.click()


@pytest.mark.parametrize("title", ["Analisi AI", "Rilancio inventario", "Rilettura parziale"])
def test_every_ai_run_starts_its_own_tally_and_forgets_the_previous_split(window, title):  # noqa: F811
    """@brief Review 2026-11-14, finding 1: a re-read or an inventory relaunch after a full analysis.

    The tally was reset only inside the full extraction: a partial re-read's summary added
    the previous analysis and named a model the re-read never called.
    """
    _bill(0.10, "openai/gpt-5.6-sol")
    window._last_analysis_cost = ("old", ["openai/gpt-5.6-sol: 0.1000 USD (1)"])
    window.analysis_overlay.set_cost_models(["openai/gpt-5.6-sol: 0.1000 USD (1)"])
    window._begin_ai_run(title)
    try:
        # CONTRACT CHANGED 2026-11-16 (review R6-8): the run's tally is its own
        # usage scope, which starts empty; the process-wide tally is no longer
        # reset (that zeroed the costs of another paid run still going).
        scope = window._usage_scope
        assert scope.totals().by_model == {} and scope.totals().calls == 0
        assert openrouter.usage_totals().calls == 1, "another run's calls are kept"
        assert window._last_analysis_cost is None
        assert window.analysis_overlay.cost_models.isHidden()
        with scope.active():                      # the worker's calls
            _bill(0.01, "deepseek/deepseek-flash")
        assert [line.split(":")[0] for line in window._run_usage_totals().model_lines()] == [
            "deepseek/deepseek-flash"]
    finally:
        window._end_extraction_busy()


def test_a_held_step_regeneration_card_stays_without_cost_after_a_late_write(window, tmp_path):  # noqa: F811
    """@brief Negative of the held-card refresh: a card held without cost never gains one.

    CONTRACT CHANGED 2026-11-14 (review 5): the held card of an automatic run now
    follows late writes (see the test below); a STEP regeneration made no call.
    """
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    window.show()
    start_run(folder)
    add_call(folder, {"cost_usd": 0.10, "model": "openai/gpt-5.6-sol"})
    overlay = window.analysis_overlay
    overlay.start("Generazione modello STEP", abortable=True)
    overlay.set_steps(["Modelli STEP dalla libreria KiCad", "Solido 3D con FreeCAD"])
    window._hold_completed_overlay("Completato: modello STEP rigenerato.")
    try:
        add_call(folder, {"cost_usd": 0.01, "model": "deepseek/deepseek-flash"})
        window._on_costs_changed()
        assert overlay.cost.isHidden() and overlay.cost_models.isHidden()
    finally:
        overlay.close_button.click()


def test_an_unreadable_cost_file_never_swaps_the_whole_analysis_for_the_last_run(window, tmp_path,  # noqa: F811
                                                                                  monkeypatch):
    """@brief Review 2026-11-14, 2: a refresh that cannot read the file keeps the file's figure."""
    from kicadgen.services import project_costs
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    window.show()
    start_run(folder)
    add_call(folder, {"cost_usd": 0.30, "model": "openai/gpt-5.6-sol"})
    window._last_analysis_cost = ("Costo ultima analisi: 0.0200 USD", ["deepseek/deepseek-flash: 0.0200 USD (1)"])
    overlay = _hold_generation_card(window)
    try:
        assert "0.3000" in overlay.cost.text()

        def busy(*_args, **_kwargs):
            raise OSError("busy")
        monkeypatch.setattr(project_costs, "load_project_costs", busy)
        window._on_costs_changed()
        assert "0.3000" in overlay.cost.text() and "deepseek" not in overlay.cost_models.text()
    finally:
        overlay.close_button.click()


def test_a_start_still_queued_shows_the_run_tally(window, tmp_path, monkeypatch):  # noqa: F811
    """@brief The file's current analysis is the previous one while the new start waits to be written."""
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.30, "model": "openai/gpt-5.6-sol"})
    monkeypatch.setattr(window.ai_account, "queued_for", lambda _folder: (True, 1))
    window._last_analysis_cost = ("Costo ultima analisi: 0.0500 USD", ["deepseek/deepseek-flash: 0.0500 USD (1)"])
    overlay = _hold_generation_card(window)
    try:
        assert overlay.cost.text() == "Costo ultima analisi: 0.0500 USD"
        assert "gpt" not in overlay.cost_models.text()
    finally:
        overlay.close_button.click()


def test_calls_waiting_for_the_file_get_a_line_of_their_own(window, tmp_path, monkeypatch):  # noqa: F811
    """@brief Review 2026-11-14, 4: the model lines say they leave out the calls not yet written."""
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.10, "model": "openai/gpt-5.6-sol"})
    monkeypatch.setattr(window.ai_account, "queued_for", lambda _folder: (False, 2))
    overlay = _hold_generation_card(window)
    try:
        lines = overlay.cost_models.text().splitlines()
        assert lines[0].startswith("openai/gpt-5.6-sol") and lines[-1].startswith("+2 ")
    finally:
        overlay.close_button.click()


def test_no_waiting_line_when_every_call_is_written(window, tmp_path):  # noqa: F811
    """@brief Negative twin: nothing queued, no «in attesa» line.

    @note One model only (user, 2026-11-15): its line would repeat the total,
        so the model is named in the total and the model row stays hidden.
    """
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.10, "model": "openai/gpt-5.6-sol"})
    overlay = _hold_generation_card(window)
    try:
        assert overlay.cost.text() == "Costo AI dell'analisi: 0.1000 USD (1 chiamata · openai/gpt-5.6-sol)"
        assert overlay.cost_models.isHidden() and "attesa" not in overlay.cost_models.text()
    finally:
        overlay.close_button.click()


def test_the_running_analysis_card_still_follows_the_cost_file(window, monkeypatch):  # noqa: F811
    """@brief Negative: outside a held card the live cost line is still refreshed."""
    overlay = window.analysis_overlay
    # The live refresh runs only for a card on screen.
    window.show()
    overlay.start("Analisi AI in corso", "in corso…")
    monkeypatch.setattr(window, "_refresh_status_bar",
                        lambda _named: window.status_cost.setText("Analisi: 0.0500 USD"))
    window._on_costs_changed()
    assert overlay.cost.text() == "Analisi: 0.0500 USD"
    overlay.stop()


# ── the whole analysis on the finished card (user decision, 2026-11-14) ─────

def _named_project(window, tmp_path, name="COSTO"):
    """@brief Open a named project in `tmp_path` and return its folder."""
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText(name)
    return window._project_dir()


def _hold_generation_card(window, **kwargs):
    """@brief Hold the finished card of an automatic generation."""
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    overlay.set_steps(["Footprint KiCadGen", "Corpo e terminali FreeCAD"])
    window._hold_completed_overlay("Completato: footprint e modello STEP generati.", analysis_cost=True, **kwargs)
    return overlay


def test_the_finished_card_counts_the_reruns_since_the_last_full_analysis(window, tmp_path):  # noqa: F811
    """@brief «dall'inizio del processo di analisi»: the full analysis AND the re-read after it."""
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    start_run(folder)                                            # «Estrai parametri con AI»
    add_call(folder, {"cost_usd": 0.30, "model": "openai/gpt-5.6-sol"})
    add_call(folder, {"cost_usd": 0.02, "model": "deepseek/deepseek-flash"})   # the re-read
    # The run tally knows only the re-read: the card must not show that alone.
    window._last_analysis_cost = ("Costo ultima analisi: 0.0200 USD", ["deepseek/deepseek-flash: 0.0200 USD (1)"])
    overlay = _hold_generation_card(window)
    try:
        assert "0.3200" in overlay.cost.text() and "2" in overlay.cost.text()
        lines = overlay.cost_models.text().splitlines()
        assert [line.split(":")[0] for line in lines] == ["openai/gpt-5.6-sol", "deepseek/deepseek-flash"]
        assert "0.3000" in lines[0] and "0.0200" in lines[1]
    finally:
        overlay.close_button.click()


def test_a_new_full_analysis_starts_the_card_count_again(window, tmp_path):  # noqa: F811
    """@brief Negative: the calls of the previous «Estrai parametri» are not this analysis."""
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.30, "model": "openai/gpt-5.6-sol"})
    start_run(folder)
    add_call(folder, {"cost_usd": 0.05, "model": "deepseek/deepseek-flash"})
    overlay = _hold_generation_card(window)
    try:
        assert "0.0500" in overlay.cost.text() and "0.30" not in overlay.cost.text()
        # Only the new analysis' model is left: named in the total, no line.
        assert overlay.cost.text().endswith("· deepseek/deepseek-flash)")
        assert "gpt" not in overlay.cost.text() and overlay.cost_models.isHidden()
    finally:
        overlay.close_button.click()


def test_the_held_card_follows_a_late_write_with_total_and_lines_together(window, tmp_path):  # noqa: F811
    """@brief A call written after the card was held (busy file) moves total and model lines together."""
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    window.show()
    start_run(folder)
    add_call(folder, {"cost_usd": 0.10, "model": "openai/gpt-5.6-sol"})
    overlay = _hold_generation_card(window)
    try:
        add_call(folder, {"cost_usd": 0.01, "model": "deepseek/deepseek-flash"})
        window._on_costs_changed()
        assert "0.1100" in overlay.cost.text()
        assert "deepseek/deepseek-flash" in overlay.cost_models.text()
    finally:
        overlay.close_button.click()


def test_a_partial_reread_adds_to_the_current_analysis(window, monkeypatch):  # noqa: F811
    """@brief The re-read no longer opens a new analysis in the cost file (2026-11-05 regression)."""
    starts, begun = [], []
    monkeypatch.setattr(window.ai_account, "start_analysis", lambda folder: starts.append(folder))
    monkeypatch.setattr(window, "_reread_block_reason", lambda _field: "")
    monkeypatch.setattr(window, "_ensure_api_key", lambda *_a, **_k: True)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([], {}))

    class Stop(Exception):
        pass

    def begin(title):
        begun.append(title)
        raise Stop
    monkeypatch.setattr(window, "_begin_ai_run", begin)
    with pytest.raises(Stop):
        window._start_reread(next(iter(window.REREAD_FIELDS)))
    assert begun and starts == []


# The positive twin, «Estrai parametri con AI» opening a new analysis, is
# test_analysis_cost_rerun.py::test_the_extraction_opens_a_new_analysis.


def test_the_held_outcome_and_its_cost_follow_a_language_switch(window, tmp_path):  # noqa: F811
    """@brief Review 2026-11-14, 1: the marked outcome is composed again, not looked up as a key."""
    from kicadgen.services.project_costs import add_call, start_run
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.10, "model": "openai/gpt-5.6-sol"})
    window.show()
    overlay = _hold_generation_card(window)
    try:
        assert overlay.status.text() == "✓ " + window.i18n.text("Completato: footprint e modello STEP generati.")
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        assert overlay.status.text().startswith("✓ ") and "Completato" not in overlay.status.text()
        assert overlay.status.text() == "✓ " + window.i18n.text("Completato: footprint e modello STEP generati.")
        assert "chiamat" not in overlay.cost_models.text() and "chiamat" not in overlay.cost.text()
    finally:
        window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
        overlay.close_button.click()


# ── the highlighted outcome ─────────────────────────────────────────────────

def test_the_outcome_stands_out_green_and_a_partial_one_amber(window):  # noqa: F811
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    overlay.set_steps(["Footprint KiCadGen"])
    running = overlay.status.font().pointSizeF()
    overlay.show_completed("Completato: footprint generato.")
    assert overlay.status.text().startswith("✓ ") and overlay.status.property("outcome") == "done"
    assert overlay.status.font().bold() and overlay.status.font().pointSizeF() > running
    # It fits: the outcome never needs more rows than the status reserves.
    assert overlay.status.heightForWidth(overlay._status_text_width()) <= overlay.status.minimumHeight()
    overlay.close_button.click()
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    # Negative: the next operation's running lines are plain again.
    assert not overlay.status.property("outcome") and not overlay.status.font().bold()
    assert overlay.status.font().pointSizeF() == running and not overlay.status.text().startswith("✓")
    overlay.set_steps(["Footprint KiCadGen"])
    overlay.show_completed("Footprint generato; modello STEP da completare.", partial=True)
    assert overlay.status.text().startswith("⚠ ") and overlay.status.property("outcome") == "partial"
    overlay.close_button.click()


def test_the_outcome_grows_under_the_application_style_sheet(qapp):
    """@brief The theme fixes every widget's font size: the larger outcome must still apply, then go away.

    A font set in code was ignored under theme.APP_QSS (found on the real window, 2026-11-14);
    the next operation measures its rows with the running size again.
    """
    from PyQt6.QtWidgets import QWidget
    from kicadgen.gui.analysis_overlay import AnalysisOverlay
    from kicadgen.gui.theme import APP_QSS
    from test_analysis_overlay import WORST_CASE_STATUS
    parent = QWidget()
    parent.setStyleSheet(APP_QSS)
    parent.resize(1000, 760)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        overlay.start("Generazione footprint e modello STEP", abortable=True)
        qapp.processEvents()
        running = overlay.status.font().pointSizeF()
        overlay.set_steps(["Footprint KiCadGen"])
        width = overlay.card.width()
        overlay.show_completed("Completato: footprint generato.")
        qapp.processEvents()
        assert overlay.status.font().pointSizeF() == pytest.approx(running * overlay.OUTCOME_SCALE, abs=0.1)
        assert overlay.status.font().bold()
        # No jump: a resize of the held card still measures the running lines.
        overlay._size_the_card()
        qapp.processEvents()
        assert overlay.card.width() == width
        overlay.close_button.click()
        overlay.start("Analisi AI in corso", "in corso…")
        qapp.processEvents()
        assert overlay.status.font().pointSizeF() == pytest.approx(running)
        assert not overlay.status.font().bold()
        overlay.set_status(WORST_CASE_STATUS)
        qapp.processEvents()
        assert overlay.status.heightForWidth(overlay._status_text_width()) <= overlay.status.minimumHeight()
    finally:
        overlay.stop()
        parent.close()
        parent.deleteLater()


def test_a_window_shrunk_after_the_outcome_gives_up_the_larger_size(qapp):
    """@brief Review 2026-11-14, 3: the fit is decided again when the held card narrows."""
    from PyQt6.QtWidgets import QWidget
    from kicadgen.gui.analysis_overlay import AnalysisOverlay
    from kicadgen.gui.theme import APP_QSS
    parent = QWidget()
    parent.setStyleSheet(APP_QSS)
    parent.resize(1000, 760)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        overlay.start("Generazione footprint e modello STEP", abortable=True)
        qapp.processEvents()
        running = overlay.status.font().pointSizeF()
        overlay.set_steps(["Footprint KiCadGen"])
        overlay.show_completed("Footprint generato; modello STEP rinviato: mancano le quote del corpo "
                               "(lunghezza, larghezza, altezza, distanza dal piano).", partial=True)
        qapp.processEvents()
        assert overlay.status.font().pointSizeF() > running
        parent.resize(400, 760)
        qapp.processEvents()
        assert overlay.status.heightForWidth(overlay._status_text_width()) <= overlay.status.minimumHeight()
        assert overlay.status.font().bold()
    finally:
        overlay.stop()
        parent.close()
        parent.deleteLater()


@pytest.mark.parametrize("point_size", [7, 16, 24])
def test_a_highlighted_outcome_is_never_cut_in_a_small_window(qapp, point_size):
    """@brief «badando che ci stiano»: the larger size gives way when it would need more rows."""
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QWidget
    from kicadgen.gui.analysis_overlay import AnalysisOverlay
    parent = QWidget()
    font = QFont(qapp.font())
    font.setPointSize(point_size)
    parent.setFont(font)
    parent.resize(640, 480)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        overlay.start("Generazione footprint e modello STEP", abortable=True)
        overlay.set_steps(["Footprint KiCadGen"])
        overlay.show_completed("Footprint generato; modello STEP rinviato: mancano le quote del corpo "
                               "(lunghezza, larghezza, altezza).", partial=True)
        qapp.processEvents()
        assert overlay.status.font().bold()
        assert overlay.status.heightForWidth(overlay._status_text_width()) <= overlay.status.minimumHeight()
    finally:
        overlay.stop()
        parent.close()
        parent.deleteLater()
