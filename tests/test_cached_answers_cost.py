"""@brief AI answers replayed from a local cache on the cost card (user, 2026-11-15).

The user saw a finished card with only GPT on it and asked «e deepseek?»
(and DeepSeek?): DeepSeek had answered, from the local cache, for free. Two
rules follow, each with a case that must trigger and one that must not:

- one model only: its line would repeat the total, so the model is named in
  the total and has no line; two models, or one model with calls without a
  cost, keep their lines;
- the models that answered from a local cache get a grey line of their own,
  counted over the same analysis as the cost, never added to the total or to
  the calls.

No network: every AI call is a fake `call=`, the cost file lives in tmp_path.
"""
from copy import deepcopy
from unittest.mock import Mock

import numpy as np
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.services import openrouter  # noqa: E402
from kicadgen.services.project_costs import (COSTS_FILE, ProjectCostsError,  # noqa: E402
                                             add_cached_answer, add_call, load_project_costs,
                                             start_run)

GPT = "openai/gpt-5.6-sol"
DEEPSEEK = "deepseek-api/deepseek-flash"


@pytest.fixture(autouse=True)
def fresh_tally():
    """@brief Every test starts with an empty run tally and no listener."""
    openrouter.set_usage_listener(None)
    openrouter.set_cached_answer_listener(None)
    openrouter.reset_usage()
    yield
    openrouter.set_usage_listener(None)
    openrouter.set_cached_answer_listener(None)
    openrouter.reset_usage()


# ── openrouter: the one place every cache reports to ──────────────────────

def test_a_cached_answer_is_counted_apart_and_never_as_a_call():
    paid, cached = [], []
    openrouter.set_usage_listener(paid.append)
    openrouter.set_cached_answer_listener(cached.append)
    openrouter.record_cached_answer(DEEPSEEK)
    openrouter.record_cached_answer(DEEPSEEK)
    totals = openrouter.usage_totals()
    assert totals.cached_by_model == {DEEPSEEK: 2}
    # Not a call: no cost, no call count, no model line, nothing for the paid listener.
    assert totals.calls == 0 and totals.cost_usd == 0 and totals.by_model == {}
    assert paid == [] and [record["model"] for record in cached] == [DEEPSEEK, DEEPSEEK]
    assert all(record["time"].endswith("+00:00") for record in cached)


def test_a_paid_call_never_reaches_the_cached_answer_listener():
    """Negative twin: the two listeners never see each other's records."""
    cached = []
    openrouter.set_cached_answer_listener(cached.append)
    openrouter._record_usage({"model": GPT, "usage": {"cost": 0.1}}, model=GPT)
    assert cached == [] and openrouter.usage_totals().cached_by_model == {}


def test_a_listener_that_raises_never_breaks_the_analysis():
    openrouter.set_cached_answer_listener(Mock(side_effect=RuntimeError("boom")))
    openrouter.record_cached_answer(DEEPSEEK)
    assert openrouter.usage_totals().cached_by_model == {DEEPSEEK: 1}


def test_cached_lines_say_singular_and_plural_and_list_the_busiest_model_first():
    assert openrouter.format_cached_lines({GPT: 1, DEEPSEEK: 3}) == [
        f"{DEEPSEEK}: 3 letture dalla cache (gratis)", f"{GPT}: 1 lettura dalla cache (gratis)"]
    # Nothing replayed, or damaged counts: no line at all.
    assert openrouter.format_cached_lines({}) == []
    assert openrouter.format_cached_lines(None) == []
    assert openrouter.format_cached_lines({GPT: 0, DEEPSEEK: True}) == []


def test_single_model_only_when_one_model_has_every_cost():
    assert openrouter.single_model({GPT: (0.4, 2, 0)}) == GPT
    # Two models: each line says something the total does not.
    assert openrouter.single_model({GPT: (0.4, 2, 0), DEEPSEEK: (0.01, 1, 0)}) is None
    # One model with a call without cost: «at least ... without cost» is information.
    assert openrouter.single_model({GPT: (0.4, 2, 1)}) is None
    assert openrouter.single_model({}) is None and openrouter.single_model(None) is None


# ── the three caches report their replays ────────────────────────────────

