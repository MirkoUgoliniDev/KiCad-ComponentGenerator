"""@brief Build, preview and install portable KiCad footprint libraries.

@details Generated working files remain isolated from real KiCad libraries.
This module copies only verified footprint and 3D artifacts, injects the model
reference required by KiCad and updates a footprint library table without
discarding unrelated entries.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from ..core.footprint import GENERATOR_VERSION, KICAD_VERSION
from ..fsutil import replace_file


class KiCadLibraryError(RuntimeError):
    """@brief Report an invalid artifact set or an unsafe library operation.

    @param message English diagnostic for logs and non-GUI callers.
    @param code Stable key used by the GUI translation catalog.
    @param context Values interpolated into the localized message.
    """

    def __init__(self, message: str, *, code: str = "unexpected", **context):
        super().__init__(message)
        self.code = code
        self.context = context


@dataclass(frozen=True)
class ComponentArtifacts:
    """@brief Describe the generated files required by the KiCad workflow."""

    name: str
    component_dir: Path
    footprint_path: Path
    step_path: Path
    wrl_path: Path | None = None


@dataclass(frozen=True)
class PreviewProject:
    """@brief Describe an isolated KiCad project created for visual review."""

    directory: Path
    board_path: Path
    project_path: Path


@dataclass(frozen=True)
class LibraryInstallResult:
    """@brief Describe files installed into one KiCad footprint library."""

    identifier: str
    footprint_path: Path
    step_path: Path
    wrl_path: Path | None
    table_path: Path


_SAFE_LIBRARY_NAME = re.compile(r"^[A-Za-z0-9_.+-]+$")


def discover_component_artifacts(
        component_dir: str | os.PathLike[str], name: str | None = None,
        *, require_3d: bool = True) -> ComponentArtifacts:
    """@brief Locate the generated footprint and matching STEP/WRL files.

    @param component_dir Working directory of one generated component.
    @param name Expected component name; defaults to the directory name.
    @param require_3d Require a STEP model in addition to the footprint.
    @return Validated artifact paths.
    @throws KiCadLibraryError If required files are missing or ambiguous.
    """
    root = Path(component_dir).expanduser().resolve()
    component_name = (name or root.name).strip()
    if not component_name or ":" in component_name or any(
            separator in component_name for separator in ("/", "\\")):
        raise KiCadLibraryError(
            "Invalid component name for a KiCad footprint.",
            code="invalid_component_name",
        )
    footprint = root / f"{component_name}.pretty" / f"{component_name}.kicad_mod"
    step = root / f"{component_name}.3dshapes" / f"{component_name}.step"
    wrl = root / f"{component_name}.3dshapes" / f"{component_name}.wrl"
    if not footprint.is_file():
        raise KiCadLibraryError(
            f"Footprint not found: {footprint}",
            code="footprint_missing", path=footprint,
        )
    if require_3d and not step.is_file():
        raise KiCadLibraryError(
            f"STEP model not found: {step}",
            code="step_missing", path=step,
        )
    return ComponentArtifacts(
        component_name, root, footprint, step,
        wrl if wrl.is_file() else None,
    )


def _direct_child_ranges(text: str, node_name: str) -> list[tuple[int, int]]:
    """@brief Find direct child nodes of the root S-expression."""
    ranges: list[tuple[int, int]] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if character == "(":
            if depth == 1:
                match = re.match(r"\(\s*([^\s()]+)", text[index:])
                if match and match.group(1) == node_name:
                    start = index
            depth += 1
        elif character == ")":
            if depth == 2 and start is not None:
                ranges.append((start, index + 1))
                start = None
            depth -= 1
            if depth < 0:
                raise KiCadLibraryError(
                    "Malformed KiCad S-expression.", code="malformed_file")
    if depth != 0 or in_string:
        raise KiCadLibraryError(
            "Malformed KiCad S-expression.", code="malformed_file")
    return ranges


def _remove_direct_children(text: str, node_name: str) -> str:
    """@brief Remove direct root children with the requested node name."""
    for start, end in reversed(_direct_child_ranges(text, node_name)):
        text = text[:start] + text[end:]
    return text


def _quote(value: str) -> str:
    """@brief Quote a KiCad string atom."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _append_root_child(text: str, child: str) -> str:
    """@brief Append one child before the root S-expression closing token."""
    closing = text.rstrip().rfind(")")
    if closing < 0 or not text.lstrip().startswith("(footprint"):
        raise KiCadLibraryError(
            "The generated file is not a KiCad footprint.",
            code="invalid_footprint",
        )
    prefix = text[:closing].rstrip()
    return f"{prefix}\n{child}\n)\n"


