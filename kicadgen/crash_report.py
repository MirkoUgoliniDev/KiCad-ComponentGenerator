"""@brief Write a text report for every unhandled exception of the program.

`install(log_dir_provider)` replaces `sys.excepthook` and
`threading.excepthook`. On an unhandled exception it writes one plain-text
file in ``<log dir>/crash-reports/`` (timestamp, program version, Python,
Qt/PyQt, operating system, traceback), keeps only the newest reports, and
then calls the hook that was installed before, so the default printing (or
any other handler) still happens.

@note Lives at the package root, next to `app_paths` and `fsutil`, and not
    in `kicadgen.services`: it is process-wide infrastructure that the
    launcher installs before the GUI and the services are imported, and it
    depends on the standard library only.
@note Privacy: the report never dumps the environment and never includes
    local variables. The user's home folder in paths is replaced by "~", any
    other ``Users\\<name>`` / ``/home/<name>`` segment by ``<user>``, and
    OpenRouter-style API keys (plus the value of ``OPENROUTER_API_KEY``, read
    alone, never listed) are masked, because the user is invited to attach
    the file to a public GitHub issue.
@note The hooks never raise: a failure while writing the report is
    swallowed, and the previous hook is always called.
"""
from __future__ import annotations

import os
import platform
import re
import struct
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Union

#: Sub-folder of the log folder that holds the reports.
REPORTS_DIRNAME = "crash-reports"
#: Reports kept after each write; the oldest ones are deleted.
DEFAULT_MAX_REPORTS = 20
#: File name pattern: sortable timestamp down to the microsecond, then the
#: process id, so that the name order is the creation order. ``.log`` so
#: that Windows opens it with Notepad and the repository's ``*.log`` ignore
#: rule covers it.
REPORT_PREFIX = "crash-"
REPORT_SUFFIX = ".log"

PathProvider = Callable[[], Union[str, os.PathLike]]

_state: dict = {
    "installed": False,
    "provider": None,
    "max_reports": DEFAULT_MAX_REPORTS,
    "previous_sys": None,
    "previous_threading": None,
    "busy": False,
}

_KEY_PATTERN = re.compile(r"sk-or-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{20,}")
_USER_SEGMENT = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+|/home/|/Users/)[^\\/\s\"':]+")


def reports_dir(base: Union[str, os.PathLike]) -> Path:
    """@brief Folder of the crash reports under a log folder.

    @param base Log (or data) folder chosen by the application.
    @return ``base/crash-reports`` (not created here).
    """
    return Path(base) / REPORTS_DIRNAME


def scrub(text: str) -> str:
    """@brief Remove personal data from a report text.

    @param text Any text that may contain local paths or keys.
    @return The text with the home folder replaced by "~", other user folder
        names by "<user>" and API keys by "<redacted>".
    """
    result = str(text)
    secret = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if len(secret) >= 8:
        result = result.replace(secret, "<redacted>")
    result = _KEY_PATTERN.sub("<redacted>", result)
    try:
        home = str(Path.home())
    except (RuntimeError, OSError, KeyError):
        home = ""
    if home and len(home) > 3:
        for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
            result = re.sub(re.escape(variant) + r"(?=[\\/\s\"':]|$)", "~", result,
                            flags=re.IGNORECASE)
    return _USER_SEGMENT.sub(lambda m: m.group(1) + "<user>", result)


def _app_version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001 - a report without version beats no report
        return "unknown"


def _qt_versions() -> str:
    """Qt and PyQt versions, only when PyQt6 is already loaded (no import cost)."""
    core = sys.modules.get("PyQt6.QtCore")
    if core is None:
        return "not loaded"
    qt = getattr(core, "QT_VERSION_STR", "?")
    pyqt = getattr(core, "PYQT_VERSION_STR", "?")
    return f"Qt {qt}, PyQt {pyqt}"


def format_report(exc_type, exc_value, exc_tb, *, thread_name: Optional[str] = None,
                  now: Optional[datetime] = None) -> str:
    """@brief Build the text of one crash report.

    @param exc_type Exception class.
    @param exc_value Exception instance.
    @param exc_tb Traceback object (may be None).
    @param thread_name Name of the thread that raised, or None for the main hook.
    @param now Timestamp to write; defaults to the current local time.
    @return The scrubbed report text, ending with a newline.
    """
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    frozen = "installed executable" if getattr(sys, "frozen", False) else "from sources"
    lines = [
        "KiCadGen crash report",
        f"Time: {stamp}",
        f"KiCadGen: {_app_version()} ({frozen})",
        f"Python: {platform.python_version()} {platform.python_implementation()}"
        f" ({8 * struct.calcsize('P')}-bit)",
        f"Qt: {_qt_versions()}",
        f"OS: {platform.platform()}",
        f"Thread: {thread_name or threading.current_thread().name}",
        "",
        "Traceback:",
    ]
    body = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    return scrub("\n".join(lines) + "\n" + body.rstrip("\n") + "\n")


def _rotate(folder: Path, keep: int) -> None:
    """Delete the oldest reports so that at most `keep` remain."""
    reports = sorted(path for path in folder.glob(f"{REPORT_PREFIX}*{REPORT_SUFFIX}")
                     if path.is_file())
    for path in reports[:max(0, len(reports) - max(1, keep))]:
        try:
            path.unlink()
        except OSError:
            pass


