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
    OpenRouter-style API keys (plus the values of the variables in
    SECRET_ENV_VARS, each read alone, never listed, plus the values the
    optional ``secrets`` callable of `install` returns) are masked, because
    the user is invited to attach the file to a public GitHub issue.
@note The hooks never raise: a failure while writing the report is
    swallowed, and the previous hook is always called.
@note Native crashes (Qt, VTK) bypass the hooks: `open_native_log` keeps a
    per-process faulthandler file that the next start turns into a report
    with `adopt_native_logs` (review 2026-11-16, R12-5).
"""
from __future__ import annotations

import faulthandler
import os
import platform
import re
import struct
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

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
#: Callable returning the secret values in use at crash time (API keys from
#: any source). It is called by `scrub` only, must not need the network and
#: may raise: a failure leaves the other masks in place.
SecretsProvider = Callable[[], Iterable[str]]
#: Shortest value worth masking by value: shorter strings would be found all
#: over an ordinary traceback and the report would become unreadable.
MIN_SECRET_LENGTH = 8

_state: dict = {
    "installed": False,
    "provider": None,
    "max_reports": DEFAULT_MAX_REPORTS,
    "previous_sys": None,
    "previous_threading": None,
    "busy": False,
    # Optional callable returning the secret values in use (see `install`).
    "secrets": None,
    # (open file, path, owned faulthandler, watch stop event) of this
    # process's native fault log, or None.
    "native": None,
    # Native logs found at this start whose process ended with neither an
    # exit nor a crash (a kill), see `sessions_ended_without_exit`.
    "unclean_sessions": 0,
}

#: Name prefix of a process's native fault log, in the reports folder. It is
#: NOT a report while its process runs (`latest_reports` never lists it):
#: `adopt_native_logs` turns the log of a process that died into one.
NATIVE_PREFIX = "native-"
#: Name suffix of a report made from a native fault log, before REPORT_SUFFIX.
NATIVE_REPORT_TAG = "-native"
#: Line written into a native log once the process is known to have gone on
#: after everything above it: what precedes it did not end the process.
NATIVE_CONTINUED = "-- KiCadGen: the process went on after the lines above --"
#: Same meaning as NATIVE_CONTINUED, written at an orderly end the application
#: saw (``aboutToQuit``, the session end): a log that ENDS with it was left by
#: a logoff or shutdown, not by a kill.
NATIVE_ORDERLY_END = "-- KiCadGen: orderly end of the session --"
#: Cancels the mark just before it (review G): written when the log grew by
#: more than the mark itself while the mark was being written. faulthandler
#: writes without this module's lock, so a dump that started meanwhile may
#: sit before, around or inside the mark, and the mark proves nothing.
NATIVE_MARK_VOID = "-- KiCadGen: the mark above was voided, another write overlapped it --"
#: File, in the reports folder, holding the name of the newest crash report
#: the user was already told about (`mark_crash_report_seen`). Its name does
#: not match the reports' pattern, so it is never listed or rotated.
SEEN_STAMP_NAME = ".last-seen-report"
#: Starts of the lines that announce an end of the process: a fatal Python
#: error (faulthandler on a signal or an abort), a Windows exception dumped
#: by faulthandler, a fatal Qt message (`main.qt_message_line`).
NATIVE_FATAL_STARTS = ("Fatal Python error", "Windows fatal exception", "Qt fatal:")
#: Seconds between two looks of the continuation watch at the log's size.
NATIVE_POLL_S = 5.0
#: Seconds the process must stay alive after the log last grew before what
#: it wrote is declared survived. HEURISTIC, declared as such: Windows gives
#: no portable signal that an exception faulthandler dumped was handled, so
#: survival is the evidence. A handled exception dumped less than this before
#: an external kill (Task Manager, shutdown) is still reported; a crash whose
#: process lingers longer than this (an error dialog left open while threads
#: still run) is missed. 30 s is far above the milliseconds between a fatal
#: dump and the end of the process, and short against a working session.
NATIVE_CONTINUATION_GRACE_S = 30.0
#: ids of the applications `watch_application` already connected.
_WATCHED_APPS: set = set()

#: Environment variables whose VALUE is masked wherever it appears, whatever
#: its shape: the ``sk-`` pattern below covers today's keys, but a provider
#: may change its key format without notice.
SECRET_ENV_VARS = ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")

_KEY_PATTERN = re.compile(r"sk-or-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{20,}")
#: Characters a path segment cannot hold: separators, whitespace, quotes,
#: the colon and the characters a Windows file name cannot hold.
_NAME_CHAR = r"[^\\/\s\"':<>|*?]"
#: One word of a user folder name. An ASCII apostrophe belongs to the name
#: only between two word characters (``O'Brien``); an apostrophe next to
#: anything else (``'/home/bob', 'x'``) closes a quoted string and is left
#: out, together with what follows it.
_WORD = _NAME_CHAR + r"+(?:(?<=\w)'(?=\w)" + _NAME_CHAR + r"+)*"
#: Words joined by single spaces and ending on a word («Mario Rossi»).
_SPACED = _WORD + r"(?: " + _WORD + r")+"
#: A user folder name in free text. A name with spaces is taken whole only
#: when a path separator follows it directly, i.e. when the text itself
#: proves it is a folder; otherwise only the run up to the first whitespace
#: is taken, so ``/home/bob and then opened`` keeps the rest of the line. A
#: sentence that merely runs into a later path (``bob and then opened /tmp``)
#: has a space before that separator and is not taken. The other proofs of
#: where a spaced name ends are handled by `_DELIMITED_USER`.
_SEGMENT = r"(?:" + _SPACED + r"(?=[\\/])|" + _WORD + r")"
#: Prefix of the user segment of a home path (Windows, Linux, macOS).
_HOME_PREFIX = r"(?:[A-Z]:[\\/]+Users[\\/]+|/home/|/Users/)"
#: Shares that hold one folder per user, by path convention (the same kind
#: of convention as ``C:\\Users\\<name>``): redirected homes and roaming
#: profiles. A UNC path on any other share (``\\\\fileserver\\Projects``)
#: names a place, not a person, and is left untouched.
_USER_SHARES = r"(?:Users|home|homes|profiles)"
#: Prefix ``\\\\host\\<user share>\\`` of a per-user UNC path. The host must
#: be a host name (letters, digits, ``.``, ``-``, ``_``, starting with a
#: letter or a digit), which excludes the ``\\\\.\\`` and ``\\\\?\\`` device
#: namespaces. Separators may be doubled (a repr). Host and share are kept:
#: they identify a machine, not a person.
_UNC_PREFIX = (r"(?:\\\\){1,2}[A-Za-z0-9][\w.-]*\\{1,2}" + _USER_SHARES
               + r"\\{1,2}")
#: A letter that is a word of its own, then a colon (``a:``, ``C:``).
_LONE_LETTER_COLON = r"(?<![^\W_])[^\W\d_]:"
#: Where a UNC path token may start: start of text, whitespace, a quote,
#: ``=``, ``(``, ``[``, ``,``, ``<``, or a colon that does not follow a
#: lone letter (``error:\\\\srv``). A doubled separator inside a path or a
#: source line (``"\\\\n\\\\t"``) has none of these before it and is not
#: read as UNC. A colon after a lone letter is handled by
#: `_UNC_AFTER_LETTER`.
_UNC_START = r"(?:^|(?<=[\s\"'=(\[,<])|(?<=:)(?<!" + _LONE_LETTER_COLON + r"))"
#: The same per-user UNC prefix with CONSISTENT separators: the leading run
#: is exactly twice the inner separator, as in raw text
#: (``\\\\srv\\home\\``, 2 then 1) or in a repr (4 then 2).
_UNC_PREFIX_CONSISTENT = (
    r"(?:\\\\[A-Za-z0-9][\w.-]*\\" + _USER_SHARES + r"\\"
    r"|\\\\\\\\[A-Za-z0-9][\w.-]*\\\\" + _USER_SHARES + r"\\\\)")
#: A per-user UNC path right after a lone letter and a colon (``a:``,
#: ``x:`` in prose or a label, or a drive letter). The letter alone cannot
#: tell a label from a drive, the separators can: a drive path keeps one
#: separator width throughout (``C:\\srv\\home``, or ``C:\\\\srv\\\\home``
#: in a repr) and never matches the consistent UNC prefix, whose leading
#: run is twice its inner separator (``a:\\\\srv\\home\\bob``). A drive
#: path with a redundant doubled separator and single ones after it has the
#: UNC shape exactly and is masked: over-masking one folder name is the
#: safe side of a report meant for a public issue.
_UNC_AFTER_LETTER = r"(?<=" + _LONE_LETTER_COLON + r")"
#: The user segment of a home path, in free text.
_USER_SEGMENT = re.compile(r"(?i)(" + _HOME_PREFIX + r")" + _SEGMENT)
#: The first segment after the prefix of a per-user UNC path, in free text.
_UNC_SEGMENT = re.compile(r"(?i)(?:" + _UNC_START + r"(" + _UNC_PREFIX + r")|"
                          + _UNC_AFTER_LETTER + r"(" + _UNC_PREFIX_CONSISTENT
                          + r"))" + _SEGMENT)
#: Any user-folder prefix, home or per-user UNC share.
_ANY_PREFIX = r"(?:" + _HOME_PREFIX + r"|" + _UNC_PREFIX + r")"
#: Spaced user names whose end the text itself proves, never guessed from
#: the words: a path separator follows the name, or the quote that opened
#: the path closes right after it (``'C:\\Users\\Mario Rossi'``). Group 1
#: (or 2) holds the name.
_PROVEN_NAMES = (
    re.compile(r"(?i)" + _ANY_PREFIX + r"(" + _SPACED + r")(?=[\\/])"),
    re.compile(r"(?i)(\"|')" + _ANY_PREFIX + r"(" + _SPACED + r")(?=\1)"),
)


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
        names and the first folder under a per-user UNC share by "<user>",
        and API keys by "<redacted>".
    @note Keys are masked by value from three sources, because the ``sk-``
        pattern alone only covers today's key shapes: the variables of
        SECRET_ENV_VARS as found in the environment, and the values the
        ``secrets`` callable given to `install` returns (`current_secrets`).
        The launcher wires that callable to the services' key readers: the
        keys of a ``.env`` file are popped from the environment at start-up
        and the Windows Credential Manager keys never enter it, so without
        the callable only the pattern protected them (review 2026-10-27, I8).
    @note A user name with spaces is masked whole only when the text proves
        where it ends: a path separator or the quote that opened the path
        follows it, here or at another occurrence of the same name. Without
        such a proof only its first word is masked, so the prose after a
        path is never eaten: an unquoted ``.../Users/Mario Rossi`` at the
        end of a line has the same shape as ``/Users/alice is missing``.
    """
    result = str(text)
    # Longest first: when one secret contains another, the longer one is
    # replaced whole instead of leaving its ends around a "<redacted>".
    for secret in sorted(current_secrets(), key=len, reverse=True):
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
    result = _mask_proven_names(result)
    for pattern in (_USER_SEGMENT, _UNC_SEGMENT):
        # The prefix is the one group that took part (two alternatives in UNC).
        result = pattern.sub(lambda m: m.group(m.lastindex) + "<user>", result)
    return result


