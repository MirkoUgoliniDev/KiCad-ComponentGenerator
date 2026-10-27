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
                   "pyvista", "pyvistaqt", "onnxruntime", "kicadgen.gui.main_window",
                   # Imported lazily by the window: the smoke test must still prove it.
                   "kicadgen.gui.preview_3d_vtk")

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
    ("kicadgen", "gui", "assets", "app-icon.svg"),
    ("kicadgen", "gui", "assets", "help", "guida.html"),
)
#: Present only in the package: collected by tools/collect_licenses.py.
PACKAGE_RESOURCES = (("licenses", "MANIFEST.txt"),)


def install_crash_reports(exit_process=os._exit):
    """@brief Write a crash report for every unhandled exception, then stop as before.

    @param exit_process Function called with the exit status after the report
        (os._exit; replaceable in tests).
    @note Reports go to `app_paths.logs_dir()/crash-reports` (see
        `kicadgen.crash_report`). PyQt6 stops the program after an exception
        in a slot only while `sys.excepthook` is the default one; any other
        hook would let the window keep running on a possibly inconsistent
        state. Until the user decides otherwise, the program stops as it did
        before, after the report and the usual traceback. Exceptions in
        worker threads keep ending only their thread, as before.
    @return The installed hook (report, flush, exit), so tests can call it
        directly.
    """
    from kicadgen import crash_report
    from kicadgen.app_paths import logs_dir
    crash_report.install(logs_dir)
    report = sys.excepthook

    def report_and_stop(exc_type, exc_value, exc_tb):
        report(exc_type, exc_value, exc_tb)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001 - exiting remains what matters
                pass
        exit_process(1)

    sys.excepthook = report_and_stop
    return report_and_stop


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
                  qt_plugins=RUNTIME_QT_PLUGINS, library_paths=None):
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
    @return 0 when everything is present, 1 otherwise (each problem printed,
        then a summary line listing what is missing).
    """
    import importlib
    print(f"Python: {sys.executable}")
    failed_modules = []
    for name in modules:
        try:
            importlib.import_module(name)
            print(f"{name}: OK")
        except (ImportError, OSError) as exc:
            print(f"{name}: {exc}")
            failed_modules.append(name)
    if resource_path is None:
        try:
            from kicadgen.app_paths import resource_path
        except ImportError as exc:
            print(f"kicadgen.app_paths: {exc}")
            return 1
    if resources is None:
        resources = RUNTIME_RESOURCES + (PACKAGE_RESOURCES if getattr(sys, "frozen", False) else ())
    missing = []
    for parts in resources:
        path = resource_path(*parts)
        if path.is_file():
            print(f"{'/'.join(parts)}: OK")
        else:
            print(f"{'/'.join(parts)}: MISSING ({path})")
            missing.append("/".join(parts))
    missing_plugins = []
    if qt_plugins:
        if library_paths is None:
            library_paths = _qt_library_paths()
        searched = ", ".join(str(path) for path in library_paths) or "no Qt library path"
        for folder, stem in qt_plugins:
            if _qt_plugin_present(library_paths, folder, stem):
                print(f"Qt plugin {folder}/{stem}: OK")
            else:
                print(f"Qt plugin {folder}/{stem}: MISSING (searched {searched})")
                missing_plugins.append(f"{folder}/{stem}")
    if failed_modules or missing or missing_plugins:
        print("Runtime check FAILED. Modules not importable: "
              f"{', '.join(failed_modules) or 'none'}. Resources missing: {', '.join(missing) or 'none'}. "
              f"Qt plugins missing: {', '.join(missing_plugins) or 'none'}.",
              file=sys.stderr)
        return 1
    print("Runtime check OK.")
    return 0


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
        return check_runtime()
    if sys.argv[1:2] == [SPLASH_FLAG]:
        # The splash process: paints until the parent closes the pipe.
        return run_splash(sys.argv[2] if len(sys.argv) > 2 else "en")
    try:
        from dotenv import load_dotenv
        from kicadgen.app_paths import env_files
        # From source, the repository's .env; installed, the one in the user's
        # data folder (Program Files is read-only).
        key_names = ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
        preset = {name for name in key_names if name in os.environ}
        for env_file in env_files():
            load_dotenv(env_file)
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
    # Before the GUI import, so that a failing import is reported too.
    install_crash_reports()
    # The splash goes up before the window module is imported: that import is
    # the slow part of the start-up, and the click must answer at once.
    try:
        splash = create_splash(startup_language())
    except Exception as exc:  # noqa: BLE001 - a splash is a courtesy, never a blocker
        print(f"KiCadGen: splash non mostrato: {exc}", file=sys.stderr)
        splash = None
    from kicadgen.gui.main_window import main as run_gui
    return run_gui(splash)

if __name__ == "__main__":
    raise SystemExit(main())
