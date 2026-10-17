"""@brief Project-local crash recovery; separate from the applied component configuration."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from ..fsutil import replace_file


def fingerprint(rows):
    """@brief Hash applied row data to bind recovery drafts to an exact baseline.

    @param rows JSON-serializable list of applied symbol rows.
    @return SHA-256 hex digest of their canonical JSON.
    """
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def save_draft(path, baseline, rows):
    """@brief Atomically persist recovery rows and the applied-baseline fingerprint.

    @param path Recovery file (`<project>/.symbol-draft.json`).
    @param baseline Applied rows the draft was made on.
    @param rows Draft rows to keep.
    @return True when the file was written; False when the project folder
        does not exist: no folder is ever created for a draft.
    @throws OSError When the folder exists but the file cannot be written.
    @note CONTRACT CHANGED (audit L3, 2026-10-15): the folder used to be
        created. A project renamed or deleted while its editor was still bound
        to the old path then came back as a ghost folder holding only the
        draft. Same rule as `services.footprint_draft`.
    """
    path = Path(path)
    if not path.parent.is_dir():
        return False
    text = json.dumps({'version': 1, 'baseline': fingerprint(baseline), 'rows': rows}, ensure_ascii=False)
    fd, temp = tempfile.mkstemp(prefix='.symbol-recovery-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)
    return True


def load_draft(path, baseline):
    """@brief Read a bounded recovery file only when its baseline and pin identities match.

    @param path Recovery file (`<project>/.symbol-draft.json`).
    @param baseline Applied rows the draft must have been made on.
    @return The draft rows, or None when the file is missing, larger than
        2 MB, unreadable, of another version or baseline, with different pin
        identities, or identical to the baseline.
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 2000000:
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or data.get('baseline') != fingerprint(baseline):
            return None
        rows = data['rows']
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            return None
        if [str(r.get('pin')) for r in rows] != [str(r.get('pin')) for r in baseline]:
            return None
        return rows if rows != baseline else None
    except (ValueError, KeyError, AttributeError, RecursionError):
        # RecursionError: a deeply nested file (well under the size bound)
        # exceeds the JSON parser's depth; it is unreadable, not fatal.
        return None
