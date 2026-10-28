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


def _windows_children_map() -> dict:
    """Parent pid -> child pids of every live process (Windows Toolhelp snapshot).

    The snapshot records the parent pid a process was STARTED with and never
    updates it: see `select_descendants` for why that alone cannot be trusted.
    """
    import ctypes
    from ctypes import wintypes

    class _Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    children = {}
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return children


def _windows_creation_time(pid: int):
    """Creation time of a live process as a FILETIME integer, or None.

    None means "could not be verified": the process cannot be opened (gone,
    or owned by another account) or GetProcessTimes refused. The caller treats
    None as "do not touch", never as "assume it is ours".
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                                      ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def select_descendants(pid: int, children: dict, creation_time) -> list:
    """@brief Processes genuinely started, directly or not, by `pid`, deepest first.

    @param pid Root of the tree (the child this program launched and still owns).
    @param children Mapping parent pid -> list of child pids, as the operating
        system reports it (Windows: the Toolhelp snapshot).
    @param creation_time Callable pid -> comparable creation stamp, or None
        when the process cannot be inspected.
    @return Pids to terminate, deepest first, so a launcher dies after the
        program it started.
    @note SAFETY BOUNDARY (2026-10-27 review, H1). Windows stores the parent
        pid a process was created with and never rewrites it when that parent
        exits. Pids are reused, so a process whose long-gone parent had pid P
        shows up as a child of whatever process holds P today -- possibly a
        descendant of the FreeCAD child we are about to kill. Killing by
        parentage alone could therefore terminate a FOREIGN process (the
        user's editor) on a cancel or a timeout. The physical criterion that
        tells the two apart is time: a real child is created AFTER its parent,
        while a process older than its claimed parent cannot have been started
        by it. Every candidate is kept only if its creation stamp is known and
        not earlier than its parent's; an unverifiable candidate (None) is
        skipped and its own claimed children are not explored, because their
        link to the tree cannot be proved either. The root itself must be
        verifiable: without its stamp nothing is selected and only the direct
        child is killed by the caller, as before the tree kill existed.
    """
    found = []
    root_born = creation_time(pid)
    if root_born is None:
        return []
    frontier = [(pid, root_born)]
    while frontier:
        nxt = []
        for parent, parent_born in frontier:
            for child in children.get(parent, []):
                if child == pid or child in found:
                    continue
                born = creation_time(child)
                if born is None or born < parent_born:
                    # Unverifiable, or older than its claimed parent: a
                    # pid-reuse orphan that is not ours. Never killed.
                    continue
                found.append(child)
                nxt.append((child, born))
        frontier = nxt
    return list(reversed(found))


def _windows_descendants(pid: int) -> list:
    """Process ids started, directly or not, by `pid`, deepest first, verified by creation time."""
    return select_descendants(pid, _windows_children_map(), _windows_creation_time)


def _windows_terminate(pid: int) -> None:
    """End one process by id; a process already gone is not an error."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if handle:
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)


def kill_process_tree(process, platform: str = sys.platform) -> None:
    """@brief Kill a child process and every process it started.

    @param process A `subprocess.Popen` still owned by the caller.
    @param platform Platform name (tests pass another one).
    @note `Popen.kill` ends only the direct child. On Windows a launcher
        (a venv `python.exe`, a FreeCAD wrapper) starts the real program as
        its own child, which then survived, kept the output file open and
        made the temporary folder undeletable (2026-10-17, WinError 32 in
        the suite). The descendants are found with a Toolhelp snapshot and
        ended deepest first, through the Windows API: no helper process is
        started (an external `taskkill` was itself a child to account for).
        Elsewhere only the direct child is killed, as before.
    @note Never raises for a process that already exited. The descendants
        are collected while the child is still alive, and each one is kept
        only when its creation time proves it was started by its claimed
        parent (`select_descendants`): a pid reused by an unrelated program
        is never terminated.
    """
    if platform == "win32" and process.poll() is None:
        try:
            for pid in _windows_descendants(process.pid):
                _windows_terminate(pid)
        except (OSError, AttributeError, ValueError):
            pass
    try:
        process.kill()
    except OSError:
        pass


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
                kill_process_tree(process)

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