def current_secrets() -> set[str]:
    """@brief The secret values to mask by value right now.

    @return The stripped values of SECRET_ENV_VARS present in the environment
        and the strings returned by the ``secrets`` callable of `install`,
        each at least MIN_SECRET_LENGTH characters long; never the names.
    @note Never raises: a callable that fails (a Credential Manager that
        cannot be read, a service that cannot be imported) contributes
        nothing and the environment values are still masked. The values are
        read at call time, so a key saved after start-up is masked too.
    """
    values: set[str] = set()
    for name in SECRET_ENV_VARS:
        values.add(os.environ.get(name, "").strip())
    provider = _state["secrets"]
    if provider is not None:
        try:
            values.update(str(value).strip() for value in provider() if value)
        except Exception:  # noqa: BLE001 - a failing provider must not lose the report
            pass
    return {value for value in values if len(value) >= MIN_SECRET_LENGTH}


def _mask_proven_names(text: str) -> str:
    """Mask every occurrence of a spaced user name the text proves somewhere.

    A spaced name is proved by a separator or a closing quote right after it
    (`_PROVEN_NAMES`). Once proved, the same name after a user-folder prefix
    is masked whole wherever it appears, also where nothing follows it (end
    of the text, a full stop): ``cwd C:\\Users\\Mario Rossi`` loses «Rossi»
    when a traceback line elsewhere holds ``C:\\Users\\Mario Rossi\\app``.
    The name ends at the end of a word: an apostrophe after it (a
    possessive, ``Mario Rossi's notes``) does not stop the masking, a longer
    word (``Mario Rossini``) does.
    An unproved spaced name keeps only its first word masked, because
    ``/Users/alice is missing`` has the same shape and its prose must stay.
    """
    names = {match.group(match.lastindex) for pattern in _PROVEN_NAMES
             for match in pattern.finditer(text)}
    if not names:
        return text
    alternatives = "|".join(re.escape(name) for name in
                            sorted(names, key=len, reverse=True))
    pattern = re.compile(r"(?i)(" + _ANY_PREFIX + r")(?:" + alternatives + r")"
                         r"(?!\w)")
    return pattern.sub(lambda m: m.group(1) + "<user>", text)


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


