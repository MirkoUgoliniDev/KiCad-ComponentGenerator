"""@brief Project cost attribution and locking (audit D7, 2026-09-25).

No network: paid calls are simulated through `openrouter._record_usage`.
"""
import os
import threading
import time

import pytest

from kicadgen.services import openrouter, project_costs
from kicadgen.services.project_costs import COSTS_FILE, add_call, load_project_costs


@pytest.fixture(autouse=True)
def no_listener():
    openrouter.set_usage_listener(None)
    yield
    openrouter.set_usage_listener(None)


def _drain(app, seconds=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def _paid_call_in_worker(cost):
    thread = threading.Thread(target=openrouter._record_usage,
                              args=({"model": "m", "usage": {"cost": cost}},))
    thread.start()
    thread.join()


def test_a_bound_run_charges_its_own_project_even_after_a_switch(app, tmp_path):
    from kicadgen.gui.ai_account import AiAccountLine
    run_project, other = tmp_path / "A", tmp_path / "B"
    current = {"dir": run_project}
    line = AiAccountLine(lambda: current["dir"], lambda m: None)
    line.bind_run_project(run_project)
    _paid_call_in_worker(0.5)        # queued signal not delivered yet
    current["dir"] = other            # the user opens another project
    line.release_run_project()
    _drain(app)
    assert load_project_costs(run_project).calls == 1
    assert not (other / COSTS_FILE).exists()
    line.detach()


def test_without_a_bound_run_the_project_in_use_is_charged(app, tmp_path):
    """Negative: no binding keeps the former rule (project in use at delivery)."""
    from kicadgen.gui.ai_account import AiAccountLine
    first, second = tmp_path / "A", tmp_path / "B"
    current = {"dir": first}
    line = AiAccountLine(lambda: current["dir"], lambda m: None)
    _paid_call_in_worker(0.5)
    current["dir"] = second
    _drain(app)
    assert load_project_costs(second).calls == 1 and not (first / COSTS_FILE).exists()
    line.detach()


def test_a_run_bound_without_a_project_name_logs_every_dropped_cost(app, tmp_path):
    from kicadgen.gui.ai_account import AiAccountLine
    log = []
    line = AiAccountLine(lambda: tmp_path, log.append)
    line.bind_run_project(None)
    _paid_call_in_worker(0.1)
    _paid_call_in_worker(0.2)
    _drain(app)
    assert len([m for m in log if "non ha un nome" in m]) == 2
    assert not (tmp_path / COSTS_FILE).exists()
    line.detach()


def test_concurrent_writers_never_lose_a_call(tmp_path):
    per_thread, threads = 15, 4

    def writer():
        for _ in range(per_thread):
            while True:
                try:
                    add_call(tmp_path, {"cost_usd": 0.01})
                    break
                except OSError:
                    time.sleep(0.001)   # busy: the GUI would keep it pending
    workers = [threading.Thread(target=writer) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    costs = load_project_costs(tmp_path)
    assert costs.calls == per_thread * threads
    assert costs.total_usd == pytest.approx(0.01 * per_thread * threads)
    assert not (tmp_path / project_costs.LOCK_FILE).exists()


def test_a_held_lock_times_out_as_oserror(tmp_path, monkeypatch):
    monkeypatch.setattr(project_costs, "LOCK_TIMEOUT_S", 0.05)
    (tmp_path / project_costs.LOCK_FILE).write_text("other", encoding="utf-8")
    with pytest.raises(TimeoutError):
        add_call(tmp_path, {"cost_usd": 0.1})
    assert not (tmp_path / COSTS_FILE).exists()


def test_a_stale_lock_left_by_a_crash_is_removed(tmp_path):
    lock = tmp_path / project_costs.LOCK_FILE
    lock.write_text("dead", encoding="utf-8")
    old = time.time() - project_costs.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert add_call(tmp_path, {"cost_usd": 0.1}).calls == 1
    assert not lock.exists()
