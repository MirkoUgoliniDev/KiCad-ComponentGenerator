"""@brief Persistent application preferences stored in config.json.

Persistent application configuration (config.json next to main.py).

The interface preferences live here. Prompts and models of earlier versions
are read only for the one-time migration into the SQLite database of the AI
profiles; save_config does not write them back into the JSON. The parameters
of the single footprint live in the window's fields.

The file is plain JSON, so it can be inspected and edited by hand; it holds
no secrets (the API key stays in the .env).
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
import math
import os
import shutil
from pathlib import Path

from ..services.datasheet_extractor import DEFAULT_SYSTEM_PROMPT
from ..services.openrouter import DEFAULT_MODEL
from ..services.photo_grounding import DEFAULT_VISION_MODEL, GROUNDING_PROMPT, VERIFICATION_PROMPT
from ..services.messages import tx
from ..app_paths import user_data_dir

#: config.json in the data folder: the repository when run from source (as
#: before), %LOCALAPPDATA%\KiCadGen in the installed program (see `app_paths`).
CONFIG_PATH = user_data_dir() / "config.json"


def config_path() -> Path:
    """@brief Return the config.json path, overridable through KICADGEN_CONFIG_PATH.

    @return The path named by the KICADGEN_CONFIG_PATH environment variable
        when set, otherwise CONFIG_PATH in the user data directory.
    """
    return Path(os.environ.get("KICADGEN_CONFIG_PATH", CONFIG_PATH))

#: SHA-256 of the system prompts that were the default in the past.
#: A config.json that still contains one of these has not been customised
#: by the user: it is updated to the current default, so that prompt
#: improvements also reach whoever already has the file saved.
#: Whoever modified it keeps it: their version is not in the list.
LEGACY_DEFAULT_PROMPT_HASHES = {
    # Default up to the version without the "layout" field (matrix only).
    "c744e556d5e1eba95f41d69b5eb635c2ab8bcc0cf77f5c1d163b18ca3a41feea",
    # Default that asked only for the main components
    # ("non serve mapparli tutti" (no need to map them all)).
    "eefd487b98f9217c71c14976fe71ac0095a14dd8ed9b8bd1b5d58e8231c0aafa",
}


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256((prompt if isinstance(prompt, str) else '').strip().encode("utf-8")).hexdigest()

DEFAULTS = {
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    # Default model of the AI extraction: an OpenRouter id, chosen from the
    # "Modello e prompt di sistema LLM" (LLM model and system prompt) window.
    # A single extraction can use another one without touching this value.
    "model": DEFAULT_MODEL,
    "vision_model": DEFAULT_VISION_MODEL,
    "grounding_prompt": GROUNDING_PROMPT,
    "verification_prompt": VERIFICATION_PROMPT,
    # Last selected component type: offered again at start-up, because
    # components of the same type are usually generated in series.
    "component_type": "",
    # NC (Not Connected) pins are electrically disconnected but physically
    # present: by default they are generated, otherwise the hole is missing
    # and the component does not fit into the board.
    "include_nc_pins": True,
    # Last folder from which documents to load were taken. Files almost
    # always arrive in series from the same folder (the views of the same
    # component), while the project folder -- where the copies end up -- is
    # never the one they are picked from.
    "last_document_dir": "",
    # Pin labels engraved on the silkscreen of the 3D model. Off: on a
    # fine-pitch module the long names overlap each other and end up on top
    # of the components, making the solid less readable instead of more.
    "silk_pin_labels": False,
    "show_grid": True,
    "grid_unit": "mm",  # "mm" or "inch" (2.54mm pitch, THT standard)
    "show_pin_numbers_3d": True,
    # Active UI language; translations are stored in the profile SQLite catalog.
    "language": "it",
}


_LOG = logging.getLogger(__name__)

#: Message templates reported by `load_config` (Italian sources of `tx`).
CONFIG_BAD_TYPE_MESSAGE = ("config.json: valore di «{key}» non valido ({found} invece di "
                           "{expected}), uso il predefinito.")
CONFIG_CORRUPT_KEPT_MESSAGE = ("config.json illeggibile ({error}): copia conservata in {backup}, "
                               "uso le impostazioni predefinite.")
CONFIG_UNREADABLE_MESSAGE = "config.json illeggibile ({error}): uso le impostazioni predefinite."


def _same_kind(value, default) -> bool:
    """True when a loaded value has the JSON kind of its default.

    bool is its own kind (in Python it is also an int); int and float are
    the same kind, finite only; anything else must share the default's type.
    """
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    return isinstance(value, type(default))


#: Problems of the most recent `load_config`, until `take_load_problems`.
_pending_problems: list = []


def _keep_corrupt_copy(path: Path) -> Path | None:
    """Copy an unparsable config.json aside before a later save replaces it.

    Returns the copy (an existing one with the same bytes is reused, so
    reading the same broken file several times keeps one copy), or None
    when it could not be made.
    """
    try:
        content = path.read_bytes()
        for existing in sorted(path.parent.glob(f"{path.name}.corrupt-*")):
            if existing.is_file() and existing.read_bytes() == content:
                return existing
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}")
        shutil.copy2(path, backup)
    except OSError:
        return None
    return backup


def _report(problems: list, template: str, **values) -> None:
    """Log a load problem and keep it, untranslated, for the caller."""
    _LOG.warning(template.format(**values))
    problems.append((template, values))


def take_load_problems() -> list:
    """@brief Return and forget the problems found by the most recent `load_config`.

    @return ``(template, values)`` pairs: the template is the Italian source
        of `messages.tx`, translated by the caller once its translator is
        installed (config.json is read before it).
    """
    problems = list(_pending_problems)
    _pending_problems.clear()
    return problems


def load_config(report: list | None = None) -> dict:
    """@brief Read config.json merged over the defaults; an unreadable file yields the defaults.

    @param report Optional list that also receives the ``(template, values)``
        problems of this call (see `take_load_problems`). Every problem is
        written to the `logging` module too.
    @return The defaults updated with the valid values of the file.
    @note A key that has a default keeps the default when the file holds a
        value of another kind (a list where a string is expected, a string
        where a boolean is expected): one bad value never reaches the widgets
        or makes the window fail at start. The rule is generic, by the kind
        of the default, not per key; keys without a default are copied as
        they are and validated where they are read.
    @note A file that cannot be parsed still yields the defaults (intended:
        the program must start), but it is first copied to
        ``config.json.corrupt-<timestamp>``, so the next save -- which
        rewrites config.json from the defaults -- does not destroy the
        user's settings without a trace.
    @note Read as ``utf-8-sig``: a file saved by an editor with a BOM is
        valid JSON for the user and must not be treated as corrupt.
    """
    problems: list = []
    config = _read_config(problems)
    _pending_problems[:] = problems
    if report is not None:
        report.extend(problems)
    return config


def _read_config(problems: list) -> dict:
    """The body of `load_config`; problems are appended to `problems`."""
    config = dict(DEFAULTS)
    path = config_path()
    if not path.is_file():
        return config
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            loaded = json.load(fh)
    except OSError as exc:
        # Locked or unreadable: nothing to copy, the file is left as it is.
        _report(problems, CONFIG_UNREADABLE_MESSAGE, error=exc)
        return config
    except ValueError as exc:
        loaded = exc
    if not isinstance(loaded, dict):
        error = loaded if isinstance(loaded, ValueError) else \
            tx("La configurazione deve essere un oggetto JSON.")
        backup = _keep_corrupt_copy(path)
        if backup is None:
            _report(problems, CONFIG_UNREADABLE_MESSAGE, error=error)
        else:
            _report(problems, CONFIG_CORRUPT_KEPT_MESSAGE, error=error, backup=backup)
        return config
    for key, value in loaded.items():
        if key in DEFAULTS and not _same_kind(value, DEFAULTS[key]):
            _report(problems, CONFIG_BAD_TYPE_MESSAGE, key=key, found=type(value).__name__,
                    expected=type(DEFAULTS[key]).__name__)
            continue
        config[key] = value
    if _prompt_hash(config.get("system_prompt") or "") in LEGACY_DEFAULT_PROMPT_HASHES:
        config["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    return config


def browse_start_dir(config, fallback="") -> str:
    """@brief Return the last document folder if it still exists, otherwise `fallback`.

    Folder a document-loading dialog should start from.

    The last folder used counts, but only while it still exists: a removed
    folder, or one on a disconnected drive, would open the dialog in a dead
    spot, and falling back to the caller's `fallback` is better.

    @param config Preferences dict (may be None); its `last_document_dir`
        entry is the remembered folder.
    @param fallback Folder returned when nothing usable is remembered.
    @return The remembered folder when it is still a directory, otherwise
        `fallback` as a string ("" when None).
    """
    remembered = (config or {}).get("last_document_dir") or ""
    try:
        if remembered and Path(remembered).is_dir():
            return remembered
    except OSError:
        pass
    return str(fallback or "")


def remember_browse_dir(config, selected) -> None:
    """@brief Remember the folder of the selected file in `config` and save config.json.

    Stores the folder of the chosen file, updating config.json as well.

    `config` is the window's live dictionary: updating it here prevents a
    later save, made from the in-memory copy, from overwriting the
    preference just recorded.

    @param config Live preferences dict of the window; None does nothing.
    @param selected Chosen file or folder; its parent folder is remembered
        when it is a file. Empty values do nothing.
    """
    if config is None or not selected:
        return
    try:
        directory = Path(selected)
        directory = str(directory if directory.is_dir() else directory.parent)
    except OSError:
        return
    if config.get("last_document_dir") == directory:
        return
    config["last_document_dir"] = directory
    # Convenience preference: if the file is not writable, save_config
    # returns False, the current session uses it anyway and loading is not
    # interrupted.
    save_config(config)


def save_config(config: dict) -> bool:
    """@brief Atomically write the interface preferences to config.json, leaving out the AI profile fields.

    The JSON is first serialised in memory, then written to a temporary file in
    the same folder, flushed, fsynced and moved onto config.json
    (`fsutil.atomic_write_bytes`, shared with the project file).
    A failure at any step (locked file, read-only folder, full disk, value
    that is not JSON-serialisable) leaves the previous config.json untouched
    and removes the temporary file.

    @param config Live configuration dictionary of the window.
    @return True when config.json now holds `config`; False when it could not
        be written (the previous file, if any, is intact).
    @note Never raises for I/O or serialisation errors: it is called from Qt
        slots, where an escaping exception ends the process through the crash
        report hook. The caller decides whether to log the failure.
    """
    # AI profiles are persisted only in SQLite after startup migration.
    from ..services.analysis_profiles import PROFILE_FIELDS
    from ..fsutil import atomic_write_bytes
    preferences = {k: v for k, v in config.items() if k not in PROFILE_FIELDS}
    try:
        # Serialising before touching the disk means an unserialisable value
        # can never leave a half-written file behind.
        # Encoded here too: a lone surrogate (a Windows file name that is not
        # valid UTF-16) raises UnicodeEncodeError, a ValueError, at encoding
        # time -- inside this try, not later in the write.
        payload = json.dumps(preferences, indent=2, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return False
    target = config_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, payload)
        return True
    except OSError:
        return False