def install(log_dir_provider: PathProvider, *, max_reports: int = DEFAULT_MAX_REPORTS,
            secrets: Optional[SecretsProvider] = None) -> None:
    """@brief Install the crash hooks for the whole process.

    @param log_dir_provider Callable returning the log folder; it is called
        at crash time, so a folder chosen after startup is honoured. The
        reports go to ``<folder>/crash-reports``.
    @param max_reports Reports kept after each write (at least 1).
    @param secrets Optional callable returning the secret values in use
        (API keys from the Credential Manager, a ``.env`` file, anywhere),
        called by `scrub` at crash time so that each value is masked
        wherever it appears in the report. None keeps the environment
        variables and the key pattern as the only masks. The caller imports
        whatever the callable needs inside it: this module stays standard
        library only.
    @note Idempotent: a second call only updates the provider, the limit and
        the secrets callable, and the hook installed before the first call
        stays the one chained.
    @note What happens after the report depends on the hook chained after
        this one. By itself this module only writes the report and calls
        the previous `sys.excepthook`, so with the default one a slot
        exception would be printed and the event loop would go on. The
        launcher (`main.install_crash_reports`) does not leave it there: it
        wraps this hook so that, after the report and the traceback, the
        process EXITS, as PyQt6 did before the reports existed, because a
        window that keeps running after an unhandled exception may be in an
        inconsistent state. Only a plain `threading.Thread` target ends
        alone: its exception goes through `threading.excepthook` and stops
        that thread only. An exception escaping a QThread's `run()` or a
        QRunnable's `run()` is delivered by PyQt6 to `sys.excepthook`, so it
        is reported AND ends the whole process like a slot exception; worker
        `run()` methods must therefore catch their own errors and report them
        through a signal.
    """
    _state["provider"] = log_dir_provider
    _state["max_reports"] = max(1, int(max_reports))
    _state["secrets"] = secrets
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
        someone else is left alone. The native log of `open_native_log`, if
        any, is closed and deleted as well.
    """
    discard_native_log()
    if not _state["installed"]:
        return
    if sys.excepthook is _sys_hook:
        sys.excepthook = _state["previous_sys"] or sys.__excepthook__
    if threading.excepthook is _threading_hook:
        threading.excepthook = _state["previous_threading"] or threading.__excepthook__
    _state.update(installed=False, provider=None, previous_sys=None,
                  previous_threading=None, max_reports=DEFAULT_MAX_REPORTS, secrets=None)


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


# -- Native crashes (review 2026-11-16, R12-5) ----------------------------------
#
# A crash inside Qt, VTK or another native library never reaches
# `sys.excepthook`: the process just ends and, in the windowed executable,
# the window disappears without a word. faulthandler can still write the
# Python stacks at that moment, but only into a file opened beforehand. So
# each process opens its own native log at start-up; a normal exit deletes
# it, a crash leaves it behind, and the next start turns it into an ordinary
# crash report, which «Segnala un problema…» (Report a problem) then names.


def _native_base(base):
    """The log folder given, else the provider's; None when neither is known."""
    if base is not None:
        return base
    provider = _state["provider"]
    return provider() if provider is not None else None


