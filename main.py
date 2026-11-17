"""@brief Launch the GUI with the project's environment via python main.py.

@note Environment selection happens before GUI, dotenv or optional imports.
No packages are installed and no global Python settings are changed.
Use --check-runtime to validate imports and required resources without opening
the GUI or reading .env.
"""

import os
import subprocess
import sys
from pathlib import Path


def _project_python(root):
    """Return a different project interpreter, or None when already selected."""
    environment = root / ".venv"
    if getattr(sys, "frozen", False) or Path(sys.prefix).resolve() == environment.resolve():
        return None
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return executable if executable.is_file() else None


#: Modules the packaged program cannot run without. The GUI module is last:
#: importing it proves the whole application imports, not only the libraries.
RUNTIME_MODULES = ("PyQt6", "PyQt6.QtWidgets", "PIL", "cv2", "numpy",
                   "pyvista", "pyvistaqt", "kicadgen.gui.main_window",
                   # Imported lazily by the window: the smoke test must still prove it.
                   "kicadgen.gui.preview_3d_vtk")

#: Modules the package always ships but a source checkout may lack, because
#: they come from an optional requirements file (onnxruntime: the local
#: detectors, requirements/yolo.txt). In the executable they are required like
#: `RUNTIME_MODULES`; from the sources a missing one is reported as optional
#: and does not fail the check.
OPTIONAL_RUNTIME_MODULES = ("onnxruntime",)

#: Command-line option and environment variable naming a file that receives
#: every line of `--check-runtime`. The executable is windowed (no console),
#: so its stdout/stderr go nowhere: the build reads this file to show WHY the
#: smoke test failed.
CHECK_RUNTIME_REPORT_OPTION = "--check-runtime-report"
CHECK_RUNTIME_REPORT_ENV = "KICADGEN_CHECK_RUNTIME_REPORT"

#: Qt plugins the program needs, as (folder, library stem) under one of Qt's
#: library paths. The toolbar icons are SVG files loaded through QIcon, which
#: goes through the image-format plugin: the PyQt6.QtSvg Python module is
#: never imported by the program and is not shipped (the 2026-10-27 build
#: failed only because the check asked for it).
RUNTIME_QT_PLUGINS = (("imageformats", "qsvg"),)

#: Read-only files the program looks up through kicadgen.app_paths.resource_path.
#: The FreeCAD scripts must be real files: freecadcmd runs them from disk.
RUNTIME_RESOURCES = (
    ("LICENSE",),
    ("THIRD_PARTY_NOTICES.md",),
    ("data", "analysis_profiles.sqlite3"),
    ("kicadgen", "cad3d", "freecad_build_body.py"),
    ("kicadgen", "cad3d", "freecad_parts.py"),
    ("kicadgen", "cad3d", "model_size_cache.py"),
    # Imported by file path inside FreeCAD's embedded interpreter, where the
    # kicadgen package is not importable (freecad_build_body.py and
    # freecad_parts.py put their own folder on sys.path).
    ("kicadgen", "cad3d", "header_geometry.py"),
    ("kicadgen", "cad3d", "lead_geometry.py"),
    ("kicadgen", "cad3d", "lead_sides.py"),
    ("kicadgen", "gui", "assets", "app-icon.svg"),
    ("kicadgen", "gui", "assets", "help", "guida.html"),
    ("kicadgen", "gui", "assets", "help", "guida.en.html"),
    ("kicadgen", "gui", "assets", "help", "guida.fr.html"),
    ("kicadgen", "gui", "assets", "help", "guida.de.html"),
)
#: Present only in the package: collected by tools/collect_licenses.py.
PACKAGE_RESOURCES = (("licenses", "MANIFEST.txt"),)


def provider_secrets():
    """@brief The provider API keys in use now, for the crash report scrubber.

    @return The OpenRouter and DeepSeek keys the services would use (empty
        strings when a provider has none), read from every source the
        services read: the environment, the Windows Credential Manager and
        the ``.env`` files.
    @note Called only while a crash report is being written. The keys of a
        ``.env`` file are popped from the process environment by `main` and
        the Credential Manager keys never enter it, so `crash_report.scrub`
        cannot find them in the environment: without this callable only the
        ``sk-`` pattern masked them (review 2026-10-27, I8). The services
        are imported here, lazily, so that `kicadgen.crash_report` stays
        standard library only and a failing import loses one mask, never the
        report (the scrubber swallows exceptions of this callable).
    """
    from kicadgen.services import openrouter, providers
    values = []
    for read_key in (openrouter.api_key, providers.deepseek_api_key):
        try:
            values.append(read_key())
        except Exception:  # noqa: BLE001 - no key, or an unreadable store: nothing to mask
            values.append("")
    return values


