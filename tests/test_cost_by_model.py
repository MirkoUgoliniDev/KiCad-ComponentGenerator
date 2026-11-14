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
        assert openrouter.usage_totals().by_model == {} and openrouter.usage_totals().calls == 0
        assert window._last_analysis_cost is None
        assert window.analysis_overlay.cost_models.isHidden()
        _bill(0.01, "deepseek/deepseek-flash")
        assert [line.split(":")[0] for line in openrouter.usage_totals().model_lines()] == [
            "deepseek/deepseek-flash"]
    finally:
        window._end_extraction_busy()


def test_a_late_cost_file_write_leaves_the_held_card_alone(window):  # noqa: F811
    """@brief Review 2026-11-14, 3: the finished card keeps the analysis total next to its model lines."""
    overlay = window.analysis_overlay
    # The live refresh runs only for a card on screen.
    window.show()
    window._last_analysis_cost = ("Costo ultima analisi: 0.1200 USD", ["openai/gpt-5.6-sol: 0.1200 USD (1)"])
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    overlay.set_steps(["Footprint KiCadGen", "Corpo e terminali FreeCAD"])
    window._hold_completed_overlay("Completato: footprint generato.", analysis_cost=True)
    window.status_cost.setText("Analisi: 9.9999 USD")
    window._on_costs_changed()
    assert overlay.cost.text() == "Costo ultima analisi: 0.1200 USD"
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
