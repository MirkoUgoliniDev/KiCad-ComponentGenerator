"""@brief Cost accounting fixes of audit 138 (B2-B11): queue, lock, dates, tokens, runs, errors.

No network: requests are faked at `urlopen`, keys are fixed test strings and
the Credential Manager is never queried.
"""
import io
import json
import os
import time
import urllib.error
from datetime import datetime, timezone

import pytest

from kicadgen.gui import ai_account as account_module
from kicadgen.gui.ai_account import AiAccountLine, _local_date
from kicadgen.services import deepseek_pricing, openrouter, project_costs, providers
from kicadgen.services.project_costs import (COSTS_FILE, LOCK_FILE, ProjectCostsError, add_call,
                                             load_project_costs, start_run)

KEY = "sk-or-v1-" + "0" * 64
DEEPSEEK_MODEL = providers.DEEPSEEK_PREFIX + "deepseek-flash"


@pytest.fixture(autouse=True)
def no_listener():
    openrouter.set_usage_listener(None)
    openrouter.reset_usage()
    yield
    openrouter.set_usage_listener(None)


@pytest.fixture
def line_factory(qapp):
    lines = []

    def make(folder_source, log):
        line = AiAccountLine(folder_source, log)
        lines.append(line)
        return line

    yield make
    for line in lines:
        line.detach()
        line.close()
        line.deleteLater()
    qapp.processEvents()


class _Busy:
    """Replaces ai_account.add_call / start_run: raises OSError while `busy`."""

    def __init__(self, real):
        self.real, self.busy = real, True

    def __call__(self, *args):
        if self.busy:
            raise OSError("file in uso")
        return self.real(*args)


def _entries(folder):
    return json.loads((folder / COSTS_FILE).read_text(encoding="utf-8"))["entries"]


def _ledger(folder, **fields):
    data = {"format": project_costs.FORMAT, "version": 1, "since": "", "total_usd": 0.0,
            "calls": 0, "calls_without_cost": 0, "entries": []}
    data.update(fields)
    (folder / COSTS_FILE).write_text(json.dumps(data), encoding="utf-8")


# ── B2: queued calls are visible, retried by a spaced single-shot timer, flushed at detach

def test_a_queued_call_makes_the_line_say_at_least_and_waiting(line_factory, tmp_path, monkeypatch):
    busy = _Busy(project_costs.add_call)
    monkeypatch.setattr(account_module, "add_call", busy)
    log = []
    line = line_factory(lambda: tmp_path, log.append)
    line.start_analysis(tmp_path)
    line._on_paid_call({"cost_usd": 0.3, "model": "m"})
    assert "almeno" in line.text() and "(+1 in attesa)" in line.text()
    assert "in attesa di essere scritte" in line.toolTip()
    assert line._retry_timer.isActive() and line._retry_timer.isSingleShot()
    busy.busy = False
    line._retry_flush()                                   # what the timer runs
    assert not line._pending and not line._retry_timer.isActive()
    assert "in attesa" not in line.text() and "0.3000" in line.text()
    assert load_project_costs(tmp_path).run_calls == 1


def test_a_written_call_never_claims_to_be_waiting(line_factory, tmp_path):
    """Negative: a free file writes at once, no timer, no «in attesa»."""
    line = line_factory(lambda: tmp_path, lambda _m: None)
    line.start_analysis(tmp_path)
    line._on_paid_call({"cost_usd": 0.3, "model": "m"})
    assert "in attesa" not in line.text() and "almeno" not in line.text()
    assert not line._retry_timer.isActive()


