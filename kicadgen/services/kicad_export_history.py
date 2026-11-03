"""@brief Persistent local history of artifacts installed or exported to KiCad.

One JSON list per user, newest first, capped at MAX_RECORDS; writes are
atomic so a crash never leaves a half-written history.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from ..app_paths import user_data_dir
from ..fsutil import replace_file


HISTORY_FILENAME = "kicad_exports.json"
MAX_RECORDS = 500


def history_path() -> Path:
    """@brief Return the per-user KiCad export history path.

    @return Path of the JSON history inside the user data directory.
    """
    return user_data_dir() / HISTORY_FILENAME


def load_history(path: Path | None = None) -> list[dict]:
    """@brief Load newest-first export records, returning an empty list on bad data.

    @param path History file to read; None means `history_path()`.
    @return Up to MAX_RECORDS dict records; [] when the file is missing or malformed.
    """
    target = path or history_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)][:MAX_RECORDS]


class _UnreadableHistory(Exception):
    """The history file exists but its content is not a JSON list."""


def _records_for_update(target: Path) -> list[dict]:
    """@brief Records to keep when a new one is added, refusing to lose any.

    @param target History file.
    @return Its records ([] when the file does not exist).
    @throws _UnreadableHistory When the file holds something that is not a
        JSON list (truncated by a crash, edited by hand).
    @throws OSError When the file exists but cannot be read: nothing is
        written then, because the old records could not be kept.
    """
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise _UnreadableHistory(str(exc)) from exc
    if not isinstance(data, list):
        raise _UnreadableHistory("not a JSON list")
    return [item for item in data if isinstance(item, dict)][:MAX_RECORDS]


def _set_aside(target: Path) -> Path:
    """@brief Move an unreadable history next to itself, never deleting it.

    @param target History file that could not be parsed.
    @return The new name, ``<name>.corrupt-<YYYYMMDD-HHMMSS>`` (a counter is
        added when that name is taken).
    @throws OSError When the file cannot be moved; the caller then writes
        nothing.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = target.with_name(f"{target.name}.corrupt-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.corrupt-{stamp}-{counter}")
        counter += 1
    os.rename(target, candidate)
    return candidate


#: Times `_previous_records` reads the history again after another instance
#: moved or replaced it in the meantime. One race is all two windows can
#: produce; the bound only keeps a file that keeps vanishing from looping.
_RACE_RETRIES = 3


def _previous_records(target: Path) -> list[dict]:
    """@brief Records to keep, setting an unreadable history aside first.

    @param target History file.
    @return The records of the history (or of the copy just set aside, when
        it turns out to be readable); [] when there are none.
    @throws OSError When the file cannot be read or moved: nothing is
        written then.
    @note Two instances can both read the same corrupt history (review
        2026-11-03, D11). The one that moves it second finds it gone
        (FileNotFoundError): the history is read again, now the valid file
        the first one wrote, instead of losing this record. When the second
        one moves the FIRST one's valid file (it was replaced between read
        and rename), the moved copy is read back and its records are kept
        (the copy stays beside it under its ``.corrupt-`` name: a duplicate,
        never a loss).
    """
    for _attempt in range(_RACE_RETRIES):
        try:
            return _records_for_update(target)
        except _UnreadableHistory:
            pass
        try:
            aside = _set_aside(target)
        except FileNotFoundError:
            continue
        try:
            return _records_for_update(aside)
        except _UnreadableHistory:
            return []
    raise FileNotFoundError(f"history kept changing: {target}")


def record_export(*, component: str, kind: str, source: str,
                  destination: str, path: Path | None = None) -> bool:
    """@brief Atomically add a successful KiCad export/install event to local history.

    @param component Component name as installed.
    @param kind What was delivered (preview, project library, global library).
    @param source Folder the files came from.
    @param destination Folder or table that received them.
    @param path History file to update; None means `history_path()`.
    @return True when the record was written, False when the write failed.
    @note A history file that exists but cannot be parsed (truncated by a
        crash during a write of an older version, edited by hand) is moved
        aside as ``<name>.corrupt-<timestamp>`` before the new history is
        written; when it cannot be moved, or cannot even be read, nothing is
        written and False is returned. Loading it as empty and overwriting it
        silently discarded every earlier record (review 2026-11-03).
    """
    target = path or history_path()
    record = {
        "component": str(component),
        "kind": str(kind),
        "source": str(source),
        "destination": str(destination),
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = _previous_records(target)
        data = [record, *previous][:MAX_RECORDS]
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, target)
        temporary = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