class NativeContinuationWatch:
    """@brief Mark a native log as survived once the process outlives what it wrote.

    On Windows faulthandler dumps every error exception at the first chance,
    also one that a handler then catches (a COM call refused by Qt, an access
    violation caught by ctypes or a driver): the dump alone does not prove a
    crash (review 2026-11-16, follow-up 1). This watch looks at the log's
    size; when the log grew and the process is still running
    NATIVE_CONTINUATION_GRACE_S later, it appends NATIVE_CONTINUED, so the
    next start ignores everything above the mark.
    """

    def __init__(self, handle, grace_s: float = NATIVE_CONTINUATION_GRACE_S):
        """@brief Watch an open log.

        @param handle The log's open text file (its descriptor gives the size).
        @param grace_s Survival time that turns new lines into survived ones.
        """
        self._handle = handle
        self._grace_s = float(grace_s)
        self._size_seen = 0
        self._grown_at: Optional[float] = None

    def poll(self, now: float) -> bool:
        """@brief Look at the log once.

        @param now Monotonic time of this look, seconds.
        @return True when this look appended the continuation mark.
        @note Never raises: a log that cannot be read or written is left as it is.
        """
        try:
            size = os.fstat(self._handle.fileno()).st_size
        except (OSError, ValueError):
            return False
        if size > self._size_seen:
            self._size_seen, self._grown_at = size, now
            return False
        if self._grown_at is None or now - self._grown_at < self._grace_s:
            return False
        self._grown_at = None
        # A failed or voided mark leaves _size_seen behind the file: the next
        # look sees the growth and the grace starts again.
        if not _write_mark(self._handle, NATIVE_CONTINUED, expected_size=size):
            return False
        try:
            self._size_seen = os.fstat(self._handle.fileno()).st_size
        except (OSError, ValueError):
            pass
        return True