def test_the_photo_cache_reports_a_replay_and_not_a_fresh_answer(tmp_path):
    from kicadgen.services import photo_grounding as grounding
    from kicadgen.services import photo_inventory as inventory
    from test_photo_grounding import KINDS, native
    photo = np.zeros((40, 60, 3), np.uint8)
    kwargs = dict(model=DEEPSEEK, system_prompt="locate", max_tokens=8192, has_pdf=False,
                  tool=grounding.grounding_tool(KINDS),
                  content_blocks=[inventory.image_block(photo)])
    call = Mock(return_value={"components": [native()]})
    grounding.cached_call(call, kwargs, tmp_path, "stage", lambda _m: None)
    assert call.call_count == 1 and openrouter.usage_totals().cached_by_model == {}
    grounding.cached_call(call, kwargs, tmp_path, "stage", lambda _m: None)
    assert call.call_count == 1
    assert openrouter.usage_totals().cached_by_model == {DEEPSEEK: 1}


def test_the_crop_cache_reports_each_replayed_reading(tmp_path):
    from test_photo_crop_reading import Script, _obs, _read, _row
    _read([_row()], Script(_obs(), _obs()), tmp_path)
    assert openrouter.usage_totals().cached_by_model == {}
    summary, _ = _read([_row()], lambda **_kw: pytest.fail("paid again"), tmp_path)
    assert summary.readings_cached == 2
    assert openrouter.usage_totals().cached_by_model == {"deepseek-api/deepseek-flash": 2}


def test_the_global_inventory_cache_reports_a_replay(tmp_path):
    from test_photo_global_inventory import candidate, run
    from test_detection_contradictions import ITEMS, GROUP, answer, body, decision
    data = answer([body('group', [1, 2], bbox=GROUP), body('r3', [3], bbox=[.3, .3, .4, .4])],
                  [decision(1, 'group'), decision(2, 'group'), decision(3, 'r3', 'keep')])
    _result, calls, _logs = run(data, deepcopy(ITEMS), cache=tmp_path)
    assert calls and openrouter.usage_totals().cached_by_model == {}
    _result, calls, _logs = run(data, deepcopy(ITEMS), cache=tmp_path)
    assert calls == []
    assert openrouter.usage_totals().cached_by_model == {"unchanged/model": 1}


# ── the project cost file ─────────────────────────────────────────────────

def test_one_item_per_analysis_and_model_however_many_answers(tmp_path):
    """Review M2: the file grows by models, not by answers."""
    import json
    start_run(tmp_path)
    add_cached_answer(tmp_path, {"model": DEEPSEEK})
    add_cached_answer(tmp_path, {"model": DEEPSEEK, "count": 149})
    add_cached_answer(tmp_path, {"model": GPT, "count": 2})
    items = json.loads((tmp_path / COSTS_FILE).read_text(encoding="utf-8"))["cached_answers"]
    assert sorted((item["model"], item["count"]) for item in items) == [(DEEPSEEK, 150), (GPT, 2)]
    assert load_project_costs(tmp_path).run_cached_by_model == {DEEPSEEK: 150, GPT: 2}
    # A new analysis opens its own item for the same model (negative twin).
    start_run(tmp_path)
    add_cached_answer(tmp_path, {"model": DEEPSEEK})
    items = json.loads((tmp_path / COSTS_FILE).read_text(encoding="utf-8"))["cached_answers"]
    assert len(items) == 3 and load_project_costs(tmp_path).run_cached_by_model == {DEEPSEEK: 1}
    # A zero or damaged count notes nothing.
    add_cached_answer(tmp_path, {"model": DEEPSEEK, "count": 0})
    add_cached_answer(tmp_path, {"model": DEEPSEEK, "count": "3"})
    assert load_project_costs(tmp_path).run_cached_by_model == {DEEPSEEK: 1}


def test_cached_answers_join_the_current_analysis_without_touching_the_total(tmp_path):
    start_run(tmp_path)
    add_call(tmp_path, {"cost_usd": 0.20, "model": GPT})
    add_cached_answer(tmp_path, {"model": DEEPSEEK})
    costs = add_cached_answer(tmp_path, {"model": DEEPSEEK})
    assert costs.run_cached_by_model == {DEEPSEEK: 2}
    assert (costs.total_usd, costs.calls, costs.run_calls) == (0.20, 1, 1)
    assert costs.run_by_model == {GPT: (0.20, 1, 0)}
    assert load_project_costs(tmp_path).run_cached_by_model == {DEEPSEEK: 2}


