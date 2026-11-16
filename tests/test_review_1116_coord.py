"""@brief Coordinator integration tests of the 2026-11-16 fix round.

Each rule has a positive and a negative case: the integration wires a service
change (made by one fixer) into a caller owned by another.
"""

import threading

import pytest

from kicadgen.gui import workers
from kicadgen.services import openrouter


def _paid(cost):
    """Record one paid response of `cost` USD, as a provider call does."""
    openrouter._record_usage({"model": "v/m", "usage": {"cost": cost, "prompt_tokens": 1,
                                                         "completion_tokens": 1}})


class _FakeWorker:
    """Stand-in for a paid worker: the window sets `usage_scope` before start()."""

    def __init__(self, scope=None):
        if scope is not None:
            self.usage_scope = scope


def _run_on_thread(worker, cost):
    """Pay once on a new thread inside the worker's usage context, as run() does."""
    def body():
        with workers._usage_scope_of(worker):
            _paid(cost)
    thread = threading.Thread(target=body)
    thread.start()
    thread.join()


def test_a_paid_worker_with_a_scope_counts_its_calls_in_that_scope():
    """Positive: the worker thread enters the scope, so the run's own tally
    (what the finished card reads) sees the call (review R6-8 integration)."""
    openrouter.reset_usage()
    scope = openrouter.UsageScope()
    _run_on_thread(_FakeWorker(scope), 0.5)
    assert scope.totals().calls == 1
    assert scope.totals().cost_usd == pytest.approx(0.5)


def test_a_scope_not_entered_on_the_worker_thread_stays_empty():
    """Negative: a scope created by the window but not entered on the worker's
    thread sees nothing (the defect: a ContextVar does not cross threads), and a
    worker without a scope still feeds the process-wide tally."""
    openrouter.reset_usage()
    scope = openrouter.UsageScope()
    _run_on_thread(_FakeWorker(), 0.5)
    assert scope.totals().calls == 0
    assert openrouter.usage_totals().calls == 1


@pytest.mark.parametrize("cls", [workers.ExtractWorker, workers.ReconcileWorker,
                                 workers.RereadWorker])
def test_every_paid_worker_runs_its_job_inside_its_scope(monkeypatch, cls):
    """Positive: run() of each paid worker enters the scope before its job;
    negative twin inside: the job body itself is the unchanged `_run_job`."""
    entered = []

    class _Scope:
        """Records whether the job ran inside it."""

        def active(self):
            """Context that marks the job as scoped while it runs."""
            from contextlib import contextmanager

            @contextmanager
            def ctx():
                entered.append("in")
                yield
                entered.append("out")
            return ctx()

    ran = []
    worker = cls.__new__(cls)
    worker.usage_scope = _Scope()
    monkeypatch.setattr(cls, "_run_job", lambda self: ran.append(list(entered)))
    cls.run(worker)
    assert ran == [["in"]]
    assert entered == ["in", "out"]
