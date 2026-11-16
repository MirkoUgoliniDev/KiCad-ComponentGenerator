"""@brief Fixes of the 2026-11-15 review of the AI-cost bookkeeping (fixer C4).

Each rule has a case that must trigger and one that must not:

- a damaged «cached_answers» never takes the paid ledger down: the paid call
  is recorded, the cached write is refused, the damaged value stays as it is;
- cached readings are written at natural points (end of the run, next paid
  call, detach), not once per second; the safety timer defers to the
  busy-file retry, which is the single retry path;
- a cached reading that cannot be queued says so with its own line, never
  with the «cost not recorded (? USD)» line of a paid call;
- every time in the ledger is UTC, whatever zone the caller passed.

No network: no AI call is made, the cost file lives in tmp_path.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PyQt6")

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


@pytest.fixture
def line(qapp, tmp_path):
    """@brief An AI account line on tmp_path, with its log; detached and released after the test."""
    from kicadgen.gui.ai_account import AiAccountLine
    start_run(tmp_path)
    logs = []
    account = AiAccountLine(lambda: tmp_path, logs.append)
    account.logs = logs
    yield account
    account.detach()
    account.close()
    account.deleteLater()
    qapp.processEvents()


def _with_cached_answers(path, value):
    """@brief Rewrite the cost file at `path` with «cached_answers» set to `value`; return its text."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["cached_answers"] = value
    text = json.dumps(data, indent=2)
    path.write_text(text, encoding="utf-8")
    return text


# ── 1. a damaged list of cached readings never blocks the paid ledger ─────

@pytest.mark.parametrize("damaged", [{"run": 1, "model": DEEPSEEK, "count": 2}, None, 5, "x"])
def test_a_damaged_cached_list_leaves_the_paid_ledger_working(tmp_path, damaged):
    start_run(tmp_path)
    path = tmp_path / COSTS_FILE
    text = _with_cached_answers(path, damaged)
    costs = load_project_costs(tmp_path)
    assert costs.cached_answers_damaged and costs.run_cached_by_model == {}
    # The cached write is refused and the file is left byte for byte.
    with pytest.raises(ProjectCostsError):
        add_cached_answer(tmp_path, {"model": DEEPSEEK})
    assert path.read_text(encoding="utf-8") == text
    # The paid call is recorded, and the damaged value is kept as it was.
    costs = add_call(tmp_path, {"cost_usd": 0.25, "model": GPT})
    assert (costs.calls, costs.run_calls, costs.total_usd) == (1, 1, 0.25)
    assert costs.cached_answers_damaged
    assert json.loads(path.read_text(encoding="utf-8"))["cached_answers"] == damaged
    assert load_project_costs(tmp_path).run_by_model == {GPT: (0.25, 1, 0)}


def test_a_valid_cached_list_takes_both_writes(tmp_path):
    """Positive twin: a list, even an empty one, is not damaged."""
    start_run(tmp_path)
    _with_cached_answers(tmp_path / COSTS_FILE, [])
    assert not load_project_costs(tmp_path).cached_answers_damaged
    add_cached_answer(tmp_path, {"model": DEEPSEEK})
    costs = add_call(tmp_path, {"cost_usd": 0.25, "model": GPT})
    assert costs.run_cached_by_model == {DEEPSEEK: 1} and costs.calls == 1
    assert not costs.cached_answers_damaged


def test_the_account_line_says_once_that_the_cached_list_is_damaged(line, tmp_path):
    _with_cached_answers(tmp_path / COSTS_FILE, {})
    line.logs.clear()
    for _ in range(3):
        line.refresh()
    damaged = [message for message in line.logs if "non valido" in message]
    assert len(damaged) == 1 and str(tmp_path) in damaged[0]
    # The paid figures are still shown, not «non leggibile» (unreadable).
    assert "non leggibile" not in line.text()


def test_a_sound_cached_list_says_nothing(line, tmp_path):
    """Negative twin: a valid file adds no line."""
    line.logs.clear()
    line.refresh()
    assert not [message for message in line.logs if "non valido" in message]