def test_a_new_analysis_starts_the_cached_count_again(tmp_path):
    """Negative twin: the replays of the previous «Estrai parametri» are not this analysis."""
    start_run(tmp_path)
    add_cached_answer(tmp_path, {"model": DEEPSEEK})
    start_run(tmp_path)
    assert load_project_costs(tmp_path).run_cached_by_model == {}
    # A replay stamped with the old analysis (written late) stays in the old one.
    add_cached_answer(tmp_path, {"model": GPT, "run": 1})
    assert load_project_costs(tmp_path).run_cached_by_model == {}
    assert load_project_costs(tmp_path, run=1).run_cached_by_model == {DEEPSEEK: 1, GPT: 1}


def test_no_analysis_no_note(tmp_path):
    """Without an analysis in the file there is nothing to show a replay with."""
    costs = add_cached_answer(tmp_path, {"model": DEEPSEEK})
    assert costs.run_cached_by_model == {} and not (tmp_path / COSTS_FILE).exists()


def test_a_replay_for_a_vanished_project_creates_no_folder(tmp_path):
    """Second review: no file means no analysis, and no lock that would recreate the folder."""
    gone = tmp_path / "moved-away"
    costs = add_cached_answer(gone, {"model": DEEPSEEK})
    assert costs.run_cached_by_model == {} and not gone.exists()


def test_damaged_cached_answers_keep_the_file_untouched(tmp_path):
    start_run(tmp_path)
    path = tmp_path / COSTS_FILE
    text = path.read_text(encoding="utf-8").rstrip()[:-1] + ', "cached_answers": {}}'
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ProjectCostsError):
        add_cached_answer(tmp_path, {"model": DEEPSEEK})
    assert path.read_text(encoding="utf-8") == text


# ── the AI account line writes them, never as calls ──────────────────────

def test_the_account_line_writes_a_replay_apart_from_the_calls(qapp, tmp_path):
    from kicadgen.gui.ai_account import AiAccountLine
    start_run(tmp_path)
    logs = []
    line = AiAccountLine(lambda: tmp_path, logs.append)
    try:
        for _ in range(150):
            openrouter.record_cached_answer(DEEPSEEK)
        qapp.processEvents()
        # A burst is grouped in the queue and written once (review M2), not now.
        assert [(item["model"], item["count"]) for _f, item in line._pending] == [(DEEPSEEK, 150)]
        assert line._cached_timer.isActive()
        assert load_project_costs(tmp_path).run_cached_by_model == {}
        # Never waiting paid calls, never the status bar's «in attesa».
        assert line._waiting_calls() == 0 and line.queued_for(tmp_path) == (False, 0)
        line._cached_timer.timeout.emit()
        costs = load_project_costs(tmp_path)
        assert costs.run_cached_by_model == {DEEPSEEK: 150}
        assert costs.calls == 0 and line._pending == []
    finally:
        line.detach()
        line.close()
        line.deleteLater()
        qapp.processEvents()
    assert openrouter._cached_answer_listener is None


def test_a_paid_call_writes_the_grouped_replays_with_it(qapp, tmp_path):
    from kicadgen.gui.ai_account import AiAccountLine
    start_run(tmp_path)
    line = AiAccountLine(lambda: tmp_path, lambda _m: None)
    try:
        openrouter.record_cached_answer(DEEPSEEK)
        openrouter._record_usage({"model": "openai/gpt-5.6-sol-20261101", "usage": {"cost": 0.1}},
                                 model=GPT)
        qapp.processEvents()
        costs = load_project_costs(tmp_path)
        assert costs.run_cached_by_model == {DEEPSEEK: 1} and costs.run_calls == 1
        # Review L3: the paid call is recorded under the model the program asked
        # for, not the versioned name the provider answered with.
        assert costs.run_by_model == {GPT: (0.1, 1, 0)}
    finally:
        line.detach()
        line.close()
        line.deleteLater()
        qapp.processEvents()


def test_replays_are_not_grouped_across_a_queued_analysis_start(qapp, tmp_path, monkeypatch):
    """A replay read after a new «Estrai parametri» must not join the analysis before it."""
    from kicadgen.gui import ai_account as account_module
    from kicadgen.gui.ai_account import AiAccountLine
    start_run(tmp_path)
    line = AiAccountLine(lambda: tmp_path, lambda _m: None)
    try:
        openrouter.record_cached_answer(DEEPSEEK)
        qapp.processEvents()

        def busy(*_args):
            raise OSError("file in uso")
        monkeypatch.setattr(account_module, "start_run", busy)
        line.start_analysis(tmp_path)        # writes the first replay, the start stays queued
        line._retry_timer.stop()
        openrouter.record_cached_answer(DEEPSEEK)
        qapp.processEvents()
        kinds = [("start" if item.get("_start_run") else item["count"]) for _f, item in line._pending]
        assert kinds == ["start", 1]
        assert load_project_costs(tmp_path).run_cached_by_model == {DEEPSEEK: 1}
    finally:
        monkeypatch.undo()
        line.detach()
        line.close()
        line.deleteLater()
        qapp.processEvents()


