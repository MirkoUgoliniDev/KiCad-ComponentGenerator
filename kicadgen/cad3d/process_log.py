"""@brief Stream CAD subprocess output to a durable, readable live log."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
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


def select_descendants(pid: int, children: dict, creation_time, root_born=None,
                       root_exited=None) -> list:
    """@brief Processes genuinely started, directly or not, by `pid`, deepest first.

    @param pid Root of the tree (the child this program launched and still owns).
    @param children Mapping parent pid -> list of child pids, as the operating
        system reports it (Windows: the Toolhelp snapshot).
    @param creation_time Callable pid -> comparable creation stamp, or None
        when the process cannot be inspected.
    @param root_born Creation stamp of the root read from the handle this
        program still holds; None = ask @p creation_time (live root only).
    @param root_exited Exit stamp of the root when it has already exited, or
        None while it runs. With it, a direct child is kept only if it was
        created BEFORE the root exited: once the root is gone its pid can be
        reused, and the children of the new holder of that pid are born after
        the root's exit, so they can never pass (2026-11-03 review).
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
    if root_born is None:
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
                if parent == pid and root_exited is not None and born > root_exited:
                    # Born after our root died: a child of whatever process
                    # reused the root's pid, not ours. Never killed.
                    continue
                found.append(child)
                nxt.append((child, born))
        frontier = nxt
    return list(reversed(found))


def _windows_handle_times(handle):
    """(creation, exit) FILETIME integers of a process handle, or None.

    The handle is the one `subprocess.Popen` keeps open for its child: it
    stays valid after the child exits, so the root's stamps survive the root.
    The exit stamp is None while the process runs (GetProcessTimes reports 0).
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    ok = kernel32.GetProcessTimes(wintypes.HANDLE(int(handle)), ctypes.byref(creation),
                                  ctypes.byref(exited), ctypes.byref(kernel),
                                  ctypes.byref(user))
    if not ok:
        return None
    born = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    died = (exited.dwHighDateTime << 32) | exited.dwLowDateTime
    return born, (died or None)


def _windows_descendants(pid: int, handle=None) -> list:
    """Process ids started, directly or not, by `pid`, deepest first, verified by creation time.

    With the root's own handle the tree is found even after the root exited
    (its orphans still name its pid as parent), bounded by the root's exit.
    Best effort only: a descendant whose own parent also exited is no longer
    linked to the root by the snapshot; the job of `contain_process` is what
    reaches it.
    """
    times = _windows_handle_times(handle) if handle is not None else None
    if times is None or times[1] is None:
        # A live root (or no handle): its pid is still its own, so the
        # stamp read by pid is the root's, exactly as before.
        return select_descendants(pid, _windows_children_map(), _windows_creation_time)
    return select_descendants(pid, _windows_children_map(), _windows_creation_time,
                              root_born=times[0], root_exited=times[1])


class _JobLimits:
    """Lazily built ctypes layout of JOBOBJECT_EXTENDED_LIMIT_INFORMATION."""

    _type = None

    @classmethod
    def type(cls):
        """Return the structure type, built on first use (Windows only)."""
        if cls._type is None:
            import ctypes
            from ctypes import wintypes

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IoCounters),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            cls._type = Extended
        return cls._type


#: Attribute under which `contain_process` stores the job handle on the Popen.
_JOB_ATTRIBUTE = "_kicadgen_job"


def contain_process(process, platform: str = sys.platform):
    """@brief Put a just-started child in a Windows Job Object that dies with it.

    @param process `subprocess.Popen` just returned (a test double without a
        real Windows handle is left alone).
    @param platform Platform name (tests pass another one).
    @return The job handle, or None when no job could be made (another
        platform, a double, the system refused): the caller then relies on
        `kill_process_tree`'s snapshot, as before.
    @note Why (2026-11-03 review): the snapshot can only find descendants
        while their links can be proved, and a grandchild that outlived the
        child kept the output pipe open, so nothing ended the run. Every
        process the child starts after this call is a member of the job,
        whatever happens to its parent, and `TerminateJobObject` ends them
        all. The job is created with KILL_ON_JOB_CLOSE, so closing it
        (`release_process`, or this program dying) ends whatever is left.
    @note DECLARED LIMIT: a process the child started BEFORE the assignment
        (a few microseconds after creation) is not in the job; the snapshot
        kill still covers it.
    """
    handle = getattr(process, "_handle", None)
    if platform != "win32" or handle is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _JobLimits.type()()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = (kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9,  # extended limits
                                               ctypes.byref(limits), ctypes.sizeof(limits))
              and kernel32.AssignProcessToJobObject(wintypes.HANDLE(job),
                                                    wintypes.HANDLE(int(handle))))
        if not ok:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    try:
        setattr(process, _JOB_ATTRIBUTE, job)
    except AttributeError:
        pass
    return job


def _terminate_job(process) -> bool:
    """End every process of the child's job; True when a job existed and was told to end."""
    job = getattr(process, _JOB_ATTRIBUTE, None)
    if not job:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(kernel32.TerminateJobObject(wintypes.HANDLE(job), 1))
    except (OSError, AttributeError, ValueError):
        return False


