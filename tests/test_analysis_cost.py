"""@brief The cost shown is the one of the current analysis, not the project total (HANDOFF point 135).

User decisions of 2026-09-28: the status bar and the «Credito AI» line show
the paid calls since the last «Estrai parametri con AI», reruns included; the
project total stays in the tooltip; reopening a project shows its last analysis.
"""
import pytest

from kicadgen.gui import ai_account as account_module
from kicadgen.gui.ai_account import AiAccountLine
from kicadgen.services.project_costs import add_call, load_project_costs, start_run


def test_calls_after_a_start_belong_to_that_analysis_only(tmp_path):
    add_call(tmp_path, {"cost_usd": 1.0, "model": "m"})          # before any analysis
    start_run(tmp_path)
    add_call(tmp_path, {"cost_usd": 0.25, "model": "m"})
    add_call(tmp_path, {"cost_usd": 0.5, "model": "m"})          # a rerun: same analysis
    costs = load_project_costs(tmp_path)
    assert (costs.run, costs.run_calls) == (1, 2)
    assert costs.run_usd == pytest.approx(0.75)
    assert costs.total_usd == pytest.approx(1.75) and costs.calls == 3


def test_a_new_analysis_starts_from_zero_and_keeps_the_total(tmp_path):
    """Negative: the previous analysis never leaks into the new one."""
    start_run(tmp_path)
    add_call(tmp_path, {"cost_usd": 0.4, "model": "m"})
    start_run(tmp_path)
    costs = load_project_costs(tmp_path)
    assert (costs.run, costs.run_calls, costs.run_usd) == (2, 0, 0.0)
    assert costs.total_usd == pytest.approx(0.4)
    add_call(tmp_path, {"cost_usd": None, "model": "m"})
    costs = load_project_costs(tmp_path)
    assert costs.run_calls == 1 and not costs.run_complete


def test_a_project_saved_before_analyses_were_recorded_says_so(qapp, tmp_path):
    add_call(tmp_path, {"cost_usd": 2.0, "model": "m"})
    assert load_project_costs(tmp_path).run == 0
    line = AiAccountLine(lambda: tmp_path, lambda _m: None)
    try:
        line.refresh()
        assert "Costo dell'analisi: nessuna analisi registrata" in line.text()
        assert "Costo totale del progetto: 2.00 USD" in line.toolTip()
    finally:
        line.detach()


def test_the_line_shows_the_analysis_and_the_tooltip_the_total(qapp, tmp_path):
    add_call(tmp_path, {"cost_usd": 1.0, "model": "m"})
    line = AiAccountLine(lambda: tmp_path, lambda _m: None)
    try:
        line.start_analysis(tmp_path)
        add_call(tmp_path, {"cost_usd": 0.5, "model": "m"})
        line.refresh()
        assert "Costo dell'analisi: 0.5000 USD" in line.text()
        assert "1.50" not in line.text()
        assert "Costo totale del progetto: 1.50 USD" in line.toolTip()
    finally:
        line.detach()


def test_a_late_call_of_the_previous_analysis_is_not_counted_in_the_new_one(
        qapp, tmp_path, monkeypatch):
    """The start is queued behind a call still waiting for a busy file."""
    line = AiAccountLine(lambda: tmp_path, lambda _m: None)
    real_add = account_module.add_call
    busy = {"left": 1}

    def add_once_busy(folder, record, now=None):
        if busy["left"]:
            busy["left"] -= 1
            raise OSError("busy")
        return real_add(folder, record, now)

    monkeypatch.setattr(account_module, "add_call", add_once_busy)
    try:
        line._on_paid_call({"cost_usd": 0.3, "model": "m"})       # stays queued
        assert load_project_costs(tmp_path).calls == 0
        line.start_analysis(tmp_path)                             # flushes in order
        costs = load_project_costs(tmp_path)
        assert costs.calls == 1 and costs.run == 1 and costs.run_calls == 0
    finally:
        line.detach()


def test_without_a_project_nothing_is_started(qapp, tmp_path):
    line = AiAccountLine(lambda: None, lambda _m: None)
    try:
        line.start_analysis(None)
        assert not line._pending
    finally:
        line.detach()


def test_the_status_bar_shows_the_analysis_and_the_tooltip_the_total(window, tmp_path):
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("SCHEDA")
    folder = window._project_dir()
    add_call(folder, {"cost_usd": 1.0, "model": "m"})
    start_run(folder)
    add_call(folder, {"cost_usd": 0.05, "model": "m"})
    window._refresh_status_bar(True)
    # Same format as the «Credito AI» line (audit 138, B4): four decimals below 1 USD.
    assert window.status_cost.text() == "Costo AI dell'analisi: 0.0500 USD (1 chiamate)"
    assert "1.05" in window.status_cost.toolTip()


def test_the_extraction_opens_a_new_analysis_before_its_first_call():
    import inspect
    from kicadgen.gui import main_window
    source = inspect.getsource(main_window.MainWindow)
    start = source.index("self.ai_account.start_analysis(self._project_dir_or_none())")
    assert source.index("self.ai_account.bind_run_project(self._project_dir_or_none())",
                        start) > start


def test_a_cheap_analysis_is_never_shown_as_zero(window, tmp_path):
    """Negative of the old «0.00 USD»: a DeepSeek analysis costs thousandths (B4)."""
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("SCHEDA")
    folder = window._project_dir()
    start_run(folder)
    add_call(folder, {"cost_usd": 0.0042, "model": "m"})
    window._refresh_status_bar(True)
    assert "0.0042 USD" in window.status_cost.text()


def test_the_status_bar_follows_every_cost_write(window, tmp_path):
    """The status bar refreshes when the account line writes the cost file (B4)."""
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText("SCHEDA")
    folder = window._project_dir()
    window.ai_account.start_analysis(folder)
    add_call(folder, {"cost_usd": 0.25, "model": "m"})
    window.ai_account.costs_changed.emit()
    assert "0.2500 USD" in window.status_cost.text()
