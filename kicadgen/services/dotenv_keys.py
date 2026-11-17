"""@brief Read one API key from a `.env` file and remove its assignments, leaving the rest of the file alone.

@details User request (2026-11-12): «Account AI» moves a key from a `.env`
file into the Windows Credential Manager with one button, then offers to
remove it from the file. Both reading and removing use python-dotenv's own
parser, the one the key lookups use (openrouter._dotenv_key,
providers._deepseek_dotenv_key): every binding it reports carries its
original text, so the file is rebuilt from those texts minus the bindings
of one name -- a quoted value spanning several lines, an «export» prefix, a
quoted name, comments and line endings are handled exactly as the parser
sees them. The file is decoded as the launcher decodes it (byte-order mark or
strict UTF-8) and written back in the same encoding with the same mark. The
new file replaces the old one
atomically (temporary file in the same folder, then os.replace on the
resolved path, so a symbolic link keeps pointing at the edited file).
"""
from __future__ import annotations

import codecs
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


def file_values(path) -> dict:
    """@brief The variables python-dotenv reads in the `.env` file `path`, decoded as the launcher does.

    @param path The `.env` file.
    @return Name -> value (interpolated as python-dotenv does).
    @throws OSError When the file cannot be read.
    @throws ValueError When its bytes are not text in a supported encoding
        (`app_paths.EnvFileEncodingError`).
    @throws ImportError When python-dotenv is not installed.
    @note The text is decoded by `app_paths.read_env_text` (byte-order mark
        or strict UTF-8), the reader of the launcher: a UTF-16 `.env` written
        by PowerShell or Notepad, which the launcher loads, was skipped here
        because python-dotenv opens files as UTF-8 only, so its key was
        invisible to the key lookups (review R12-1 follow-up). One reader for
        every key lookup (openrouter._dotenv_key,
        providers._deepseek_dotenv_key, `read_key`).
    """
    from dotenv import dotenv_values
    from ..app_paths import read_env_text
    return dotenv_values(stream=io.StringIO(read_env_text(path)))


def read_key(path, name: str) -> str:
    """@brief The value `name` has in the `.env` file `path`, stripped; "" when absent or unreadable.

    @param path The `.env` file.
    @param name The variable, e.g. «DEEPSEEK_API_KEY».
    @return The value (interpolated as python-dotenv does), never logged by this module.
    """
    try:
        return (file_values(Path(path)).get(name) or "").strip()
    except (ImportError, OSError, ValueError):
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
    @throws OSError When the file cannot be read, decoded (`app_paths.read_env_text`:
        byte-order mark or strict UTF-8), parsed or replaced; the file is
        then unchanged.
    @note The file is written back in the encoding it was read in, with the
        same byte-order mark: the readers find keys in a UTF-16 `.env`
        (`file_values`), so «Spostala in Windows» (move it to Windows) must
        be able to remove them from one too (adversarial review of R12-1).
        The new content is flushed and fsynced before the atomic replace.
    """
    from ..app_paths import read_env_text
    path = Path(path).resolve()
    raw = path.read_bytes()
    try:
        body = read_env_text(path)
    except ValueError as exc:
        raise OSError(str(exc)) from exc
    mark, codec = _file_encoding(raw)
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
    data = mark + kept.encode(codec)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return REMOVED


#: Byte-order marks of a `.env` file and the codec that writes the text after
#: each one (no mark of its own): the marks `app_paths.read_env_text` decodes.
#: UTF-32 LE comes before UTF-16 LE, whose mark is its prefix.
_MARKED_CODECS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _file_encoding(raw: bytes) -> tuple[bytes, str]:
    """(byte-order mark, codec of the text after it) of a `.env` file's bytes; no mark is UTF-8."""
    for mark, codec in _MARKED_CODECS:
        if raw.startswith(mark):
            return mark, codec
    return b"", "utf-8"


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
