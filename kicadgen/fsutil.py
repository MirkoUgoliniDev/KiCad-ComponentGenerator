"""@brief File-system helpers shared by the atomic writes of the application.

On Windows an ``os.replace`` onto a file that another process holds open for
an instant (antivirus scan, search indexer, a KiCad or editor preview) fails
with ``PermissionError`` (WinError 5 or 32) even though a moment later it
would succeed. Seen on 2026-09-25 in the test suite on `.document-roles.json`.
Every atomic write goes through `replace_file` so this transient refusal is
retried briefly instead of losing the user's change.
"""
from __future__ import annotations

import os
import time

#: Attempts and first pause of `replace_file`. The pauses double each time
#: (0.05 + 0.1 + 0.2 + 0.4 s): under a second in total, long enough for a
#: scanner to release the file, short enough not to freeze the interface.
REPLACE_ATTEMPTS = 5
REPLACE_FIRST_PAUSE_S = 0.05


def replace_file(source, target, *, attempts: int = REPLACE_ATTEMPTS,
                 first_pause: float = REPLACE_FIRST_PAUSE_S, sleep=time.sleep) -> None:
    """@brief Atomically move `source` onto `target`, retrying transient access refusals.

    @param source Existing file (normally a temporary file in the target folder).
    @param target Destination path; replaced atomically when it exists.
    @param attempts Total attempts, at least 1.
    @param first_pause Seconds before the second attempt; doubled afterwards.
    @param sleep Pause function, replaceable in tests.
    @throws PermissionError When the target is still refused after `attempts`.
    @throws OSError Any other error immediately: only PermissionError is
        transient (a missing file or a full disk does not heal by waiting).
    """
    pause = first_pause
    for attempt in range(max(1, int(attempts))):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt >= max(1, int(attempts)) - 1:
                raise
            sleep(pause)
            pause *= 2


def atomic_write_bytes(target, payload: bytes, *, prefix: str | None = None) -> None:
    """@brief Write `payload` to `target` durably: temporary file, flush, fsync, atomic replace.

    @param target Destination path; its folder must exist.
    @param payload Complete file content.
    @param prefix Prefix of the temporary file in the target folder
        (default: ``.<target name>.``).
    @throws OSError When the temporary file cannot be written or the replace
        is refused (see `replace_file`); the previous `target` is then
        untouched and the temporary file is removed.
    @note The fsync before the replace makes the rename publish data that is
        already on disk: without it a power loss right after the replace can
        leave an empty or truncated file under the final name on some file
        systems. Shared by the project file, config.json and the other
        settings files so they cannot drift apart.
    """
    import tempfile
    from pathlib import Path
    target = Path(target)
    handle, temporary = tempfile.mkstemp(prefix=prefix or f".{target.name}.", suffix=".tmp",
                                         dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
