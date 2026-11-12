"""@brief Read one API key from a `.env` file and remove its assignments, leaving the rest of the file alone.

@details User request (2026-11-12): «Account AI» moves a key from a `.env`
file into the Windows Credential Manager with one button, then offers to
remove it from the file. Both reading and removing use python-dotenv's own
parser, the one the key lookups use (openrouter._dotenv_key,
providers._deepseek_dotenv_key): every binding it reports carries its
original text, so the file is rebuilt from those texts minus the bindings
of one name -- a quoted value spanning several lines, an «export» prefix, a
quoted name, comments and line endings are handled exactly as the parser
sees them. A leading UTF-8 BOM is kept. The new file replaces the old one
atomically (temporary file in the same folder, then os.replace on the
resolved path, so a symbolic link keeps pointing at the edited file).
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

# Imported by name, like openrouter and providers do, so tests can point
# this module alone at a temporary file without replacing app_paths.env_files
# for the tests of app_paths itself.
from ..app_paths import env_files

#: What `remove_key_result` did.
REMOVED = "removed"
#: No binding of that name in the file: nothing changed.
ABSENT = "absent"
#: The binding only refers to another variable («${OTHER}»): removing it would
#: leave the secret in that other variable, so nothing changed.
INDIRECT = "indirect"

_BOM = "﻿"


def read_key(path, name: str) -> str:
    """@brief The value `name` has in the `.env` file `path`, stripped; "" when absent or unreadable.

    @param path The `.env` file.
    @param name The variable, e.g. «DEEPSEEK_API_KEY».
    @return The value (interpolated as python-dotenv does), never logged by this module.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return ""
    try:
        return (dotenv_values(Path(path)).get(name) or "").strip()
    except (OSError, ValueError):
        return ""


def _bindings(text: str):
    """The python-dotenv bindings of `text` (BOM already removed), in file order."""
    from dotenv.parser import parse_stream
    return list(parse_stream(io.StringIO(text)))


def remove_key_result(path, name: str) -> str:
    """@brief Remove from `path` every binding of `name`; everything else stays byte for byte.

    @param path The `.env` file (a symbolic link is followed to its target).
    @param name The variable whose bindings go away.
    @return REMOVED, ABSENT (no binding of `name`) or INDIRECT (a binding of
        `name` only refers to another variable with «${…}»: nothing removed,
        because the secret lives in that other variable).
    @throws OSError When the file cannot be read, decoded as UTF-8, parsed
        or replaced; the file is then unchanged.
    """
    path = Path(path).resolve()
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError(f"{path}: not UTF-8 ({exc.reason})") from exc
    bom = text.startswith(_BOM)
    body = text[len(_BOM):] if bom else text
    try:
        bindings = _bindings(body)
    except ImportError as exc:
        raise OSError("python-dotenv is not available") from exc
    if "".join(binding.original.string for binding in bindings) != body:
        # The parser must account for every character, or a rebuild could
        # lose text: refuse rather than guess.
        raise OSError(f"{path}: the .env parser did not account for the whole file")
    mine = [binding for binding in bindings if binding.key == name]
    if not mine:
        return ABSENT
    if any("${" in (binding.value or "") for binding in mine):
        return INDIRECT
    kept = "".join(binding.original.string for binding in bindings if binding.key != name)
    data = ((_BOM if bom else "") + kept).encode("utf-8")
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return REMOVED


def remove_key(path, name: str) -> bool:
    """@brief `remove_key_result` as a yes/no: True only when bindings were removed.

    @param path The `.env` file.
    @param name The variable whose bindings go away.
    @return True when the file changed.
    @throws OSError As `remove_key_result`.
    """
    return remove_key_result(path, name) == REMOVED


def other_files_setting(name: str, except_path) -> list[str]:
    """@brief The other `.env` files the program reads that still set `name`.

    @param name The variable.
    @param except_path The file already handled.
    @return Their paths, in the order the program reads them (app_paths.env_files).
    @note Run from the sources, two `.env` files are read: removing the key
        from one leaves any copy in the other, which the window must say.
    """
    handled = Path(except_path).resolve()
    found = []
    for candidate in env_files():
        try:
            if candidate.is_file() and candidate.resolve() != handled and read_key(candidate, name):
                found.append(str(candidate))
        except OSError:
            continue
    return found
