"""@brief The status bar says a cost is a minimum while calls wait to be written (HANDOFF 139, final review).

The «Credito AI» line already said «almeno … (+N in attesa)» when the cost
file was busy; the status bar showed the file's figure as if complete, and
the previous analysis while the start of a new one was still queued.
"""
from kicadgen.gui import ai_account as account_module
from kicadgen.services.project_costs import add_call, start_run


def _busy_file(monkeypatch):
    """Every write fails as a busy file (the calls stay queued)."""
    def busy(*_args, **_kwargs):
        raise OSError("busy")
    monkeypatch.setattr(account_module, "add_call", busy)
    monkeypatch.setattr(account_module, "start_run", busy)


def _project(window, tmp_path):
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("SCHEDA")
    return window._project_dir()


def test_queued_calls_make_the_status_bar_a_minimum(window, tmp_path, monkeypatch):
    folder = _project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.25, "model": "m"})
    _busy_file(monkeypatch)
    window.ai_account._on_paid_call({"cost_usd": 0.5, "model": "m", "_run_bound": True,
                                     "_run_project": folder})
    window._refresh_status_bar(True)
    assert window.status_cost.text() == \
        "Costo AI dell'analisi: almeno 0.2500 USD (+1 in attesa)"


def test_a_queued_start_does_not_show_the_previous_analysis(window, tmp_path, monkeypatch):
    folder = _project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.75, "model": "m"})
    _busy_file(monkeypatch)
    window.ai_account.start_analysis(folder)            # stays queued
    window._refresh_status_bar(True)
    assert "0.7500" not in window.status_cost.text()
    assert window.status_cost.text().startswith("Costo AI dell'analisi: almeno 0.0000 USD")


def test_nothing_queued_shows_the_plain_figure(window, tmp_path):
    """Negative: with an empty queue the figure is complete, no «almeno»."""
    folder = _project(window, tmp_path)
    start_run(folder)
    add_call(folder, {"cost_usd": 0.25, "model": "m"})
    window._refresh_status_bar(True)
    assert window.status_cost.text() == "Costo AI dell'analisi: 0.2500 USD (1 chiamate)"


def test_the_retry_timer_does_not_restart_after_detach(qapp, tmp_path, monkeypatch):
    line = account_module.AiAccountLine(lambda: tmp_path, lambda _m: None)
    _busy_file(monkeypatch)
    line.detach()
    line._on_paid_call({"cost_usd": 0.1, "model": "m"})     # delivered late
    assert not line._retry_timer.isActive()
