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
                   "pyvista", "pyvistaqt", "onnxruntime", "kicadgen.gui.main_window")

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
)
#: Present only in the package: collected by tools/collect_licenses.py.
PACKAGE_RESOURCES = (("licenses", "MANIFEST.txt"),)


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
        for env_file in env_files():
            load_dotenv(env_file)
    except ImportError:
        pass
    from kicadgen.gui.main_window import main as run_gui
    return run_gui()

if __name__ == "__main__":
    raise SystemExit(main())
