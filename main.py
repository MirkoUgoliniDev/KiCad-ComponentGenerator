"""@brief Launch the GUI with the project's environment via python main.py.

@note Environment selection happens before GUI, dotenv or optional imports.
No packages are installed and no global Python settings are changed.
Use --check-runtime to validate imports without opening the GUI or reading .env.
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
        import importlib
        print(f"Python: {sys.executable}")
        failed = False
        for name in ("PyQt6", "PIL", "cv2", "pyvista", "pyvistaqt", "numpy", "onnxruntime"):
            try:
                importlib.import_module(name)
                print(f"{name}: OK")
            except (ImportError, OSError) as exc:
                print(f"{name}: {exc}")
                failed = True
        return int(failed)
    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env")
    except ImportError:
        pass
    from kicadgen.gui.main_window import main as run_gui
    return run_gui()

if __name__ == "__main__":
    raise SystemExit(main())
