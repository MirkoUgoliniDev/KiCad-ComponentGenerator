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
priority over both layouts. ``KICADGEN_DATA_DIR`` moves what is resolved
through this module: ``user_data_dir()``, ``data_file()``, ``env_files()``
and ``default_projects_dir()`` (``<dir>/generated``). A file whose path is
computed elsewhere does not follow it, and a path computed at import time
(e.g. ``app_config.CONFIG_PATH``) follows it only when the variable is set
before the program starts.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from .fsutil import replace_file
import time
from pathlib import Path

#: Folder name under %LOCALAPPDATA% and under Documents.
APP_FOLDER = "KiCadGen"


def is_frozen() -> bool:
    """@brief Tell whether the program runs as the packaged executable.

    @return True under the PyInstaller bundle (`sys.frozen` set), False from
        the sources.
    """
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
        ``("models", "yolo", "pcb_yolov8s.onnx")``.
    @return The absolute path (it may not exist; callers check).
    """
    return install_dir().joinpath(*parts)


#: Folder of the detectors (YOLO weights, .onnx or .pt), relative to the
#: program root. User decision 2026-10-02: the detectors and the SMD network
#: live in two separate folders, so the YOLO picker lists only detectors.
YOLO_MODELS_DIR = ("models", "yolo")
#: Folder of the local SMD network that classifies the detected bodies.
SMD_MODELS_DIR = ("models", "smd")
#: Default weights of each folder.
DEFAULT_YOLO_ONNX = "pcb_yolov8s.onnx"
DEFAULT_YOLO_PT = "pcb_component_detector_yolov8m_best.pt"
#: ONNX export of DEFAULT_YOLO_PT (tools/export_yolo_onnx.py): the detector
#: of the «YOLO + rete SMD» route where PyTorch is absent (installed program).
DEFAULT_YOLO_SMD_ONNX = "pcb_component_detector_yolov8m_best.onnx"
DEFAULT_SMD_MODEL = "smd_classifier.pt"
#: ONNX export of the SMD network (tools/export_smd_onnx.py): the file the
#: Windows installer ships, read with onnxruntime only.
DEFAULT_SMD_ONNX = "smd_classifier.onnx"


def yolo_model_path(name: str = DEFAULT_YOLO_ONNX) -> Path:
    """@brief Path of a detector's weights in the YOLO folder (it may not exist).

    @param name File name inside YOLO_MODELS_DIR.
    @return The absolute path.
    """
    return resource_path(*YOLO_MODELS_DIR, name)


def default_yolo_smd_model() -> Path:
    """@brief Default YOLO weights of the «YOLO + rete SMD» (YOLO + SMD network) route.

    @return The PyTorch checkpoint DEFAULT_YOLO_PT when it is there (the route
        was built on it), else its ONNX export DEFAULT_YOLO_SMD_ONNX, which is
        what the installed program has. With neither file the checkpoint's
        path is returned, so the caller's «file not found» names the usual
        file. Same rule as smd_model_path(None).
    @note The ONE place where this choice is made (review 2026-10-29, P3):
        the GUI, photo_inventory and datasheet_extractor used to decide it
        each on their own, and two of them forced the checkpoint even where
        only the export existed. KICADGEN_YOLO_MODEL is not read here: the
        callers that honour it check it before calling this function.
    """
    checkpoint = yolo_model_path(DEFAULT_YOLO_PT)
    if checkpoint.is_file():
        return checkpoint
    exported = yolo_model_path(DEFAULT_YOLO_SMD_ONNX)
    return exported if exported.is_file() else checkpoint


def smd_model_path(name: str | None = None) -> Path:
    """@brief Path of the SMD network's weights in its folder (it may not exist).

    @param name File name inside SMD_MODELS_DIR; None chooses the default:
        the PyTorch checkpoint (DEFAULT_SMD_MODEL) when it is there, else the
        ONNX export (DEFAULT_SMD_ONNX), which is what the installed program
        has. With neither file the checkpoint's path is returned, so the
        caller's «file not found» names the usual file.
    @return The absolute path.
    """
    if name is not None:
        return resource_path(*SMD_MODELS_DIR, name)
    checkpoint = resource_path(*SMD_MODELS_DIR, DEFAULT_SMD_MODEL)
    if checkpoint.is_file():
        return checkpoint
    exported = resource_path(*SMD_MODELS_DIR, DEFAULT_SMD_ONNX)
    return exported if exported.is_file() else checkpoint


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


def logs_dir() -> Path:
    """@brief Folder of the program's own logs (crash reports among them).

    @return ``<user_data_dir()>/logs``: ``%LOCALAPPDATA%\\KiCadGen\\logs`` when
        installed, ``<repository>/logs`` from the sources.
    @note Never created here, like `user_data_dir`. The logs of an analysis
        (`analisi-ai.log`, `freecad.log`) stay in the project folder.
    """
    return user_data_dir() / "logs"


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


#: First 16 bytes of every SQLite 3 database file.
SQLITE_HEADER = b"SQLite format 3\x00"
#: Files SQLite keeps next to a database; they follow it when it is set aside.
_SQLITE_COMPANIONS = ("-journal", "-wal", "-shm")


def _has_sqlite_header(path: Path) -> bool:
    """True when the file starts with the SQLite 3 magic header."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def is_damaged_sqlite(path: Path) -> bool:
    """@brief Tell whether a file is definitely not a usable SQLite database.

    @param path Existing file to check (opened read-only, never modified).
    @return True when the header is wrong (empty or truncated copy, another
        format) or SQLite itself reports it as not a database or corrupt;
        False for a sound database and for any doubtful case.
    @note A locked or busy database, or an I/O error, is NOT damage: those
        answers are transient, and a False positive would set user data aside.
    """
    if not _has_sqlite_header(path):
        return True
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute("PRAGMA quick_check(1)").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        return getattr(exc, "sqlite_errorname", "") in ("SQLITE_NOTADB", "SQLITE_CORRUPT")
    return not row or row[0] != "ok"


