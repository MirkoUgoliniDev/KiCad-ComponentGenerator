"""@brief Project-local crash recovery of the unapplied footprint edits.

The footprint preview keeps its edits (user drawings, Reference/Value text
positions, host pad overrides, package pad edits) as a DRAFT until «Applica»
(user, 2026-10-14). The draft is checkpointed here, beside the project file,
with the same conventions as the symbol's recovery file
(`services.symbol_draft`): JSON files in the project folder, written
atomically, bound to the applied state they were made on by a fingerprint,
read with a size bound, never creating folders.

Two files:
- FOOTPRINT_DRAFT_FILE holds the current draft (checkpoint of every edit);
- KEPT_DRAFTS_FILE holds older drafts that were offered after a crash and
  displaced by newer edits before the user recovered or discarded them
  (newest first). They stay offered until recovered or discarded.

@note The state handled here is the JSON form of the footprint edits:
    {"drawings": [...], "texts": {key: [x, y]}, "pad_overrides": {...},
    "package_pins": [pin mapping, ...] or None}. Validating the values
    against the open project is the caller's job (the same validators that
    read the project file).
"""
import json
import os
from pathlib import Path
import tempfile

from ..fsutil import replace_file
from .symbol_draft import fingerprint

#: Name of the recovery file inside the project folder (sibling of
#: `.symbol-draft.json`).
FOOTPRINT_DRAFT_FILE = '.footprint-draft.json'

#: Offered crash drafts displaced by newer edits, kept until the user
#: recovers or discards them.
KEPT_DRAFTS_FILE = '.footprint-draft-kept.json'

#: Keys of a footprint edit state, in the order they are written.
STATE_KEYS = ('drawings', 'texts', 'pad_overrides', 'package_pins')

#: Size bound of a recovery file read back (same bound as the symbol's).
MAX_DRAFT_BYTES = 2000000

#: Disk-usage bound of the kept drafts (oldest dropped first).
MAX_KEPT_DRAFTS = 10


def _write_json(path, data):
    """Atomically write `data` as JSON at `path`; False when the folder does not exist."""
    path = Path(path)
    if not path.parent.is_dir():
        return False
    text = json.dumps(data, ensure_ascii=False, allow_nan=False)
    fd, temp = tempfile.mkstemp(prefix='.footprint-recovery-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)
    return True


def _read_json(path):
    """Bounded read of a JSON file; None when missing, too large or unreadable as JSON.

    @throws OSError When the file exists but cannot be read.
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_DRAFT_BYTES:
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, RecursionError):
        # RecursionError: a deeply nested file (well under the size bound)
        # exceeds the parser's depth; unreadable, never fatal for the caller.
        return None


def _valid_state(draft):
    """The draft restricted to STATE_KEYS, or None when it is not a complete state mapping."""
    if not isinstance(draft, dict) or any(key not in draft for key in STATE_KEYS):
        return None
    return {key: draft[key] for key in STATE_KEYS}


def save_footprint_draft(path, baseline, draft):
    """@brief Atomically write the draft, bound to the fingerprint of the applied state.

    @param path Recovery file (`<project>/FOOTPRINT_DRAFT_FILE`).
    @param baseline JSON form of the applied footprint state.
    @param draft JSON form of the draft footprint state.
    @return True when the file was written; False when the project folder
        does not exist (no folder is ever created for a draft).
    @throws OSError When the folder exists but the file cannot be written.
    """
    return _write_json(path, {'version': 1, 'baseline': fingerprint(baseline), 'draft': draft})


def load_footprint_draft(path, baseline):
    """@brief Read a recovery file only when it is bound to `baseline` and differs from it.

    @param path Recovery file.
    @param baseline JSON form of the applied footprint state now in the project.
    @return The draft state (JSON form, every key of STATE_KEYS present), or
        None when the file is missing, too large, malformed (also too deeply
        nested), bound to another applied state, or holds nothing more than
        the applied state.
    @throws OSError When the file exists but cannot be read.
    """
    data = _read_json(path)
    if not isinstance(data, dict) or data.get('version') != 1:
        return None
    if data.get('baseline') != fingerprint(baseline):
        return None
    draft = _valid_state(data.get('draft'))
    return None if draft is None or draft == baseline else draft


def remove_footprint_draft(path):
    """@brief Delete the recovery file, if any.

    @param path Recovery file.
    @throws OSError When the file exists and cannot be deleted.
    """
    Path(path).unlink(missing_ok=True)


def load_kept_drafts(path):
    """@brief Read the kept (displaced) crash drafts, newest first.

    @param path Kept-drafts file (`<project>/KEPT_DRAFTS_FILE`).
    @return List of draft states (JSON form); malformed entries are dropped,
        an unreadable file gives [].
    @note Not bound to the applied state: each entry is a complete footprint
        state the user has not yet recovered or discarded, and recovering it
        re-validates every value against the open project.
    @throws OSError When the file exists but cannot be read.
    """
    data = _read_json(path)
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('drafts'), list):
        return []
    return [state for state in (_valid_state(d) for d in data['drafts']) if state is not None]


def save_kept_drafts(path, drafts):
    """@brief Write the kept drafts (newest first, at most MAX_KEPT_DRAFTS); remove the file when empty.

    @param path Kept-drafts file.
    @param drafts Draft states (JSON form), newest first; duplicates are dropped.
    @return True when written or removed; False when the project folder does
        not exist.
    @throws OSError When the file cannot be written or removed.
    """
    unique = []
    for draft in drafts:
        if draft not in unique:
            unique.append(draft)
    if not unique:
        Path(path).unlink(missing_ok=True)
        return True
    return _write_json(path, {'version': 1, 'drafts': unique[:MAX_KEPT_DRAFTS]})
