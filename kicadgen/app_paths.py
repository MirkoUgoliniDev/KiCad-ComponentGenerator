"""@brief Where the program reads its resources and writes its data.

Two layouts, one rule each:

- **From the sources** (``python main.py``): everything stays where it has
  always been, in the repository: ``config.json``, ``data/``, ``generated/``,
  ``.env``, the model cache. Nothing changes for development.
- **Installed** (PyInstaller executable under ``Program Files``): the install
  folder is read-only. The program's own files (YOLO weights, FreeCAD scripts,
  icons, the initial profile database) are read from the package; what the
  program writes goes to ``%LOCALAPPDATA%\\KiCadGen`` (configuration,
  databases, caches, ``.env``) and the projects to ``Documents\\KiCadGen``.

The environment variables already used by the tests (``KICADGEN_CONFIG_PATH``,
``KICADGEN_PROFILE_DB``, ``KICADGEN_MATCH_DB``, ``KICADGEN_YOLO_MODEL``) keep
priority over both layouts; ``KICADGEN_DATA_DIR`` moves the whole data folder.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: Folder name under %LOCALAPPDATA% and under Documents.
APP_FOLDER = "KiCadGen"


def is_frozen() -> bool:
    """@brief Tell whether the program runs as the packaged executable."""
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """@brief Folder holding the program's own read-only files.

    @return The repository root from the sources; the PyInstaller bundle
        folder (``sys._MEIPASS``) in the executable.
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def resource_path(*parts: str) -> Path:
    """@brief Path of a read-only file shipped with the program.

    @param parts Path components relative to the program root, e.g.
        ``("models", "pcb_yolov8s.onnx")``.
    @return The absolute path (it may not exist; callers check).
    """
    return install_dir().joinpath(*parts)


def _local_app_data() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) if base else Path.home() / "AppData" / "Local"


def user_data_dir() -> Path:
    """@brief Folder where the program writes its own data.

    @return ``KICADGEN_DATA_DIR`` when set; ``%LOCALAPPDATA%\\KiCadGen`` in
        the executable; the repository root from the sources (unchanged).
    @note Never created here: the callers that write create it.
    """
    override = os.environ.get("KICADGEN_DATA_DIR")
    if override:
        return Path(override)
    if is_frozen():
        return _local_app_data() / APP_FOLDER
    return install_dir()


def _documents_dir() -> Path:
    """The user's Documents folder, following a redirection (e.g. OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            # FOLDERID_Documents: {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            guid = (ctypes.c_ubyte * 16)(*bytes.fromhex(
                "D09AD3FD8F23AF46ADB46C85480369C7"))
            path = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.argtypes = [
                ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) == 0:
                try:
                    return Path(path.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(path)
        except (OSError, AttributeError, ValueError):
            pass
    return Path.home() / "Documents"


def default_projects_dir() -> Path:
    """@brief Default workspace for the projects.

    @return ``generated/`` in the repository from the sources (unchanged);
        ``Documents\\KiCadGen`` in the executable.
    """
    if is_frozen() and not os.environ.get("KICADGEN_DATA_DIR"):
        return _documents_dir() / APP_FOLDER
    if os.environ.get("KICADGEN_DATA_DIR"):
        return Path(os.environ["KICADGEN_DATA_DIR"]) / "generated"
    return install_dir() / "generated"


def data_file(name: str, *, seed: bool = False) -> Path:
    """@brief Path of a writable file in the data folder's ``data/``.

    @param name File name, e.g. ``analysis_profiles.sqlite3``.
    @param seed True to copy the copy shipped with the program the first
        time, when the user's file does not exist yet.
    @return The path in the user's data folder.
    @note The shipped copy is never modified: it only seeds a new user.
    """
    target = user_data_dir() / "data" / name
    if seed and not target.exists():
        shipped = resource_path("data", name)
        if shipped.is_file() and shipped.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shipped, target)
    return target


def env_files() -> list[Path]:
    """@brief ``.env`` files to read, in order (the first value set wins).

    @return From the sources, the repository's ``.env``. In the executable,
        the one in the user's data folder: the install folder is read-only.
    """
    files = [user_data_dir() / ".env"]
    root = install_dir() / ".env"
    if root not in files:
        files.append(root)
    return files