def install_crash_reports(exit_process=os._exit):
    """@brief Write a crash report for every unhandled exception, then stop as before.

    @param exit_process Function called with the exit status after the report
        (os._exit; replaceable in tests).
    @note Reports go to `app_paths.logs_dir()/crash-reports` (see
        `kicadgen.crash_report`). PyQt6 stops the program after an exception
        in a slot only while `sys.excepthook` is the default one; any other
        hook would let the window keep running on a possibly inconsistent
        state. Until the user decides otherwise, the program stops as it did
        before, after the report and the usual traceback.
    @note The hook is process-wide, worker threads included: PyQt6 routes an
        exception that escapes a Python ``QRunnable.run`` or ``QThread.run``
        to ``sys.excepthook`` as well, so it too writes a report and stops
        the whole program. A worker that must survive its own failure has to
        catch the exception inside ``run`` and report it through a signal.
        Plain ``threading.Thread`` targets go to ``threading.excepthook``
        instead (`crash_report.install` reports them and chains to the
        previous hook): only that thread ends.
    @return The installed hook (report, flush, exit), so tests can call it
        directly.
    """
    from kicadgen import crash_report
    from kicadgen.app_paths import logs_dir
    crash_report.install(logs_dir, secrets=provider_secrets)
    report = sys.excepthook

    def report_and_stop(exc_type, exc_value, exc_tb):
        """Write the report, drop the native log this crash does not need, flush and exit."""
        if late_worker_exception():
            # The user already closed the program: a worker thread left behind
            # by the bounded exit wait (gui/quit_wait.py) failing during the
            # interpreter shutdown is not a crash of the session. It is
            # printed, never reported, and the exit status stays the user's
            # (review 2026-11-16, follow-up 4).
            try:
                import traceback
                traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
            except Exception:  # noqa: BLE001 - no stderr in the windowed executable
                pass
            return
        report(exc_type, exc_value, exc_tb)
        # The Python report explains this exit: a native log left behind
        # would become a second, misleading report at the next start.
        crash_report.discard_native_log()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001 - exiting remains what matters
                pass
        exit_process(1)

    sys.excepthook = report_and_stop
    return report_and_stop


#: Module that knows whether the application has begun to quit (fixer C9).
QUIT_STATE_MODULE = "kicadgen.gui.quit_wait"


def late_worker_exception():
    """@brief Tell whether the exception being handled is a worker's, raised after the quit began.

    @return True only when the current thread is not the main thread AND the
        GUI's `quit_wait.is_quitting()` says the exit has begun. False in
        every other case: on the main thread, before the quit, and when the
        module or its function is missing or fails (the report is then
        written, as before).
    @note The module is looked up in ``sys.modules`` only, never imported:
        this runs inside the crash hook, possibly during the interpreter
        shutdown, and a GUI that never loaded it has not begun to quit
        through it.
    """
    import threading
    if threading.current_thread() is threading.main_thread():
        return False
    module = sys.modules.get(QUIT_STATE_MODULE)
    is_quitting = getattr(module, "is_quitting", None)
    try:
        return bool(is_quitting()) if callable(is_quitting) else False
    except Exception:  # noqa: BLE001 - in doubt, report
        return False


#: Reference to the Qt message handler installed by `install_native_crash_log`:
#: Qt keeps only a C pointer, the Python callable must stay alive here.
_QT_MESSAGE_HANDLER = None


def install_native_crash_log(register_exit=None):
    """@brief Keep a trace of native crashes, which the Python hooks never see.

    @param register_exit Function registering a callable for a normal exit;
        default `atexit.register` (replaceable in tests).
    @return The path of this process's native log, or None when it could not
        be opened (the program starts anyway).
    @note Review 2026-11-16, R12-5. Three steps: (1) the native logs left by
        earlier processes that died are turned into crash reports
        (`crash_report.adopt_native_logs`), so «Segnala un problema…»
        (Report a problem) names them; (2) this process opens its own log,
        with faulthandler bound to it; (3) Qt's critical and fatal messages
        go to that log too (`install_qt_message_log`): a fatal one is
        followed by an abort, and in the windowed executable stderr goes
        nowhere. A normal exit deletes the log; the Python crash hook deletes
        it too, since its own report already explains the exit.
    """
    import atexit
    from kicadgen import crash_report
    register_exit = register_exit or atexit.register
    crash_report.adopt_native_logs()
    path = crash_report.open_native_log()
    if path is not None:
        register_exit(crash_report.discard_native_log)
        install_qt_message_log(register_exit=register_exit)
    return path