#: One writer at a time on the native log: the Qt message handler runs on any
#: thread and the continuation watch on its own, and two interleaved
#: buffered writes could break a NATIVE_CONTINUED line (final review E).
#: Reentrant, so a write that triggers another on the same thread cannot
#: deadlock. faulthandler writes to the descriptor without this lock, at
#: the crash only.
_NATIVE_WRITE_LOCK = threading.RLock()


def _write_native_line(handle, text: str) -> bool:
    """Append one line to an open native log and flush it, under the write lock; False on failure."""
    line = str(text).rstrip("\n") + "\n"
    try:
        with _NATIVE_WRITE_LOCK:
            handle.write(line)
            handle.flush()
        return True
    except Exception:  # noqa: BLE001 - logging must never add a failure of its own
        return False


def _write_mark(handle, mark: str, expected_size: Optional[int] = None) -> bool:
    """Write a continuation mark under the write lock, voiding it when another writer overlapped it.

    The size is read before and after the mark, all under the lock that every
    Python writer takes; only faulthandler writes outside it. A size before
    that differs from `expected_size` (the size the survival was judged on)
    means a write landed since that judgement: no mark. A size after that is
    not the size before plus the mark means a write overlapped the mark:
    NATIVE_MARK_VOID follows it. Residual window: a dump that starts after
    the second size read lands after the mark and stays in the reported
    tail, which is right; a void line that is itself split by a dump is not
    recognised and the mark stands (two overlaps in a row, within the
    microseconds of two buffered writes). Returns True when a valid mark is
    in the log. The handle must write "\\n" untranslated (``newline="\\n"``,
    as `open_native_log` opens it), so the mark's byte count is known.
    """
    expected_growth = len((mark + "\n").encode("utf-8"))
    with _NATIVE_WRITE_LOCK:
        try:
            before = os.fstat(handle.fileno()).st_size
        except (OSError, ValueError):
            return False
        if expected_size is not None and before != expected_size:
            return False
        if not _write_native_line(handle, mark):
            return False
        try:
            after = os.fstat(handle.fileno()).st_size
        except (OSError, ValueError):
            return True
        if after != before + expected_growth:
            _write_native_line(handle, NATIVE_MARK_VOID)
            return False
        return True


def _run_watch(watch: NativeContinuationWatch, stop: threading.Event, poll_s: float) -> None:
    """Body of the watch thread: one look every `poll_s` seconds until `stop` is set."""
    while not stop.wait(poll_s):
        watch.poll(time.monotonic())


def mark_native_continued(orderly: bool = False) -> None:
    """@brief Declare that everything written so far into the native log did not end the process.

    @param orderly True at an orderly end the application saw (``aboutToQuit``,
        the session end, see `watch_application`): NATIVE_ORDERLY_END is
        written, which also tells a logoff from a kill at the next start.
    @note Never raises; nothing without an open log.
    """
    native = _state["native"]
    if native is not None:
        _write_mark(native[0], NATIVE_ORDERLY_END if orderly else NATIVE_CONTINUED)


def watch_application(app) -> bool:
    """@brief Mark the native log as survived at the application's orderly ends.

    @param app The QGuiApplication (duck-typed: this module imports no Qt).
    @return True when the signals were connected, False for an object
        without them or one already watched.
    @note ``aboutToQuit`` precedes a normal exit; ``commitDataRequest`` comes
        at a logoff or shutdown, after which Windows may end the process
        without running atexit. Either way what the log holds so far was
        survived. Calling it twice on one application connects once.
    """
    if app is None or id(app) in _WATCHED_APPS:
        return False
    connected = False
    # Each signal on its own: a QCoreApplication has aboutToQuit only.
    for name in ("aboutToQuit", "commitDataRequest"):
        try:
            getattr(app, name).connect(lambda *args: mark_native_continued(orderly=True))
            connected = True
        except Exception:  # noqa: BLE001 - a missing hook never blocks the start-up
            continue
    if connected:
        # One application per process: its id stays valid while it lives.
        _WATCHED_APPS.add(id(app))
    return connected