def test_a_file_that_stays_busy_spaces_out_the_retries(line_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(account_module, "add_call", _Busy(project_costs.add_call))
    line = line_factory(lambda: tmp_path, lambda _m: None)
    line._on_paid_call({"cost_usd": 0.1, "model": "m"})
    first = line._retry_timer.interval()
    line._retry_timer.stop()
    line._retry_flush()
    assert line._retry_timer.interval() == 2 * first      # doubled, never a tight loop
    assert first == account_module.FLUSH_RETRY_FIRST_MS


def test_detach_writes_the_queue_when_the_file_is_free_again(line_factory, tmp_path, monkeypatch):
    busy = _Busy(project_costs.add_call)
    monkeypatch.setattr(account_module, "add_call", busy)
    log = []
    line = line_factory(lambda: tmp_path, log.append)
    line._on_paid_call({"cost_usd": 0.2, "model": "m"})
    busy.busy = False
    line.detach()
    assert load_project_costs(tmp_path).calls == 1
    assert not any("non scritte" in entry for entry in log)


def test_detach_with_the_file_still_busy_declares_the_loss(line_factory, tmp_path, monkeypatch):
    """Negative: nothing is lost silently at closing."""
    monkeypatch.setattr(account_module, "add_call", _Busy(project_costs.add_call))
    log = []
    line = line_factory(lambda: tmp_path, log.append)
    line._on_paid_call({"cost_usd": 0.2, "model": "m"})
    line.detach()
    assert any("1 chiamate a pagamento non scritte" in entry for entry in log)
    assert not line._retry_timer.isActive()


def _aged_lock(folder, offset_s):
    lock = folder / LOCK_FILE
    lock.write_text("424242", encoding="ascii")
    moment = time.time() + offset_s
    os.utime(lock, (moment, moment))
    return lock


@pytest.mark.parametrize("offset", [3600.0, -3600.0])
def test_a_lock_far_from_now_in_either_direction_is_stale(tmp_path, monkeypatch, offset):
    monkeypatch.setattr(project_costs, "LOCK_TIMEOUT_S", 0.2)
    lock = _aged_lock(tmp_path, offset)
    assert add_call(tmp_path, {"cost_usd": 0.1}).calls == 1
    assert not lock.exists()


def test_a_fresh_lock_is_respected(tmp_path, monkeypatch):
    """Negative: a lock written just now belongs to a live writer."""
    monkeypatch.setattr(project_costs, "LOCK_TIMEOUT_S", 0.2)
    _aged_lock(tmp_path, 0.0)
    with pytest.raises(TimeoutError):
        add_call(tmp_path, {"cost_usd": 0.1})


# ── B3: damaged dates never escape, refresh never raises ────────────────────

def test_a_date_at_the_edge_of_the_calendar_does_not_raise():
    assert _local_date("0001-01-01T00:00:00+00:00") == "0001-01-01"
    assert _local_date("rotta") == "rotta"


def test_refresh_survives_a_ledger_dated_year_one(line_factory, tmp_path):
    _ledger(tmp_path, since="0001-01-01T00:00:00+00:00", total_usd=0.5, calls=1, run=1,
            runs=[{"run": 1, "started": "0001-01-01T00:00:00+00:00"}],
            entries=[{"time": "0001-01-01T00:00:00+00:00", "model": "x/y", "cost_usd": 0.5,
                      "prompt_tokens": 1, "completion_tokens": 1, "run": 1}])
    log = []
    line = line_factory(lambda: tmp_path, log.append)
    line.refresh()
    assert "0.5000" in line.text() and "0001-01-01" in line.toolTip()
    assert not log


def test_an_unexpected_error_in_refresh_is_logged_not_raised(line_factory, tmp_path, monkeypatch):
    log = []
    line = line_factory(lambda: tmp_path, log.append)
    before = line.text()

    def broken(*_a, **_k):
        raise RuntimeError("guasto")
    monkeypatch.setattr(account_module, "load_project_costs", broken)
    line.refresh()                                         # must not raise
    assert any("Riga del credito AI non aggiornata" in entry and "guasto" in entry
               for entry in log)
    assert line.text() == before


# ── B5: unusable token counts are unknown, never zero or priced ─────────────

@pytest.mark.parametrize("bad", ["12", -5, True, 1.5, float("nan")])
def test_unusable_token_counts_are_recorded_as_none(tmp_path, bad):
    add_call(tmp_path, {"cost_usd": None, "model": "m", "prompt_tokens": bad,
                        "completion_tokens": 7})
    [entry] = _entries(tmp_path)
    assert entry["prompt_tokens"] is None and entry["completion_tokens"] == 7


def test_the_listener_gets_none_for_tokens_not_reported():
    seen = []
    openrouter.set_usage_listener(seen.append)
    openrouter._record_usage({"model": "x/y", "usage": {"cost": 0.1}})
    assert seen[0]["prompt_tokens"] is None and seen[0]["completion_tokens"] is None


def _deepseek_entry(prompt, completion):
    return {"time": "2026-09-27T12:00:00+00:00", "model": DEEPSEEK_MODEL, "cost_usd": None,
            "prompt_tokens": prompt, "completion_tokens": completion}


@pytest.mark.parametrize("prompt, completion", [(None, 100), (1000, None), (-1, 100), (0, 0)])
def test_a_deepseek_call_without_both_valid_counts_is_not_repriced(tmp_path, prompt, completion):
    _ledger(tmp_path, calls=1, calls_without_cost=1, entries=[_deepseek_entry(prompt, completion)])
    costs = load_project_costs(tmp_path)
    assert costs.calls_without_cost == 1 and costs.estimated_calls == 0 and costs.total_usd == 0


def test_a_deepseek_call_with_both_counts_is_repriced(tmp_path):
    """Positive counterpart: both counts recorded -> an upper-bound estimate."""
    _ledger(tmp_path, calls=1, calls_without_cost=1, entries=[_deepseek_entry(1000, 100)])
    costs = load_project_costs(tmp_path)
    assert costs.calls_without_cost == 0 and costs.estimated_calls == 1 and costs.total_usd > 0


def test_call_cost_needs_both_counts_valid():
    assert deepseek_pricing.call_cost("deepseek-flash", {"prompt_tokens": 1000}) is None
    assert deepseek_pricing.call_cost("deepseek-flash", {"prompt_tokens": 1000,
                                                         "completion_tokens": -3}) is None
    assert deepseek_pricing.call_cost("deepseek-flash", {"prompt_tokens": 1000,
                                                         "completion_tokens": 0}) > 0


# ── B6: negative costs are missing; a run without its start has no date ────

def test_a_negative_entry_cost_counts_as_missing(tmp_path):
    _ledger(tmp_path, total_usd=0.2, calls=2, entries=[
        {"time": "", "model": "x/y", "cost_usd": -0.5, "prompt_tokens": 1, "completion_tokens": 1},
        {"time": "", "model": "x/y", "cost_usd": 0.2, "prompt_tokens": 1, "completion_tokens": 1}])
    [(total, calls, missing)] = load_project_costs(tmp_path).by_provider.values()
    assert total == pytest.approx(0.2) and calls == 2 and missing == 1


def test_a_run_without_a_start_record_shows_no_empty_date(line_factory, tmp_path):
    _ledger(tmp_path, total_usd=0.2, calls=1, run=3, entries=[
        {"time": "", "model": "x/y", "cost_usd": 0.2, "prompt_tokens": 1, "completion_tokens": 1,
         "run": 3}])
    line = line_factory(lambda: tmp_path, lambda _m: None)
    assert "iniziata il" not in line.toolTip()
    assert "Analisi corrente: 1 chiamate a pagamento" in line.toolTip()


def test_a_run_with_its_start_record_keeps_its_date(line_factory, tmp_path):
    """Positive counterpart."""
    start_run(tmp_path)
    line = line_factory(lambda: tmp_path, lambda _m: None)
    assert "Analisi corrente iniziata il " in line.toolTip()


# ── B7: the DeepSeek tooltip tells the truth ────────────────────────────────

def test_the_deepseek_tooltip_says_the_cost_is_computed_from_the_price_list(line_factory):
    line = line_factory(lambda: None, lambda _m: None)
    line.provider = providers.PROVIDER_DEEPSEEK
    line.refresh()
    assert "non riporta il costo" not in line.toolTip()
    assert "listino ufficiale" in line.toolTip()
    assert deepseek_pricing.PRICES_CHECKED in line.toolTip()


# ── B8: a call belongs to the analysis of the moment it was made ────────────

def test_two_analyses_interleaved_with_a_queued_call(line_factory, tmp_path, monkeypatch):
    busy = _Busy(project_costs.add_call)
    monkeypatch.setattr(account_module, "add_call", busy)
    line = line_factory(lambda: tmp_path, lambda _m: None)
    line.start_analysis(tmp_path)                          # run 1, this instance
    line.bind_run_project(tmp_path)
    openrouter.set_usage_listener(line._listener)
    openrouter._record_usage({"model": "x/y", "usage": {"cost": 0.3}})   # stamped, queued
    assert len(line._pending) == 1
    start_run(tmp_path)                                    # run 2, another instance
    busy.busy = False
    line._retry_flush()
    [entry] = _entries(tmp_path)
    assert entry["run"] == 1                               # not the file's current run 2
    assert load_project_costs(tmp_path).run_calls == 0
    assert load_project_costs(tmp_path, run=1).run_calls == 1
    assert "0.3000" in line.text()                         # this instance shows its analysis


def test_a_call_without_an_analysis_stamp_goes_to_the_current_analysis(line_factory, tmp_path):
    """Negative / compatibility: old callers (no bound run) keep the file's current run."""
    line = line_factory(lambda: tmp_path, lambda _m: None)
    start_run(tmp_path)
    start_run(tmp_path)
    line._on_paid_call({"cost_usd": 0.1, "model": "m"})
    assert _entries(tmp_path)[0]["run"] == 2


def test_a_run_unknown_to_the_file_falls_back_to_the_current_one(tmp_path):
    start_run(tmp_path)
    add_call(tmp_path, {"cost_usd": 0.1, "run": 9})
    add_call(tmp_path, {"cost_usd": 0.1, "run": 1})
    assert [entry["run"] for entry in _entries(tmp_path)] == [1, 1]


# ── B9: a failed analysis start has its own message and is not a waiting call

def test_a_damaged_file_at_the_start_says_the_start_was_not_recorded(line_factory, tmp_path,
                                                                      monkeypatch):
    def damaged(*_a, **_k):
        raise ProjectCostsError("ai-costs.json: unknown format")
    monkeypatch.setattr(account_module, "start_run", damaged)
    log = []
    line = line_factory(lambda: tmp_path, log.append)
    line.start_analysis(tmp_path)
    assert any("Inizio dell'analisi non registrato" in entry for entry in log)
    assert not any("Costo di una chiamata" in entry for entry in log)


def test_a_queued_start_is_not_counted_among_waiting_calls(line_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(account_module, "start_run", _Busy(project_costs.start_run))
    log = []
    line = line_factory(lambda: tmp_path, log.append)
    line.start_analysis(tmp_path)
    assert line._waiting_calls() == 0
    assert not any("chiamate in attesa" in entry for entry in log)
    assert any("inizio dell'analisi" in entry for entry in log)
    assert "in attesa" not in line.text()


# ── B10: the time of the call, and answers lost after sending ───────────────

def test_a_late_write_keeps_the_time_of_the_call(line_factory, tmp_path, monkeypatch):
    busy = _Busy(project_costs.add_call)
    monkeypatch.setattr(account_module, "add_call", busy)
    line = line_factory(lambda: tmp_path, lambda _m: None)
    line._on_paid_call({"cost_usd": 0.1, "model": "m", "time": "2026-01-05T02:00:00+00:00"})
    busy.busy = False
    line._retry_flush()
    assert _entries(tmp_path)[0]["time"] == "2026-01-05T02:00:00+00:00"


class _Clock(datetime):
    moment = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.moment if tz is not None else cls.moment.replace(tzinfo=None)


@pytest.mark.parametrize("moment", [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc),
                                    datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc)])
