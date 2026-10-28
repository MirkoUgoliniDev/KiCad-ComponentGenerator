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
}

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
        inconsistent state. Worker-thread exceptions end only their thread.
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
        someone else is left alone.
    """
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