def native_crash_text(text: str) -> str:
    """@brief The part of a native log that describes how the process ended, or "".

    @param text A native log as left on disk.
    @return What follows the last NATIVE_CONTINUED mark when at least one of
        its lines starts with NATIVE_FATAL_STARTS; "" otherwise.
    @note The criterion: a process crashed when the log holds a fatal line
        that the process was NOT seen to survive. Lines above a mark were
        survived (the watch, ``aboutToQuit``, the session end); a tail with
        only critical Qt messages is a process killed from outside, not a
        crash. Limits: see NATIVE_CONTINUATION_GRACE_S; and a crash that
        writes no fatal line (a native stack overflow that kills faulthandler
        too) is not reported.
    @note A mark is recognised wherever it appears whole on a line, also
        after a foreign prefix (faulthandler writes to the descriptor without
        the module's lock and may leave a line unfinished before it). That is
        safe: only this module writes the mark, and only after the process
        survived everything written before it, the glued prefix included.
        What follows the mark on its line starts the tail. A mark that is
        itself cut is not recognised: the earlier blocks then count again,
        which can only add a false report, never hide a real one. A
        NATIVE_MARK_VOID cancels the last mark still standing before it.
    """
    lines = text.splitlines()
    # Marks still standing, as (line, end offset); a void cancels the last one.
    standing: list[tuple[int, int]] = []
    tokens = (NATIVE_CONTINUED, NATIVE_ORDERLY_END, NATIVE_MARK_VOID)
    for index, line in enumerate(lines):
        position = 0
        while True:
            found = [(line.find(token, position), token) for token in tokens]
            found = [(at, token) for at, token in found if at >= 0]
            if not found:
                break
            at, token = min(found)
            if token == NATIVE_MARK_VOID:
                if standing:
                    standing.pop()
            else:
                standing.append((index, at + len(token)))
            position = at + len(token)
    tail = lines
    if standing:
        index, end = standing[-1]
        rest = lines[index][end:]
        tail = ([rest] if rest.strip() else []) + lines[index + 1:]
    if any(line.lstrip().startswith(NATIVE_FATAL_STARTS) for line in tail):
        return "\n".join(tail).strip("\n")
    return ""


def open_native_log(base: Union[str, os.PathLike, None] = None, *,
                    enable_faulthandler: bool = True, watch: bool = True,
                    poll_s: float = NATIVE_POLL_S,
                    grace_s: float = NATIVE_CONTINUATION_GRACE_S) -> Optional[Path]:
    """@brief Open this process's native fault log and point faulthandler at it.

    @param base Log folder; defaults to the provider given to `install`.
    @param enable_faulthandler False to open the file without touching
        faulthandler. Even when True, a faulthandler that is already enabled
        is left as it is: `discard_native_log` disables only the setup this
        function made.
    @param watch True to start the `NativeContinuationWatch` thread (daemon).
    @param poll_s Seconds between two looks of the watch.
    @param grace_s Survival time of the watch (NATIVE_CONTINUATION_GRACE_S).
    @return Path of the log (``<reports>/native-<time>-<pid>.log``, empty
        while nothing goes wrong), or None when it could not be opened.
    @note Never raises. Idempotent: a second call returns the open log.
        Nothing is written while the program runs normally; `discard_native_log`
        removes the file at a normal exit.
    """
    try:
        if _state["native"] is not None:
            return _state["native"][1]
        base = _native_base(base)
        if base is None:
            return None
        folder = reports_dir(base)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = folder / f"{NATIVE_PREFIX}{stamp}-{os.getpid()}{REPORT_SUFFIX}"
        # Kept open for the life of the process: faulthandler writes to its
        # descriptor at the moment of the crash, when nothing can be opened.
        # O_APPEND: faulthandler writes to the descriptor and this module
        # through the buffered handle; both must land at the end.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                             | getattr(os, "O_BINARY", 0))
        handle = os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
        owned = False
        # A faulthandler already enabled was set up on purpose by someone
        # else (``python -X faulthandler``, PYTHONFAULTHANDLER, pytest): its
        # destination is kept, and only Qt's messages come to this file.
        if enable_faulthandler and not faulthandler.is_enabled():
            try:
                faulthandler.enable(file=handle, all_threads=True)
                owned = True
            except Exception:  # noqa: BLE001 - a log without stacks still keeps Qt's messages
                pass
        stop = None
        if watch:
            stop = threading.Event()
            threading.Thread(target=_run_watch, name="kicadgen-native-log-watch", daemon=True,
                             args=(NativeContinuationWatch(handle, grace_s), stop, poll_s)).start()
        _state["native"] = (handle, path, owned, stop)
        return path
    except Exception:  # noqa: BLE001 - a missing native log must never stop the start-up
        return None