def test_a_damaged_file_says_it_once_for_many_replays(qapp, tmp_path):
    """Review L1: free answers do not flood the log, one line per problem."""
    from kicadgen.gui.ai_account import AiAccountLine
    start_run(tmp_path)
    (tmp_path / COSTS_FILE).write_text("{broken", encoding="utf-8")
    logs = []
    line = AiAccountLine(lambda: tmp_path, logs.append)
    try:
        logs.clear()
        for _ in range(3):
            openrouter.record_cached_answer(DEEPSEEK)
            openrouter.record_cached_answer(GPT)
            qapp.processEvents()
            line._cached_timer.timeout.emit()
        # One line per model, not one per answer.
        assert len([message for message in logs if "cache" in message]) == 2
    finally:
        line.detach()
        line.close()
        line.deleteLater()
        qapp.processEvents()


def test_a_replay_delivered_after_detach_never_writes_later(qapp, tmp_path):
    """Second review: the final write is final, as for a busy file after detach."""
    from kicadgen.gui.ai_account import AiAccountLine
    start_run(tmp_path)
    line = AiAccountLine(lambda: tmp_path, lambda _m: None)
    try:
        line.detach()
        # A queued signal from a worker thread arriving after the window closed.
        line._on_paid_call({"model": DEEPSEEK, "time": "2026-11-15T10:00:00+00:00",
                            "_cached_answer": True})
        assert not line._cached_timer.isActive()
        assert load_project_costs(tmp_path).run_cached_by_model == {}
    finally:
        line.close()
        line.deleteLater()
        qapp.processEvents()


def test_a_replay_without_a_project_is_dropped_without_a_cost_line(qapp):
    """Negative: a paid call without a project says it went unrecorded; a free replay has nothing to say."""
    from kicadgen.gui.ai_account import AiAccountLine
    logs = []
    line = AiAccountLine(lambda: None, logs.append)
    try:
        openrouter.record_cached_answer(DEEPSEEK)
        qapp.processEvents()
        assert line._pending == [] and logs == []
    finally:
        line.detach()
        line.close()
        line.deleteLater()
        qapp.processEvents()


# ── the cards ─────────────────────────────────────────────────────────────

def _named_project(window, tmp_path, name="CACHE"):
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText(name)
    return window._project_dir()


def _hold_generation_card(window):
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    overlay.set_steps(["Footprint KiCadGen", "Corpo e terminali FreeCAD"])
    window._hold_completed_overlay("Completato: footprint e modello STEP generati.",
                                   analysis_cost=True)
    return overlay


def test_the_finished_card_names_the_one_model_and_lists_the_cached_one(window, tmp_path):  # noqa: F811
    """The user's case: two GPT calls, two DeepSeek readings from the cache."""
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.2224, "model": GPT})
    add_call(folder, {"cost_usd": 0.1746, "model": GPT})
    add_cached_answer(folder, {"model": DEEPSEEK})
    add_cached_answer(folder, {"model": DEEPSEEK})
    overlay = _hold_generation_card(window)
    try:
        assert overlay.cost.text() == f"Costo AI dell'analisi: 0.3970 USD (2 chiamate · {GPT})"
        assert overlay.cost_models.isHidden()
        assert overlay.cost_cached.text() == f"{DEEPSEEK}: 2 letture dalla cache (gratis)"
        assert not overlay.cost_cached.isHidden()
    finally:
        overlay.close_button.click()


def test_an_analysis_answered_only_from_the_cache_is_read_from_the_file(window, tmp_path):  # noqa: F811
    """Review M1: no paid call, yet the whole analysis counts, not the last run's tally."""
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_cached_answer(folder, {"model": DEEPSEEK, "count": 84})    # «Estrai parametri»
    add_cached_answer(folder, {"model": DEEPSEEK, "count": 2})     # the re-read
    window._last_analysis_cost = ("Ultima analisi: nessuna chiamata", [],
                                  [f"{DEEPSEEK}: 2 letture dalla cache (gratis)"])
    overlay = _hold_generation_card(window)
    try:
        assert overlay.cost.text() == "Costo AI dell'analisi: 0 USD (nessuna chiamata a pagamento)"
        assert overlay.cost_cached.text() == f"{DEEPSEEK}: 86 letture dalla cache (gratis)"
        # Read from the file: the held card follows a language switch.
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        assert overlay.cost.text() == "AI cost of the analysis: 0 USD (no paid call)"
        assert overlay.cost_cached.text() == f"{DEEPSEEK}: 86 readings from the cache (free)"
    finally:
        window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
        overlay.close_button.click()