def watch_existing_application():
    """@brief Connect the native log's orderly-end marks to the QApplication, when one exists.

    @return True when an application was found and connected.
    @note The in-process splash creates the application here; with the
        animated splash process it is created later by the window module,
        which then has to call `crash_report.watch_application(app)` itself
        (idempotent). Without either, the continuation watch thread still
        marks what the process survived.
    """
    try:
        from PyQt6.QtCore import QCoreApplication
    except (ImportError, OSError):
        return False
    from kicadgen import crash_report
    app = QCoreApplication.instance()
    return app is not None and crash_report.watch_application(app)


def qt_message_line(kind_name, message):
    """@brief The native-log line for one Qt message, or None when it is not kept.

    @param kind_name Name of the Qt message type (``QtCriticalMsg``, ...).
    @param message The message text.
    @return ``"Qt critical: <message>"`` / ``"Qt fatal: <message>"`` for the
        two kinds that announce a failure; None for debug, info and warning
        messages, which are frequent and harmless and would bury the reason.
    """
    labels = {"QtCriticalMsg": "critical", "QtFatalMsg": "fatal"}
    label = labels.get(str(kind_name).rsplit(".", 1)[-1])
    return None if label is None else f"Qt {label}: {message}"


def install_qt_message_log(register_exit=None):
    """@brief Route Qt's messages to stderr as before and its failures to the native log.

    @param register_exit Function registering a callable for a normal exit;
        default `atexit.register`.
    @return True when the handler was installed, False without PyQt6.
    @note A Python handler installed before this one receives every message
        as before (it is chained); without one, every message is printed on
        stderr when there is one, so a source run in a terminal still sees
        Qt's warnings. At a normal exit the previous handler comes back,
        before the interpreter is torn down, unless someone replaced this
        one meanwhile.
    """
    global _QT_MESSAGE_HANDLER
    import atexit
    try:
        from PyQt6.QtCore import qInstallMessageHandler
    except (ImportError, OSError):
        return False
    from kicadgen import crash_report
    previous = None

    def handler(kind, context, message):
        """Qt message handler: the native log for failures, then the previous handler or stderr."""
        try:
            line = qt_message_line(getattr(kind, "name", kind), message)
            if line is not None:
                crash_report.log_native_line(line)
            if previous is not None:
                previous(kind, context, message)
            elif sys.stderr is not None:
                print(message, file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001 - a message handler must never raise into Qt
            pass

    def restore():
        """Give Qt back the previous handler, unless ours was already replaced."""
        current = qInstallMessageHandler(previous)
        if current is not handler:
            qInstallMessageHandler(current)

    _QT_MESSAGE_HANDLER = handler
    previous = qInstallMessageHandler(handler)
    (register_exit or atexit.register)(restore)
    return True


def load_env_files(files=None, load=None, warn=None):
    """@brief Load the ``.env`` files into the process environment, skipping unusable ones.

    @param files Files in priority order; default `kicadgen.app_paths.env_files()`.
    @param load Function receiving one decoded text (``dotenv.load_dotenv``
        with a stream, which keeps the first value set); default python-dotenv's.
    @param warn Function receiving one English line per skipped file;
        default a print on stderr.
    @return The (path, kind, detail) of every file skipped, as
        `app_paths.unreadable_env_files` reports them.
    @note Review 2026-11-16, R12-1: ``load_dotenv(path)`` decodes as UTF-8
        only, and a UTF-16 or ANSI file raised UnicodeDecodeError before the
        window and the crash hooks existed, so the program never started.
        Each file is now decoded by `app_paths.read_env_text` (byte-order
        mark, else UTF-8); a file that cannot be read or decoded is skipped
        and named, and the start-up goes on.
    """
    import io
    from kicadgen.app_paths import EnvFileEncodingError, env_files, read_env_text
    if load is None:
        from dotenv import load_dotenv

        def load(text):
            """Load one decoded text without overriding values already set."""
            load_dotenv(stream=io.StringIO(text))
    if warn is None:
        def warn(line):
            """Print one skipped-file line on stderr, when there is one."""
            if sys.stderr is not None:
                print(line, file=sys.stderr)
    skipped = []
    for path in env_files() if files is None else files:
        path = Path(path)
        try:
            if not path.is_file():
                continue
            text = read_env_text(path)
        except EnvFileEncodingError as exc:
            skipped.append((path, "encoding", str(exc)))
            warn(f"KiCadGen: .env file skipped, not text in a supported encoding: {exc}")
            continue
        except OSError as exc:
            skipped.append((path, "io", str(exc)))
            warn(f"KiCadGen: .env file skipped, unreadable: {exc}")
            continue
        load(text)
    return skipped


def _qt_library_paths():
    """Folders Qt searches for plugins, without creating an application.

    ``QCoreApplication.libraryPaths`` is static: it lists ``QT_PLUGIN_PATH``
    (set by PyInstaller's runtime hook in the executable) and then Qt's own
    plugins folder (from the sources). An unimportable QtCore yields no path,
    so the plugin check reports the plugin as missing.
    """
    from pathlib import Path
    try:
        from PyQt6.QtCore import QCoreApplication
    except (ImportError, OSError):
        return []
    return [Path(path) for path in QCoreApplication.libraryPaths()]


def _qt_plugin_present(library_paths, folder, stem):
    """True when a plugin library ``stem`` (any platform prefix/suffix) is in ``folder`` of a library path."""
    from pathlib import Path
    for base in library_paths:
        try:
            names = [Path(entry).stem.lower() for entry in (Path(base) / folder).iterdir()]
        except OSError:
            continue
        if stem in names or f"lib{stem}" in names:
            return True
    return False


def check_runtime(resource_path=None, modules=RUNTIME_MODULES, resources=None,
                  qt_plugins=RUNTIME_QT_PLUGINS, library_paths=None,
                  optional_modules=None, report_file=None):
    """@brief Check that the program's modules import and its resources exist.

    Used by ``--check-runtime`` (the build's smoke test of KiCadGen.exe).
    Never opens a window, reads ``.env`` or calls any AI service.

    @param resource_path Function mapping path parts to a Path; default
        ``kicadgen.app_paths.resource_path`` (package or repository layout).
    @param modules Module names to import.
    @param resources Path-part tuples that must exist as files; default
        ``RUNTIME_RESOURCES``, plus ``PACKAGE_RESOURCES`` in the executable.
    @param qt_plugins (folder, stem) pairs of Qt plugins that must be found
        under one of ``library_paths``; default ``RUNTIME_QT_PLUGINS``.
    @param library_paths Folders searched for the plugins; default the ones
        Qt itself would search (`_qt_library_paths`).
    @param optional_modules Modules required only in the executable; from the
        sources a failed import is printed as optional and does not fail.
        None means `OPTIONAL_RUNTIME_MODULES` when @p modules is the default
        list, and none when the caller chose its own modules.
    @param report_file Path of a text file that receives every printed line
        (UTF-8, overwritten), or None. A file that cannot be written is
        reported on stderr and never changes the result.
    @return 0 when everything is present, 1 otherwise (each problem printed,
        then a summary line listing what is missing).
    @note The windowed executable has no console: without @p report_file its
        lines are lost, which is why the build passes one.
    """
    import importlib
    lines = []

    def say(text, error=False):
        """Print one line (stdout, or stderr for the failure summary) and keep it for the file."""
        lines.append(text)
        print(text, file=sys.stderr if error else sys.stdout)

    def finish(status):
        """Write the collected lines to the report file, then return the status unchanged."""
        if report_file:
            try:
                Path(report_file).write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"Runtime check report not written ({report_file}): {exc}", file=sys.stderr)
        return status

    frozen = getattr(sys, "frozen", False)
    if optional_modules is None:
        optional_modules = OPTIONAL_RUNTIME_MODULES if modules is RUNTIME_MODULES else ()
    say(f"Python: {sys.executable}")
    failed_modules = []
    for name in tuple(modules) + tuple(optional_modules or ()):
        optional = name not in modules and not frozen
        try:
            importlib.import_module(name)
            say(f"{name}: OK")
        # Exception, not only ImportError/OSError: a module that raises while
        # it is imported (a RuntimeError of a native library, a SyntaxError,
        # an AttributeError at module level) used to escape the check with a
        # traceback the windowed executable never shows and no report file
        # (review 2026-11-16, R12-3). It is a failed import like the others.
        except Exception as exc:  # noqa: BLE001 - see above
            reason = str(exc) if isinstance(exc, (ImportError, OSError)) else f"{type(exc).__name__}: {exc}"
            if optional:
                say(f"{name}: not installed (optional from the sources: {reason})")
            else:
                say(f"{name}: {reason}")
                failed_modules.append(name)
    if resource_path is None:
        try:
            from kicadgen.app_paths import resource_path
        except Exception as exc:  # noqa: BLE001 - same reason as the module imports above
            say(f"kicadgen.app_paths: {type(exc).__name__}: {exc}", error=True)
            return finish(1)
    if resources is None:
        resources = RUNTIME_RESOURCES + (PACKAGE_RESOURCES if frozen else ())
    missing = []
    for parts in resources:
        path = resource_path(*parts)
        if path.is_file():
            say(f"{'/'.join(parts)}: OK")
        else:
            say(f"{'/'.join(parts)}: MISSING ({path})")
            missing.append("/".join(parts))
    missing_plugins = []
    if qt_plugins:
        if library_paths is None:
            library_paths = _qt_library_paths()
        searched = ", ".join(str(path) for path in library_paths) or "no Qt library path"
        for folder, stem in qt_plugins:
            if _qt_plugin_present(library_paths, folder, stem):
                say(f"Qt plugin {folder}/{stem}: OK")
            else:
                say(f"Qt plugin {folder}/{stem}: MISSING (searched {searched})")
                missing_plugins.append(f"{folder}/{stem}")
    if failed_modules or missing or missing_plugins:
        say("Runtime check FAILED. Modules not importable: "
            f"{', '.join(failed_modules) or 'none'}. Resources missing: {', '.join(missing) or 'none'}. "
            f"Qt plugins missing: {', '.join(missing_plugins) or 'none'}.", error=True)
        return finish(1)
    say("Runtime check OK.")
    return finish(0)


