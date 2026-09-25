"""@brief Persist optional CAD measurements with atomic writes and fallback paths.

No CAD or filesystem discovery is performed here. The caller supplies cache
paths and remains responsible for measurement validity and search completeness.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from threading import RLock
from typing import Iterable


_LOGGER = logging.getLogger(__name__)
_LOCK = RLock()
_PathInput = Iterable[str | os.PathLike[str]]
_MAX_TEMP_ATTEMPTS = 3


def _warn(operation: str, slot: int | None = None) -> None:
    # File names, measurement keys and exception messages can contain private
    # paths; diagnostics deliberately identify only the operation/cache slot.
    _LOGGER.warning("CAD measurement cache: %s%s", operation,
                    "" if slot is None else f" (path #{slot + 1})")


def _paths(paths: _PathInput) -> list[Path]:
    result, seen = [], set()
    try:
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        for slot, value in enumerate(paths):
            try:
                path = Path(value)
                key = os.path.normcase(os.fspath(path))
                if key not in seen:
                    result.append(path)
                    seen.add(key)
            except (OSError, TypeError, ValueError):
                _warn("invalid path ignored", slot)
    except (OSError, TypeError, ValueError):
        _warn("path list unavailable")
    return result


def legacy_cache_dir() -> Path:
    """@brief Historic folder of the CAD measurement caches.

    @return ``%LOCALAPPDATA%\\kicadgen``, or ``~/.cache/kicadgen`` without it.
    @note Kept readable forever: `load` merges every path it is given, so an
        index written here before the data folder became configurable is still
        replayed, and the next `save` copies it into the preferred folder.
    """
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".cache") / "kicadgen"


def user_cache_dirs() -> list[Path]:
    """@brief Folders for the user-level CAD caches, preferred first.

    @return ``<user data folder>/cache`` followed by `legacy_cache_dir`, or
        only the legacy folder when the program runs from the sources without
        ``KICADGEN_DATA_DIR``.
    @note The user data folder comes from `kicadgen.app_paths.user_data_dir`,
        so ``KICADGEN_DATA_DIR`` and the installed layout are honoured. From
        the sources that folder is the repository itself: writing caches there
        would leave untracked files in the checkout, so the historic location
        is kept, which is exactly the previous behaviour.
    @note Imported lazily: this module also runs inside FreeCAD's embedded
        interpreter, where the ``kicadgen`` package is not importable. The
        FreeCAD child receives the folders from the parent in its params file.
    """
    legacy = legacy_cache_dir()
    try:
        from ..app_paths import install_dir, is_frozen, user_data_dir
        if is_frozen() or os.environ.get("KICADGEN_DATA_DIR"):
            preferred = user_data_dir() / "cache"
            if os.path.normcase(os.fspath(preferred)) != os.path.normcase(os.fspath(legacy)):
                return [preferred, legacy]
        elif user_data_dir() != install_dir():
            return [user_data_dir() / "cache", legacy]
    except (ImportError, OSError, ValueError):
        pass
    return [legacy]


def file_identity(path: str | os.PathLike[str]) -> str | None:
    """@brief Identify a file by absolute path, byte size and nanosecond mtime.

    @param path File whose measurements are about to be stored or replayed.
    @return "<absolute path>:<size>:<mtime_ns>", or None when the file cannot
        be inspected.
    @note Questa e' la chiave che rende una misura ripetibile: un file
        riscritto cambia dimensione o data e quindi cambia chiave, cosi' una
        voce vecchia non puo' essere scambiata per una misura del file nuovo.
    @note None non e' una chiave degradata ma l'assenza di chiave: senza
        dimensione e data non si puo' invalidare nulla, quindi il chiamante
        deve rimisurare invece di fidarsi. Per questo non si ripiega sul solo
        percorso.
    @note `kicad_models.index_key` calcola oggi la stessa stringa per l'indice
        degli ingombri; le due formule devono restare identiche (un test lo
        verifica) e quel modulo dovrebbe passare di qui alla prossima modifica,
        invece di conservare una seconda copia della ricetta.
    """
    try:
        resolved = Path(path).absolute()
        status = resolved.stat()
        return f"{resolved}:{status.st_size}:{status.st_mtime_ns}"
    except (OSError, TypeError, ValueError):
        _warn("file identity unavailable; measurement stays retryable")
        return None


def _object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _read(paths: list[Path]) -> dict:
    snapshots = []
    for slot, path in enumerate(paths):
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle, object_pairs_hook=_object,
                                  parse_constant=_constant)
                if not isinstance(value, dict):
                    raise ValueError("Cache must contain a JSON object")
                # Use the opened file: an atomic replacement at the same path
                # must not give this snapshot a different file's timestamp.
                modified = os.fstat(handle.fileno()).st_mtime_ns
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError):
            _warn("read failed; measurements remain retryable", slot)
            continue
        snapshots.append((modified, -slot, value))
    merged = {}
    for _, _, value in sorted(snapshots, key=lambda item: item[:2]):
        # Whole-record replacement preserves each measurement's provenance;
        # nested merging could fabricate a body/total pair never measured.
        merged.update(value)
    return merged


def load(paths: _PathInput) -> dict:
    """@brief Merge readable JSON caches without certifying their measurements.

    @param paths Preferred cache path followed by any user-cache fallback paths.
    @return Union of stored keys, or an empty dictionary when none can be read.
    @note Newer file timestamps win duplicate keys; equal timestamps prefer
    earlier paths. Timestamps come from the opened file descriptors, allowing
    an updated fallback or a recovered primary to supersede a stale copy.
    Missing or malformed files are nonfatal. Records are never field-merged,
    and missing keys are not fabricated or marked as completed measurements.
    """
    with _LOCK:
        return _read(_paths(paths))


def _write(path: Path, payload: str, slot: int) -> bool:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # tempfile's Windows permission handling may retry denied creations.
        # Retry only actual name collisions, with a small explicit bound.
        for _ in range(_MAX_TEMP_ATTEMPTS):
            candidate = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
            try:
                handle = candidate.open("x", encoding="utf-8", newline="\n")
            except FileExistsError:
                continue
            # Only a successful exclusive open establishes cleanup ownership.
            temporary = candidate
            break
        else:
            _warn("temporary name collision limit reached; trying next path", slot)
            return False
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    except (OSError, ValueError):
        _warn("atomic write failed; trying next path", slot)
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                _warn("temporary file cleanup failed", slot)


def save(index: dict, paths: _PathInput) -> Path | None:
    """@brief Merge and atomically save measurements, trying fallback paths.

    @param index JSON-serializable dictionary keyed by model cache identifiers.
    Supplied records replace existing records with the same key as a whole.
    @param paths Preferred destination followed by user-cache fallbacks.
    @return Path successfully replaced, or None if no write succeeds.
    @note Existing keys from every readable destination are preserved. Input
    dictionaries are not mutated. No permission precheck is used; each write is
    attempted with an exclusively created same-directory UUID file and
    os.replace. Only name collisions are retried (at most three attempts per
    path); permission errors immediately try the next destination. Failures
    are nonfatal and never imply that measurement/search work is complete.
    Calls in this process are serialized; cross-process read/merge/write is
    not a transaction and callers sharing concurrent writers must coordinate.
    """
    with _LOCK:
        if not isinstance(index, dict) or any(not isinstance(key, str) for key in index):
            _warn("invalid index; nothing saved")
            return None
        destinations = _paths(paths)
        merged = _read(destinations)
        merged.update(index)
        try:
            payload = json.dumps(merged, ensure_ascii=True, allow_nan=False, indent=1)
        except (TypeError, ValueError, OverflowError, RecursionError):
            _warn("index serialization failed; nothing saved")
            return None
        for slot, path in enumerate(destinations):
            if _write(path, payload, slot):
                return path
        _warn("no writable destination; measurements remain retryable")
        return None
