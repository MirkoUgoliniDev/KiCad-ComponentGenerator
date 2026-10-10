"""Persistent local history of artifacts installed or exported to KiCad."""
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
    """Return the per-user KiCad export history path."""
    return user_data_dir() / HISTORY_FILENAME


def load_history(path: Path | None = None) -> list[dict]:
    """Load newest-first export records, returning an empty list on bad data."""
    target = path or history_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)][:MAX_RECORDS]


def record_export(*, component: str, kind: str, source: str,
                  destination: str, path: Path | None = None) -> bool:
    """Atomically add a successful KiCad export/install event to local history."""
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
        data = [record, *load_history(target)][:MAX_RECORDS]
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