def log_native_line(text: str) -> None:
    """@brief Append one line to this process's native log, flushed at once.

    @param text The line (e.g. a critical or fatal Qt message). Written as it
        is: the whole log is scrubbed when it becomes a report.
    @note Never raises; does nothing without an open native log. The flush
        matters: a fatal Qt message is followed by an abort, and an unflushed
        buffer would be lost with the process.
    """
    native = _state["native"]
    if native is not None:
        _write_native_line(native[0], text)


def discard_native_log() -> None:
    """@brief Close and delete this process's native log.

    @note Called at a normal exit (the launcher registers it with atexit)
        and by the launcher's Python crash hook just before it stops the
        process: that crash already has its own report, and a native log
        left behind would make a second, misleading one. Never raises.
    """
    native, _state["native"] = _state["native"], None
    if native is None:
        return
    handle, path, owned, stop = native
    if stop is not None:
        stop.set()
    try:
        # Only our own faulthandler setup is undone: the one bound to the file.
        if owned and faulthandler.is_enabled():
            faulthandler.disable()
    except Exception:  # noqa: BLE001
        pass
    for action in (handle.close, lambda: path.unlink(missing_ok=True)):
        try:
            action()
        except Exception:  # noqa: BLE001 - a leftover empty log is removed at the next start
            pass


def _process_alive_posix(pid: int) -> bool:
    """True when a POSIX process with this id exists (signal 0 probes without signalling)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM: the process exists but belongs to someone else.
        return True
    return True


def _native_log_pid(path: Path) -> Optional[int]:
    """The process id written at the end of a native log's name, or None."""
    stem = path.name[len(NATIVE_PREFIX):-len(REPORT_SUFFIX)]
    tail = stem.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def format_native_report(text: str, *, crashed_at: datetime) -> str:
    """@brief Build the text of a crash report from a native fault log.

    @param text What faulthandler and the Qt message handler wrote.
    @param crashed_at Last write of the log, i.e. about when the process died.
    @return The scrubbed report text, ending with a newline.
    @note The version lines describe the program that FOUND the log at its
        start, which is normally the one that crashed but may be a newer
        one after an update: the report says so.
    """
    frozen = "installed executable" if getattr(sys, "frozen", False) else "from sources"
    lines = [
        "KiCadGen crash report (native crash)",
        f"Time: {crashed_at.isoformat(timespec='seconds')}",
        f"Found at start-up by KiCadGen: {_app_version()} ({frozen})",
        f"Python: {platform.python_version()} {platform.python_implementation()}"
        f" ({8 * struct.calcsize('P')}-bit)",
        f"OS: {platform.platform()}",
        "",
        "The program ended without a Python exception: a crash inside Qt, VTK or",
        "another native library. Below, what faulthandler (Python stacks) and Qt",
        "(critical and fatal messages) wrote before the end.",
        "",
    ]
    return scrub("\n".join(lines) + "\n" + text.rstrip("\n") + "\n")


def sessions_ended_without_exit() -> int:
    """@brief How many earlier sessions `adopt_native_logs` found ended by a kill.

    @return The number of native logs, adopted at this start, whose process
        ended with no exit, no crash and no orderly end the application saw
        (Task Manager, a forced shutdown). They have no report: nothing in
        the log says why. 0 when none, or before the adoption ran.
    """
    return int(_state["unclean_sessions"])


def _seen_stamp(base) -> Optional[Path]:
    """The seen-stamp file of a log folder (`base`, else the provider's), or None."""
    base = _native_base(base)
    return None if base is None else reports_dir(base) / SEEN_STAMP_NAME