def attach_3d_model(footprint_text: str, model_reference: str) -> str:
    """@brief Replace root model nodes with one normalized STEP reference."""
    without_models = _remove_direct_children(footprint_text, "model")
    model = (
        f"\t(model {_quote(model_reference)}\n"
        "\t\t(offset (xyz 0 0 0))\n"
        "\t\t(scale (xyz 1 1 1))\n"
        "\t\t(rotate (xyz 0 0 0))\n"
        "\t)"
    )
    return _append_root_child(without_models, model)


def _board_footprint(footprint_text: str, model_reference: str) -> str:
    """@brief Convert a library footprint into a positioned board footprint."""
    result = attach_3d_model(footprint_text, model_reference)
    result = _remove_direct_children(result, "at")
    return _append_root_child(result, "\t(at 100 100)")


def create_preview_project(
        artifacts: ComponentArtifacts,
        output_dir: str | os.PathLike[str] | None = None) -> PreviewProject:
    """@brief Create an isolated board that can be opened in KiCad for review.

    @details The generated board embeds the footprint and references a local copy
    of the STEP model through ``${KIPRJMOD}``. No KiCad library table is changed.
    The footprint is parsed before anything is written, the board and model
    copies are written through a temporary file and an atomic replace, and a
    board that KiCad currently holds open is never rewritten.
    @param artifacts Validated generated artifacts of one component.
    @param output_dir Preview directory; defaults to ``<component>/.kicad-preview``.
    @return Paths of the preview directory, board and project files.
    @throws KiCadLibraryError ``invalid_footprint``/``malformed_file`` when the
        footprint cannot be converted (nothing is written), ``preview_locked``
        when KiCad's lock file ``~<board>.lck`` exists next to the board.
    @note A lock file left behind by a crashed KiCad also blocks the preview:
        the error names the lock file so the user can delete it once KiCad is
        closed. Stale locks are not removed automatically because a running
        KiCad and a crashed one cannot be told apart from the file alone.
    """
    preview_dir = Path(output_dir or artifacts.component_dir / ".kicad-preview")
    board_path = preview_dir / f"{artifacts.name}-preview.kicad_pcb"
    project_path = preview_dir / f"{artifacts.name}-preview.kicad_pro"
    # Parse and convert the footprint first: a malformed file must not leave a
    # half-refreshed preview directory behind.
    footprint = _board_footprint(
        artifacts.footprint_path.read_text(encoding="utf-8"),
        f"${{KIPRJMOD}}/{artifacts.name}.step",
    )
    lock_path = kicad_lock_file(board_path)
    if lock_path.exists():
        raise KiCadLibraryError(
            f"The preview board is open in KiCad (lock file {lock_path}). "
            "Close it in KiCad before refreshing the preview.",
            code="preview_locked", path=lock_path, board=board_path,
        )
    preview_dir.mkdir(parents=True, exist_ok=True)
    _atomic_copy(artifacts.step_path, preview_dir / f"{artifacts.name}.step")
    if artifacts.wrl_path is not None:
        _atomic_copy(artifacts.wrl_path, preview_dir / f"{artifacts.name}.wrl")

    outline_uuid = uuid.uuid4()
    board = (
        f"(kicad_pcb (version {KICAD_VERSION}) (generator \"kicadgen_tool\")\n"
        f"\t(generator_version \"{GENERATOR_VERSION}\")\n"
        "\t(general (thickness 1.6))\n"
        "\t(paper \"A4\")\n"
        "\t(layers\n"
        "\t\t(0 \"F.Cu\" signal)\n"
        "\t\t(31 \"B.Cu\" signal)\n"
        "\t\t(36 \"B.SilkS\" user \"b.silkscreen\")\n"
        "\t\t(37 \"F.SilkS\" user \"f.silkscreen\")\n"
        "\t\t(44 \"Edge.Cuts\" user)\n"
        "\t\t(46 \"B.CrtYd\" user \"b.courtyard\")\n"
        "\t\t(47 \"F.CrtYd\" user \"f.courtyard\")\n"
        "\t\t(48 \"B.Fab\" user)\n"
        "\t\t(49 \"F.Fab\" user)\n"
        "\t)\n"
        "\t(setup (pad_to_mask_clearance 0))\n"
        f"{footprint.rstrip()}\n"
        "\t(gr_rect (start 65 75) (end 135 125)\n"
        "\t\t(stroke (width 0.05) (type default)) (fill none)\n"
        f"\t\t(layer \"Edge.Cuts\") (uuid \"{outline_uuid}\"))\n"
        ")\n"
    )
    _atomic_write_text(board_path, board)
    if not project_path.exists():
        _atomic_write_text(project_path, "{}\n")
    return PreviewProject(preview_dir, board_path, project_path)