def check_runtime_report_path(argv=None, environ=None):
    """@brief The file `--check-runtime` must also write, or None.

    @param argv Command-line arguments after the program name; default
        ``sys.argv[1:]``.
    @param environ Environment mapping; default ``os.environ``.
    @return The path given by ``--check-runtime-report PATH`` (or
        ``--check-runtime-report=PATH``), else by the
        ``KICADGEN_CHECK_RUNTIME_REPORT`` variable, else None.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    for index, argument in enumerate(argv):
        if argument.startswith(CHECK_RUNTIME_REPORT_OPTION + "="):
            return argument.split("=", 1)[1] or None
        if argument == CHECK_RUNTIME_REPORT_OPTION and index + 1 < len(argv):
            return argv[index + 1]
    return environ.get(CHECK_RUNTIME_REPORT_ENV) or None


# The splash lives in kicadgen.splash (stdlib-only at import time); the
# names stay here for the launcher's tests and callers.
from kicadgen.splash import (  # noqa: E402,F401 - re-exported
    SPLASH_FLAG, SPLASH_IMAGE, SPLASH_MESSAGES, SPLASH_WIDTH, ProcessSplash, run_splash, splash_pixmap)
#: The QApplication created for the in-process splash. Kept here on purpose:
#: a local reference dies with the function and Qt destroys the application
#: with it, splash included (found by the real start-up check, 2026-10-12).
_QT_APPLICATION = None


def startup_language(default="it"):
    """@brief The interface language saved in config.json, read without the GUI package.

    @param default Language when the file is missing, unreadable or has none.
    @return A language code; unknown codes are returned as they are (the
        splash falls back to the default text for them).
    @note Reads the same file `kicadgen.gui.app_config.load_config` reads
        (``KICADGEN_CONFIG_PATH`` or the user data folder), but that module
        imports the AI services: too slow for a splash.
    """
    import json
    try:
        from kicadgen.app_paths import user_data_dir
        path = Path(os.environ.get("KICADGEN_CONFIG_PATH") or user_data_dir() / "config.json")
        with open(path, "r", encoding="utf-8") as handle:
            language = json.load(handle).get("language")
        return language if isinstance(language, str) and language else default
    except (OSError, ValueError, ImportError):
        return default


def create_splash(language, start_process=ProcessSplash.start):
    """@brief Show the start-up splash: the animated process, or the still in-process one.

    @param language Interface language code (see `SPLASH_MESSAGES`).
    @param start_process Factory of the splash process (replaceable in tests).
    @return A ProcessSplash, or the QSplashScreen of `create_splash_widget`
        when the process cannot be started; the GUI closes either with
        `finish(window)` once the main window is on screen.
    @note The animation needs its own process (user, 2026-10-13): this
        process's GUI thread is busy building the window for seconds, and a
        splash it painted could not move. The still splash remains the
        fallback so the click always answers at once.
    """
    try:
        return start_process(language)
    except (OSError, ValueError) as exc:
        print(f"KiCadGen: splash animato non avviato: {exc}", file=sys.stderr)
        return create_splash_widget(language)


def create_splash_widget(language):
    """@brief Show the still in-process splash: artwork or icon, the name and «Starting…».

    @param language Interface language code (see `SPLASH_MESSAGES`).
    @return The QSplashScreen, already shown and painted; the GUI closes it
        with `finish(window)` once the main window is on screen.
    @note Creates the QApplication when there is none: PyQt6 alone imports
        in well under a second, while the window module takes several, so
        the user sees this long before the window (user, 2026-10-12).
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QApplication, QSplashScreen
    from kicadgen.app_paths import resource_path
    global _QT_APPLICATION
    app = QApplication.instance() or QApplication(sys.argv)
    _QT_APPLICATION = app
    screen = app.primaryScreen()
    pixmap = splash_pixmap(resource_path, screen.devicePixelRatio() if screen is not None else 1.0)
    splash = QSplashScreen(pixmap)
    splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
    message = SPLASH_MESSAGES.get(language, SPLASH_MESSAGES["en"])
    # A widget style sheet outranks the application's (theme.APP_QSS sets
    # 10 pt on every widget once the window module is loaded): the message
    # would otherwise shrink while the splash is still on screen.
    splash.setStyleSheet("QSplashScreen { font-size: 11pt; }")
    splash.showMessage("\n" + message + "\n", Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
                       QColor("#a6adc8"))
    splash.show()
    app.processEvents()
    return splash


