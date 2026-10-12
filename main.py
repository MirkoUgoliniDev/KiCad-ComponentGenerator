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
RUNTIME_MODULES = ("PyQt6", "PyQt6.QtWidgets", "PyQt6.QtSvg", "PIL", "cv2", "numpy",
                   "pyvista", "pyvistaqt", "onnxruntime", "kicadgen.gui.main_window",
                   # Imported lazily by the window: the smoke test must still prove it.
                   "kicadgen.gui.preview_3d_vtk")

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
            except Exception:  # noqa: BLE001 - uscire resta la cosa importante
                pass
        exit_process(1)

    sys.excepthook = report_and_stop
    return report_and_stop


def check_runtime(resource_path=None, modules=RUNTIME_MODULES, resources=None):
    """@brief Check that the program's modules import and its resources exist.

    Used by ``--check-runtime`` (the build's smoke test of KiCadGen.exe).
    Never opens a window, reads ``.env`` or calls any AI service.

    @param resource_path Function mapping path parts to a Path; default
        ``kicadgen.app_paths.resource_path`` (package or repository layout).
    @param modules Module names to import.
    @param resources Path-part tuples that must exist as files; default
        ``RUNTIME_RESOURCES``, plus ``PACKAGE_RESOURCES`` in the executable.
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
    if failed_modules or missing:
        print("Runtime check FAILED. Modules not importable: "
              f"{', '.join(failed_modules) or 'none'}. Resources missing: {', '.join(missing) or 'none'}.",
              file=sys.stderr)
        return 1
    print("Runtime check OK.")
    return 0


#: Splash text per interface language (the launcher must not import the
#: translation catalogue: the splash exists to appear before the heavy imports).
SPLASH_MESSAGES = {"it": "Avvio in corso…", "en": "Starting…", "fr": "Démarrage…", "de": "Wird gestartet…"}
#: The QApplication created for the splash. Kept here on purpose: a local
#: reference dies with the function and Qt destroys the application with
#: it, splash included (found by the real start-up check, 2026-10-12).
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


#: Logical size of the splash, pixels: the artwork (`SPLASH_IMAGE`) is
#: scaled to this width; the drawn fallback is painted at this size.
SPLASH_WIDTH = 800
#: Optional artwork, 16:9, no text (the name and the message are painted
#: over it): drop a PNG here to replace the drawn splash (user, 2026-10-12).
SPLASH_IMAGE = ("kicadgen", "gui", "assets", "splash.png")


def splash_pixmap(resource_path, device_pixel_ratio=1.0):
    """@brief The splash picture: the artwork when present, else icon and name on the theme colour.

    @param resource_path Function mapping path parts to a Path (the
        application's `kicadgen.app_paths.resource_path`).
    @param device_pixel_ratio Screen scale; the pixmap is rendered at
        `SPLASH_WIDTH` × ratio so it stays sharp on high-density screens.
    @return A QPixmap with its device pixel ratio set. An unreadable
        artwork falls back to the drawn picture: the splash never fails.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
    ratio = max(1.0, float(device_pixel_ratio))
    width = int(SPLASH_WIDTH * ratio)
    artwork = resource_path(*SPLASH_IMAGE)
    pixmap = QPixmap(str(artwork)) if artwork.is_file() else QPixmap()
    if not pixmap.isNull():
        pixmap = pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    else:
        pixmap = QPixmap(width, int(width * 9 / 16))
        pixmap.fill(QColor("#1e1e2e"))
        painter = QPainter(pixmap)
        side = int(180 * ratio)
        icon = QIcon(str(resource_path("kicadgen", "gui", "assets", "app-icon.svg"))).pixmap(side, side)
        painter.drawPixmap((pixmap.width() - icon.width()) // 2, int(pixmap.height() * 0.22), icon)
        painter.end()
    painter = QPainter(pixmap)
    font = QFont(painter.font())
    font.setPixelSize(int(30 * ratio))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#cdd6f4"))
    band = pixmap.rect().adjusted(0, int(pixmap.height() * 0.66), 0, -int(pixmap.height() * 0.2))
    painter.drawText(band, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "KiCadGen")
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def create_splash(language):
    """@brief Show the start-up splash: artwork or icon, the name and «Starting…» in the given language.

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
    font = splash.font()
    font.setPointSize(11)
    splash.setFont(font)
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
    try:
        from dotenv import load_dotenv
        from kicadgen.app_paths import env_files
        # Da sorgente il .env del repository; installato, quello nella
        # cartella dati dell'utente (Program Files e' in sola lettura).
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
    splash = create_splash(startup_language())
    from kicadgen.gui.main_window import main as run_gui
    return run_gui(splash)

if __name__ == "__main__":
    raise SystemExit(main())
