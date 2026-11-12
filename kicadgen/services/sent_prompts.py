"""@brief Registry of the prompt texts sent to a model: a number the analysis log shows, back to the exact text.

@details User request (2026-11-12): the progress card and the analysis log
named each prompt sent by the first 8 characters of its SHA-256; the user
wants a number that leads back to the prompt. Every text sent gets one
(the same text of the same prompt field always the same number), and
«Strumenti > Prompt inviati...» turns the number back into the text.

The registry is its OWN file, ``data/sent_prompts.sqlite3`` in the user's
data folder, NOT a table of the profile database: that database is committed
and published, and a custom prompt may hold the user's notes (adversarial
review of 2026-11-12); a schema bump there would also lock older builds out
of the user's profiles. ``.gitignore`` excludes ``data/*.sqlite3`` but the
profile database. In the tests the data folder is the test's own (conftest).

@note Numbers are unique within ONE registry file: deleting or replacing the
    file starts again from 1. The full digest stays in the log file's
    ``AI_PROMPT_SENT`` line and in the dialog's tooltip, to check that a number
    of an old log still names the same text.
"""
from __future__ import annotations

import datetime
import sqlite3
import threading
from pathlib import Path
from typing import Optional

#: File name of the registry in the data folder.
REGISTRY_FILE = "sent_prompts.sqlite3"

#: Seconds a write waits for another connection's lock before failing; a
#: failure only drops the number from one log line (see `register`).
_TIMEOUT_S = 2.0

_SCHEMA = ("CREATE TABLE IF NOT EXISTS sent_prompts ("
           "number INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL, field TEXT NOT NULL, "
           "status TEXT NOT NULL, text TEXT NOT NULL, first_sent TEXT NOT NULL, "
           "UNIQUE(sha256, field))")

#: Paths whose table already exists in this process: the schema statement is
#: run once per file, not at every prompt (review 2026-11-12, finding 6).
_READY: set[str] = set()
_READY_LOCK = threading.Lock()


def registry_path() -> Path:
    """@brief The registry file of the running program.

    @return ``<user data folder>/data/sent_prompts.sqlite3`` (app_paths).
    """
    from ..app_paths import data_file
    return data_file(REGISTRY_FILE)


def _connect(path: Path) -> sqlite3.Connection:
    """Open the registry, creating the folder and the table the first time in this process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    # A file deleted while the program runs must get its table again.
    with _READY_LOCK:
        ready = key in _READY and path.exists()
    connection = sqlite3.connect(path, timeout=_TIMEOUT_S)
    connection.row_factory = sqlite3.Row
    if not ready:
        try:
            with connection:
                connection.execute(_SCHEMA)
        except sqlite3.Error:
            connection.close()
            raise
        with _READY_LOCK:
            _READY.add(key)
    return connection


def register(field: str, text: str, status: str, path: Optional[Path] = None) -> int:
    """@brief The number of a prompt text sent, registering it the first time.

    @param field Profile prompt field (prompt_catalog.PROMPTS).
    @param text The exact text sent.
    @param status prompt_catalog status of `text` when first sent (kept as it was then).
    @param path Registry file; None uses `registry_path()`.
    @return The number: the same (text, field) always the same number, a new
        text the next one.
    @throws sqlite3.Error When the file cannot be written.
    @throws OSError When the folder cannot be created.
    """
    from .component_types import text_digest
    digest = text_digest(text)
    connection = _connect(Path(path) if path else registry_path())
    try:
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO sent_prompts(sha256, field, status, text, first_sent) "
                "VALUES (?,?,?,?,?)",
                (digest, str(field), str(status), text,
                 datetime.datetime.now().astimezone().isoformat(timespec="seconds")))
            row = connection.execute("SELECT number FROM sent_prompts WHERE sha256=? AND field=?",
                                     (digest, str(field))).fetchone()
    finally:
        connection.close()
    return int(row["number"])


def lookup(number, path: Optional[Path] = None) -> Optional[dict]:
    """@brief One registered prompt by its number.

    @param number Registry number.
    @param path Registry file; None uses `registry_path()`.
    @return {number, sha256, field, status, text, first_sent}, or None when
        no prompt has that number or the registry does not exist yet.
    @throws sqlite3.Error When the file cannot be read.
    """
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    target = Path(path) if path else registry_path()
    if not target.exists():
        return None
    connection = _connect(target)
    try:
        row = connection.execute("SELECT number,sha256,field,status,text,first_sent FROM sent_prompts "
                                 "WHERE number=?", (number,)).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None