def release_process(process) -> None:
    """@brief Close the child's job handle: with KILL_ON_JOB_CLOSE, any process left in it ends.

    @param process `subprocess.Popen` passed to `contain_process` (idempotent;
        a process without a job is a no-op).
    @note Called when the caller is done with the child. A descendant that
        outlived a finished FreeCAD child is a leftover of that job, never a
        process the user started, so ending it is the intended effect.
    """
    job = getattr(process, _JOB_ATTRIBUTE, None)
    if not job:
        return
    try:
        setattr(process, _JOB_ATTRIBUTE, None)
    except AttributeError:
        pass
    try:
        import ctypes
        from ctypes import wintypes
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(job))
    except (OSError, AttributeError, ValueError):
        pass


def child_environment(extra=None) -> dict:
    """@brief Environment of every FreeCAD child: os.environ plus PYTHONDONTWRITEBYTECODE=1.

    @param extra Optional mapping of further variables for this child.
    @return A new dict; os.environ is not modified.
    @note The child imports scripts from the installation folder, which is
        read-only for an installed program: no __pycache__ may be written next
        to them. One definition for `run_logged` and the other launchers.
    """
    environment = dict(os.environ)
    environment.update(extra or {})
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


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
    @note Never raises for a process that already exited. Each descendant is
        kept only when its creation time proves it was started by its claimed
        parent (`select_descendants`): a pid reused by an unrelated program
        is never terminated.
    @note 2026-11-03 review: a child that already exited no longer means
        "nothing to kill". Its job (`contain_process`) is terminated whatever
        the child's state, and without a job the descendants are still
        looked for through the handle Popen keeps, bounded by the child's
        exit time, so a grandchild holding the output pipe is ended.
    """
    if platform == "win32":
        _terminate_job(process)
        try:
            handle = getattr(process, "_handle", None)
            if process.poll() is None or handle is not None:
                for pid in _windows_descendants(process.pid, handle):
                    _windows_terminate(pid)
        except (OSError, AttributeError, ValueError, TypeError):
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


#: Seconds a killed child is waited for before it is abandoned. A
#: TerminateProcess the system refuses (process stuck in a kernel call) must
#: not hang the caller -- and the cancel that asked for the kill -- forever
#: (2026-10-27 review, H9; 2026-10-29 review, H2). A bound on the reaping,
#: not on the work.
KILL_REAP_GRACE_S = 5.0

#: Seconds the output of a child that has ALREADY exited is still read while
#: the pipe stays open (2026-11-03 review). Only a descendant that inherited
#: the pipe can keep it open after the child's exit; what the child itself
#: wrote is already in the pipe buffer and arrives at once, so the grace only
#: pays for scheduling, and it restarts at every line that still arrives. It
#: is not a timeout of the work and not tuned on a run: without it the read
#: never ended (the watchdog cannot fire for a process that is gone), and a
#: later forced end reported a «no output» timeout for a child that had
#: finished, so `_run_freecad` threw away a healthy STEP.
EXIT_DRAIN_GRACE_S = 2.0

#: Size above which a run log is trimmed when a new run starts, and the tail
#: kept (2026-10-29 review, H9). `<project>/freecad.log` is appended to by
#: every 3D generation and was never trimmed. A run writes one line per
#: child output line, heartbeats included (one every 60 s): tens of
#: kilobytes even for a large module, so the kept 2 MiB still hold dozens of
#: recent runs, which is what a diagnosis or an issue report needs. The
#: numbers are a disk budget, not a measurement of any particular run.
LOG_CAP_BYTES = 8 * 1024 * 1024
LOG_KEEP_BYTES = 2 * 1024 * 1024


def cap_log(log_path, limit: int = LOG_CAP_BYTES, keep: int = LOG_KEEP_BYTES) -> bool:
    """@brief Trim a log that grew past @p limit, keeping its last @p keep bytes.

    @param log_path Log file about to be appended to.
    @param limit Size in bytes above which the file is trimmed.
    @param keep Bytes of the tail to keep; the cut moves forward to the next
        line start, so no half line survives.
    @return True when the file was trimmed.
    @note Atomic: the tail is written to a sibling temporary file that then
        replaces the log with `os.replace`, so a failure (the file open in
        another program, a full disk) leaves the whole log, never a truncated
        one. A failure is logged and never raised: the run goes on appending.
    """
    path = Path(log_path)
    temporary = None
    try:
        size = path.stat().st_size
        if size <= limit:
            return False
        with path.open("rb") as handle:
            handle.seek(max(0, size - keep))
            tail = handle.read()
        cut = tail.find(b"\n")
        tail = tail[cut + 1:] if cut >= 0 else tail
        note = (f"[log trimmed: {size - len(tail)} bytes of older runs removed, "
                f"limit {limit} bytes]\n").encode("utf-8")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("xb") as handle:
            handle.write(note + tail)
        os.replace(temporary, path)
        temporary = None
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logging.getLogger(__name__).warning("log %s not trimmed: %s", path, exc)
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def reap_killed_child(process, grace_s: float = KILL_REAP_GRACE_S) -> bool:
    """@brief Wait for a child that was just killed, with a bound.

    @param process `subprocess.Popen` whose tree was just terminated.
    @param grace_s Seconds to wait before abandoning it.
    @return True when the child exited within the grace, False when it is
        still alive and has been abandoned (logged).
    """
    try:
        process.wait(timeout=grace_s)
        return True
    except subprocess.TimeoutExpired:
        logging.getLogger(__name__).warning(
            "child pid %s did not exit within %.1f s after the kill; left behind",
            getattr(process, "pid", "?"), grace_s)
        return False


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
    @param env Environment of the child; None means `child_environment()`, a
        copy of os.environ with PYTHONDONTWRITEBYTECODE=1, so the FreeCAD
        child never writes __pycache__ next to scripts installed read-only.
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
    @note The run ends with the CHILD, not with the pipe (2026-11-03 review):
        a descendant holding the inherited pipe used to block the reading
        forever, then let the watchdog raise a timeout for a child that had
        exited with its success marker. Now, once the child has exited and
        `EXIT_DRAIN_GRACE_S` passed without output, the reading stops, the
        survivors are ended (`contain_process` job, then the tree) and the
        child's own exit code is returned. Watchdog and cancel are checked
        by this thread every `CANCEL_POLL_INTERVAL_S`, whatever the pipe does.
    """
    lines = []
    timed_out = threading.Event()
    cancelled = threading.Event()
    stragglers = False
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cap_log(log_path)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] FreeCAD start\n")
        handle.flush()
        # NOT a context manager (2026-10-29 review, H2): Popen.__exit__ waits
        # for the child without a timeout, which after a refused kill hung
        # the caller for good. The pipe is closed and the child reaped in the
        # `finally` below, with a bound on every kill path.
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace",
                                   creationflags=creationflags, bufsize=1,
                                   env=child_environment() if env is None else env)
        # Every process the child starts from now on dies with it.
        contain_process(process)

        def kill_child():
            """The only place where the child process is killed: by the watchdog and on cancellation."""
            kill_process_tree(process)

        # The pipe is read by a helper thread, never by this one (2026-11-03
        # review): a descendant that inherited the pipe keeps it open after
        # the child exits, and a blocking read here would then never return
        # -- no watchdog, no cancel. This thread only waits on the queue with
        # a bound, so it always sees the child's exit, the silence and the
        # give-up.
        incoming = queue.Queue()
        end_of_output = object()

        def pump():
            try:
                for text in process.stdout:
                    incoming.put(text)
            except (OSError, ValueError):
                pass
            finally:
                incoming.put(end_of_output)

        reader = threading.Thread(target=pump, daemon=True, name="kicadgen-child-output")
        reader.start()
        try:
            if cancel is not None and cancel.is_set():
                # Give-up arrived BEFORE reading the first line (the caller
                # had already started): kill right away.
                cancelled.set()
                kill_child()
            deadline = time.monotonic() + timeout
            exited_at = None
            while not (cancelled.is_set() or timed_out.is_set()):
                try:
                    line = incoming.get(timeout=CANCEL_POLL_INTERVAL_S)
                except queue.Empty:
                    line = None
                if line is end_of_output:
                    break
                now = time.monotonic()
                if line is not None:
                    # Every line is proof that the process is working: the
                    # silence count restarts from zero.
                    deadline = now + timeout
                    if exited_at is not None:
                        exited_at = now
                    lines.append(line)
                    handle.write(line)
                    handle.flush()
                    if progress and line.strip():
                        # The child's warnings arrive as template + values:
                        # they are translated here (see messages.render_child_line).
                        progress("FreeCAD: " + render_child_line(line.rstrip()))
                if cancel is not None and cancel.is_set():
                    cancelled.set()
                    kill_child()
                    break
                if line is not None:
                    continue
                if process.poll() is not None:
                    # The child is gone but the pipe is still open: only a
                    # descendant can hold it. What the child wrote is drained
                    # for a short grace, then the reading stops -- the run is
                    # over and its verdict is the child's own exit, never a
                    # «no output» timeout.
                    if exited_at is None:
                        exited_at = now
                    elif now - exited_at >= EXIT_DRAIN_GRACE_S:
                        stragglers = True
                        break
                    continue
                if now >= deadline:
                    timed_out.set()
                    kill_child()
            if timed_out.is_set() or cancelled.is_set():
                # Killed: reaped with a bound and abandoned if the kill was
                # refused (logged by the reaper), never waited for forever.
                reap_killed_child(process)
            elif stragglers:
                # The descendants left holding the pipe belong to this run:
                # they are ended (job, then the tree bounded by the child's
                # exit), so the reader thread sees the end of the pipe.
                kill_child()
                process.wait()
            else:
                process.wait()
        except BaseException:
            # Anything raised while reading -- a failing progress callback, a
            # log write error, KeyboardInterrupt -- must not leave a live
            # child behind: kill it first, then reap it with the bound.
            kill_child()
            reap_killed_child(process)
            raise
        finally:
            release_process(process)
            reader.join(timeout=2 * CANCEL_POLL_INTERVAL_S)
            if not reader.is_alive():
                # Closing a stream another thread is still reading would wait
                # on its lock: a pipe still held by a survivor (another
                # platform, a refused kill) is left to the daemon reader,
                # which ends with it.
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass
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
                     + (tx(" (uscita con l'output ancora aperto da processi discendenti, "
                           "terminati)") if stragglers else "")
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
