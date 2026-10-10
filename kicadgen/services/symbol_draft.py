"""@brief Project-local crash recovery; separate from the applied component configuration."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from ..fsutil import replace_file


def fingerprint(rows):
    """@brief Hash applied row data to bind recovery drafts to an exact baseline."""
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def save_draft(path, baseline, rows):
    """@brief Atomically persist recovery rows and the applied-baseline fingerprint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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


def load_draft(path, baseline):
    """@brief Read a bounded recovery file only when its baseline and pin identities match."""
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
    except (ValueError, KeyError, AttributeError):
        return None