# ── 3. cached readings are written at the end of the run ──────────────────

def test_the_end_of_a_run_writes_the_grouped_readings(line, qapp, tmp_path):
    from kicadgen.gui.ai_account import CACHED_SAFETY_MS
    line.bind_run_project(tmp_path)
    for _ in range(40):
        openrouter.record_cached_answer(DEEPSEEK)
    qapp.processEvents()
    # Not once per second any more: only a long safety bound is pending.
    assert line._cached_timer.isActive() and line._cached_timer.interval() == CACHED_SAFETY_MS
    assert CACHED_SAFETY_MS >= 10000
    assert load_project_costs(tmp_path).run_cached_by_model == {}
    line.release_run_project()
    assert load_project_costs(tmp_path).run_cached_by_model == {DEEPSEEK: 40}
    assert line._pending == [] and not line._cached_timer.isActive()


def test_while_the_run_goes_on_nothing_is_written(line, qapp, tmp_path):
    """Negative twin: without the run's end (or a paid call) the file is not rewritten."""
    line.bind_run_project(tmp_path)
    before = (tmp_path / COSTS_FILE).read_bytes()
    for _ in range(40):
        openrouter.record_cached_answer(DEEPSEEK)
        qapp.processEvents()
    assert (tmp_path / COSTS_FILE).read_bytes() == before
    assert [(item["model"], item["count"]) for _f, item in line._pending] == [(DEEPSEEK, 40)]


def _named_project(window, tmp_path, name="CACHE"):
    window.field_library_root.setText(str(tmp_path))
    window.field_name.setText(name)
    return window._project_dir()


def test_an_analysis_served_from_the_cache_reaches_the_finished_card(window, qapp, tmp_path):  # noqa: F811
    """Review M1 kept: the readings go through the account line, written at the run's end."""
    folder = _named_project(window, tmp_path)
    account = window.ai_account
    account.start_analysis(folder)
    account.bind_run_project(folder)
    for _ in range(3):
        openrouter.record_cached_answer(DEEPSEEK)
    qapp.processEvents()
    assert load_project_costs(folder).run_cached_by_model == {}
    account.release_run_project()      # what `_on_extract_finished` does first
    overlay = window.analysis_overlay
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    overlay.set_steps(["Footprint KiCadGen", "Corpo e terminali FreeCAD"])
    window._hold_completed_overlay("Completato: footprint e modello STEP generati.",
                                   analysis_cost=True)
    try:
        assert overlay.cost.text() == "Costo AI dell'analisi: 0 USD (nessuna chiamata a pagamento)"
        assert overlay.cost_cached.text() == f"{DEEPSEEK}: 3 letture dalla cache (gratis)"
    finally:
        overlay.close_button.click()


# ── 4. one retry path while the file is busy ──────────────────────────────

def _count_cached_writes(monkeypatch, busy=False):
    """@brief Replace the account line's cached write with a counter; busy raises like a locked file."""
    from kicadgen.gui import ai_account as account_module
    attempts = []

    def write(*args):
        attempts.append(args)
        if busy:
            raise OSError("file in uso")
        return add_cached_answer(*args)
    monkeypatch.setattr(account_module, "add_cached_answer", write)
    return attempts


def test_the_safety_timer_defers_to_a_pending_busy_retry(line, qapp, monkeypatch):
    attempts = _count_cached_writes(monkeypatch)
    openrouter.record_cached_answer(DEEPSEEK)
    qapp.processEvents()
    line._retry_timer.start(60000)          # a busy file was met: the retry owns the next try
    line._cached_timer.timeout.emit()
    line.release_run_project()
    assert attempts == [] and len(line._pending) == 1
    line._retry_timer.timeout.emit()        # the retry writes the cached group too
    assert len(attempts) == 1 and line._pending == []


