"""@brief Per-project lock file: which KiCadGen window has a project open (user decision 2026-11-16, R12-4).

Several KiCadGen windows may run at once; a project must not be edited by two
of them unknowingly (the last writer of `project_config.json` would win). A
window that opens a project writes a small lock file in the project folder
holding its process id, host name and process start time; it removes the file
when the project is closed, switched or the program exits.

A lock is LIVE when:

* it was written on this host and its process still runs with the same start
  time (a pid reused by another program after a crash does not count);
* it was written on another host (a shared or network folder) and is younger
  than `FOREIGN_LOCK_MAX_AGE_S`: another machine's processes cannot be
  inspected, so its lock is trusted for that long. DECLARED BOUND, not a
  measure: the owning window refreshes the lock at every project save
  (`ProjectLock.refresh`), so only a window left idle on another host for
  longer than the bound reads as stale.

Anything else (a crashed window, an unreadable or damaged file) is STALE and
is simply replaced. The lock is advisory: the user may open a project locked
elsewhere anyway, after a question.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: File name of the lock, inside the project folder.
LOCK_FILE_NAME = ".kicadgen-project.lock"
#: How long a lock written on ANOTHER host is trusted, in seconds (12 hours).
#: Declared bound (see the module note): this host cannot see that host's processes.
FOREIGN_LOCK_MAX_AGE_S = 12 * 3600


def _host() -> str:
    """The name of this machine, lower case ('' when it cannot be read)."""
    try:
        return socket.gethostname().strip().lower()
    except OSError:
        return ""


#: Start stamp of a process that runs but cannot be inspected (another
#: account, an elevated process): alive, start time unknown.
STARTED_UNKNOWN = -1
#: How far in the future a foreign lock's time may be before it reads as
#: stale (clock skew between hosts), in seconds. Declared bound: 10 minutes.
FOREIGN_CLOCK_SKEW_S = 600


def _windows_process_started(pid: int) -> Optional[int]:
    """Creation FILETIME of a live process; STARTED_UNKNOWN when access is denied; None when gone.

    OpenProcess failing with ERROR_ACCESS_DENIED means the process exists
    but belongs to another account or runs elevated (review F, 7): it is
    alive. Any other failure (ERROR_INVALID_PARAMETER: no such pid) is gone.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return STARTED_UNKNOWN if ctypes.get_last_error() == 5 else None   # 5 = ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:
            return None                                        # exited (259 = STILL_ACTIVE)
        creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return STARTED_UNKNOWN
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _process_started(pid: int) -> Optional[int]:
    """Start stamp of a live process of this host; STARTED_UNKNOWN when alive but unreadable; None when gone."""
    if sys.platform == "win32":
        try:
            return _windows_process_started(int(pid))
        except (OSError, ValueError, AttributeError):
            return None
    try:
        os.kill(int(pid), 0)          # POSIX: signal 0 only checks existence
    except (OSError, ValueError):
        return None
    # No portable start time: a live pid counts with a neutral stamp.
    return 0


def _own_info() -> dict:
    """The lock content of this process: pid, host, start stamp and the time it is written."""
    pid = os.getpid()
    return {"pid": pid, "host": _host(), "started": _process_started(pid), "since": time.time()}


def lock_path(folder) -> Path:
    """@brief The lock file of a project folder.

    @param folder The project folder.
    @return `folder / LOCK_FILE_NAME`.
    """
    return Path(folder) / LOCK_FILE_NAME


def read_lock(folder) -> Optional[dict]:
    """@brief The lock written in a project folder, or None when there is none or it is unreadable.

    @param folder The project folder.
    @return The decoded dict with an int `pid` and a str `host`; None for a
        missing, unreadable or malformed file (a malformed lock is stale).
    """
    try:
        data = json.loads(lock_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or isinstance(data.get("pid"), bool)
            or not isinstance(data.get("pid"), int) or not isinstance(data.get("host"), str)):
        return None
    return data


def is_own(info: Optional[dict]) -> bool:
    """@brief True when a lock was written by this very process.

    @param info A lock (`read_lock`), or None.
    @return Same host, same pid and same start stamp.
    """
    own = _own_info()
    return (isinstance(info, dict) and info.get("host") == own["host"]
            and info.get("pid") == own["pid"] and info.get("started") == own["started"])


def lock_is_live(info: Optional[dict], now: Optional[float] = None) -> bool:
    """@brief Whether a lock still belongs to a running KiCadGen window (see the module note).

    @param info A lock (`read_lock`), or None.
    @param now Current time in seconds (default `time.time()`), for tests.
    @return True for a live lock; False for None or a stale one.
    """
    if not isinstance(info, dict):
        return False
    if info.get("host") != _host():
        since = info.get("since")
        if isinstance(since, bool) or not isinstance(since, (int, float)):
            return False
        age = (time.time() if now is None else now) - float(since)
        # A time far in the future would keep the lock live forever (review
        # F, 6): beyond the declared clock skew it is stale.
        return -FOREIGN_CLOCK_SKEW_S <= age <= FOREIGN_LOCK_MAX_AGE_S
    started = _process_started(info["pid"])
    if started is None:
        return False
    recorded = info.get("started")
    # Alive but not inspectable (another account): it cannot be told from a
    # reused pid, so it counts as live (the user is asked, never blocked).
    if started == STARTED_UNKNOWN:
        return True
    # A recorded stamp that differs is a pid reused by another process.
    return recorded is None or recorded == started


def held_elsewhere(folder) -> Optional[dict]:
    """@brief The live lock of ANOTHER window on a project folder, or None.

    @param folder The project folder.
    @return The lock dict when it is live and not this process's; None otherwise.
    """
    info = read_lock(folder)
    if info is None or is_own(info) or not lock_is_live(info):
        return None
    return info


@dataclass
class ProjectLock:
    """@brief The lock this window holds on one project folder."""

    folder: Path

    def refresh(self) -> bool:
        """@brief Rewrite the lock with the current time (the heartbeat for other hosts).

        @return True when written; False when the folder refused it, or the
            file now belongs to another window (it is then left alone).
        @note A missing file is re-taken: after «Apri comunque» the other
            window overwrote the lock and removed it when it closed, while
            this window still has the project open (review F, 10). So is a
            lock of another window that is no longer live (it crashed:
            review H, 2). Between the other window's exit and this window's
            next refresh (at most the window's heartbeat, 10 minutes) the
            project reads as free: declared, accepted gap.
        """
        current = read_lock(self.folder)
        if current is not None and not is_own(current) and lock_is_live(current):
            return False
        return _write(self.folder)

    def release(self) -> bool:
        """@brief Remove the lock file, only when it is still this process's.

        @return True when nothing of ours is left; False when the file could
            not be removed. A lock another window took over is left in place.
        """
        current = read_lock(self.folder)
        if current is not None and not is_own(current):
            return True
        try:
            lock_path(self.folder).unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True


def _write(folder) -> bool:
    """Write this process's lock atomically into `folder`; False on an OS error."""
    from ..fsutil import atomic_write_bytes
    try:
        atomic_write_bytes(lock_path(folder), json.dumps(_own_info()).encode("utf-8"))
    except OSError:
        return False
    return True


def acquire(folder) -> Optional[ProjectLock]:
    """@brief Take (or take over) the lock of a project folder for this window.

    @param folder The project folder; it must exist.
    @return The ProjectLock, or None when the file could not be written (a
        read-only folder: the project still opens, unlocked).
    @note Never refuses: the caller asked the user first when another live
        window held it (`held_elsewhere`); an older lock is replaced.
    """
    return ProjectLock(Path(folder)) if _write(folder) else None
