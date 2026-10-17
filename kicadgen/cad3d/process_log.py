"""@brief Stream CAD subprocess output to a durable, readable live log."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from ..services.messages import render_child_line, tx


def no_window_flags(platform: str = sys.platform) -> int:
    """@brief Creation flags that keep a child process from opening a console window.

    @param platform Value of `sys.platform` to decide for.
    @return CREATE_NO_WINDOW on Windows (the GUI process has no console, and
        each FreeCAD child would otherwise flash one), 0 elsewhere.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform == "win32" else 0


#: Creation flags of every FreeCAD child: one value for every launcher in
#: the package (solid build, model measurement).
NO_WINDOW_FLAGS = no_window_flags()


#: How many heartbeats must fit in one silence window.
#:
#: The watchdog below kills the child after `timeout` seconds without output.
#: For that criterion to stay honest, every long atomic step of the child must
#: beat faster than that; the interval must NOT be chosen separately, or the two
#: constants silently diverge and the original defect returns (a healthy
#: import killed at exactly 300 s). With 5 beats per window, FIVE beats must be
#: missed in a row -- not one -- for live work to be mistaken for stalled: it is
#: the margin that pays for the operating system scheduler, a congested pipe
#: and the cost of formatting the line. Raising the number narrows the error
#: margin on the single beat; lowering it fills the log with lines. It is not
#: calibrated on an observed component: it is a ratio, so it holds unchanged
#: if the timeout changes.
HEARTBEAT_BEATS_PER_TIMEOUT = 5

#: How often the give-up channel is checked while the child works.
#:
#: It is NOT a threshold calibrated on an observed case: it is the maximum delay
#: between the click on «Interrompi» (Stop) and the death of the child process,
#: i.e. a promise made to whoever is watching. The channel cannot be checked
#: only where the silence watchdog acts -- that fires after `timeout` seconds of
#: silence (300 s in this program), so on a mute child the interruption would
#: arrive five minutes after the request -- nor only at every line read,
#: because a silent child produces none. A tenth of a second is below the
#: threshold at which a person perceives a delay and costs ten wake-ups per
#: second of a sleeping thread: raising it makes the interruption noticeable,
#: lowering it does not make it more useful.
CANCEL_POLL_INTERVAL_S = 0.1


class ProcessCancelled(Exception):
    """@brief Raised when the child was killed because the caller asked to stop.

    @note Kept distinct from `subprocess.TimeoutExpired` ON PURPOSE: the
        watchdog kills a job that looks stuck (and that is a diagnosis, to be
        written in the log as such), while this one says that a person
        stopped a healthy job. Confusing them would mean writing «nessun output
        per 300 s» (no output for 300 s) under a button that was just pressed, i.e. blaming the
        program for a failure that never happened.
    """

    def __init__(self, command, output=""):
        """@brief Carry the command and the output collected before the kill.

        @param command Command line whose child was killed.
        @param output Text the child had already written, kept for the log.
        """
        super().__init__(tx("Esecuzione interrotta su richiesta."))
        self.cmd = command
        self.output = output


def heartbeat_interval(timeout) -> float:
    """@brief Derive the child's heartbeat period from this watchdog's own timeout.

    @param timeout Seconds of silence after which `run_logged` kills the child.
    @return Seconds between two heartbeat lines, strictly smaller than @p timeout.
    @throws ValueError When @p timeout is not a positive finite number.
    @note This function is the only place where the link between the two
        numbers exists. Whoever launches the child process must pass it THIS
        value (for KiCadGen: the `heartbeat_interval_s` field of the JSON
        parameter file), never rewrite the same number by hand in the child's
        script: two copies of the same constant drift apart at the first
        change and the watchdog goes back to killing healthy jobs.
    """
    seconds = float(timeout)
    if not (seconds > 0) or seconds in (float("inf"),):
        raise ValueError(f"timeout must be a positive finite number, got {timeout!r}")
    return seconds / HEARTBEAT_BEATS_PER_TIMEOUT


