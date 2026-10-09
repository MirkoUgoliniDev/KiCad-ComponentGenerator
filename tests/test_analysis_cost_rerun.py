"""@brief Only «Estrai parametri con AI» opens a new analysis for the cost shown (HANDOFF 138-139, B1).

User decision of 2026-09-28: the cost shown is the one of the current
analysis, paid reruns INCLUDED. The audit found that the «Rilancio
inventario» opened a new analysis too (the start was in `_begin_ai_run`,
shared by both), so a rerun reset the cost shown. These tests drive the two
real entry points of the window, not the source text.
"""
import pytest

from test_ai_account_menu import _ready_for_extraction, _saved_review_bundle, no_key, questions  # noqa: F401
from kicadgen.services.project_costs import add_call, load_project_costs, start_run


def _record_starts(window, monkeypatch):
    starts = []
    monkeypatch.setattr(window.ai_account, "start_analysis", lambda folder: starts.append(folder))
    return starts


def test_the_extraction_opens_a_new_analysis(window, monkeypatch, questions):  # noqa: F811
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    starts = _record_starts(window, monkeypatch)
    started = _ready_for_extraction(window, monkeypatch)
    window._extract_with_ai()
    assert started == ["Analisi AI"]
    assert len(starts) == 1


def test_the_inventory_relaunch_adds_to_the_current_analysis(no_key, monkeypatch,
                                                             questions, tmp_path):  # noqa: F811
    """Negative: the rerun is paid, charged, but does not start a new analysis."""
    window = no_key
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("ReviewCost")
    folder = window._project_dir()
    start_run(folder)
    add_call(folder, {"cost_usd": .25, "model": "test"})
    starts = _record_starts(window, monkeypatch)
    begun = []
    bundle = _saved_review_bundle(tmp_path)
    monkeypatch.setattr(window, "_ensure_review_bundle", lambda: bundle)
    monkeypatch.setattr(window, "_review_photo", lambda bundle: b"png")
    monkeypatch.setattr(window, "_start_log_session", begun.append)
    paid = []
    def reconcile(*args):
        paid.append(args)
        window.ai_account._on_paid_call({"cost_usd": .1, "model": "test"})
    monkeypatch.setattr(window, "_start_reconcile_worker", reconcile)
    try:
        window._relaunch_inventory()
        assert begun == ["Rilancio inventario"]
        assert len(paid) == 1 and paid[0][0] is bundle
        assert starts == []
        costs = load_project_costs(folder)
        assert costs.run == 1 and costs.run_calls == 2
        assert costs.run_usd == pytest.approx(.35)
    finally:
        window._extraction_busy = False
        window.ai_account.release_run_project()


def test_the_shared_run_setup_never_opens_an_analysis(window, monkeypatch):  # noqa: F811
    """`_begin_ai_run` is shared by the extraction and the rerun: it binds, never starts."""
    starts = _record_starts(window, monkeypatch)
    bound = []
    monkeypatch.setattr(window.ai_account, "bind_run_project", lambda folder: bound.append(folder))
    monkeypatch.setattr(window, "_start_log_session", lambda title: None)
    window._begin_ai_run("Rilancio inventario")
    try:
        assert starts == [] and len(bound) == 1
    finally:
        window._extraction_busy = False