def kicad_lock_file(document_path: str | os.PathLike[str]) -> Path:
    """@brief Return the lock file KiCad creates while a document is open.

    @details KiCad's ``LOCKFILE`` convention places ``~<file name>.lck`` next
    to the open document, e.g. ``~Demo-preview.kicad_pcb.lck``.
    @param document_path Board, schematic or project file path.
    @return Path of the corresponding lock file (it may not exist).
    """
    document = Path(document_path)
    return document.with_name(f"~{document.name}.lck")


def _temporary_sibling(target: Path, suffix: str = ".kicadgen.tmp") -> Path:
    """Return a scratch path in the target directory, so os.replace is atomic."""
    return target.with_name(f"{target.name}{suffix}")


def _unlink_quietly(path: Path) -> None:
    """Remove a scratch file; a cleanup failure must not hide the real error."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _atomic_write_text(target: Path, text: str) -> None:
    """Write UTF-8 text through a sibling temporary file and an atomic replace."""
    temporary = _temporary_sibling(target)
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        replace_file(temporary, target)
    finally:
        _unlink_quietly(temporary)


def _atomic_copy(source: Path, target: Path) -> None:
    """Copy a file through a sibling temporary file and an atomic replace."""
    temporary = _temporary_sibling(target)
    try:
        shutil.copy2(source, temporary)
        replace_file(temporary, target)
    finally:
        _unlink_quietly(temporary)


def launch_preview(pcbnew_path: str | os.PathLike[str], board_path: Path) -> None:
    """@brief Open an isolated preview board in the KiCad PCB Editor."""
    executable = Path(pcbnew_path)
    if not executable.is_file():
        raise KiCadLibraryError(
            f"KiCad PCB Editor not found: {executable}",
            code="pcbnew_missing", path=executable,
        )
    if not board_path.is_file():
        raise KiCadLibraryError(
            f"Preview board not found: {board_path}",
            code="preview_missing", path=board_path,
        )
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    try:
        subprocess.Popen(
            [str(executable), str(board_path)], cwd=str(board_path.parent),
            close_fds=True, creationflags=creation_flags,
        )
    except OSError as exc:
        raise KiCadLibraryError(
            f"Unable to start KiCad: {exc}",
            code="launch_failed", error=exc,
        ) from exc


_EMPTY_TABLE = "(fp_lib_table\n  (version 7)\n)\n"
#: Number of pre-change copies of a library table kept by `update_fp_lib_table`.
TABLE_BACKUP_RETENTION = 3
_UNQUOTED_ATOM = re.compile(r'[^\s()"]+')
_STRING_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


@dataclass(frozen=True)
class LibraryTableEntry:
    """@brief Describe one ``(lib ...)`` row of a footprint library table.

    @note ``uri`` and ``library_type`` are the raw values stored in the table:
        environment variables such as ``${KIPRJMOD}`` are not expanded.
    """

    name: str
    library_type: str | None
    uri: str | None
    disabled: bool = False
    hidden: bool = False


def _library_entry(library_name: str, uri: str) -> str:
    """@brief Serialize one native KiCad footprint-library table entry."""
    return (
        f"(lib (name {_quote(library_name)})(type \"KiCad\")"
        f"(uri {_quote(uri)})(options \"\")"
        "(descr \"Generated by KiCadGen-Tool\"))"
    )


def _node_arguments(node_text: str) -> list[str]:
    """Return the atoms directly inside one S-expression node, after its head.

    Quoted atoms (with backslash escapes) and bare atoms are both accepted,
    because KiCad reads either form: ``(name KiCadGen)`` and
    ``(name "KiCadGen")`` name the same library.
    """
    arguments: list[str] = []
    depth = 0
    index = 0
    length = len(node_text)
    while index < length:
        character = node_text[index]
        if character.isspace():
            index += 1
            continue
        if character == "(":
            depth += 1
            index += 1
            continue
        if character == ")":
            depth -= 1
            index += 1
            continue
        if character == '"':
            characters: list[str] = []
            index += 1
            while True:
                if index >= length:
                    raise KiCadLibraryError(
                        "Malformed KiCad S-expression.", code="malformed_file")
                current = node_text[index]
                if current == "\\" and index + 1 < length:
                    following = node_text[index + 1]
                    characters.append(_STRING_ESCAPES.get(following, following))
                    index += 2
                    continue
                index += 1
                if current == '"':
                    break
                characters.append(current)
            atom = "".join(characters)
        else:
            match = _UNQUOTED_ATOM.match(node_text, index)
            atom = match.group(0)
            index = match.end()
        if depth == 1:
            arguments.append(atom)
    return arguments[1:]


def _entry_value(block: str, field: str) -> str | None:
    """Return the first argument of a direct child of one ``lib`` block."""
    for start, end in _direct_child_ranges(block, field):
        arguments = _node_arguments(block[start:end])
        return arguments[0] if arguments else ""
    return None


def _library_entries(text: str) -> list[tuple[int, int, LibraryTableEntry]]:
    """Parse every direct ``lib`` row of a table with its source range."""
    entries = []
    for start, end in _direct_child_ranges(text, "lib"):
        block = text[start:end]
        entries.append((start, end, LibraryTableEntry(
            name=_entry_value(block, "name") or "",
            library_type=_entry_value(block, "type"),
            uri=_entry_value(block, "uri"),
            disabled=bool(_direct_child_ranges(block, "disabled")),
            hidden=bool(_direct_child_ranges(block, "hidden")),
        )))
    return entries


def _same_uri(first: str | None, second: str) -> bool:
    """Compare two table URIs, ignoring separator style and a trailing slash.

    Windows paths are compared case-insensitively because the file system is.
    Different spellings of the same place through environment variables are
    deliberately NOT equated: they are reported as a conflict for the user.
    """
    if first is None:
        return False

    def normalized(value: str) -> str:
        value = value.strip().replace("\\", "/").rstrip("/")
        return value.casefold() if sys.platform == "win32" else value

    return normalized(first) == normalized(second)


def _replace_entry_child(block: str, field: str, value: str) -> str:
    """Replace (or append) one ``(field "value")`` child of a ``lib`` block."""
    child = f"({field} {_quote(value)})"
    ranges = _direct_child_ranges(block, field)
    if ranges:
        start, end = ranges[0]
        block = block[:start] + child + block[end:]
        for start, end in reversed(_direct_child_ranges(block, field)[1:]):
            block = block[:start] + block[end:]
        return block
    closing = block.rstrip().rfind(")")
    return block[:closing] + child + block[closing:]


def find_library_entry(
        table_path: str | os.PathLike[str],
        library_name: str) -> LibraryTableEntry | None:
    """@brief Read the row registered under one nickname, without writing.

    @param table_path Footprint library table to inspect.
    @param library_name Library nickname (exact, case-sensitive like KiCad).
    @return The matching row, or None when the table or the row is missing.
    @throws KiCadLibraryError ``invalid_table``/``malformed_file`` for an
        unreadable table, ``duplicate_library_entry`` for an ambiguous one.
    """
    table = Path(table_path)
    if not table.is_file():
        return None
    text = table.read_text(encoding="utf-8")
    if not text.lstrip().startswith("(fp_lib_table"):
        raise KiCadLibraryError(
            f"Invalid footprint library table: {table}",
            code="invalid_table", path=table,
        )
    matches = [entry for _start, _end, entry in _library_entries(text)
               if entry.name == library_name]
    if len(matches) > 1:
        raise KiCadLibraryError(
            f"The library table {table} lists '{library_name}' more than once.",
            code="duplicate_library_entry", library=library_name, path=table,
        )
    return matches[0] if matches else None


def _rotate_table_backups(table: Path) -> None:
    """Keep the pre-change copy of ``table`` plus a bounded history.

    ``<table>.kicadgen-backup`` is always the state immediately before the
    latest KiCadGen write; older copies shift to ``.kicadgen-backup.1`` and
    so on, up to `TABLE_BACKUP_RETENTION` files, the oldest being discarded.
    The new copy is written before any shift, so a failed copy changes nothing.
    """
    backup = table.with_name(table.name + ".kicadgen-backup")
    history = [backup] + [
        backup.with_name(f"{backup.name}.{index}")
        for index in range(1, TABLE_BACKUP_RETENTION)
    ]
    fresh = _temporary_sibling(backup)
    try:
        shutil.copy2(table, fresh)
        for index in range(len(history) - 1, 0, -1):
            if history[index - 1].exists():
                replace_file(history[index - 1], history[index])
        replace_file(fresh, backup)
    finally:
        _unlink_quietly(fresh)


def _global_table_missing(table: Path) -> KiCadLibraryError:
    """Build the error for a global table KiCad has not created yet."""
    return KiCadLibraryError(
        f"KiCad's global footprint library table does not exist: {table}. "
        "Start KiCad once so it can set up its libraries, then retry.",
        code="global_table_missing", path=table,
    )


def update_fp_lib_table(
        table_path: str | os.PathLike[str], library_name: str, uri: str,
        *, replace_entry: bool = False, create_missing: bool = True) -> bool:
    """@brief Add or update one entry while preserving the rest of fp-lib-table.

    @param table_path Footprint library table to update.
    @param library_name Nickname of the library row.
    @param uri Library location written in the row.
    @param replace_entry Allow repointing an existing row with the same
        nickname to a different URI/type (explicit user confirmation).
    @param create_missing Create a new table when the file does not exist;
        pass False for KiCad's global table, which KiCad must create itself.
    @return True when the table contents changed.
    @throws KiCadLibraryError ``global_table_missing`` when the table is
        absent and ``create_missing`` is False; see
        `_updated_fp_lib_table_text` for the conflict codes.
    @note Every changing write refreshes ``<table>.kicadgen-backup`` (bounded
        history, see `TABLE_BACKUP_RETENTION`) and replaces the table
        atomically; the temporary file never survives a failed replace.
    """
    table = Path(table_path)
    if table.is_file():
        original = table.read_text(encoding="utf-8")
    elif create_missing:
        original = _EMPTY_TABLE
    else:
        raise _global_table_missing(table)
    updated = _updated_fp_lib_table_text(
        original, library_name, uri, table, replace_entry=replace_entry)
    if updated == original:
        return False
    table.parent.mkdir(parents=True, exist_ok=True)
    if table.is_file():
        _rotate_table_backups(table)
    _atomic_write_text(table, updated)
    return True


def _updated_fp_lib_table_text(
        original: str, library_name: str, uri: str,
        table_path: Path | None = None, *, replace_entry: bool = False) -> str:
    """@brief Render a table update without touching the filesystem.

    @details A row already registered under ``library_name`` with the same
    URI and the ``KiCad`` type is kept verbatim (options, description and
    ``disabled``/``hidden`` flags included). A row pointing elsewhere is a
    conflict: it is repointed only with ``replace_entry``, and even then only
    its ``type`` and ``uri`` children change.
    @throws KiCadLibraryError ``invalid_table`` for a non-table file,
        ``duplicate_library_entry`` when the nickname already appears more than
        once, ``library_entry_conflict`` (context ``library``, ``old_uri``,
        ``new_uri``, ``old_type``, ``path``) when the existing row points to a
        different location and ``replace_entry`` is False.
    """
    location = table_path or "<memory>"
    if not original.lstrip().startswith("(fp_lib_table"):
        raise KiCadLibraryError(
            f"Invalid footprint library table: {location}",
            code="invalid_table", path=location,
        )
    matches = [(start, end, entry)
               for start, end, entry in _library_entries(original)
               if entry.name == library_name]
    if len(matches) > 1:
        raise KiCadLibraryError(
            f"The library table {location} lists '{library_name}' more than once.",
            code="duplicate_library_entry", library=library_name, path=location,
        )
    if matches:
        start, end, entry = matches[0]
        same_type = (entry.library_type or "").casefold() == "kicad"
        if same_type and _same_uri(entry.uri, uri):
            return original
        if not replace_entry:
            raise KiCadLibraryError(
                f"The library table {location} already maps '{library_name}' to "
                f"{entry.uri!r} (type {entry.library_type!r}); installing would "
                f"repoint it to {uri!r}.",
                code="library_entry_conflict", library=library_name,
                old_uri=entry.uri or "", new_uri=uri,
                old_type=entry.library_type or "", path=location,
            )
        block = _replace_entry_child(original[start:end], "type", "KiCad")
        block = _replace_entry_child(block, "uri", uri)
        return original[:start] + block + original[end:]
    closing = original.rstrip().rfind(")")
    if closing < 0:
        raise KiCadLibraryError(
            f"Invalid footprint library table: {location}",
            code="invalid_table", path=location,
        )
    replacement = _library_entry(library_name, uri)
    return original[:closing].rstrip() + "\n  " + replacement + "\n)\n"


def _restore_file(backup: Path, target: Path) -> None:
    """Put a rollback copy back in place atomically.

    A rename is tried first (same volume, cannot run out of space); when it is
    impossible the copy goes through a sibling temporary file. Either way the
    target is replaced in one step and the backup is never deleted first.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(backup, target)
    except OSError:
        _atomic_copy(backup, target)


