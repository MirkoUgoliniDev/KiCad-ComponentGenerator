"""@brief Bounded wait for the background QThreads when the application quits.

A QThread destroyed while it is still running aborts the whole process
(Qt's «Destroyed while thread is still running»), so the threads owned by
QApplication used to connect `aboutToQuit` straight to `QThread.wait`: no
deadline, and a window closed during a slow HTTP read or a kicad-cli render
left an invisible process alive for up to a minute (super review 2026-11-16,
R4-5).

Here the exit first asks each thread to stop (when it has a way), then waits
for it within ONE exit deadline shared by every thread: the first exit hook
sets it QUIT_WAIT_MS ahead, and each later wait gets only what remains, so N
threads never cost N x QUIT_WAIT_MS. A thread still running after its wait
is taken away from QApplication and left with no owner: nothing destroys it
while it runs, and the end of the process ends it. Same rule as
`component_check_dialog._CheckWorker._stop_for_quit` and
`photo_review_dialog`'s candidate search.

`is_quitting()` tells, from any thread, that this exit has begun: a thread
abandoned after that point may still raise on its way out, and the crash
report must not take that for a crash of the program.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from PyQt6 import sip
from PyQt6.QtWidgets import QApplication

#: Longest time the application exit spends waiting for ALL its background
#: threads together, ms: the budget of one component check or candidate search.
QUIT_WAIT_MS = 5000

#: Attribute of the thread holding its exit hook, so that it can be dropped.
_HOOK = "_kicadgen_quit_hook"

#: Clock of the exit deadline; a module attribute so that tests can drive it.
_clock = time.monotonic

#: Guards the first setting of `_deadline` (two hooks may race to set it).
_lock = threading.Lock()
#: Monotonic time by which the exit stops waiting, None before the exit began.
_deadline: Optional[float] = None
#: True once the exit began; a plain bool, so reading it is thread-safe.
_quitting = False


def is_quitting() -> bool:
    """@brief Whether the application exit has begun (a quit hook or `stop_and_wait` ran).

    @return True from the first exit hook on; never goes back to False.
    @note Safe to call from any thread: it reads one module-level bool.
    """
    return _quitting


def begin_exit(budget_ms: int = QUIT_WAIT_MS) -> int:
    """@brief Mark the exit as begun and return what is left of the shared exit deadline, ms.

    @param budget_ms Budget of the whole exit, used only by the FIRST caller,
        which fixes the deadline `budget_ms` from now; later callers only read it.
    @return Milliseconds left before the deadline, never negative.
    @note Every exit hook of the program goes through here (the ones of
        `connect_quit_wait` and the dialogs' own hooks), so the whole exit is
        bounded by one deadline and `is_quitting()` turns on with the first.
    """
    global _deadline, _quitting
    with _lock:
        _quitting = True
        if _deadline is None:
            _deadline = _clock() + budget_ms / 1000.0
        deadline = _deadline
    return max(0, int((deadline - _clock()) * 1000))


def stop_and_wait(thread, stop: Optional[Callable[[], None]] = None,
                  timeout_ms: int = QUIT_WAIT_MS) -> bool:
    """@brief Ask `thread` to stop, wait a bounded time, then let it go.

    @param thread QThread still owned by the application.
    @param stop Callable that asks the thread's work to end (an event, a
        process kill), or None when the work cannot be interrupted.
    @param timeout_ms Longest wait for this thread, ms (0 only checks it).
    @return True when the thread had finished within the wait; False when it
        was left running with no owner for the process exit to end.
    @note Marks the exit as begun (`is_quitting`): the thread may be abandoned.
    """
    global _quitting
    _quitting = True
    if stop is not None:
        try:
            stop()
        except Exception:  # noqa: BLE001 - a failed stop must not skip the bounded wait
            pass
    try:
        if thread.wait(max(0, int(timeout_ms))):
            return True
        thread.setParent(None)
        sip.transferto(thread, None)
    except RuntimeError:
        # The C++ thread is already gone: nothing is running any more.
        return True
    return False


def connect_quit_wait(thread, stop: Optional[Callable[[], None]] = None,
                      timeout_ms: int = QUIT_WAIT_MS) -> None:
    """@brief At application exit, stop `thread` and wait for it within the shared exit deadline.

    @param thread QThread owned by QApplication.
    @param stop See `stop_and_wait`.
    @param timeout_ms Exit budget used when this hook is the first to run, ms;
        a later hook waits only for what remains of the deadline already set.
    @note Pair it with `disconnect_quit_wait` once the thread has finished:
        left connected, the hook would reach a thread already deleted.
    """
    application = QApplication.instance()
    if application is None:
        return

    def on_quit():
        """Exit hook: the bounded stop of this one thread, within the shared deadline."""
        remaining = begin_exit(timeout_ms)
        stop_and_wait(thread, stop, remaining)

    setattr(thread, _HOOK, on_quit)
    application.aboutToQuit.connect(on_quit)


def disconnect_quit_wait(thread) -> None:
    """@brief Drop the exit hook of `thread`; harmless when there is none.

    @param thread QThread passed to `connect_quit_wait`.
    """
    hook = getattr(thread, _HOOK, None)
    if hook is None:
        return
    setattr(thread, _HOOK, None)
    application = QApplication.instance()
    if application is not None:
        try:
            application.aboutToQuit.disconnect(hook)
        except TypeError:
            pass  # already disconnected