def write_report(exc_type, exc_value, exc_tb, *, thread_name: Optional[str] = None,
                 base: Union[str, os.PathLike, None] = None) -> Optional[Path]:
    """@brief Write one report file and rotate the folder.

    @param exc_type Exception class.
    @param exc_value Exception instance.
    @param exc_tb Traceback object.
    @param thread_name Name of the raising thread, or None.
    @param base Log folder; defaults to the provider given to `install`.
    @return Path of the written report, or None when nothing could be written.
    @note Never raises.
    """
    try:
        if base is None:
            provider = _state["provider"]
            if provider is None:
                return None
            base = provider()
        folder = reports_dir(base)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = folder / f"{REPORT_PREFIX}{stamp}-{os.getpid()}{REPORT_SUFFIX}"
        counter = 1
        while target.exists():
            target = folder / f"{REPORT_PREFIX}{stamp}-{os.getpid()}-{counter}{REPORT_SUFFIX}"
            counter += 1
        text = format_report(exc_type, exc_value, exc_tb, thread_name=thread_name)
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(text)
        _rotate(folder, int(_state["max_reports"]))
        return target
    except Exception:  # noqa: BLE001 - the crash hook itself must never crash
        return None


def _should_report(exc_type) -> bool:
    """Interrupts and normal exits are not crashes."""
    try:
        return not issubclass(exc_type, (KeyboardInterrupt, SystemExit))
    except TypeError:
        return True


def _sys_hook(exc_type, exc_value, exc_tb) -> None:
    """Replacement of sys.excepthook: report, then chain."""
    if _should_report(exc_type) and not _state["busy"]:
        _state["busy"] = True
        try:
            write_report(exc_type, exc_value, exc_tb)
        finally:
            _state["busy"] = False
    previous = _state["previous_sys"] or sys.__excepthook__
    try:
        previous(exc_type, exc_value, exc_tb)
    except Exception:  # noqa: BLE001 - a broken previous hook must not escalate
        pass


def _threading_hook(args) -> None:
    """Replacement of threading.excepthook: report, then chain."""
    exc_type = getattr(args, "exc_type", None)
    if exc_type is not None and _should_report(exc_type):
        thread = getattr(args, "thread", None)
        write_report(exc_type, getattr(args, "exc_value", None),
                     getattr(args, "exc_traceback", None),
                     thread_name=getattr(thread, "name", None))
    previous = _state["previous_threading"] or threading.__excepthook__
    try:
        previous(args)
    except Exception:  # noqa: BLE001 - see _sys_hook
        pass


def install(log_dir_provider: PathProvider, *, max_reports: int = DEFAULT_MAX_REPORTS) -> None:
    """@brief Install the crash hooks for the whole process.

    @param log_dir_provider Callable returning the log folder; it is called
        at crash time, so a folder chosen after startup is honoured. The
        reports go to ``<folder>/crash-reports``.
    @param max_reports Reports kept after each write (at least 1).
    @note Idempotent: a second call only updates the provider and the limit,
        and the hook installed before the first call stays the one chained.
    @note Behaviour change to be aware of: PyQt6 aborts the process on an
        exception raised in a slot only while `sys.excepthook` is the default
        one. With these hooks installed the report is written and the event
        loop continues.
    """
    _state["provider"] = log_dir_provider
    _state["max_reports"] = max(1, int(max_reports))
    if _state["installed"]:
        return
    _state["previous_sys"] = sys.excepthook
    _state["previous_threading"] = threading.excepthook
    sys.excepthook = _sys_hook
    threading.excepthook = _threading_hook
    _state["installed"] = True


def uninstall() -> None:
    """@brief Restore the hooks that were active before `install`.

    @note Only restores a hook that is still ours: a hook installed later by
        someone else is left alone.
    """
    if not _state["installed"]:
        return
    if sys.excepthook is _sys_hook:
        sys.excepthook = _state["previous_sys"] or sys.__excepthook__
    if threading.excepthook is _threading_hook:
        threading.excepthook = _state["previous_threading"] or threading.__excepthook__
    _state.update(installed=False, provider=None, previous_sys=None,
                  previous_threading=None, max_reports=DEFAULT_MAX_REPORTS)


def latest_reports(limit: Optional[int] = None,
                   base: Union[str, os.PathLike, None] = None) -> list[Path]:
    """@brief List the crash reports, newest first.

    @param limit Maximum number of paths, or None for all.
    @param base Log folder; defaults to the provider given to `install`.
    @return Report paths; empty when there is no folder, no provider or an
        error occurs.
    """
    try:
        if base is None:
            provider = _state["provider"]
            if provider is None:
                return []
            base = provider()
        folder = reports_dir(base)
        if not folder.is_dir():
            return []
        reports = sorted((path for path in folder.glob(f"{REPORT_PREFIX}*{REPORT_SUFFIX}")
                          if path.is_file()), reverse=True)
        return reports if limit is None else reports[:max(0, int(limit))]
    except Exception:  # noqa: BLE001 - listing is informative only
        return []