def test_deepseek_is_priced_at_the_time_the_request_was_sent(monkeypatch, moment):
    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    monkeypatch.setattr(_Clock, "moment", moment)
    monkeypatch.setattr(openrouter, "datetime", _Clock)
    monkeypatch.setattr(providers, "deepseek_request", lambda *a, **k: {
        "usage": dict(usage), "choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"function": {"name": "report", "arguments": "{}"}}]}}]})
    seen = []
    openrouter.set_usage_listener(seen.append)
    openrouter.call_tool(DEEPSEEK_MODEL, "p", [], {"function": {"name": "report"}})
    expected = deepseek_pricing.call_cost("deepseek-flash", usage, moment)
    assert seen[0]["cost_usd"] == pytest.approx(expected)
    assert seen[0]["time"] == moment.isoformat(timespec="seconds")
    peak = deepseek_pricing.is_peak(moment)
    off = deepseek_pricing.call_cost("deepseek-flash", usage,
                                     datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc))
    assert seen[0]["cost_usd"] == pytest.approx(off * (2 if peak else 1))


def test_an_answer_lost_after_sending_is_a_call_without_cost(monkeypatch):
    monkeypatch.setenv(openrouter.API_KEY_ENV, KEY)

    def timeout(*_a, **_k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", timeout)
    seen = []
    openrouter.set_usage_listener(seen.append)
    with pytest.raises(openrouter.OpenRouterError) as caught:
        openrouter.call_tool("vendor/model", "p", [], {"function": {"name": "report"}})
    assert caught.value.possibly_billed
    assert len(seen) == 1 and seen[0]["cost_usd"] is None and seen[0]["model"] == "vendor/model"
    totals = openrouter.usage_totals()
    assert totals.calls == 1 and not totals.complete


def test_a_request_that_was_never_sent_is_not_counted(monkeypatch):
    """Negative: urllib wraps a connect timeout in URLError -- nothing billed."""
    monkeypatch.setenv(openrouter.API_KEY_ENV, KEY)

    def refused(*_a, **_k):
        raise urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", refused)
    seen = []
    openrouter.set_usage_listener(seen.append)
    with pytest.raises(openrouter.OpenRouterError) as caught:
        openrouter.call_tool("vendor/model", "p", [], {"function": {"name": "report"}})
    assert not caught.value.possibly_billed and not seen
    assert openrouter.usage_totals().calls == 0


@pytest.mark.parametrize("failure, billed", [(TimeoutError("timed out"), True),
                                             (urllib.error.URLError("refused"), False)])
def test_deepseek_marks_only_answers_lost_after_sending(monkeypatch, failure, billed):
    def fail(*_a, **_k):
        raise failure
    monkeypatch.setattr(providers.urllib.request, "urlopen", fail)
    with pytest.raises(providers.ProviderError) as caught:
        providers.deepseek_request(providers.DEEPSEEK_CHAT_URL, {"model": "x"}, key="k-test")
    assert caught.value.possibly_billed is billed
    monkeypatch.setattr(providers, "deepseek_request", lambda *a, **k: (_ for _ in ()).throw(
        caught.value))
    seen = []
    openrouter.set_usage_listener(seen.append)
    with pytest.raises(providers.ProviderError):
        openrouter.call_tool(DEEPSEEK_MODEL, "p", [], {"function": {"name": "report"}})
    assert len(seen) == (1 if billed else 0)


# ── B11: DeepSeek network errors carry only safe categories ─────────────────

@pytest.mark.parametrize("failure", [
    urllib.error.URLError("Tunnel connection failed: 407 http://user:s3cret@proxy.corp:8080"),
    urllib.error.URLError(OSError("getaddrinfo failed for private.example.org")),
    ConnectionResetError("reset by 10.1.2.3 via proxy.corp"),
])
def test_deepseek_network_errors_never_echo_urls_or_credentials(monkeypatch, failure):
    def fail(*_a, **_k):
        raise failure
    monkeypatch.setattr(providers.urllib.request, "urlopen", fail)
    with pytest.raises(providers.ProviderError) as caught:
        providers.deepseek_request(providers.DEEPSEEK_BALANCE_URL, key="k-test")
    text = str(caught.value)
    for secret in ("s3cret", "proxy.corp", "private.example", "10.1.2.3", "user:"):
        assert secret not in text
    assert "Connessione a DeepSeek fallita" in text


def test_a_deepseek_timeout_still_says_timeout(monkeypatch):
    """Positive: a known category survives the reduction."""
    def fail(*_a, **_k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(providers.urllib.request, "urlopen", fail)
    with pytest.raises(providers.ProviderError, match="timed out"):
        providers.deepseek_request(providers.DEEPSEEK_BALANCE_URL, key="k-test")


# ── costs_changed: emitted after every successful write, never after a failed one

def test_costs_changed_follows_the_writes(line_factory, tmp_path, monkeypatch):
    line = line_factory(lambda: tmp_path, lambda _m: None)
    emitted = []
    line.costs_changed.connect(lambda: emitted.append(1))
    line.start_analysis(tmp_path)
    assert len(emitted) == 1
    line._on_paid_call({"cost_usd": 0.1, "model": "m"})
    assert len(emitted) == 2
    busy = _Busy(project_costs.add_call)
    monkeypatch.setattr(account_module, "add_call", busy)
    line._on_paid_call({"cost_usd": 0.1, "model": "m"})
    assert len(emitted) == 2                               # nothing written, nothing emitted
    busy.busy = False
    line._retry_flush()
    assert len(emitted) == 3