def _remove_created_directories(directories: list[Path]) -> None:
    """Remove directories this operation created, deepest first, if empty."""
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass


def _rollback_install(
        installed: list[Path], backups: dict[Path, Path], table: Path,
        table_backup: Path | None, table_existed: bool) -> list[str]:
    """Undo a failed install; return one message per step that failed.

    Every step is attempted independently, so a locked file does not prevent
    the other files and the table from being restored.
    """
    failures: list[str] = []
    for target in installed:
        if target in backups:
            continue
        try:
            if target.exists():
                target.unlink()
        except OSError as error:
            failures.append(f"{target}: {error}")
    for target, backup in backups.items():
        try:
            _restore_file(backup, target)
        except OSError as error:
            failures.append(f"{target} <- {backup}: {error}")
    try:
        if table_backup is not None:
            _restore_file(table_backup, table)
        elif not table_existed and table.exists():
            table.unlink()
    except OSError as error:
        failures.append(f"{table}: {error}")
    return failures


def install_component_library(
        artifacts: ComponentArtifacts, destination_root: str | os.PathLike[str],
        *, library_name: str = "KiCadGen", project_library: bool = True,
        table_path: str | os.PathLike[str] | None = None,
        overwrite: bool = False,
        replace_library_entry: bool = False) -> LibraryInstallResult:
    """@brief Install one component into a project or global KiCad library.

    @param artifacts Validated generated artifacts of one component.
    @param destination_root Folder receiving ``<library>.pretty`` and
        ``<library>.3dshapes``.
    @param library_name Library nickname registered in the table.
    @param project_library Use portable ``${KIPRJMOD}`` paths when True;
        absolute paths and an already existing global table when False.
    @param table_path Table to update; defaults to the project table.
    @param overwrite Replace files for an existing component when True.
    @param replace_library_entry Repoint an existing table row with the same
        nickname but a different URI/type when True (explicit confirmation).
    @return Installed paths and the ``library:footprint`` identifier.
    @throws KiCadLibraryError ``files_exist``, ``global_table_missing``,
        ``library_entry_conflict``, ``duplicate_library_entry`` and footprint
        parsing errors are raised before anything is written.
        ``rollback_incomplete`` (context ``rollback_dir``, ``error``,
        ``failures``) means the install failed AND some files could not be
        restored: the backups are kept in ``rollback_dir``. Any other failure
        is re-raised unchanged after a complete rollback.
    @note Rollback restores replaced component files and the table from copies
        kept in a rollback folder, removes files and empty folders the install
        created, and deletes the rollback folder only when every step
        succeeded. Each rollback step runs even if an earlier one failed.
    """
    if not _SAFE_LIBRARY_NAME.fullmatch(library_name):
        raise KiCadLibraryError(
            "Library name may contain only letters, numbers, '.', '_', '+' and '-'.",
            code="invalid_library_name",
        )
    root = Path(destination_root).expanduser().resolve()
    pretty_dir = root / f"{library_name}.pretty"
    shapes_dir = root / f"{library_name}.3dshapes"
    target_footprint = pretty_dir / f"{artifacts.name}.kicad_mod"
    target_step = shapes_dir / f"{artifacts.name}.step"
    target_wrl_path = shapes_dir / f"{artifacts.name}.wrl"
    target_wrl = target_wrl_path if artifacts.wrl_path else None
    targets = [target_footprint, target_step, target_wrl_path]
    existing = [path for path in targets if path.exists()]
    if existing and not overwrite:
        raise KiCadLibraryError(
            "Component files already exist:\n" + "\n".join(map(str, existing)),
            code="files_exist", files="\n".join(map(str, existing)),
        )

    destination_table = Path(table_path) if table_path else root / "fp-lib-table"
    table_existed = destination_table.is_file()
    if not table_existed and not project_library:
        # A global table holding only KiCadGen would make KiCad skip its
        # first-run setup of the standard libraries.
        raise _global_table_missing(destination_table)
    table_original = (destination_table.read_text(encoding="utf-8")
                      if table_existed else _EMPTY_TABLE)
    library_uri = (f"${{KIPRJMOD}}/{library_name}.pretty"
                   if project_library else pretty_dir.as_posix())
    # Validate the table update and the footprint before the first write.
    _updated_fp_lib_table_text(
        table_original, library_name, library_uri, destination_table,
        replace_entry=replace_library_entry)
    model_reference = (
        f"${{KIPRJMOD}}/{library_name}.3dshapes/{artifacts.name}.step"
        if project_library else target_step.as_posix()
    )
    rendered = attach_3d_model(
        artifacts.footprint_path.read_text(encoding="utf-8"), model_reference)

    # Folders created here are removed again by a failed install when empty.
    created_dirs = [
        directory
        for directory in (*reversed(root.parents), root, pretty_dir, shapes_dir)
        if not directory.exists()
    ]
    staged_footprint = _temporary_sibling(target_footprint)
    staged_step = _temporary_sibling(target_step)
    staged = [(staged_footprint, target_footprint), (staged_step, target_step)]
    staged_wrl = None
    if target_wrl is not None:
        staged_wrl = _temporary_sibling(target_wrl)
        staged.append((staged_wrl, target_wrl))
    rollback_dir = root / f".kicadgen-rollback-{uuid.uuid4().hex}"
    try:
        pretty_dir.mkdir(parents=True, exist_ok=True)
        shapes_dir.mkdir(parents=True, exist_ok=True)
        staged_footprint.write_text(rendered, encoding="utf-8", newline="\n")
        shutil.copy2(artifacts.step_path, staged_step)
        if staged_wrl is not None and artifacts.wrl_path is not None:
            shutil.copy2(artifacts.wrl_path, staged_wrl)
        rollback_dir.mkdir()
    except BaseException:
        for temporary, _target in staged:
            _unlink_quietly(temporary)
        _remove_created_directories(created_dirs)
        raise

    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    table_backup: Path | None = None
    try:
        for target in targets:
            if target.is_file():
                backup = rollback_dir / f"{len(backups)}-{target.name}"
                shutil.copy2(target, backup)
                backups[target] = backup
        if table_existed:
            table_backup = rollback_dir / "table-backup"
            shutil.copy2(destination_table, table_backup)
        for temporary, target in staged:
            os.replace(temporary, target)
            installed.append(target)
        if artifacts.wrl_path is None and target_wrl_path.is_file():
            target_wrl_path.unlink()
            installed.append(target_wrl_path)
        update_fp_lib_table(
            destination_table, library_name, library_uri,
            replace_entry=replace_library_entry,
            create_missing=project_library)
    except BaseException as exc:
        failures = _rollback_install(
            installed, backups, destination_table, table_backup, table_existed)
        for temporary, _target in staged:
            _unlink_quietly(temporary)
        if failures:
            # Keep the rollback folder: it holds the only copies left.
            raise KiCadLibraryError(
                f"Installation failed ({exc}) and the rollback is incomplete. "
                f"Backups were kept in {rollback_dir}:\n" + "\n".join(failures),
                code="rollback_incomplete", rollback_dir=rollback_dir,
                error=exc, failures="\n".join(failures),
            ) from exc
        shutil.rmtree(rollback_dir, ignore_errors=True)
        _remove_created_directories(created_dirs)
        raise
    for temporary, _target in staged:
        _unlink_quietly(temporary)
    shutil.rmtree(rollback_dir, ignore_errors=True)
    return LibraryInstallResult(
        f"{library_name}:{artifacts.name}", target_footprint, target_step,
        target_wrl, destination_table,
    )