def unseen_crash_report(base: Union[str, os.PathLike, None] = None) -> Optional[Path]:
    """@brief The newest crash report the user has not been told about yet, or None.

    @param base Log folder; defaults to the provider given to `install`.
    @return The newest report (Python crash, or native crash adopted at this
        start) when its name sorts after the one recorded by
        `mark_crash_report_seen`; None when there is none or it was seen.
    @note Report names start with their time, so their order is the time
        order. Call it after `adopt_native_logs` (the launcher runs it before
        the window). A normal exit writes no report, so it never makes a
        notice; a kill writes none either (see `sessions_ended_without_exit`).
        Without a stamp yet (first start of this version), the newest
        existing report counts as unseen and is shown once. Never raises.
    """
    try:
        reports = latest_reports(limit=1, base=base)
        if not reports:
            return None
        stamp = _seen_stamp(base)
        try:
            seen = stamp.read_text(encoding="utf-8").strip() if stamp is not None else ""
        except OSError:
            seen = ""
        return reports[0] if not seen or reports[0].name > seen else None
    except Exception:  # noqa: BLE001 - a notice is a courtesy
        return None


def mark_crash_report_seen(path: Union[str, os.PathLike],
                           base: Union[str, os.PathLike, None] = None) -> bool:
    """@brief Record that the user was told about a report, so the notice appears once.

    @param path The report shown (from `unseen_crash_report`).
    @param base Log folder; defaults to the provider given to `install`.
    @return True when the stamp now covers @p path; False when it could not
        be written.
    @note The stamp only moves forward: marking an older report keeps the
        newer name already recorded. Written through a temporary file and a
        rename, so an interrupted write never leaves half a name. Never raises.
    """
    try:
        stamp = _seen_stamp(base)
        if stamp is None:
            return False
        name = Path(path).name
        try:
            current = stamp.read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        if current and current >= name:
            return True
        stamp.parent.mkdir(parents=True, exist_ok=True)
        temporary = stamp.with_name(f"{SEEN_STAMP_NAME}.{os.getpid()}.tmp")
        temporary.write_text(name + "\n", encoding="utf-8")
        os.replace(temporary, stamp)
        return True
    except Exception:  # noqa: BLE001 - a notice shown twice beats a crash
        return False


def adopt_native_logs(base: Union[str, os.PathLike, None] = None) -> list[Path]:
    """@brief Turn the native logs left by processes that died into crash reports.

    @param base Log folder; defaults to the provider given to `install`.
    @return The reports written, oldest first; empty when there was nothing
        to adopt or nothing could be done.
    @note An empty log (a process killed from outside, which faulthandler
        cannot see) is only deleted. The log of a process still running is
        left alone: on Windows its open handle makes the deletion fail, and
        the text read just before is then dropped; on POSIX the process id
        in the name is probed first. The report is scrubbed like every other
        one and the reports folder is rotated afterwards. Never raises.
    """
    written: list[Path] = []
    try:
        base = _native_base(base)
        if base is None:
            return written
        folder = reports_dir(base)
        if not folder.is_dir():
            return written
        own = _state["native"][1] if _state["native"] is not None else None
        for path in sorted(folder.glob(f"{NATIVE_PREFIX}*{REPORT_SUFFIX}")):
            if own is not None and path == own:
                continue
            pid = _native_log_pid(path)
            if os.name != "nt" and pid is not None and pid != os.getpid() and _process_alive_posix(pid):
                continue
            try:
                crashed_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
                text = path.read_text(encoding="utf-8", errors="replace")
                # The deletion doubles as the liveness test on Windows: a
                # running process holds the file open without sharing delete.
                path.unlink()
            except OSError:
                continue
            # Only what the process did not survive is a crash (see
            # native_crash_text): a log of first-chance dumps followed by a
            # kill from outside is dropped like an empty one.
            crash = native_crash_text(text)
            if not crash:
                if not text.rstrip().endswith(NATIVE_ORDERLY_END):
                    # No exit, no crash, no orderly end seen: killed from outside.
                    _state["unclean_sessions"] += 1
                continue
            text = crash
            stamp = crashed_at.strftime("%Y%m%d-%H%M%S-%f")
            target = folder / f"{REPORT_PREFIX}{stamp}-{pid or 0}{NATIVE_REPORT_TAG}{REPORT_SUFFIX}"
            counter = 1
            while target.exists():
                target = folder / (f"{REPORT_PREFIX}{stamp}-{pid or 0}-{counter}"
                                   f"{NATIVE_REPORT_TAG}{REPORT_SUFFIX}")
                counter += 1
            try:
                with open(target, "x", encoding="utf-8") as handle:
                    handle.write(format_native_report(text, crashed_at=crashed_at))
            except OSError:
                continue
            written.append(target)
        if written:
            _rotate(folder, int(_state["max_reports"]))
    except Exception:  # noqa: BLE001 - adoption is a courtesy of the start-up
        pass
    return written