def main():
    """@brief Select the local environment and start the application.
    @return GUI or child-process exit status.
    @note Arguments, working directory and environment are preserved on relaunch.
    """
    root = Path(__file__).resolve().parent
    executable = _project_python(root)
    if executable is not None:
        if os.environ.get("KICADGEN_BOOTSTRAP_ROOT") == str(root):
            print("Project .venv is invalid: recreate its Python environment.", file=sys.stderr)
            return 1
        environment = os.environ.copy()
        environment["KICADGEN_BOOTSTRAP_ROOT"] = str(root)
        print(f"KiCadGen: Python ambiente progetto: {executable}", flush=True)
        try:
            return subprocess.call([str(executable), str(root / "main.py"), *sys.argv[1:]], env=environment)
        except OSError as exc:
            print(f"Cannot start project Python: {exc}", file=sys.stderr)
            return 1
    if "--check-runtime" in sys.argv[1:]:
        return check_runtime(report_file=check_runtime_report_path())
    if sys.argv[1:2] == [SPLASH_FLAG]:
        # The splash process: paints until the parent closes the pipe.
        return run_splash(sys.argv[2] if len(sys.argv) > 2 else "en")
    # Before the .env files and the GUI import, so that a failure in either
    # leaves a report (review 2026-11-16, R12-1: a .env decoding error used
    # to stop the program before any hook existed).
    install_crash_reports()
    try:
        install_native_crash_log()
    except Exception as exc:  # noqa: BLE001 - a missing native log never blocks the start-up
        print(f"KiCadGen: native crash log not opened: {exc}", file=sys.stderr)
    try:
        # From source, the repository's .env; installed, the one in the user's
        # data folder (Program Files is read-only).
        key_names = ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
        preset = {name for name in key_names if name in os.environ}
        # A file that cannot be read or decoded is skipped and named on
        # stderr; app_paths.unreadable_env_files() tells the window which.
        load_env_files()
        # The provider keys of a .env file must not stay in the process
        # environment: there they would outrank the keys saved with «Account
        # AI…» in the Windows Credential Manager. openrouter.api_key() and
        # providers.deepseek_api_key() read the .env files themselves, after
        # the Credential Manager. A key already in the real environment is
        # left alone.
        for name in key_names:
            if name not in preset:
                os.environ.pop(name, None)
    except ImportError:
        pass
    # The splash goes up before the window module is imported: that import is
    # the slow part of the start-up, and the click must answer at once.
    try:
        splash = create_splash(startup_language())
    except Exception as exc:  # noqa: BLE001 - a splash is a courtesy, never a blocker
        print(f"KiCadGen: splash non mostrato: {exc}", file=sys.stderr)
        splash = None
    watch_existing_application()
    from kicadgen.gui.main_window import main as run_gui
    return run_gui(splash)

if __name__ == "__main__":
    raise SystemExit(main())
