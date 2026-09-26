"""@brief Recently used projects, remembered in config.json under `recent_projects`.

PIANO-FIX item 1: with no project open the main window shows a start page with
the projects used last, so that one click reopens one. This module owns the
list; it knows nothing about Qt and nothing about the window.

The list is a JSON array of absolute folder paths, most recent first, at most
`MAX_RECENT` entries. Two spellings of the same folder (letter case on Windows,
forward or back slashes, a trailing separator) count as one entry.

The pure functions (`stored`, `add`, `remove`, `load`) take the configuration
dictionary and never touch the disk except to check that a folder is still a
project. `remember` and `forget` also write config.json through `save_config`.

@note The window keeps ONE live configuration dictionary and saves it whole
    (`app_config.save_config`). A helper that loaded and saved its own copy
    would be overwritten by the next save of the window, which still holds
    the old list: this is why every function here works on the caller's
    dictionary instead of reading config.json again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path

#: Key of config.json that holds the list.
CONFIG_KEY = "recent_projects"

#: Longest list kept. A reading choice, not a measurement: ten rows fit on the
#: start page without scrolling at 100% scaling, and older projects are still
#: reachable from «Apri progetto…».
MAX_RECENT = 10

#: File that makes a folder a project the window can reopen. It must stay
#: equal to `kicadgen.gui.project_state.PROJECT_CONFIG_NAME`; that module
#: imports Qt, which this one must not, so the name is repeated here and a
#: test checks that the two agree.
PROJECT_CONFIG_NAME = "project_config.json"


@dataclass(frozen=True)
class RecentProject:
    """@brief What the start page shows about one recent project.

    @note Every field except `path` and `name` may be empty: a project file
        that cannot be read still leaves a project that can be opened (the
        window then says why its settings were not applied).
    """

    #: Absolute project folder, as stored.
    path: str
    #: Folder name, which is also the project name.
    name: str
    #: Component type label saved in the project file ("" when unknown).
    component_type: str = ""
    #: Component type key saved in the project file ("" when unknown).
    component_type_key: str = ""
    #: Last modification of the project file, or None when unreadable.
    modified: datetime | None = None


def _key(path: str) -> str:
    """Comparison key: same folder, same key (case-insensitive on Windows)."""
    return os.path.normcase(os.path.normpath(path))


def _clean(path) -> str | None:
    """Absolute, normalised spelling of `path`, or None when it cannot be one.

    Relative paths are refused instead of being resolved against the current
    directory: the stored list must mean the same folders whatever directory
    the program was started from.
    """
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str) or not path.strip():
        return None
    path = path.strip()
    if not os.path.isabs(path):
        return None
    return os.path.normpath(path)


def stored(config) -> list[str]:
    """@brief The stored list, cleaned, without looking at the disk.

    @param config Configuration dictionary (None counts as empty).
    @return Absolute paths, most recent first, without duplicates, at most
        `MAX_RECENT`. A value that is not a list gives an empty list; entries
        that are not non-empty absolute path strings are dropped.
    @note Robust to a hand-edited or corrupted config.json: nothing here raises.
    """
    value = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    if not isinstance(value, list):
        return []
    result, seen = [], set()
    for entry in value:
        path = _clean(entry)
        if path is None or _key(path) in seen:
            continue
        seen.add(_key(path))
        result.append(path)
        if len(result) == MAX_RECENT:
            break
    return result


def is_project(path) -> bool:
    """@brief Tell whether a folder can be reopened as a project.

    @param path Candidate folder.
    @return True when the folder exists and holds `PROJECT_CONFIG_NAME`.
    @note Every project the window creates or opens gets its project file at
        once (`MainWindow._new_project`, `_open_project`), so a folder
        without it is not one of ours any more (deleted, moved, emptied).
    """
    try:
        return Path(path).is_dir() and (Path(path) / PROJECT_CONFIG_NAME).is_file()
    except (OSError, TypeError, ValueError):
        return False


def add(config: dict, path) -> list[str]:
    """@brief Put a project at the top of the list.

    @param config Live configuration dictionary; updated in place.
    @param path Project folder; must be absolute.
    @return The new list.
    @throws ValueError When `path` is not an absolute path.
    @note An entry equal to `path` (ignoring case on Windows) is moved, not
        duplicated, and takes the new spelling. The oldest entries beyond
        `MAX_RECENT` are dropped, even those whose folder is only missing
        for now (a disconnected drive): the cap counts what is stored.
    """
    clean = _clean(path)
    if clean is None:
        raise ValueError(f"Recent project path must be absolute: {path!r}")
    others = [entry for entry in stored(config) if _key(entry) != _key(clean)]
    config[CONFIG_KEY] = ([clean] + others)[:MAX_RECENT]
    return list(config[CONFIG_KEY])


def remove(config: dict, path) -> list[str]:
    """@brief Take a project off the list; the folder on disk is not touched.

    @param config Live configuration dictionary; updated in place.
    @param path Project folder to forget (any spelling of it).
    @return The new list; unchanged when the project was not listed or
        `path` is not an absolute path.
    """
    clean = _clean(path)
    current = stored(config)
    if clean is not None:
        current = [entry for entry in current if _key(entry) != _key(clean)]
    config[CONFIG_KEY] = current
    return list(current)


def load(config) -> list[str]:
    """@brief The recent projects that can be reopened now.

    @param config Configuration dictionary.
    @return Stored paths, in order, whose folder still exists and holds a
        project file.
    @note A folder that is missing or is no longer a project is skipped,
        not removed from the stored list: a drive that comes back makes it
        reappear. Only `remove` forgets an entry.
    """
    return [path for path in stored(config) if is_project(path)]


def describe(path) -> RecentProject:
    """@brief Read what the start page shows about a project folder.

    @param path Project folder.
    @return The summary; type and date stay empty when the project file is
        missing, unreadable or not in the expected layout.
    @note Only reads the file; it never validates or applies it. The window
        does that when the project is opened, and reports the problems there.
    """
    folder = Path(path)
    summary = {"path": str(path), "name": folder.name or str(path)}
    config_file = folder / PROJECT_CONFIG_NAME
    try:
        summary["modified"] = datetime.fromtimestamp(config_file.stat().st_mtime)
    except (OSError, OverflowError, ValueError):
        pass
    try:
        document = json.loads(config_file.read_text(encoding="utf-8"))
        combo = document["state"]["widgets"]["combo_component_type"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        combo = None
    if isinstance(combo, dict):
        if isinstance(combo.get("text"), str):
            summary["component_type"] = combo["text"].strip()
        if isinstance(combo.get("data"), str):
            summary["component_type_key"] = combo["data"].strip()
    return RecentProject(**summary)


def remember(config: dict, path, save=None) -> bool:
    """@brief `add` and save config.json.

    @param config Live configuration dictionary of the window.
    @param path Project folder just created, opened or renamed.
    @param save Function that writes the whole dictionary; None means
        `kicadgen.gui.app_config.save_config` (Qt-free, imported lazily).
    @return True when config.json was written; False when the path is not
        absolute or the file cannot be written (the list in memory is still
        updated in the second case, so the current session keeps it).
    """
    try:
        add(config, path)
    except ValueError:
        return False
    return _save(config, save)


def forget(config: dict, path, save=None) -> bool:
    """@brief `remove` and save config.json.

    @param config Live configuration dictionary of the window.
    @param path Project folder to take off the list.
    @param save Same as in `remember`.
    @return True when config.json was written.
    """
    remove(config, path)
    return _save(config, save)


def _save(config: dict, save) -> bool:
    """Write config.json; a convenience list must never stop the window."""
    if save is None:
        from ..gui.app_config import save_config as save
    try:
        save(config)
    except OSError:
        return False
    return True