def test_without_a_pending_retry_the_safety_timer_writes(line, qapp, monkeypatch):
    """Negative twin: no busy retry pending, the timer is the one that writes."""
    attempts = _count_cached_writes(monkeypatch)
    openrouter.record_cached_answer(DEEPSEEK)
    qapp.processEvents()
    assert not line._retry_timer.isActive()
    line._cached_timer.timeout.emit()
    assert len(attempts) == 1 and line._pending == []


def test_a_busy_file_hands_the_cached_group_to_the_retry(line, qapp, monkeypatch):
    attempts = _count_cached_writes(monkeypatch, busy=True)
    openrouter.record_cached_answer(DEEPSEEK)
    qapp.processEvents()
    line._cached_timer.timeout.emit()
    # One blocking attempt, then the retry's backoff owns the next one.
    assert len(attempts) == 1
    assert line._retry_timer.isActive() and not line._cached_timer.isActive()
    openrouter.record_cached_answer(DEEPSEEK)
    qapp.processEvents()
    assert not line._cached_timer.isActive() and len(attempts) == 1


# ── 5. a cached reading that cannot be queued has its own line ────────────

def test_a_failing_cached_reading_never_logs_a_cost(line, qapp, monkeypatch):
    def broken(*_args):
        raise RuntimeError("guasto")
    monkeypatch.setattr(line, "_queue_cached_answer", broken)
    line.logs.clear()
    for _ in range(3):
        openrouter.record_cached_answer(DEEPSEEK)
        qapp.processEvents()
    assert line.logs == [f"Lettura dalla cache non registrata nel file dei costi ({DEEPSEEK}): "
                         "guasto"]


def test_a_failing_paid_call_keeps_its_cost_line(line, qapp, monkeypatch):
    """Negative twin: money keeps one line per call, with its cost."""
    def broken(*_args, **_kwargs):
        raise RuntimeError("guasto")
    monkeypatch.setattr(line, "_flush_pending", broken)
    line.logs.clear()
    for _ in range(2):
        openrouter._record_usage({"model": GPT, "usage": {"cost": 0.1}}, model=GPT)
        qapp.processEvents()
    assert line.logs == ["Costo di una chiamata AI non registrato nel progetto (0.1000 USD): "
                         "guasto"] * 2


# ── 6. one time convention in the ledger: UTC ─────────────────────────────

ROME = timezone(timedelta(hours=2))


def test_a_time_in_another_zone_is_stored_in_utc(tmp_path):
    start_run(tmp_path, datetime(2026, 11, 15, 10, 0, tzinfo=ROME))
    add_call(tmp_path, {"cost_usd": 0.1, "model": GPT}, datetime(2026, 11, 15, 10, 5, tzinfo=ROME))
    add_cached_answer(tmp_path, {"model": DEEPSEEK}, datetime(2026, 11, 15, 10, 6, tzinfo=ROME))
    data = json.loads((tmp_path / COSTS_FILE).read_text(encoding="utf-8"))
    stamps = [data["since"], data["runs"][0]["started"], data["entries"][0]["time"],
              data["cached_answers"][0]["time"]]
    assert stamps == ["2026-11-15T08:00:00+00:00", "2026-11-15T08:00:00+00:00",
                      "2026-11-15T08:05:00+00:00", "2026-11-15T08:06:00+00:00"]
    # Same instants, read back as aware datetimes.
    assert datetime.fromisoformat(stamps[2]) == datetime(2026, 11, 15, 10, 5, tzinfo=ROME)
    assert all(datetime.fromisoformat(stamp).tzinfo is not None for stamp in stamps)


def test_a_utc_or_naive_time_is_not_shifted(tmp_path):
    """Negative twin: a UTC time is kept as it is, a naive one is taken as UTC (never as local)."""
    start_run(tmp_path, datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc))
    add_call(tmp_path, {"cost_usd": 0.1, "model": GPT}, datetime(2026, 11, 15, 10, 5))
    data = json.loads((tmp_path / COSTS_FILE).read_text(encoding="utf-8"))
    assert data["runs"][0]["started"] == "2026-11-15T10:00:00+00:00"
    assert data["entries"][0]["time"] == "2026-11-15T10:05:00+00:00"