def _set_aside(target: Path) -> Path:
    """Rename a damaged file (and its SQLite companions) to ``.corrupt-<time>``."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = target.with_name(f"{target.name}.corrupt-{stamp}")
    counter = 1
    while aside.exists():
        aside = target.with_name(f"{target.name}.corrupt-{stamp}-{counter}")
        counter += 1
    os.replace(target, aside)
    for suffix in _SQLITE_COMPANIONS:
        companion = target.with_name(target.name + suffix)
        if companion.exists():
            # A stale journal must never be replayed onto the fresh seed.
            os.replace(companion, aside.with_name(aside.name + suffix))
    return aside


def _copy_atomically(source: Path, target: Path) -> None:
    """Copy through a temporary file in the target folder, then rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                         dir=target.parent)
    os.close(handle)
    try:
        shutil.copy2(source, temporary)
        replace_file(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def data_file(name: str, *, seed: bool = False) -> Path:
    """@brief Path of a writable file in the data folder's ``data/``.

    @param name File name, e.g. ``analysis_profiles.sqlite3``.
    @param seed True to copy the copy shipped with the program when the
        user's file does not exist yet, or when the shipped copy is a SQLite
        database and the user's file is definitely not one (see
        ``is_damaged_sqlite``, e.g. a copy truncated by a crash).
    @return The path in the user's data folder.
    @throws OSError When a damaged file cannot be set aside (the user's file
        is then left as it was) or the seed cannot be copied. In the second
        case a damaged file may ALREADY have been set aside: it stays under
        its ``.corrupt-`` name, nothing exists under `name`, and the next call
        with `seed` True copies the seed again.
    @note The shipped copy is never modified: it only seeds a new user. The
        copy is atomic (temporary file in the same folder, then
        ``fsutil.replace_file``): an interrupted copy never leaves a truncated
        file under the final name. A damaged file is never deleted: it is
        renamed ``<name>.corrupt-<YYYYmmdd-HHMMSS>`` next to the new seed.
    """
    target = user_data_dir() / "data" / name
    if not seed:
        return target
    shipped = resource_path("data", name)
    if not shipped.is_file() or shipped.resolve() == target.resolve():
        return target
    if target.exists():
        # Only a shipped SQLite seed defines what a valid user file looks like.
        if not (_has_sqlite_header(shipped) and target.is_file() and is_damaged_sqlite(target)):
            return target
        _set_aside(target)
    _copy_atomically(shipped, target)
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