def global_fp_lib_table_path(
        kicad_cli_path: str | os.PathLike[str],
        *, environ: dict[str, str] | None = None,
        platform: str | None = None) -> Path:
    """@brief Resolve the per-user global fp-lib-table for the installed KiCad.

    @details Mirrors KiCad's settings path: ``$KICAD_CONFIG_HOME/<version>``
    when that variable is set and not empty, otherwise the platform default
    (``%APPDATA%/kicad/<version>``, ``~/Library/Preferences/kicad/<version>``
    or ``$XDG_CONFIG_HOME/kicad/<version>``).
    @param kicad_cli_path Path of kicad-cli; its ``<major>.<minor>`` folder
        gives the settings version.
    @param environ Environment used instead of ``os.environ`` (tests).
    @param platform Platform used instead of ``sys.platform`` (tests).
    @return Path of the table; the file may not exist yet.
    @throws KiCadLibraryError ``version_unknown`` when no version is found.
    """
    executable = Path(kicad_cli_path)
    version = next(
        (part for part in reversed(executable.parts)
         if re.fullmatch(r"\d+\.\d+", part)),
        None,
    )
    if version is None:
        raise KiCadLibraryError(
            "Unable to determine the installed KiCad version.",
            code="version_unknown",
        )
    values = os.environ if environ is None else environ
    override = (values.get("KICAD_CONFIG_HOME") or "").strip()
    if override:
        return Path(override).expanduser() / version / "fp-lib-table"
    current_platform = sys.platform if platform is None else platform
    if current_platform == "win32":
        base = Path(values.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif current_platform == "darwin":
        base = Path.home() / "Library" / "Preferences"
    else:
        base = Path(values.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "kicad" / version / "fp-lib-table"