def test_nothing_paid_and_nothing_cached_keeps_the_fallback(window, tmp_path):  # noqa: F811
    """Negative twin: an analysis with neither calls nor replays has nothing in the file to show."""
    folder = _named_project(window, tmp_path)
    start_run(folder)
    window._last_analysis_cost = ("Ultima analisi: nessuna chiamata", [], [])
    overlay = _hold_generation_card(window)
    try:
        assert overlay.cost.text() == "Ultima analisi: nessuna chiamata"
        assert overlay.cost_cached.isHidden()
    finally:
        overlay.close_button.click()


def test_the_grey_line_follows_a_language_switch_with_paid_calls(window, tmp_path):  # noqa: F811
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.10, "model": GPT})
    add_cached_answer(folder, {"model": DEEPSEEK})
    overlay = _hold_generation_card(window)
    try:
        window.language_combo.setCurrentIndex(window.language_combo.findData("de"))
        assert overlay.cost_cached.text() == f"{DEEPSEEK}: 1 Abruf aus dem Cache (kostenlos)"
        assert overlay.cost.text().endswith(f"· {GPT})")
    finally:
        window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
        overlay.close_button.click()


def test_two_models_keep_their_lines_and_no_cache_no_grey_line(window, tmp_path):  # noqa: F811
    """Negative twin of both rules."""
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.30, "model": GPT})
    add_call(folder, {"cost_usd": 0.02, "model": DEEPSEEK})
    overlay = _hold_generation_card(window)
    try:
        assert overlay.cost.text() == "Costo AI dell'analisi: 0.3200 USD (2 chiamate)"
        assert [line.split(":")[0] for line in overlay.cost_models.text().splitlines()] == [
            GPT, DEEPSEEK]
        assert overlay.cost_cached.isHidden() and overlay.cost_cached.text() == ""
    finally:
        overlay.close_button.click()


def test_one_model_with_a_call_without_cost_keeps_its_line(window, tmp_path):  # noqa: F811
    folder = _named_project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.30, "model": GPT})
    add_call(folder, {"cost_usd": None, "model": GPT})
    overlay = _hold_generation_card(window)
    try:
        assert GPT not in overlay.cost.text()
        assert "senza costo" in overlay.cost_models.text()
    finally:
        overlay.close_button.click()


def test_the_cached_line_sits_under_the_total_and_above_the_time(window):  # noqa: F811
    overlay = window.analysis_overlay
    layout = overlay.cost.parentWidget().layout()
    order = [layout.indexOf(widget) for widget in
             (overlay.cost_models, overlay.cost, overlay.cost_cached, overlay.elapsed)]
    assert order == sorted(order) and -1 not in order


def test_a_new_analysis_clears_the_grey_line(window):  # noqa: F811
    overlay = window.analysis_overlay
    overlay.set_cost_cached([f"{DEEPSEEK}: 1 lettura dalla cache (gratis)"])
    overlay.clear_cost()
    assert overlay.cost_cached.isHidden() and overlay.cost_cached.text() == ""


def test_the_analysis_card_uses_the_same_two_rules(window):  # noqa: F811
    """The card at the end of an AI run (run tally) reads like the finished card."""
    openrouter._record_usage({"model": GPT, "usage": {"cost": 0.10, "prompt_tokens": 10,
                                                      "completion_tokens": 5}}, model=GPT)
    openrouter.record_cached_answer(DEEPSEEK)
    window._show_ai_cost()
    overlay = window.analysis_overlay
    assert overlay.cost.text().endswith(f"· 1 chiamata · 15 token · {GPT}")
    assert overlay.cost_models.isHidden()
    assert overlay.cost_cached.text() == f"{DEEPSEEK}: 1 lettura dalla cache (gratis)"
    # Negative: a second model brings the lines back and the total names no model.
    openrouter._record_usage({"model": DEEPSEEK, "usage": {"cost": 0.01}}, model=DEEPSEEK)
    window._show_ai_cost()
    assert GPT not in overlay.cost.text() and len(overlay.cost_models.text().splitlines()) == 2