def run_logged(command, log_path: Path, progress=None, timeout=120, creationflags=0,
               cancel=None, env=None):
    """@brief Execute CAD, flushing each output line before notifying the GUI.

    @param command Command line of the child process, as accepted by
        `subprocess.Popen` (typically the FreeCAD launcher plus its script).
    @param log_path UTF-8 append-only diagnostic file for the component.
    @param progress Optional callable receiving one translated status string
        for each non-blank output line of the child; None disables the
        notifications.
    @param timeout Seconds of SILENCE after which the child is considered hung.
        The count restarts at every output line, so it is not a budget for
        the whole job.
    @param creationflags Process creation flags passed to `subprocess.Popen`
        (see `NO_WINDOW_FLAGS` to avoid a console window on Windows).
    @param cancel Cancellation channel: an object with `is_set()` and
        `wait(seconds)` (typically `threading.Event`) that the caller sets to
        ask for the job to end. None = nobody can stop it.
    @param env Environment of the child; None means a copy of os.environ with
        PYTHONDONTWRITEBYTECODE=1, so the FreeCAD child never writes
        __pycache__ next to scripts installed read-only.
    @return CompletedProcess with merged stdout/stderr.
    @throws subprocess.TimeoutExpired After killing and reaping a hung child.
    @throws ProcessCancelled After killing the child because @p cancel was set.
    @note Cancellation uses the SAME machinery as the watchdog -- the only
        place where this module kills the child -- because the two things are
        the same action with two different reasons; what changes is what gets
        written in the log and the exception raised. A child killed on
        request does NOT carry the line «nessun output per N s» (no output for
        N s): it was not found
        stuck, it was stopped.
    @note This used to be a limit on the overall duration, and on a real
        module it lost everything: the import of the first 3D model (an
        8.8 MB STEP) ate the 300 seconds on its own, and the process was
        killed before the second of fifty components. A single limit cannot
        tell apart the two things that matter -- a slow job and a stuck
        job -- and punishes precisely the large jobs, which are the ones
        where redoing everything costs the most.
    @note Silence is the right criterion ONLY as long as the premise holds:
        that the child announces what it is doing. On a real run the premise
        was false for one step only -- the import of a library model, which
        parses a large STEP without printing anything -- and that gap killed
        a healthy process at exactly 300 s, on the thirteenth component out
        of fifty-two. The fix was NOT to raise the timeout (a larger number
        would only be the same threshold tuned on the observed case, and it
        would delay the diagnosis of every job that is really stuck): it was
        to make the silent step speak. See `heartbeat_interval` and
        `freecad_parts._ImportHeartbeat`: now every long step beats, so
        silence means "stuck" again.
    @note There is no absolute cap: a job that makes progress is not
        interrupted for having progressed for a long time.
    """
    lines = []
    timed_out = threading.Event()
    cancelled = threading.Event()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] FreeCAD start\n")
        handle.flush()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              creationflags=creationflags, bufsize=1,
                              env=(dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
                                   if env is None else env)) as process:
            def kill_child():
                """The only place where the child process is killed: by the watchdog and on cancellation."""
                try:
                    process.kill()
                except OSError:
                    pass

            def expire():
                timed_out.set()
                kill_child()

            # The watcher thread exists only if someone can really
            # ask to stop, and it dies with the reading: it is tied to the
            # lifetime of this call, not of the program.
            reading_over = threading.Event()

            def watch_cancel():
                while not reading_over.is_set():
                    if cancel.wait(CANCEL_POLL_INTERVAL_S):
                        cancelled.set()
                        kill_child()
                        return

            watcher = None
            if cancel is not None:
                if cancel.is_set():
                    # Give-up arrived BEFORE reading the first line (the
                    # caller had already started): kill right away instead of
                    # waiting for the watcher's first round.
                    cancelled.set()
                    kill_child()
                else:
                    watcher = threading.Thread(target=watch_cancel, daemon=True)
                    watcher.start()
            timer = threading.Timer(timeout, expire)
            timer.daemon = True
            timer.start()
            try:
                for line in process.stdout:
                    # Every line is proof that the process is working: the
                    # silence count restarts from zero.
                    timer.cancel()
                    lines.append(line)
                    handle.write(line)
                    handle.flush()
                    if progress and line.strip():
                        # The child's warnings arrive as template + values:
                        # they are translated here (see messages.render_child_line).
                        progress("FreeCAD: " + render_child_line(line.rstrip()))
                    if timed_out.is_set() or cancelled.is_set():
                        break
                    timer = threading.Timer(timeout, expire)
                    timer.daemon = True
                    timer.start()
                process.wait()
            except BaseException:
                # Anything raised while reading -- a failing progress
                # callback, a log write error, KeyboardInterrupt -- leaves
                # through Popen.__exit__, which waits for the child: a child
                # still running would make that wait endless. Kill it first.
                kill_child()
                raise
            finally:
                timer.cancel()
                # Wake the watcher even when the reading ends on its
                # own: otherwise it would stay asleep on an already dead process.
                reading_over.set()
                if watcher is not None:
                    watcher.join(timeout=2 * CANCEL_POLL_INTERVAL_S)
            output = "".join(lines)
            handle.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                         f"FreeCAD exit={process.returncode}, timeout={timed_out.is_set()}"
                         f", cancelled={cancelled.is_set()}"
                         # The watchdog diagnosis is written only if IT was the one
                         # that killed the child. When the give-up has already
                         # arrived, the silence that follows is the effect
                         # of the kill, not its cause: announcing it
                         # would accuse healthy work of a hang.
                         + (tx(" (nessun output per {timeout} s)", timeout=timeout)
                            if timed_out.is_set() and not cancelled.is_set() else "")
                         + (tx(" (interrotto su richiesta dell'utente)")
                            if cancelled.is_set() else "")
                         + "\n")
            handle.flush()
            # The give-up comes BEFORE the watchdog: if the silence fired
            # right after the request, what really happened is that
            # someone pressed the button, and the log must not tell of a
            # hang that never happened.
            if cancelled.is_set():
                raise ProcessCancelled(command, output=output)
            if timed_out.is_set():
                raise subprocess.TimeoutExpired(command, timeout, output=output)
            return subprocess.CompletedProcess(command, process.returncode, output, "")
