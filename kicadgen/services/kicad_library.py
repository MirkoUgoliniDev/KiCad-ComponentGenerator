"""@brief Build, preview and install portable KiCad footprint libraries.

@details Generated working files remain isolated from real KiCad libraries.
This module copies only verified footprint and 3D artifacts, injects the model
reference required by KiCad and updates a footprint library table without
discarding unrelated entries.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from ..core.footprint import GENERATOR_VERSION, KICAD_VERSION
from ..core.sexpr import format_number, quote
from ..kicad_versions import version_at_least
from ..fsutil import replace_file, retry_transient


class KiCadLibraryError(RuntimeError):
    """@brief Report an invalid artifact set or an unsafe library operation.

    @param message English diagnostic for logs and non-GUI callers.
    @param code Stable key used by the GUI translation catalog.
    @param context Values interpolated into the localized message.
    """

    def __init__(self, message: str, *, code: str = "unexpected", **context):
        """@brief Keep the English message, the catalog code and its context.

        @param message English diagnostic for logs and non-GUI callers.
        @param code Stable key used by the GUI translation catalog.
        @param context Values interpolated into the localized message.
        """
        super().__init__(message)
        self.code = code
        self.context = context


#: UTF-8 byte order mark some Windows editors put at the start of a file.
UTF8_BOM = b"\xef\xbb\xbf"


def _decode_utf8(data: bytes, path: Path, *, keep_newlines: bool = False) -> str:
    """@brief Decode a KiCad text file read as bytes.

    @param data Raw content of the file.
    @param path File the bytes come from, named in the error.
    @param keep_newlines Return the line endings exactly as stored (CRLF
        kept), for a caller that rewrites the file and must change only what
        it adds; by default they are universal newlines like ``read_text``.
    @return The text without a leading UTF-8 byte order mark (`UTF8_BOM`).
        A BOM made a valid table look like ``invalid_table``: the text no
        longer started with ``(fp_lib_table``. A caller that rewrites the file
        restores the BOM itself (see update_fp_lib_table).
    @throws KiCadLibraryError ``file_not_utf8`` (context ``path``, ``error``)
        when the bytes are not valid UTF-8.
    @note KiCad reads and writes its tables and footprints in UTF-8. A table
        re-saved by another editor in the ANSI code page (cp1252 with an
        accented folder name) raised a bare UnicodeDecodeError, a ValueError
        that the GUI handlers (KiCadLibraryError, OSError) did not catch, and
        the application closed. Guessing the encoding is refused: rewriting the
        table in a guessed encoding could corrupt the user's other rows.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KiCadLibraryError(
            f"{path} is not UTF-8 text (invalid byte at offset {exc.start}); "
            "KiCad files must be saved as UTF-8. No file was changed.",
            code="file_not_utf8", path=path, error=exc,
        ) from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    if keep_newlines:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_utf8(path: Path) -> str:
    """@brief Read a KiCad text file as UTF-8, mapping a decode error.

    @param path File to read.
    @return Its text with universal newlines (see `_decode_utf8`).
    @throws KiCadLibraryError ``file_not_utf8`` for a non-UTF-8 file.
    @throws OSError When the file cannot be read.
    """
    return _decode_utf8(path.read_bytes(), path)


@dataclass(frozen=True)
class ComponentArtifacts:
    """@brief Describe the generated files required by the KiCad workflow.

    @note ``model_offset`` is the ``(offset (xyz x y z))`` of the model node
        every delivery writes (preview board, project and global install), in
        mm and in KiCad's 3D axes (Y up). It is (0, 0, 0) unless the footprint
        anchor was moved; then it comes from `model_offset_for_anchor`, so the
        STEP model, built in the generator's coordinates, stays on the pads.
    @note ``step_path`` is always the EXPECTED model path
        (``<name>.3dshapes/<name>.step``), so a caller can name it even when
        the model was never generated; `has_step` says whether it exists.
        USER DECISION 2026-11-12: a user without FreeCAD has a footprint but
        no STEP, and the preview, the project and the global installation
        must still deliver the footprint. Without a STEP every delivery
        writes the footprint with NO ``(model ...)`` node and copies no
        STEP/WRL (the WRL is the mesh twin FreeCAD writes next to the STEP,
        never referenced alone); a STEP that exists but is truncated is
        still refused by `discover_component_artifacts` (``step_incomplete``).
    """

    name: str
    component_dir: Path
    footprint_path: Path
    step_path: Path
    wrl_path: Path | None = None
    model_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def has_step(self) -> bool:
        """@brief Whether the STEP model exists, so a delivery ships a 3D model.

        @return True only when `step_path` is an existing file. Read from the
            file system at each call; the preview and `plan_component_install`
            read it once (`InstallPlan.with_model`) so board, plan and
            install agree.
        """
        return self.step_path.is_file()


@dataclass(frozen=True)
class PreviewProject:
    """@brief Describe an isolated KiCad project created for visual review.

    @note ``schematic_path`` and ``symbol_library_path`` (USER DECISION
        2026-11-13, option B) are the temporary schematic with the component
        symbol placed and the project-local copy of the ``.kicad_sym`` its
        ``sym-lib-table`` points to; both None when no schematic was written,
        and ``schematic_skipped`` then holds the reason code (see
        `PREVIEW_SCHEMATIC_SKIP_REASONS`). The board is written in every case.
    """

    directory: Path
    board_path: Path
    project_path: Path
    schematic_path: Path | None = None
    symbol_library_path: Path | None = None
    schematic_skipped: str | None = None


@dataclass(frozen=True)
class LibraryInstallResult:
    """@brief Describe files installed into one KiCad footprint library.

    @note ``replaced`` and ``removed`` (review 2026-10-27, A3) say what the
        install did to files that existed before it: ``replaced`` were
        overwritten with the new copies, ``removed`` were deleted because the
        source no longer provides them (a WRL the component lost). The GUI
        can word its report with them; both are empty for a first install.
    @note ``rollback_left`` is the temporary rollback folder of this install
        when it could not be removed after a successful install (a file in it
        held open by a scanner); None otherwise. It holds only copies of the
        files as they were before, is marked disposable and is swept by a
        later install once older than `STALE_SCRATCH_AGE_S`
        (`_sweep_stale_rollback_dirs`); the GUI may mention it.
    @note ``step_path`` and ``wrl_path`` name the model files installed and
        are None when none was installed (CONTRACT CHANGED 2026-11-12, USER
        DECISION: a footprint without a STEP is installed without a model;
        ``step_path`` was always a path before). A STEP of an earlier
        install deleted for that reason is in ``removed``.
    """

    identifier: str
    footprint_path: Path
    step_path: Path | None
    wrl_path: Path | None
    table_path: Path
    replaced: tuple[Path, ...] = ()
    removed: tuple[Path, ...] = ()
    rollback_left: Path | None = None


@dataclass(frozen=True)
class LibraryShadowing:
    """@brief A row of the OTHER library table under the nickname being installed.

    @details KiCad looks a ``<library>:<footprint>`` reference up in the
    project table first and falls back to the global one, so the same
    nickname registered in both scopes with different locations makes one
    of them invisible inside that project: a project install hides the
    global library of that name, a global install is hidden by the
    project's row (review 2026-11-03).
    @note ``scope`` is the scope of `table_path`: ``"global"`` when the
        install goes to a project, ``"project"`` when it goes to the global
        table. ``uri`` is the raw value of the row (variables unexpanded).
    @note LIMIT: the check runs only when the caller names the other table
        (`plan_component_install` ``other_table_path``). The KiCad tab does
        so for a PROJECT install (the global table is known); for a GLOBAL
        install it passes none, because a library folder belongs to no
        project and the projects that will use it are unknown, so a project
        row hiding the global library is NOT reported there.
    """

    table_path: Path
    scope: str
    library: str
    uri: str
    library_type: str


@dataclass(frozen=True)
class InstallPlan:
    """@brief What `install_component_library` would do, computed without writing.

    @details Returned by `plan_component_install` so the GUI can ask the
    overwrite confirmation with the same facts the install will act on, in
    one place (review 2026-10-27, A1/A3/D1). Every path is the user's path,
    links not resolved.
    @note ``replaced`` lists the existing targets the install overwrites and
        ``removed`` those it deletes (a WRL present in the library when the
        source has none). A dangling symbolic link counts as existing: it is
        listed in ``replaced`` so the user is asked, and the install then
        refuses it (``target_link_unresolved``) because nothing can be
        written through it.
    @note ``linked`` lists the targets, the table included, that are symbolic
        or hard links: the install writes THROUGH them so the other name sees
        the new content too, and a rollback restores both.
    @note ``shadowing`` (review 2026-11-03) is the enabled row of the other
        scope's table registering the same nickname at a different location
        (`LibraryShadowing`), None when there is none, when no other table
        was given, or when it cannot be read (an unreadable table of the
        other scope must not block this install; ``shadowing_error`` then
        names the reason). The install does not refuse it: the GUI asks.
    @note ``open_locks`` (review 2026-11-03) are KiCad's lock files of the
        destination project (``~<name>.kicad_pro.lck``, ``.kicad_pcb``,
        ``.kicad_sch``): KiCad has that project open and keeps its own copy
        of the library table in memory, so it neither sees the new row until
        the project is reopened nor must save its library settings over it.
        Checked for project installs only: the global table has no lock file.
    @note ``kicad_version`` is the KiCad settings version the caller says
        receives the install and ``kicad_too_old`` is True when it is older
        than `MINIMUM_KICAD_VERSION` (the install then refuses with
        ``kicad_too_old``).
    @note ``detected_kicad_version`` is the version of the KiCad found on
        this computer, given by the caller for a PROJECT install (whose table
        belongs to no KiCad version), and ``detected_kicad_too_old`` is True
        when it is older than the minimum. A warning, never a refusal: the
        project may be opened by a newer KiCad on another computer (review
        2026-11-03, D2). None/False when the version is unknown.
    @note ``shadowing`` is not computed, and stays None, when the destination
        table already maps the nickname to the same place: a re-install
        (`_destination_row_already_there`, review 2026-11-03, D7).
    @note ``with_model`` (USER DECISION 2026-11-12) is True when the install
        ships the STEP model (`ComponentArtifacts.has_step`, read once here so
        plan and install agree). When False the installed footprint carries
        no model node, no STEP/WRL is copied, and a ``<name>.step``/``.wrl``
        left in the library by an earlier install WITH a model is listed in
        ``removed``: it no longer matches the footprint, so it is deleted
        under the same confirmation, backup and rollback as any other target.
    """

    targets: tuple[Path, ...]
    replaced: tuple[Path, ...]
    removed: tuple[Path, ...]
    linked: tuple[Path, ...]
    table_path: Path
    table_exists: bool
    library_uri: str
    shadowing: LibraryShadowing | None = None
    shadowing_error: str | None = None
    open_locks: tuple[Path, ...] = ()
    kicad_version: str | None = None
    kicad_too_old: bool = False
    detected_kicad_version: str | None = None
    detected_kicad_too_old: bool = False
    with_model: bool = True

    @property
    def kicad_open(self) -> bool:
        """@brief Whether KiCad holds the destination project open.

        @return True when at least one lock file is in `open_locks`.
        """
        return bool(self.open_locks)


@dataclass(frozen=True)
class GlobalTableLocation:
    """@brief The per-user global fp-lib-table of one installed KiCad.

    @note ``version`` is the ``<major>.<minor>`` settings folder and
        ``version_source`` says where it came from: ``"kicad-cli"`` (the
        output of ``kicad-cli version``) or ``"folder"`` (the version folder
        in the executable path, used when the command gives nothing usable).
        Exposed before any write so the GUI can name the table and the
        version in its confirmation (review 2026-10-27, D1).
    """

    path: Path
    version: str
    version_source: str

    @property
    def supported(self) -> bool | None:
        """@brief Whether this KiCad reads the files the generators write.

        @return True/False against `MINIMUM_KICAD_VERSION`; None when
            `version` is not a dotted number.
        """
        return kicad_version_supported(self.version)


_SAFE_LIBRARY_NAME = re.compile(r"^[A-Za-z0-9_.+-]+$")

#: Closing keyword of every ISO 10303-21 (STEP physical file) exchange file.
STEP_TRAILER = b"END-ISO-10303-21;"

#: Bytes read from the end of a STEP file to look for its trailer. Writers may
#: append blank lines or comments after it, never kilobytes of data.
_STEP_TAIL_BYTES = 4096


def step_file_is_complete(path: str | os.PathLike[str]) -> bool:
    """@brief Tell whether a STEP file ends with the ISO 10303-21 trailer.

    @param path STEP file to inspect; only its last few kilobytes are read.
    @return True when the last non-blank content is ``END-ISO-10303-21;``.
    @throws OSError If the file cannot be opened or read.
    @note A FreeCAD process killed during the export (cancel, watchdog) leaves
        a syntactically truncated file that KiCad would load as an empty or
        partial model. The trailer is the last thing a writer emits, so its
        absence is a structural proof of truncation, whatever the file size.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - _STEP_TAIL_BYTES))
        tail = handle.read()
    return tail.rstrip(b" \t\r\n\x00").endswith(STEP_TRAILER)


def discover_component_artifacts(
        component_dir: str | os.PathLike[str], name: str | None = None,
        *, require_3d: bool = True,
        model_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> ComponentArtifacts:
    """@brief Locate the generated footprint and matching STEP/WRL files.

    @param component_dir Working directory of one generated component.
    @param name Expected component name; defaults to the directory name.
    @param require_3d Require a STEP model in addition to the footprint.
        False lets a footprint without a model reach KiCad (USER DECISION
        2026-11-12: a user without FreeCAD); `ComponentArtifacts.has_step`
        then says the model is absent and ``wrl_path`` is None.
    @param model_offset Model offset, mm, KiCad 3D axes (Y up), carried into
        the artifacts for every delivery; `model_offset_for_anchor` gives it
        from the footprint anchor the .kicad_mod was written with.
    @return Validated artifact paths.
    @throws KiCadLibraryError If required files are missing or ambiguous
        (``footprint_missing``; ``step_missing`` only with `require_3d`), or
        if the STEP file lacks the ISO 10303-21 trailer (code
        ``step_incomplete``), even when ``require_3d`` is False.
    @throws ValueError When `model_offset` is not three finite numbers.
    @throws OSError If the STEP file exists but cannot be read.
    """
    offset = _checked_offset(model_offset)
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
    if step.is_file() and not step_file_is_complete(step):
        # Never ship a truncated model: it is newer than the footprint, so the
        # "older STEP" warning cannot catch it, and KiCad would show it broken.
        raise KiCadLibraryError(
            f"STEP model is incomplete (no END-ISO-10303-21; trailer, the "
            f"export was probably interrupted): {step}",
            code="step_incomplete", path=step,
        )
    # A WRL is the mesh twin of the STEP: without the STEP it is never
    # referenced, so it is not reported as an artifact (USER DECISION
    # 2026-11-12, see ComponentArtifacts).
    return ComponentArtifacts(
        component_name, root, footprint, step,
        wrl if wrl.is_file() and step.is_file() else None, offset,
    )


#: Name of the S-expression node opening at a given index. Matched in place
#: with ``pattern.match(text, index)``: slicing ``text[index:]`` copied the
#: rest of the file for every node, quadratic on large library tables.
_NODE_NAME = re.compile(r"\(\s*([^\s()]+)")


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
                match = _NODE_NAME.match(text, index)
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


_FOOTPRINT_HEAD = re.compile(
    r'\A(\s*\(\s*footprint\s+)("(?:[^"\\]|\\.)*"|[^\s()"]+)')


_VALUE_HEAD = re.compile(
    r'\A(\(\s*property\s+(?:"Value"|Value)\s+)("(?:[^"\\]|\\.)*"|[^\s()"]+)')


def name_footprint(footprint_text: str, name: str) -> str:
    """@brief Rename a footprint to `name`, as the project rename does.

    @param footprint_text Content of a .kicad_mod file.
    @param name Name the footprint must carry: the stem of the file it is
        written to (see core.footprint.footprint_file_name).
    @return The text with the root ``(footprint "<old>" ...)`` atom set to
        `name` and, when the root ``(property "Value" ...)`` still reads the
        old name, that value too; unchanged when the name already matches.
    @throws KiCadLibraryError ``invalid_footprint`` when the text does not
        start with a named footprint node.
    @note KiCad resolves ``library:<name>`` through the file name and flags a
        footprint whose internal name differs from it. A source footprint can
        carry another name (a file renamed by hand, an older project), so the
        copy the installer writes is renamed instead of trusting the source.
    @note Rule for Value, the same as the project rename of the GUI: the
        generators write the footprint name as Value, so a Value equal to the
        OLD name is that default and follows the rename; any other Value was
        customised by the user and is kept as it is.
    """
    match = _FOOTPRINT_HEAD.match(footprint_text)
    if match is None:
        raise KiCadLibraryError(
            "The generated file is not a KiCad footprint.",
            code="invalid_footprint",
        )
    old = _node_arguments(f"(footprint {match.group(2)})")[0]
    if old == name:
        return footprint_text
    renamed = footprint_text[:match.start(2)] + quote(name) + footprint_text[match.end(2):]
    for start, end in _direct_child_ranges(renamed, "property"):
        block = renamed[start:end]
        arguments = _node_arguments(block)
        if arguments[:1] != ["Value"]:
            continue
        value = _VALUE_HEAD.match(block)
        if value is not None and len(arguments) > 1 and arguments[1] == old:
            block = block[:value.start(2)] + quote(name) + block[value.end(2):]
            renamed = renamed[:start] + block + renamed[end:]
        break
    return renamed


#: Decimals of a model offset, mm: the precision `core.sexpr.format_number`
#: writes the pad and graphic coordinates of the .kicad_mod with (1 nm), so
#: the model moves by exactly the shift the pads moved by after an anchor
#: move (review 2026-11-09: 4 decimals left the model up to 50 nm off the
#: pads for an anchor such as 0.33333 mm).
_OFFSET_DECIMALS = 6


def _finite_number(value) -> bool:
    """True for a non-boolean int or float that converts to a finite float.

    A huge JSON int (hundreds of digits) makes `math.isfinite` raise
    OverflowError; it is not a finite float, so it is refused here instead.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _checked_offset(offset) -> tuple[float, float, float]:
    """Three finite, non-boolean numbers in mm, rounded to `_OFFSET_DECIMALS`, without ``-0``.

    @throws ValueError For anything else (an int too large for a float
        included): an offset is written into every delivered footprint, so a
        NaN or a wrong arity must never reach a file.
    """
    try:
        values = tuple(offset)
    except TypeError:
        raise ValueError(f"model offset must be (x, y, z) in mm, got {offset!r}") from None
    if len(values) != 3 or not all(_finite_number(v) for v in values):
        raise ValueError(f"model offset must be three finite numbers in mm, got {offset!r:.200}")
    # `+ 0.0` turns a rounded -0.0 into 0.0, so the tuple compares and prints clean.
    return tuple(round(float(v), _OFFSET_DECIMALS) + 0.0 for v in values)  # type: ignore[return-value]


def model_offset_for_anchor(anchor_x: float, anchor_y: float) -> tuple[float, float, float]:
    """@brief Model offset that keeps the STEP body on the pads after an anchor move.

    @details The footprint writer moves every pad and graphic by
    (-anchor_x, -anchor_y) in footprint coordinates (mm, Y down) so the chosen
    point becomes the footprint origin; the STEP model, built in the
    generator's coordinates, must move by the same amount. KiCad's model
    offset is expressed in its 3D axes, X right and Y UP, so the same shift is
    ``(offset (xyz -anchor_x anchor_y 0))``.
    @param anchor_x Anchor X in the generator's coordinates, mm.
    @param anchor_y Anchor Y in the generator's coordinates, mm, Y down.
    @return (x, y, z) for `attach_3d_model`, mm, rounded to 1 nm like the
        pad coordinates (`_OFFSET_DECIMALS`), no ``-0``.
    @throws ValueError When either coordinate is not a finite number (an int
        too large for a float included).
    @note MEASURED with kicad-cli 10.0.8 (2026-11-09), not reasoned: a
        footprint at (at 130 110) with a pad at (0 0) and one at (0 2), its
        model with ``(offset (xyz 2 3 0))``, exported by ``kicad-cli pcb
        export glb --no-board-body --include-pads --user-origin 130x110mm``.
        In the GLB (metres, glTF Z = board Y down, checked by the pad at
        (0 2) landing at Z +2 mm) the model's box centre moved from (0, 0) to
        X +2 mm, Z -3 mm: offset X is board X, offset Y is board -Y. The same
        holds for a WRL model (hand-written box, same export). The end-to-end
        check is ``tests/test_kicad_model_offset.py``, which runs whenever
        kicad-cli is installed.
    """
    x, y, _ = _checked_offset((anchor_x, anchor_y, 0.0))
    return _checked_offset((-x, y, 0.0))


def attach_3d_model(footprint_text: str, model_reference: str,
                    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> str:
    """@brief Replace root model nodes with one normalized STEP reference.

    @param footprint_text Text of a .kicad_mod footprint.
    @param model_reference Model path written in the `(model ...)` node, e.g.
        a `${KIPRJMOD}` or absolute STEP reference.
    @param offset Model offset, mm, in KiCad's 3D axes (X right, Y UP, Z up),
        written as ``(offset (xyz x y z))``; `model_offset_for_anchor` gives
        it for a moved footprint anchor. Numbers are written like the pad
        coordinates (`format_number`: 1 nm, no trailing zeros, no ``-0``),
        so the model moves by exactly the shift the pads moved by.
    @return The footprint text with exactly one model node, with the given
        offset, zero rotation and unit scale.
    @throws ValueError When `offset` is not three finite numbers.
    @note The default (0, 0, 0) writes exactly what this function wrote
        before the offset existed, byte for byte.
    """
    ox, oy, oz = _checked_offset(offset)
    without_models = _remove_direct_children(footprint_text, "model")
    model = (
        f"\t(model {quote(model_reference)}\n"
        f"\t\t(offset (xyz {format_number(ox)} {format_number(oy)} {format_number(oz)}))\n"
        "\t\t(scale (xyz 1 1 1))\n"
        "\t\t(rotate (xyz 0 0 0))\n"
        "\t)"
    )
    return _append_root_child(without_models, model)


def detach_3d_model(footprint_text: str) -> str:
    """@brief Remove every root ``(model ...)`` node of a footprint.

    @param footprint_text Text of a .kicad_mod footprint.
    @return The footprint text without any root model node.
    @throws KiCadLibraryError ``malformed_file`` for an unbalanced file.
    @note Used when no STEP is delivered (USER DECISION 2026-11-12): a
        model node would point KiCad at a file that is not there, or at a
        model of an earlier installation that no longer matches the pads.
        The generators write no model node, so this only removes references
        a hand-edited footprint may carry.
    """
    return _remove_direct_children(footprint_text, "model")


def _board_footprint(footprint_text: str, model_reference: str | None,
                     offset: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> str:
    """@brief Convert a library footprint into a positioned board footprint.

    @param model_reference Model path of the board's model node; None writes
        no model node at all (no STEP delivered, USER DECISION 2026-11-12).
    """
    if model_reference is None:
        # The offset is still validated: a bad value is a caller error with
        # or without a model.
        _checked_offset(offset)
        result = detach_3d_model(footprint_text)
    else:
        result = attach_3d_model(footprint_text, model_reference, offset)
    result = _remove_direct_children(result, "at")
    return _append_root_child(
        result, f"\t(at {_mm(_PREVIEW_ORIGIN[0])} {_mm(_PREVIEW_ORIGIN[1])})")


def _mm(value: float) -> str:
    """Board coordinate in mm as KiCad writes it: 0.1 um resolution, no trailing zeros.

    Rounded to 4 decimals, then written by the shared `format_number` (no
    ``-0``, a non-finite value refused) like every other KiCad number.
    """
    return format_number(round(float(value), 4))


#: Name of the preview folder the program creates inside a component folder.
#: Everything in it is generated (board, project, model copies), never user
#: content, which is what allows create_preview_project to unlock its files.
PREVIEW_DIR_NAME = ".kicad-preview"

#: Position of the footprint on the preview board, mm.
_PREVIEW_ORIGIN = (100.0, 100.0)
#: Public name of `_PREVIEW_ORIGIN`: where the footprint sits on the preview
#: board (mm), so a reader of a DRC report can bring a position back to the
#: footprint's own coordinates.
PREVIEW_FOOTPRINT_ORIGIN = _PREVIEW_ORIGIN

#: Smallest preview outline, as (min x, min y, max x, max y) in board mm: the
#: historical 70 x 50 mm frame around the footprint, kept for small parts.
_PREVIEW_MIN_OUTLINE = (65.0, 75.0, 135.0, 125.0)

#: Free board around the footprint courtyard, mm.
PREVIEW_OUTLINE_MARGIN_MM = 5.0

#: Layer table of the preview board as (KiCad ordinal, canonical name, type,
#: user name or ""), in the order KiCad 10 writes a new two-layer board.
#: @note KiCad enables on a board only the layers its table declares: a
#:     layer missing here is dropped when the board is loaded, so whatever a
#:     footprint draws on it (pad solder mask and paste, Cmts.User drawings)
#:     is neither shown in the PCB Editor nor checked by DRC. The ordinals are
#:     those of the board format written (KICAD_VERSION, KiCad 9+ numbering:
#:     front/back copper 0/2, technical layers odd); a ``kicad-cli pcb upgrade``
#:     keeps this table verbatim (checked with kicad-cli 10.0).
PREVIEW_BOARD_LAYERS: tuple[tuple[int, str, str, str], ...] = (
    (0, "F.Cu", "signal", ""),
    (2, "B.Cu", "signal", ""),
    (9, "F.Adhes", "user", "F.Adhesive"),
    (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user", ""),
    (15, "B.Paste", "user", ""),
    (5, "F.SilkS", "user", "F.Silkscreen"),
    (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user", ""),
    (3, "B.Mask", "user", ""),
    (17, "Dwgs.User", "user", "User.Drawings"),
    (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"),
    (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user", ""),
    (27, "Margin", "user", ""),
    (31, "F.CrtYd", "user", "F.Courtyard"),
    (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user", ""),
    (33, "B.Fab", "user", ""),
)

_COURTYARD_LAYERS = ("F.CrtYd", "B.CrtYd")
_GRAPHIC_NODES = ("fp_rect", "fp_line", "fp_poly", "fp_circle", "fp_arc")
_NUMBER = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
_POINT = re.compile(r"\((start|end|mid|center|xy)\s+" + _NUMBER + r"\s+" + _NUMBER)
_LAYER = re.compile(r"\(layer\s+\"?([^\s\")]+)")
_PAD_AT = re.compile(r"\(at\s+" + _NUMBER + r"\s+" + _NUMBER)
_PAD_SIZE = re.compile(r"\(size\s+" + _NUMBER + r"\s+" + _NUMBER)


def footprint_extent(footprint_text: str) -> tuple[float, float, float, float] | None:
    """@brief Bounding box of a footprint in its own coordinates, mm.

    @param footprint_text Content of a .kicad_mod file.
    @return (min x, min y, max x, max y) of the courtyard graphics; without a
        courtyard, of the pads (each grown by its half diagonal, so rotated
        pads are covered); None when neither exists.
    @throws KiCadLibraryError ``malformed_file`` for an unbalanced file.
    @note The courtyard is the KiCad-defined keep-out of a part, so it is the
        right envelope for sizing a board around it. Circles use their radius;
        arcs are bounded by their three points (a slight underestimate that
        the board margin covers).
    """
    xs: list[float] = []
    ys: list[float] = []
    for name in _GRAPHIC_NODES:
        for start, end in _direct_child_ranges(footprint_text, name):
            block = footprint_text[start:end]
            layer = _LAYER.search(block)
            if not layer or layer.group(1) not in _COURTYARD_LAYERS:
                continue
            points = {}
            for key, x, y in _POINT.findall(block):
                points.setdefault(key, []).append((float(x), float(y)))
                xs.append(float(x))
                ys.append(float(y))
            if name == "fp_circle" and "center" in points and "end" in points:
                (cx, cy), (ex, ey) = points["center"][0], points["end"][0]
                radius = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
                xs.extend((cx - radius, cx + radius))
                ys.extend((cy - radius, cy + radius))
    if not xs:
        for start, end in _direct_child_ranges(footprint_text, "pad"):
            block = footprint_text[start:end]
            at, size = _PAD_AT.search(block), _PAD_SIZE.search(block)
            if not at or not size:
                continue
            x, y = float(at.group(1)), float(at.group(2))
            reach = (float(size.group(1)) ** 2 + float(size.group(2)) ** 2) ** 0.5 / 2
            xs.extend((x - reach, x + reach))
            ys.extend((y - reach, y + reach))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def preview_board_outline(footprint_text: str) -> tuple[float, float, float, float]:
    """@brief Edge.Cuts rectangle of the preview board, in board mm.

    @param footprint_text Content of the library footprint (before placement).
    @return (min x, min y, max x, max y): the historical 70 x 50 mm frame,
        grown where the footprint courtyard plus PREVIEW_OUTLINE_MARGIN_MM
        would reach beyond it. The footprint sits at _PREVIEW_ORIGIN.
    @throws KiCadLibraryError ``malformed_file`` for an unbalanced file.
    """
    outline = _PREVIEW_MIN_OUTLINE
    extent = footprint_extent(footprint_text)
    if extent is None:
        return outline
    ox, oy = _PREVIEW_ORIGIN
    margin = PREVIEW_OUTLINE_MARGIN_MM
    grown = (ox + extent[0] - margin, oy + extent[1] - margin,
             ox + extent[2] + margin, oy + extent[3] + margin)
    return (round(min(outline[0], grown[0]), 4), round(min(outline[1], grown[1]), 4),
            round(max(outline[2], grown[2]), 4), round(max(outline[3], grown[3]), 4))


#: Nickname of the symbol library in the preview project's own
#: ``sym-lib-table`` and of the library part of the schematic's footprint
#: field. It names only the copy inside the temporary project.
PREVIEW_LIBRARY_NICKNAME = "KiCadGen_Preview"

#: Schematic file format written by `preview_schematic_text`: the
#: ``(version ...)`` token that ``kicad-cli 10.0.8 sch upgrade`` writes
#: (checked 2026-11-13), the same that KiCad 10's demo schematics carry. It
#: needs KiCad 10, the oldest version the generated files already require
#: (`MINIMUM_KICAD_VERSION`).
SCHEMATIC_FORMAT_VERSION = 20260306

#: Centre of the placed symbol on the A4 sheet (297 x 210 mm), in schematic
#: mm (Y down), on the 1.27 mm (50 mil) grid KiCad snaps symbols to.
_SCHEMATIC_CENTRE = (148.59, 105.41)

#: Horizontal distance between two units of a multi-unit symbol, mm (40 x
#: 1.27, on the grid).
_SCHEMATIC_UNIT_STEP = 50.8

#: Reasons why no preview schematic was written, as the codes stored in
#: `PreviewProject.schematic_skipped`; the GUI words them.
PREVIEW_SCHEMATIC_SKIP_REASONS = (
    "symbol_missing",          # no <name>.kicad_sym next to the footprint
    "symbol_unreadable",       # not UTF-8 or not a balanced S-expression
    "symbol_not_found",        # the library holds no symbol of that name
    "symbol_derived",          # the symbol extends another one (not flattened)
    "sym_lib_table_foreign",   # a caller-chosen folder already has another table
    "fp_lib_table_foreign",    # same, for the footprint library table
    "symbol_copy_foreign",     # same, for an existing <name>.kicad_sym
    "footprint_copy_foreign",  # same, for KiCadGen_Preview.pretty
    "schematic_locked",        # the Schematic Editor holds the preview schematic open
)

_SYMBOL_HEAD = re.compile(r'\A(\(\s*symbol\s+)("(?:[^"\\]|\\.)*"|[^\s()"]+)')
_PROPERTY_HEAD = re.compile(
    r'\A(\(\s*property\s+)("(?:[^"\\]|\\.)*"|[^\s()"]+)(\s+)("(?:[^"\\]|\\.)*"|[^\s()"]+)')
_PROPERTY_AT = re.compile(r"\(at\s+" + _NUMBER + r"\s+" + _NUMBER + r"(?:\s+" + _NUMBER + r")?\s*\)")
_PIN_NUMBER = re.compile(r'\(number\s+("(?:[^"\\]|\\.)*"|[^\s()"]+)')
_UNIT_NAME = re.compile(r"_(\d+)_(\d+)\Z")
_FLAG = re.compile(r"\((exclude_from_sim|in_bom|on_board|in_pos_files)\s+(yes|no)\s*\)")


def _atom_value(token: str) -> str:
    """Text of one S-expression atom: a quoted string unescaped, a bare atom as is."""
    if token.startswith('"'):
        return _node_arguments(f"(x {token})")[0]
    return token


class _SymbolUnusable(Exception):
    """A symbol library that cannot feed the preview schematic; ``args[0]`` is the reason code."""


def _library_symbol(library_text: str, name: str) -> str:
    """@brief Text of the top-level symbol `name` of a ``.kicad_sym`` file.

    @param library_text Content of the symbol library.
    @param name Symbol name; when the library holds exactly one symbol of
        another name, that one is taken (a library renamed by hand).
    @return The ``(symbol ...)`` block, verbatim.
    @throws _SymbolUnusable ``symbol_unreadable``, ``symbol_not_found`` or
        ``symbol_derived``.
    """
    if not library_text.lstrip().startswith("(kicad_symbol_lib"):
        raise _SymbolUnusable("symbol_unreadable")
    try:
        ranges = _direct_child_ranges(library_text, "symbol")
    except KiCadLibraryError as exc:
        raise _SymbolUnusable("symbol_unreadable") from exc
    blocks = [library_text[start:end] for start, end in ranges]
    named = [block for block in blocks
             if (head := _SYMBOL_HEAD.match(block)) and _atom_value(head.group(2)) == name]
    chosen = named[0] if named else (blocks[0] if len(blocks) == 1 else None)
    if chosen is None:
        raise _SymbolUnusable("symbol_not_found")
    if _direct_child_ranges(chosen, "extends"):
        # A derived symbol must be flattened with its parent before it can be
        # embedded in lib_symbols; the generators never write one.
        raise _SymbolUnusable("symbol_derived")
    return chosen


def _symbol_units(symbol_block: str, name: str) -> dict[int, list[str]]:
    """@brief Pin numbers of each unit of a library symbol.

    @param symbol_block The ``(symbol ...)`` block of the library.
    @param name Its name, prefix of the unit names ``<name>_<unit>_<style>``.
    @return {unit number: sorted unique pin numbers}; pins of unit 0 belong
        to every unit. A symbol without numbered units yields {1: pins}.
    """
    common: list[str] = []
    per_unit: dict[int, list[str]] = {}
    for start, end in _direct_child_ranges(symbol_block, "symbol"):
        block = symbol_block[start:end]
        head = _SYMBOL_HEAD.match(block)
        unit_match = _UNIT_NAME.search(_atom_value(head.group(2))) if head else None
        unit = int(unit_match.group(1)) if unit_match else 1
        numbers = [_atom_value(token) for token in _PIN_NUMBER.findall(block)]
        (common if unit == 0 else per_unit.setdefault(unit, [])).extend(numbers)
    if not per_unit:
        per_unit[1] = []
    return {unit: sorted(set(pins + common), key=_pin_sort_key)
            for unit, pins in sorted(per_unit.items())}


def _pin_sort_key(number: str) -> tuple:
    """Natural order of pin numbers (``2`` before ``10``, ``A1`` before ``A10``).

    Only ASCII digit runs are numbers: ``str.isdigit`` accepts «²», which
    ``int`` refuses, and the key would raise instead of sorting (review
    2026-11-14).
    """
    return tuple((0, int(part), "") if part.isascii() and part.isdigit() else (1, 0, part)
                 for part in re.findall(r"[0-9]+|[^0-9]+", number))


def preview_reference(prefix: str) -> str:
    """@brief Reference of the placed preview symbol: the designator plus 1.

    @param prefix Value of the library symbol's ``Reference`` property
        (``U``, ``K``, ``J?``...).
    @return The designator with trailing ``?`` and digits removed, followed
        by ``1``; ``U1`` when nothing usable is left.
    """
    letters = re.sub(r"[?\d\s]+\Z", "", prefix or "")
    return f"{letters or 'U'}1"


def preview_schematic_text(symbol_block: str, name: str, *, footprint_id: str,
                           project_name: str, nickname: str = PREVIEW_LIBRARY_NICKNAME,
                           sheet_uuid: str | None = None) -> str:
    """@brief A KiCad 10 schematic with the component symbol placed on an A4 sheet.

    @param symbol_block The ``(symbol ...)`` block of the component library.
    @param name Symbol name (also the placed Value).
    @param footprint_id Footprint field of the placed symbol, ``nick:name``.
    @param project_name Stem of the ``.kicad_pro`` the schematic belongs to,
        named in the symbol instances as KiCad writes them.
    @param nickname Library nickname of the ``lib_id``.
    @param sheet_uuid UUID of the root sheet; a fresh one when None.
    @return The schematic text (format `SCHEMATIC_FORMAT_VERSION`).
    @throws KiCadLibraryError ``malformed_file`` for an unbalanced block.
    @note The symbol is embedded in ``lib_symbols`` as KiCad writes it: the
        top-level name becomes ``nickname:name`` and the unit children keep
        their unprefixed ``name_<unit>_<style>`` names. Every unit is placed,
        side by side, with the same reference. The placed properties copy the
        library ones (effects, visibility) with their positions moved by the
        placement (library Y up, schematic Y down); ``ki_*`` properties stay
        in the library, as KiCad does.
    """
    lib_id = f"{nickname}:{name}"
    head = _SYMBOL_HEAD.match(symbol_block)
    if not head:
        raise KiCadLibraryError("The symbol block is not a KiCad symbol.",
                                code="malformed_file")
    embedded = f"{head.group(1)}{quote(lib_id)}{symbol_block[head.end():]}"
    units = _symbol_units(symbol_block, _atom_value(head.group(2)))
    # The placed symbol keeps the library's BOM/board/simulation flags.
    flags: dict[str, str] = {}
    for node in ("exclude_from_sim", "in_bom", "on_board", "in_pos_files"):
        for start, _end in _direct_child_ranges(symbol_block, node):
            found = _FLAG.match(symbol_block, start)
            if found:
                flags[found.group(1)] = found.group(2)
    properties = [symbol_block[start:end]
                  for start, end in _direct_child_ranges(symbol_block, "property")]
    reference = "U1"
    for block in properties:
        match = _PROPERTY_HEAD.match(block)
        if match and _atom_value(match.group(2)) == "Reference":
            reference = preview_reference(_atom_value(match.group(4)))
    overrides = {"Reference": reference, "Value": name, "Footprint": footprint_id}
    sheet = sheet_uuid or str(uuid.uuid4())
    centre_x, centre_y = _SCHEMATIC_CENTRE
    first_x = centre_x - _SCHEMATIC_UNIT_STEP * (len(units) - 1) / 2
    placed: list[str] = []
    for index, (unit, pins) in enumerate(units.items()):
        x, y = round(first_x + index * _SCHEMATIC_UNIT_STEP, 4), centre_y
        rows = [
            "\t(symbol",
            f"\t\t(lib_id {quote(lib_id)})",
            f"\t\t(at {_mm(x)} {_mm(y)} 0)",
            f"\t\t(unit {unit})",
            "\t\t(body_style 1)",
        ]
        rows.extend(f"\t\t({flag} {flags.get(flag, default)})" for flag, default in (
            ("exclude_from_sim", "no"), ("in_bom", "yes"), ("on_board", "yes"),
            ("in_pos_files", "yes")))
        rows.append("\t\t(dnp no)")
        rows.append(f"\t\t(uuid {quote(str(uuid.uuid4()))})")
        for block in properties:
            match = _PROPERTY_HEAD.match(block)
            if not match:
                continue
            key = _atom_value(match.group(2))
            if key.startswith("ki_"):
                continue
            value = overrides.get(key, _atom_value(match.group(4)))
            text = f"{match.group(1)}{quote(key)}{match.group(3)}{quote(value)}{block[match.end():]}"

            def moved(at: re.Match, x=x, y=y) -> str:
                """Library position of a property moved onto the sheet."""
                angle = at.group(3) or "0"
                return (f"(at {_mm(x + float(at.group(1)))} "
                        f"{_mm(y - float(at.group(2)))} {angle})")
            rows.append("\t\t" + _PROPERTY_AT.sub(moved, text, count=1))
        rows.extend(f"\t\t(pin {quote(pin)} (uuid {quote(str(uuid.uuid4()))}))" for pin in pins)
        rows.extend((
            "\t\t(instances",
            f"\t\t\t(project {quote(project_name)}",
            f"\t\t\t\t(path {quote('/' + sheet)}",
            f"\t\t\t\t\t(reference {quote(reference)})",
            f"\t\t\t\t\t(unit {unit})",
            "\t\t\t\t)",
            "\t\t\t)",
            "\t\t)",
            "\t)",
        ))
        placed.append("\n".join(rows))
    return (
        "(kicad_sch\n"
        f"\t(version {SCHEMATIC_FORMAT_VERSION})\n"
        f"\t(generator {quote('kicadgen_tool')})\n"
        f"\t(generator_version {quote(GENERATOR_VERSION)})\n"
        f"\t(uuid {quote(sheet)})\n"
        f"\t(paper {quote('A4')})\n"
        "\t(lib_symbols\n"
        f"\t\t{embedded.strip()}\n"
        "\t)\n"
        + "\n".join(placed) + "\n"
        "\t(sheet_instances\n"
        f"\t\t(path {quote('/')}\n"
        f"\t\t\t(page {quote('1')})\n"
        "\t\t)\n"
        "\t)\n"
        "\t(embedded_fonts no)\n"
        ")\n"
    )


def preview_sym_lib_table(library_file_name: str,
                          nickname: str = PREVIEW_LIBRARY_NICKNAME) -> str:
    """@brief The preview project's own ``sym-lib-table``: ONE row, inside the project.

    @param library_file_name File name of the symbol library copy in the
        preview folder.
    @param nickname Library nickname.
    @return The table text; its only URI is ``${KIPRJMOD}/<file>``, so
        nothing outside the temporary project is named.
    """
    uri = "${KIPRJMOD}/" + library_file_name
    return (
        "(sym_lib_table\n"
        "  (version 7)\n"
        f"  (lib (name {quote(nickname)})(type {quote('KiCad')})(uri {quote(uri)})"
        f"(options {quote('')})(descr {quote('KiCadGen preview copy')}))\n"
        ")\n"
    )


def preview_fp_lib_table(nickname: str = PREVIEW_LIBRARY_NICKNAME) -> str:
    """@brief The preview project's own ``fp-lib-table``: ONE row, inside the project.

    @param nickname Library nickname, the one of the schematic's footprint field.
    @return The table text; its only URI is ``${KIPRJMOD}/<nickname>.pretty``,
        the footprint copy inside the temporary project.
    @note USER DECISION 2026-11-13: this temporary, project-local table is
        the only ``fp-lib-table`` the preview writes; the user's project
        tables and KiCad's global table are never written or read.
    """
    uri = "${KIPRJMOD}/" + nickname + ".pretty"
    return (
        "(fp_lib_table\n"
        "  (version 7)\n"
        f"  (lib (name {quote(nickname)})(type {quote('KiCad')})(uri {quote(uri)})"
        f"(options {quote('')})(descr {quote('KiCadGen preview copy')}))\n"
        ")\n"
    )


def create_preview_project(
        artifacts: ComponentArtifacts,
        output_dir: str | os.PathLike[str] | None = None) -> PreviewProject:
    """@brief Create an isolated board that can be opened in KiCad for review.

    @details The generated board embeds the footprint and references a local copy
    of the STEP model through ``${KIPRJMOD}``. No library table of the user's
    projects or of KiCad's configuration is changed: the only tables written
    are the preview folder's own one-row ``sym-lib-table`` and
    ``fp-lib-table``, through ``${KIPRJMOD}`` (below).
    Without a STEP (`ComponentArtifacts.has_step` False) the board footprint
    has no model node and no model is copied.
    USER DECISION 2026-11-13 (option B): the same folder also receives a
    schematic ``<name>-preview.kicad_sch`` with the component symbol placed
    (`preview_schematic_text`), a copy ``<name>.kicad_sym`` of the symbol
    library and a project-local ``sym-lib-table`` whose only row points to
    that copy through ``${KIPRJMOD}`` (`preview_sym_lib_table`), and (same
    decision, follow-up) a copy of the footprint in
    ``KiCadGen_Preview.pretty`` with a project-local ``fp-lib-table`` naming
    only it (`preview_fp_lib_table`), so the schematic's footprint field
    resolves. No other library table, project or global, is written. Without
    a usable symbol the schematic and both tables are skipped
    (`PreviewProject.schematic_skipped`), never the board.
    The board outline is at least 70 x 50 mm and grows to keep the footprint
    courtyard plus a margin inside (see preview_board_outline).
    The footprint is parsed before anything is written, the board and model
    copies are written through a temporary file and an atomic replace, and a
    board that KiCad currently holds open is never rewritten.
    @param artifacts Validated generated artifacts of one component.
    @param output_dir Preview directory; defaults to ``<component>/.kicad-preview``.
    @return Paths of the preview directory, board and project files.
    @throws KiCadLibraryError ``invalid_footprint``/``malformed_file`` when the
        footprint cannot be converted (nothing is written), ``file_not_utf8``
        when it is not UTF-8 text (nothing is written), ``preview_locked``
        when KiCad's lock file ``~<board>.lck`` exists next to the board,
        ``preview_write_refused`` (context ``path``, ``error``) when the
        operating system refuses to replace a preview file (read-only
        attribute, file held open by another program).
    @throws OSError Any other file-system failure, unchanged.
    @note The copies never carry the read-only attribute of their source
        (see `_copy_writable`). In the default ``.kicad-preview`` folder
        (`PREVIEW_DIR_NAME`) the read-only attribute of the files about to be
        replaced is cleared: that folder is generated by the program and never
        holds user content, and older versions copied the attribute onto it
        from read-only sources. Files of any other folder chosen by the caller
        are not unlocked; a refusal there, or a file held open, is reported as
        ``preview_write_refused``.
    @note USER DECISION 2026-11-12: the preview works without a STEP model
        (no FreeCAD). The board then carries no ``(model ...)`` node, and in
        the program's own ``.kicad-preview`` folder the ``<name>.step`` and
        ``<name>.wrl`` copied by an earlier preview are removed, so KiCad
        does not show a stale model. A caller-chosen folder is never cleaned.
    @note A lock file left behind by a crashed KiCad also blocks the preview:
        the error names the lock file so the user can delete it once KiCad is
        closed. Stale locks are not removed automatically because a running
        KiCad and a crashed one cannot be told apart from the file alone.
    """
    preview_dir = Path(output_dir or artifacts.component_dir / PREVIEW_DIR_NAME)
    board_path = preview_dir / f"{artifacts.name}-preview.kicad_pcb"
    project_path = preview_dir / f"{artifacts.name}-preview.kicad_pro"
    # Parse and convert the footprint first: a malformed file must not leave a
    # half-refreshed preview directory behind.
    footprint_text = _read_utf8(artifacts.footprint_path)
    # Read once: the board text and the copies below must agree.
    with_model = artifacts.has_step
    # The WRL travels only with its STEP (see ComponentArtifacts).
    shipped_wrl = artifacts.wrl_path if with_model else None
    footprint = _board_footprint(
        footprint_text,
        f"${{KIPRJMOD}}/{artifacts.name}.step" if with_model else None,
        artifacts.model_offset,
    )
    x0, y0, x1, y1 = preview_board_outline(footprint_text)
    lock_path = kicad_lock_file(board_path)
    if lock_path.exists():
        raise KiCadLibraryError(
            f"The preview board is open in KiCad (lock file {lock_path}). "
            "Close it in KiCad before refreshing the preview.",
            code="preview_locked", path=lock_path, board=board_path,
        )
    own_folder = preview_dir.name == PREVIEW_DIR_NAME
    schematic_path = preview_dir / f"{artifacts.name}-preview.kicad_sch"
    symbol_copy = preview_dir / f"{artifacts.name}.kicad_sym"
    table_path = preview_dir / "sym-lib-table"
    # Footprint library of the preview project (USER DECISION 2026-11-13):
    # a copy of the footprint inside the temporary folder, named by a table
    # inside the same folder, so the schematic's footprint field resolves.
    footprint_library = preview_dir / f"{PREVIEW_LIBRARY_NICKNAME}.pretty"
    footprint_copy = footprint_library / f"{artifacts.name}.kicad_mod"
    fp_table_path = preview_dir / "fp-lib-table"
    # Only the schematic open in the Schematic Editor (review 2026-11-13): the
    # board is still refreshed, the schematic set is left exactly as it is
    # (neither rewritten nor removed) and the reason is reported.
    schematic_locked = kicad_lock_file(schematic_path).exists()
    # The schematic is composed before the first write too; a symbol that
    # cannot feed it only skips the schematic, the board is still written.
    # The library footprint carries the same model node as the board one, so
    # «Update PCB from schematic» brings the 3D model too.
    library_footprint = (
        attach_3d_model(footprint_text, f"${{KIPRJMOD}}/{artifacts.name}.step",
                        artifacts.model_offset)
        if with_model else detach_3d_model(footprint_text))
    if schematic_locked:
        schematic, table, fp_table, skipped = None, None, None, "schematic_locked"
    else:
        schematic, table, fp_table, skipped = _preview_schematic_parts(
            artifacts, project_path.stem, own_folder,
            {table_path: None, fp_table_path: None, symbol_copy: None,
             footprint_copy: library_footprint})
    outline_uuid = uuid.uuid4()
    layer_rows = "".join(
        f"\t\t({ordinal} {quote(name)} {kind}"
        + (f" {quote(user_name)}" if user_name else "") + ")\n"
        for ordinal, name, kind, user_name in PREVIEW_BOARD_LAYERS)
    # Every string atom goes through the shared quoter and every number
    # through the shared formatter (`kicadgen.core.sexpr`), as in the
    # generated footprints: no value can break the S-expression.
    board = (
        f"(kicad_pcb (version {KICAD_VERSION}) (generator {quote('kicadgen_tool')})\n"
        f"\t(generator_version {quote(GENERATOR_VERSION)})\n"
        f"\t(general (thickness {format_number(1.6)}))\n"
        f"\t(paper {quote('A4')})\n"
        "\t(layers\n"
        f"{layer_rows}"
        "\t)\n"
        f"\t(setup (pad_to_mask_clearance {format_number(0)}))\n"
        f"{footprint.rstrip()}\n"
        f"\t(gr_rect (start {_mm(x0)} {_mm(y0)}) (end {_mm(x1)} {_mm(y1)})\n"
        f"\t\t(stroke (width {format_number(0.05)}) (type default)) (fill none)\n"
        f"\t\t(layer {quote('Edge.Cuts')}) (uuid {quote(str(outline_uuid))}))\n"
        ")\n"
    )
    # The board text is complete before the first write, so a refusal below
    # can only come from the file system, never from the footprint.
    try:
        preview_dir.mkdir(parents=True, exist_ok=True)
        if preview_dir.name == PREVIEW_DIR_NAME:
            # Files of the program's own preview folder, about to be replaced:
            # a read-only attribute on them was copied from a read-only source
            # by older versions (shutil.copy2), never set to protect content.
            for own in (preview_dir / f"{artifacts.name}.step",
                        preview_dir / f"{artifacts.name}.wrl",
                        board_path, project_path,
                        schematic_path, symbol_copy, table_path,
                        fp_table_path, footprint_copy):
                if own.is_file():
                    _clear_readonly(own)
        preview_step = preview_dir / f"{artifacts.name}.step"
        if with_model:
            _atomic_copy(artifacts.step_path, preview_step)
        elif preview_dir.name == PREVIEW_DIR_NAME and preview_step.is_file():
            # No STEP now (USER DECISION 2026-11-12): the copy of an earlier
            # preview no longer matches the footprint and must not linger.
            # Only in the program's own preview folder, as for the WRL below.
            _remove_clearing_readonly(preview_step)
        preview_wrl = preview_dir / f"{artifacts.name}.wrl"
        if shipped_wrl is not None:
            _atomic_copy(shipped_wrl, preview_wrl)
        elif preview_dir.name == PREVIEW_DIR_NAME and preview_wrl.is_file():
            # The component lost its WRL (or its whole model): the copy of an
            # earlier preview no longer matches it and must not linger (review
            # 2026-11-03). Only in the program's own preview folder, where
            # nothing is user content.
            _remove_clearing_readonly(preview_wrl)
        _atomic_write_text(board_path, board)
        if schematic is not None:
            _write_preview_set(
                ((symbol_copy, _component_symbol_path(artifacts).read_bytes()),
                 (footprint_copy, library_footprint), (table_path, table),
                 (fp_table_path, fp_table), (schematic_path, schematic)))
        elif schematic_locked and footprint_copy.is_file():
            # The open schematic is left alone, but its footprint library is
            # refreshed: «Aggiorna PCB dallo schema» (Update PCB from
            # schematic) would otherwise bring the OLD footprint back into the
            # board just rewritten (review 2026-11-14).
            _atomic_write_text(footprint_copy, library_footprint)
        elif own_folder and not schematic_locked:
            # No usable symbol now: the schematic of an earlier preview would
            # show a stale part. Only in the program's own preview folder; an
            # open schematic is never touched.
            for stale in (schematic_path, symbol_copy, table_path,
                          fp_table_path, footprint_copy):
                if stale.is_file():
                    _remove_clearing_readonly(stale)
            if footprint_library.is_dir() and not any(footprint_library.iterdir()):
                footprint_library.rmdir()
        if not project_path.exists():
            _atomic_write_text(project_path, "{}\n")
    except PermissionError as exc:
        # A bare PermissionError reached the user as "unexpected OS error"
        # naming a scratch file; name the preview file that was refused.
        refused = _refused_path(exc, preview_dir)
        raise KiCadLibraryError(
            f"Unable to write the preview file {refused}: it is read-only or "
            f"held open by another program ({exc}).",
            code="preview_write_refused", path=refused, error=exc,
        ) from exc
    if schematic is None:
        return PreviewProject(preview_dir, board_path, project_path,
                              schematic_skipped=skipped)
    return PreviewProject(preview_dir, board_path, project_path,
                          schematic_path, symbol_copy)


def _write_preview_set(files) -> None:
    """@brief Write the preview schematic's files as one set, as far as the file system allows.

    @param files (path, text or bytes) pairs, the library copies first and the
        schematic last, so a schematic never names a library that is missing.
    @throws OSError The first failure, after the cleanup below.
    @note Every text is first written to a sibling scratch file; only when
        all scratch files exist are they moved onto their targets, each by an
        atomic replace. A failure while preparing leaves every target as it
        was. A failure while moving removes the targets this call CREATED
        (they did not exist before), so no half-new set is left; a target it
        had already replaced keeps its new, complete content (restoring the
        old one would need a backup copy of every file, not worth it for a
        generated preview).
    """
    staged: list[tuple[Path, Path, bool]] = []
    created_dirs: list[Path] = []
    try:
        for path, content in files:
            if not path.parent.is_dir():
                path.parent.mkdir(parents=True)
                created_dirs.append(path.parent)
            target = _write_target(path)
            scratch = _temporary_sibling(target)
            staged.append((target, scratch, target.exists()))
            _write_durably(scratch, content if isinstance(content, bytes)
                           else content.encode("utf-8"))
        published: list[tuple[Path, bool]] = []
        try:
            for target, scratch, existed in staged:
                _publish_scratch(scratch, target)
                published.append((target, existed))
        except OSError:
            for target, existed in published:
                if not existed:
                    _unlink_quietly(target)
            raise
    except OSError:
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    finally:
        for _target, scratch, _existed in staged:
            _unlink_quietly(scratch)


def _component_symbol_path(artifacts: ComponentArtifacts) -> Path:
    """Symbol library the generator writes next to the footprint library: ``<dir>/<name>.kicad_sym``."""
    return artifacts.component_dir / f"{artifacts.name}.kicad_sym"


def _preview_schematic_parts(
        artifacts: ComponentArtifacts, project_name: str, own_folder: bool,
        targets: dict[Path, str | None],
) -> tuple[str | None, str | None, str | None, str | None]:
    """@brief Compose the preview schematic and its two library tables, writing nothing.

    @param artifacts The component being previewed.
    @param project_name Stem of the preview ``.kicad_pro``.
    @param own_folder True for the program's ``.kicad-preview`` folder.
    @param targets The library files the schematic needs, in this order:
        ``sym-lib-table``, ``fp-lib-table``, the ``<name>.kicad_sym`` copy and
        the footprint copy in ``KiCadGen_Preview.pretty``, each mapped to the
        text it would receive (None for the first three: composed here).
    @return (schematic, sym-lib-table, fp-lib-table, None), or (None, None,
        None, reason code of `PREVIEW_SCHEMATIC_SKIP_REASONS`).
    @note In a caller-chosen folder none of those files is overwritten when
        it exists with other content, and the footprint copy is not added to
        a ``KiCadGen_Preview.pretty`` that holds anything else (unrelated
        library content is preserved): the schematic is skipped instead and
        none of the four files is written.
    """
    table_path, fp_table_path, symbol_copy, footprint_copy = targets
    nothing = (None, None, None)
    source = _component_symbol_path(artifacts)
    if not source.is_file():
        return *nothing, "symbol_missing"
    try:
        library_text = _read_utf8(source)
    except KiCadLibraryError:
        return *nothing, "symbol_unreadable"
    try:
        block = _library_symbol(library_text, artifacts.name)
        symbol_name = _atom_value(_SYMBOL_HEAD.match(block).group(2))
        schematic = preview_schematic_text(
            block, symbol_name,
            footprint_id=f"{PREVIEW_LIBRARY_NICKNAME}:{artifacts.name}",
            project_name=project_name)
    except _SymbolUnusable as exc:
        return *nothing, str(exc.args[0])
    except KiCadLibraryError:
        return *nothing, "symbol_unreadable"
    table = preview_sym_lib_table(f"{artifacts.name}.kicad_sym")
    fp_table = preview_fp_lib_table()
    if not own_folder:
        wanted_bytes = {
            table_path: table.encode("utf-8"),
            fp_table_path: fp_table.encode("utf-8"),
            symbol_copy: source.read_bytes(),
            footprint_copy: targets[footprint_copy].encode("utf-8"),
        }
        reasons = {table_path: "sym_lib_table_foreign", fp_table_path: "fp_lib_table_foreign",
                   symbol_copy: "symbol_copy_foreign", footprint_copy: "footprint_copy_foreign"}
        for path, wanted in wanted_bytes.items():
            if not path.exists():
                continue
            try:
                foreign = path.read_bytes() != wanted
            except OSError:
                foreign = True
            if foreign:
                return *nothing, reasons[path]
        library = footprint_copy.parent
        try:
            others = library.is_dir() and any(
                entry.name != footprint_copy.name for entry in library.iterdir())
        except OSError:
            others = True
        if others or (library.exists() and not library.is_dir()):
            return *nothing, "footprint_copy_foreign"
    return schematic, table, fp_table, None


def _refused_path(error: OSError, fallback: Path) -> Path:
    """@brief File a refused operation was writing to, for the user message.

    @param error The PermissionError raised by the file system.
    @param fallback Path reported when the error names no file.
    @return For a refused ``os.replace`` the destination (``filename2``, the
        real file), otherwise the file named by the error, otherwise
        `fallback`; a scratch name of ours is mapped back to its target.
    """
    named = getattr(error, "filename2", None) or getattr(error, "filename", None)
    if not named:
        return fallback
    path = Path(os.fsdecode(named))
    match = re.fullmatch(r"(.+)\.[0-9a-f]{%d}%s" % (
        TEMPORARY_TOKEN_HEX, re.escape(TEMPORARY_SUFFIX)), path.name)
    return path.with_name(match.group(1)) if match else path


def kicad_lock_file(document_path: str | os.PathLike[str]) -> Path:
    """@brief Return the lock file KiCad creates while a document is open.

    @details KiCad's ``LOCKFILE`` convention places ``~<file name>.lck`` next
    to the open document, e.g. ``~Demo-preview.kicad_pcb.lck``.
    @param document_path Board, schematic or project file path.
    @return Path of the corresponding lock file (it may not exist).
    """
    document = Path(document_path)
    return document.with_name(f"~{document.name}.lck")


#: Suffix of every scratch file this module writes next to a target.
TEMPORARY_SUFFIX = ".kicadgen.tmp"
#: Hex characters of the random token in a scratch name: short, because the
#: name is appended to the target's (Windows MAX_PATH without long paths);
#: uniqueness comes from the exclusive creation, not from the length.
TEMPORARY_TOKEN_HEX = 8
#: Attempts at an unused scratch name before giving up (a collision of
#: 8 hex characters is already a one-in-four-billion event).
TEMPORARY_ATTEMPTS = 32
#: Age after which a scratch file of ours next to a target is considered
#: left by a killed run and removed, seconds. A live run replaces or removes
#: its own scratch file within seconds; a day leaves a wide margin for a
#: slow network folder or a paused machine.
STALE_SCRATCH_AGE_S = 24 * 3600


def _scratch_pattern(target: Path, suffix: str) -> "re.Pattern[str]":
    """Names of OUR scratch files for `target`: legacy fixed, 32-hex and short-hex tokens."""
    return re.compile(re.escape(target.name)
                      + r"(?:\.(?:[0-9a-f]{%d}|[0-9a-f]{32}))?" % TEMPORARY_TOKEN_HEX
                      + re.escape(suffix) + r"\Z")


def _remove_stale_scratch(target: Path, suffix: str = TEMPORARY_SUFFIX,
                          now: float | None = None) -> list[Path]:
    """Remove scratch files a killed run left next to `target`; return those removed.

    Only regular files whose name matches `_scratch_pattern` (ours, for this
    very target) and whose modification time is older than
    STALE_SCRATCH_AGE_S are removed. A file another process holds open is
    refused by Windows and kept; folders, other names and young files are
    never touched, so a concurrent run's scratch file survives. Failures
    are ignored: cleanup must never block the operation itself.
    """
    import time
    pattern = _scratch_pattern(target, suffix)
    limit = (time.time() if now is None else now) - STALE_SCRATCH_AGE_S
    removed: list[Path] = []
    try:
        entries = list(os.scandir(target.parent))
    except OSError:
        return removed
    for entry in entries:
        if not pattern.match(entry.name):
            continue
        try:
            if not entry.is_file(follow_symlinks=False) or entry.stat().st_mtime > limit:
                continue
            # A scratch copy made by an older version could carry the source's
            # read-only attribute: it is ours, so unlock it rather than keep it.
            _remove_clearing_readonly(Path(entry.path))
            removed.append(Path(entry.path))
        except OSError:
            continue
    return removed


def _temporary_sibling(target: Path, suffix: str = TEMPORARY_SUFFIX) -> Path:
    """Create and return a fresh, empty scratch file in the target directory.

    The scratch file sits in the target's own folder so the final
    ``os.replace`` is an atomic rename on one volume. Its name is
    ``<target>.<8 hex>`` + suffix, created with O_EXCL and retried on a
    collision: a fixed name let two concurrent runs (two windows, two
    instances) write over each other's half-written copy, and a stale one
    left by a killed process (or a folder in its place) made every later
    run fail. Stale scratch files of ours older than STALE_SCRATCH_AGE_S are
    removed first (see `_remove_stale_scratch`); their suffix keeps them out
    of KiCad's scan of ``*.kicad_mod`` and ``*.step`` meanwhile.
    Raises FileExistsError when TEMPORARY_ATTEMPTS names are all taken and
    OSError when the folder does not accept a new file.
    """
    import secrets
    _remove_stale_scratch(target, suffix)
    for _attempt in range(TEMPORARY_ATTEMPTS):
        candidate = target.with_name(
            f"{target.name}.{secrets.token_hex(TEMPORARY_TOKEN_HEX // 2)}{suffix}")
        try:
            handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        os.close(handle)
        return candidate
    raise FileExistsError(f"No free scratch name next to {target}")


#: Windows FILE_ATTRIBUTE_REPARSE_POINT (symbolic links, junctions, mount
#: points), from ``stat.FILE_ATTRIBUTE_REPARSE_POINT`` where available.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _clear_readonly(path: Path) -> None:
    """@brief Give the owner write permission back (Windows: clear read-only).

    @param path File of OURS: a scratch file, a copy we made, a backup we
        wrote. Never called on a user file or on a source artifact.
    @note Links and reparse points are never unlocked, even under our names:
        unlocking through them would change the attribute of their target.
    @note Best effort: a failure is ignored, the operation that needed the
        file writable then reports its own error.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
        # Only a plain file is unlocked: a symbolic link, a junction or any
        # other reparse point may lead to a user file, and os.chmod follows
        # links (asking it not to is unsupported on Linux).
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
            return
        if not info.st_mode & stat.S_IWRITE:
            os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IWRITE)
    except OSError:
        pass


def _remove_clearing_readonly(path: Path) -> None:
    """@brief Delete one of OUR files even when it carries the read-only attribute.

    @param path Scratch, rollback or backup file written by this module.
    @throws OSError When the file still cannot be removed (held open).
    @note On Windows ``os.remove`` refuses a read-only file; older versions
        copied that attribute from read-only sources (``shutil.copy2``), so
        their leftovers could never be cleaned up.
    """
    try:
        os.remove(path)
    except PermissionError:
        _clear_readonly(path)
        os.remove(path)


def _unlink_quietly(path: Path) -> None:
    """Remove a scratch file; a cleanup failure must not hide the real error."""
    try:
        if path.exists():
            _remove_clearing_readonly(path)
    except OSError:
        pass


#: Windows file attribute of a junction, symbolic link or other reparse point.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: Path | os.DirEntry) -> bool:
    """@brief Whether `path` is a link of any kind, never followed.

    @param path Path or directory entry to test.
    @return True for a symbolic link, a Windows junction or any other
        reparse point; False for a plain file or folder, or a missing path.
    @note ``is_dir(follow_symlinks=False)`` is True for a junction on
        Python 3.13 on Windows, so it cannot tell a junction from our own
        folder; the link tests and the reparse-point attribute can.
    """
    try:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            return True
        info = path.stat(follow_symlinks=False) if isinstance(path, os.DirEntry) \
            else os.lstat(path)
    except (OSError, ValueError):
        return False
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _empty_folder_quietly(directory: Path) -> None:
    """Delete what `directory` holds, removing links themselves and never entering them."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        path = Path(entry.path)
        try:
            if _is_reparse_point(entry):
                # rmdir removes a directory link (junction) without touching
                # its target; a file link is removed like a file.
                try:
                    os.rmdir(path)
                except OSError:
                    os.remove(path)
            elif entry.is_dir(follow_symlinks=False):
                _empty_folder_quietly(path)
                os.rmdir(path)
            else:
                _remove_clearing_readonly(path)
        except OSError:
            # The caller reports the folder as left; nothing else is tried.
            pass


def _remove_tree_quietly(directory: Path) -> bool:
    """@brief Remove a folder of ours (rollback copies), read-only files included.

    @param directory Folder created by this module.
    @return True when the folder is gone; False when it is left, including
        when `directory` is a link (`_is_reparse_point`).
    @note ``shutil.rmtree(ignore_errors=True)`` silently left the folder
        behind when one copy was read-only; files are unlocked first here.
    @note SAFETY BOUNDARY (review 2026-11-03, D3): a junction, symbolic link
        or other reparse point in the place of the folder is refused, never
        followed: walking it emptied the folder it points to, which is not
        ours. A link found INSIDE is removed itself, never entered
        (``os.walk`` enters junctions, which are not symbolic links).
    """
    if _is_reparse_point(directory):
        return False
    if not directory.exists():
        return True
    _empty_folder_quietly(directory)
    try:
        os.rmdir(directory)
    except OSError:
        return False
    return True


def _copy_writable(source: Path, target: Path) -> None:
    """@brief Copy content and timestamps of `source`, never its read-only attribute.

    @param source File to copy (left untouched, attribute included).
    @param target Destination: one of our scratch or rollback files.
    @throws OSError When the copy fails.
    @note ``shutil.copy2`` copies the permission bits, i.e. the Windows
        read-only attribute: a read-only source (VCS checkout with locking,
        files restored from read-only media) produced read-only copies that
        broke the next preview refresh or overwrite, and scratch files that
        could not be deleted. The timestamps are kept as before; only the
        write permission is given back to the copy.
    """
    shutil.copy2(source, target)
    _clear_readonly(target)


def _write_durably(path: Path, payload: bytes) -> None:
    """@brief Write `payload` into an existing scratch file and fsync it.

    @param path Scratch file created by `_temporary_sibling`.
    @param payload Complete content.
    @throws OSError When the write or the flush to disk fails.
    @note Same guarantee as `kicadgen.fsutil.atomic_write_bytes`: the data is
        on disk before the rename publishes it, so a power loss right after
        the replace cannot leave an empty or truncated fp-lib-table. That
        helper is not used directly because its scratch name would escape the
        stale-scratch cleanup of this module (`_scratch_pattern`).
    """
    with open(path, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _is_symlink(path: Path) -> bool:
    """Whether `path` itself is a symbolic link (module-level so tests can fake one)."""
    return path.is_symlink()


def _resolve_link(path: Path) -> Path:
    """@brief Final target of a symbolic link, which must exist.

    @param path A symbolic link (see `_is_symlink`).
    @return The real path, every link of the chain followed.
    @throws OSError When the chain does not end at an existing entry
        (dangling link) or cannot be followed (a loop, a denied folder).
    @note Module-level so tests can fake a link without the privilege Windows
        requires to create one.
    """
    return path.resolve(strict=True)


def _write_target(path: Path) -> Path:
    """@brief The file the bytes meant for `path` must be written to.

    @param path A target of ours: an installed file, a library table, a
        preview file. May be a symbolic link kept by the user (a dotfiles
        checkout of ``~/.config/kicad/<version>/fp-lib-table``).
    @return `path` itself for a plain file or a missing one; for a symbolic
        link, the real file it leads to.
    @throws KiCadLibraryError ``target_link_unresolved`` (context ``path``,
        ``error``) when the link is dangling or cannot be followed: nothing
        can be written through it, and writing a plain file in its place
        would silently discard the user's link.
    @throws KiCadLibraryError ``install_target_is_folder`` when the link
        leads to a folder.
    @note SAFETY BOUNDARY (review 2026-10-27, A1): every publisher of this
        module (`_atomic_write_bytes`, `_atomic_copy`, `_restore_file`, the
        install staging, `update_fp_lib_table`) resolves its target here
        BEFORE creating the scratch file, so the scratch is a sibling of the
        real file (same volume, atomic rename) and the rename replaces the
        real file, never the link. ``os.replace`` onto a symbolic link used
        to delete the link and leave a plain file in its place: the linked
        copy kept the old content and the user was never told.
    """
    if not _is_symlink(path):
        return path
    try:
        real = _resolve_link(path)
    except OSError as exc:
        raise KiCadLibraryError(
            f"{path} is a symbolic link that cannot be followed ({exc}); "
            "nothing can be written through it. Repair or remove the link, "
            "then retry. No file was changed.",
            code="target_link_unresolved", path=path, error=exc,
        ) from exc
    if real.is_dir():
        raise _target_is_folder(path)
    return real


def _hard_linked(target: Path) -> bool:
    """@brief Whether `target` is a regular file reachable under another name.

    @param target An existing path, links already resolved (`_write_target`).
    @return True when the file has more than one hard link; False for a
        missing path, a folder or a file with a single name.
    @note ``st_nlink`` is reported by Windows (NTFS) and POSIX alike; a file
        system that does not count links reports 1, so such a file is
        published by the ordinary atomic replace.
    """
    try:
        info = os.stat(target)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink > 1


def _write_in_place(source: Path, target: Path) -> None:
    """@brief Overwrite the existing inode of `target` with the bytes of `source`.

    @param source Complete file already on disk (a scratch or rollback copy).
    @param target Existing hard-linked file (`_hard_linked`); it keeps its
        inode, so every other name of it sees the new content.
    @throws OSError When `target` cannot be opened for writing (read-only
        attribute, held open) or the write fails: the same PermissionError
        class a refused replace raises, mapped by the callers to
        ``install_write_refused`` naming `target`.
    @note Opening `target` is retried with the bounded policy of
        `kicadgen.fsutil.retry_transient` (the one `replace_file` uses): a
        scanner or KiCad's file watcher holding the table for an instant
        refused the atomic rename and the in-place write alike, but only the
        rename was waited for (review 2026-10-29, L3). Only the open is
        retried: once the file is truncated a retry would start from a
        half-written file.
    @note SAFETY BOUNDARY (review 2026-10-27, A1): unlike the atomic rename,
        this write is not crash-atomic, so it is used ONLY where a copy of
        the previous content already exists on disk before it starts: the
        staged ``.kicadgen-backup`` of `update_fp_lib_table`, the rollback
        folder of `install_component_library`, or the rollback copy itself
        in `_restore_file` (which is never deleted before the write
        succeeded). Refusing hard links instead would have kept the atomic
        guarantee but left the user with a dotfiles table that cannot be
        updated at all; the in-place write keeps BOTH names of the file in
        agreement, which is what the link is for.
    """
    with open(source, "rb") as stream, retry_transient(
            lambda: open(target, "r+b"), sleep=_retry_pause) as out:
        out.truncate(0)
        shutil.copyfileobj(stream, out)
        out.flush()
        os.fsync(out.fileno())


def _retry_pause(seconds: float) -> None:
    """Pause between two attempts of `retry_transient` (looked up at call time so tests can replace it)."""
    time.sleep(seconds)


def _publish_scratch(scratch: Path, target: Path) -> None:
    """@brief Make `target` hold the complete, fsynced bytes of `scratch`.

    @param scratch Sibling scratch file of `target`, fully written and
        flushed; consumed (renamed or removed) on success.
    @param target Real file to publish (links already resolved by
        `_write_target`), existing or not.
    @throws OSError From `replace_file` or `_write_in_place`; `scratch` is
        then left for the caller's cleanup.
    @note The ONE place that chooses between the atomic rename (plain or
        missing target) and the in-place overwrite (hard-linked target, see
        `_write_in_place`). Every publisher of this module goes through it so
        the link classes are handled identically for the library table, the
        installed footprint and models, the preview files and the rollback.
    """
    if _hard_linked(target):
        _write_in_place(scratch, target)
        _unlink_quietly(scratch)
        return
    replace_file(scratch, target)


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write bytes durably through a sibling scratch file and an atomic replace.

    The target is resolved through a symbolic link first (`_write_target`,
    may raise ``target_link_unresolved``) and published by `_publish_scratch`.
    """
    target = _write_target(target)
    temporary = _temporary_sibling(target)
    try:
        _write_durably(temporary, payload)
        _publish_scratch(temporary, target)
    finally:
        _unlink_quietly(temporary)


def _atomic_write_text(target: Path, text: str) -> None:
    """Write UTF-8 text (LF newlines) durably through a scratch file and a replace."""
    _atomic_write_bytes(target, text.encode("utf-8"))


def _atomic_copy(source: Path, target: Path) -> None:
    """Copy a file (writable, see `_copy_writable`) durably through a scratch file and a replace.

    Like `_atomic_write_bytes`, the target is resolved through a symbolic
    link first and published by `_publish_scratch`.
    """
    target = _write_target(target)
    temporary = _temporary_sibling(target)
    try:
        _copy_writable(source, temporary)
        _fsync_file(temporary)
        _publish_scratch(temporary, target)
    finally:
        _unlink_quietly(temporary)


def launch_preview(pcbnew_path: str | os.PathLike[str], board_path: Path) -> None:
    """@brief Open an isolated preview board in the KiCad PCB Editor.

    @param pcbnew_path Path of the pcbnew executable.
    @param board_path Path of the preview .kicad_pcb to open.
    @throws KiCadLibraryError When the executable or the board file is
        missing (codes pcbnew_missing, preview_missing) or the process
        cannot be started (launch_failed).
    """
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
    try:
        _start_editor(executable, board_path, neutral_working_dir(executable))
    except OSError as exc:
        raise KiCadLibraryError(
            f"Unable to start KiCad: {exc}",
            code="launch_failed", error=exc,
        ) from exc


def neutral_working_dir(executable: str | os.PathLike[str] | None) -> Path:
    """@brief Working folder for a KiCad process, OUTSIDE the user's project.

    @param executable The program about to run; None uses the system
        temporary folder.
    @return The executable's own folder when it exists, otherwise the system
        temporary folder.
    @note Review 2026-11-13: on Windows a process whose working folder lies
        inside a folder keeps that folder from being renamed or deleted
        ([WinError 5]); an editor opened by «Prova in KiCad» (Test in KiCad)
        with its working folder in ``<project>/.kicad-preview`` blocked
        renaming the project for as long as KiCad stayed open. Every file
        argument is absolute, so the working folder carries no meaning;
        KiCad resolves ``${KIPRJMOD}`` from the document's own folder.
    """
    if executable:
        folder = Path(executable).absolute().parent
        if folder.is_dir():
            return folder
    return Path(tempfile.gettempdir())


def _start_editor(executable: Path, document: Path, cwd: Path) -> None:
    """@brief Start one KiCad editor on one document, detached from this program.

    @param executable Editor executable (pcbnew, eeschema).
    @param document File to open, absolute path.
    @param cwd Working folder, outside the project (`neutral_working_dir`).
    @throws OSError When the process cannot be started.
    """
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen(
        [str(executable), str(Path(document).absolute())], cwd=str(cwd),
        close_fds=True, creationflags=creation_flags,
    )


def eeschema_beside(pcbnew_path: str | os.PathLike[str] | None) -> Path | None:
    """@brief The Schematic Editor installed with a given PCB Editor.

    @param pcbnew_path Path of pcbnew; None gives None.
    @return The ``eeschema`` executable in the same folder, when it exists:
        both editors ship in KiCad's ``bin`` folder, so the sibling is the
        one of the same KiCad version.
    """
    if not pcbnew_path:
        return None
    candidate = Path(pcbnew_path).with_name(
        "eeschema.exe" if os.name == "nt" else "eeschema")
    return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class PreviewLaunch:
    """@brief What `launch_preview_editors` opened.

    @note ``launched`` lists (editor executable, document) in launch order;
        the board is always first. ``schematic_skipped`` is None when the
        schematic was opened, otherwise a reason code: one of
        `PREVIEW_SCHEMATIC_SKIP_REASONS` (no schematic written),
        ``eeschema_missing`` or ``eeschema_launch_failed``.
    """

    launched: tuple[tuple[Path, Path], ...]
    schematic_skipped: str | None = None
    schematic_error: str | None = None


def launch_preview_editors(
        preview: PreviewProject, pcbnew_path: str | os.PathLike[str],
        eeschema_path: str | os.PathLike[str] | None = None, *,
        launcher=None) -> PreviewLaunch:
    """@brief Open the preview board in the PCB Editor and its schematic in the Schematic Editor.

    @param preview Result of `create_preview_project`.
    @param pcbnew_path Path of pcbnew.
    @param eeschema_path Path of eeschema; None looks for it beside pcbnew
        (`eeschema_beside`).
    @param launcher Callable ``(executable, document, cwd)`` starting one
        editor; defaults to a detached process. Tests pass a fake. ``cwd`` is
        always outside the project (`neutral_working_dir`).
    @return The editors started and, when the schematic was not opened, why.
    @throws KiCadLibraryError As `launch_preview` for the board
        (``pcbnew_missing``, ``preview_missing``, ``launch_failed``): the
        board is the preview's purpose, the schematic is an addition, so a
        schematic that cannot be opened never raises.
    """
    start = launcher or _start_editor
    executable = Path(pcbnew_path)
    if not executable.is_file():
        raise KiCadLibraryError(
            f"KiCad PCB Editor not found: {executable}",
            code="pcbnew_missing", path=executable,
        )
    if not preview.board_path.is_file():
        raise KiCadLibraryError(
            f"Preview board not found: {preview.board_path}",
            code="preview_missing", path=preview.board_path,
        )
    try:
        start(executable, preview.board_path.absolute(),
              neutral_working_dir(executable))
    except OSError as exc:
        raise KiCadLibraryError(
            f"Unable to start KiCad: {exc}",
            code="launch_failed", error=exc,
        ) from exc
    launched = [(executable, preview.board_path)]
    schematic = preview.schematic_path
    if schematic is None or not schematic.is_file():
        return PreviewLaunch(tuple(launched), preview.schematic_skipped or "symbol_missing")
    editor = Path(eeschema_path) if eeschema_path else eeschema_beside(executable)
    if editor is None or not editor.is_file():
        return PreviewLaunch(tuple(launched), "eeschema_missing")
    try:
        start(editor, schematic.absolute(), neutral_working_dir(editor))
    except OSError as exc:
        return PreviewLaunch(tuple(launched), "eeschema_launch_failed", str(exc))
    launched.append((editor, schematic))
    return PreviewLaunch(tuple(launched))


#: Image file `render_preview_image` writes into the preview folder.
PREVIEW_RENDER_SUFFIX = "-preview-3d.png"

#: Seconds `render_preview_image` waits for ``kicad-cli pcb render``. KiCad
#: 10.0.8 renders the preview board of a relay in about 1 s at ``basic``
#: quality (19 s at ``high``, measured 2026-11-13); the bound only keeps a
#: hung kicad-cli from holding the preview forever.
PREVIEW_RENDER_TIMEOUT_S = 60

#: Size of the rendered image, px.
PREVIEW_RENDER_SIZE = (1600, 1200)

#: View of the render: the board turned like KiCad's isometric example of
#: ``--rotate`` ('-45,0,45'), so the body's top and two sides are visible.
PREVIEW_RENDER_ROTATION = "-45,0,45"


@dataclass(frozen=True)
class PreviewRender:
    """@brief Outcome of `render_preview_image`.

    @note ``image_path`` is the PNG when the render succeeded, otherwise
        None and ``reason`` is one of ``no_step``, ``kicad_cli_missing``,
        ``render_timeout``, ``render_failed``, ``render_no_image`` or
        ``render_start_failed``; ``detail`` carries the last lines kicad-cli
        printed or the OS error, for the log.
    """

    image_path: Path | None
    reason: str | None = None
    detail: str = ""


def preview_render_command(kicad_cli: str | os.PathLike[str], board_path: Path,
                           output_path: Path) -> list[str]:
    """@brief The ``kicad-cli pcb render`` command of the preview image.

    @param kicad_cli Path of kicad-cli.
    @param board_path Preview board.
    @param output_path PNG to write.
    @return The argument list (options of kicad-cli 10's ``pcb render``).
    @note Opaque background, so the image reads the same on a light and a
        dark window; the isometric turn shows the body's height.
        ``basic`` quality: ``high`` (ray tracing) took 19 s for the
        same picture of a relay where ``basic`` took 1 s (kicad-cli 10.0.8).
    """
    width, height = PREVIEW_RENDER_SIZE
    return [
        str(kicad_cli), "pcb", "render",
        "--output", str(output_path),
        "--width", str(width), "--height", str(height),
        "--side", "top",
        "--rotate", PREVIEW_RENDER_ROTATION,
        "--background", "opaque",
        "--quality", "basic",
        "--zoom", "1",
        str(board_path),
    ]


def render_preview_image(preview: PreviewProject, kicad_cli: str | os.PathLike[str] | None,
                         *, has_step: bool, runner=None,
                         timeout: float = PREVIEW_RENDER_TIMEOUT_S) -> PreviewRender:
    """@brief Render the preview board in 3D to a PNG with ``kicad-cli pcb render``.

    @param preview Result of `create_preview_project`.
    @param kicad_cli Path of kicad-cli; None when KiCad's CLI was not found.
    @param has_step Whether the preview carries a STEP model
        (`ComponentArtifacts.has_step`); without one there is nothing to see
        in 3D but the bare board, so nothing is rendered.
    @param runner Callable with the signature of ``subprocess.run``; tests
        pass a fake.
    @param timeout Seconds to wait for kicad-cli.
    @return The image, or None with the reason. Never raises for a missing,
        old, failing or hung kicad-cli: the preview opens without image.
    @note Read-only for KiCad: kicad-cli reads the temporary board and
        writes only the PNG, first to a scratch name in the preview folder,
        then moved onto ``<name>-preview-3d.png``; an image of an earlier
        preview is removed before rendering, so a failure never leaves a
        stale picture of another footprint.
    @note Slow (seconds): call it off the GUI thread.
    """
    image = preview.board_path.with_name(
        preview.board_path.stem.removesuffix("-preview") + PREVIEW_RENDER_SUFFIX)
    try:
        if image.is_file():
            _remove_clearing_readonly(image)
    except OSError as exc:
        return PreviewRender(None, "render_failed", str(exc))
    if not has_step:
        return PreviewRender(None, "no_step")
    if not kicad_cli or not Path(kicad_cli).is_file():
        return PreviewRender(None, "kicad_cli_missing")
    scratch = image.with_name(f".{image.stem}.{uuid.uuid4().hex[:8]}.png")
    run = runner or subprocess.run
    try:
        try:
            proc = run(
                preview_render_command(kicad_cli, preview.board_path.absolute(),
                                       scratch.absolute()),
                capture_output=True, text=True, errors="replace", timeout=timeout,
                # Outside the project, see neutral_working_dir.
                cwd=str(neutral_working_dir(kicad_cli)),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return PreviewRender(None, "render_timeout", f"{timeout:g} s")
        except (OSError, subprocess.SubprocessError) as exc:
            return PreviewRender(None, "render_start_failed", str(exc))
        output = "\n".join(
            ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()[-5:])
        if proc.returncode != 0:
            return PreviewRender(None, "render_failed", output)
        if not scratch.is_file() or scratch.stat().st_size == 0:
            return PreviewRender(None, "render_no_image", output)
        try:
            replace_file(scratch, image)
        except OSError as exc:
            return PreviewRender(None, "render_failed", str(exc))
        return PreviewRender(image)
    finally:
        _unlink_quietly(scratch)


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
    """@brief Serialize one native KiCad footprint-library table entry.

    @param library_name Nickname of the row.
    @param uri Library location written in the row.
    @return The ``(lib ...)`` row, every string quoted by the shared quoter.
    """
    return (
        f"(lib (name {quote(library_name)})(type {quote('KiCad')})"
        f"(uri {quote(uri)})(options {quote('')})"
        # «KiCadGen», the program's name (was «KiCadGen-Tool», user 2026-11-12).
        # Only new rows: an existing row is kept verbatim on reinstallation.
        f"(descr {quote('Generated by KiCadGen')}))"
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


def _same_place(first: str | None, second: str,
                project_dir: Path | None = None) -> bool:
    """@brief Whether two table URIs name the same folder on this computer.

    @param first URI of an existing table row (None when the row has none).
    @param second URI the install would register.
    @param project_dir Folder that ``${KIPRJMOD}`` stands for; None leaves
        the variable unexpanded (the URI is then not absolute).
    @return True when `_same_uri` says so, or when both URIs are absolute
        once ``${KIPRJMOD}`` is expanded and the file system says they are
        one folder (``os.path.samefile``, else equal ``Path.resolve``).
    @note The destination is made absolute with ``os.path.abspath`` and not
        resolved (see `plan_component_install`), so re-installing through a
        junction, a SUBST drive or a mapped drive into the folder an earlier
        install registered under its other spelling was a false
        ``library_entry_conflict`` and a false shadowing (review 2026-11-03,
        D1). Only the COMPARISON follows links: the row is kept verbatim and
        the user's spelling is never rewritten.
    @note A URI still holding another variable is never resolved: where it
        points depends on KiCad's environment, so it stays a conflict for the
        user to decide, as `_same_uri` documents.
    """
    if _same_uri(first, second):
        return True
    if first is None:
        return False
    one = _expand_project_uri(first.strip(), project_dir)
    other = _expand_project_uri(second.strip(), project_dir)
    if any(token in text for text in (one, other) for token in ("${", "$(")):
        return False
    if not (os.path.isabs(one) and os.path.isabs(other)):
        return False
    try:
        if os.path.samefile(one, other):
            return True
    except (OSError, ValueError):
        # A folder not created yet (or an unreachable drive) has no identity:
        # the resolved spellings are compared instead.
        pass
    try:
        return (os.path.normcase(str(Path(one).resolve()))
                == os.path.normcase(str(Path(other).resolve())))
    except (OSError, RuntimeError, ValueError):
        return False


def _replace_entry_child(block: str, field: str, value: str) -> str:
    """Replace (or append) one ``(field "value")`` child of a ``lib`` block."""
    child = f"({field} {quote(value)})"
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
        unreadable table, ``file_not_utf8`` for a table not saved as UTF-8,
        ``duplicate_library_entry`` for an ambiguous one.
    """
    table = Path(table_path)
    if not table.is_file():
        return None
    text = _read_utf8(table)
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


def _table_backup_history(table: Path) -> list[Path]:
    """@brief Backup files of `table`, newest first (``.kicadgen-backup``, ``.1``...).

    @param table Footprint library table.
    @return `TABLE_BACKUP_RETENTION` paths; they may not exist.
    """
    backup = table.with_name(table.name + ".kicadgen-backup")
    return [backup] + [
        backup.with_name(f"{backup.name}.{index}")
        for index in range(1, TABLE_BACKUP_RETENTION)
    ]


def _stage_table_backup(table: Path, original: bytes) -> Path:
    """@brief Write the pre-change table bytes into a scratch file, durably.

    @param table Table about to be rewritten.
    @param original Its exact bytes before the change.
    @return The scratch file, next to ``<table>.kicadgen-backup``.
    @throws OSError When the scratch file cannot be written (then removed).
    @note Staged BEFORE the table write and published only after it (see
        `_publish_table_backup`): written from bytes, not copied, so it never
        inherits the table's read-only attribute.
    """
    fresh = _temporary_sibling(_table_backup_history(table)[0])
    try:
        _write_durably(fresh, original)
    except BaseException:
        _unlink_quietly(fresh)
        raise
    return fresh


def _publish_table_backup(table: Path, fresh: Path) -> None:
    """@brief Shift the backup history of `table` and move `fresh` into first place.

    @param table Table that has just been rewritten successfully.
    @param fresh Scratch file holding its previous bytes (`_stage_table_backup`).
    @throws OSError When a backup file cannot be moved; every move already
        made is undone, so the history is exactly as before (oldest included).
    @throws KiCadLibraryError ``table_backup_rotation_incomplete`` (context
        ``path``, ``failures``) when that undo itself fails: the failures say
        which copy is where, nothing is silently lost.
    @note ``<table>.kicadgen-backup`` is always the state immediately before
        the latest KiCadGen write; older copies shift to ``.1`` and so on, up
        to `TABLE_BACKUP_RETENTION` files, the oldest being discarded. The
        files are ours: a read-only attribute that older versions copied onto
        them is cleared first, otherwise Windows refused every later rotation
        and with it every global installation.
    @note Called only after the table write succeeded: rotating before it
        shifted, and discarded, the history even when the write then failed.
    @note The history is COMPACTED, not shifted slot by slot (review
        2026-10-27, A2): the copies that exist, newest first, take the slots
        after `fresh` in order, whatever gaps the history had. A missing
        ``.kicadgen-backup`` with ``.1`` and ``.2`` present used to leave
        ``.1`` empty and discard the content of ``.2``, keeping two states
        where three fit. Only the exact names of `_table_backup_history` are
        touched: a ``.3`` or a ``.2.notes.txt`` next to them is foreign and
        never moved.
    """
    history = _table_backup_history(table)
    # Existing copies, newest first, gaps skipped: these are the states kept.
    present = [path for path in history if path.exists()]
    for path in present:
        _clear_readonly(path)
    moved: list[tuple[Path, Path]] = []
    # The copy that falls off the retained history is moved aside, never
    # deleted, so a rotation failing halfway can put it back: an undo that
    # restores only the shifted copies lost it (review 2026-10-15, L1).
    aside: Path | None = None
    try:
        # Oldest first: each copy moves to a slot no earlier than the one the
        # next-older copy just left, so no copy is overwritten before it moved.
        for position in range(len(present) - 1, -1, -1):
            source = present[position]
            if position + 1 < len(history):
                destination = history[position + 1]
            else:
                destination = _temporary_sibling(source)
                aside = destination
            if source == destination:
                continue
            try:
                replace_file(source, destination)
            except BaseException:
                if destination == aside:
                    # Still empty: it must never be "restored" over the copy.
                    _unlink_quietly(aside)
                    aside = None
                raise
            moved.append((source, destination))
        replace_file(fresh, history[0])
    except BaseException as exc:
        failures = _undo_backup_moves(moved)
        if aside is not None and aside.exists() and not failures:
            # Put back by the undo above when it was moved; an aside that
            # still exists after a complete undo is the empty scratch.
            _unlink_quietly(aside)
        if failures:
            raise KiCadLibraryError(
                f"The backups of {table} could not be put back in order after "
                f"a failed rotation ({exc}):\n" + "\n".join(failures),
                code="table_backup_rotation_incomplete", path=table,
                failures="\n".join(failures), error=exc,
            ) from exc
        raise
    if aside is not None:
        _unlink_quietly(aside)


def _undo_backup_moves(moved: list[tuple[Path, Path]]) -> list[str]:
    """@brief Reverse the moves of a failed backup rotation, newest move first.

    @param moved (source, destination) of every move that succeeded, in
        order; the copy moved aside (to a scratch name) is one of them.
    @return One message per copy that could not be put back; empty when the
        history is exactly as before.
    @note At the first failed undo the remaining ones stop: each of them would
        replace a file that still holds a copy not yet moved back. The
        messages name every copy left out of place, the scratch name of the
        copy moved aside included, so none is lost silently.
    """
    failures: list[str] = []
    for position, (source, destination) in enumerate(reversed(moved)):
        try:
            replace_file(destination, source)
        except OSError as error:
            failures.append(f"{destination} -> {source}: {error}")
            for earlier_source, earlier_destination in list(reversed(moved))[position + 1:]:
                failures.append(
                    f"{earlier_destination} -> {earlier_source}: not attempted")
            break
    return failures


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
        nickname to a different URI/type, and enabling a disabled one
        (explicit user confirmation).
    @param create_missing Create a new table when the file does not exist;
        pass False for KiCad's global table, which KiCad must create itself.
    @return True when the table contents changed.
    @throws KiCadLibraryError ``global_table_missing`` when the table is
        absent and ``create_missing`` is False, ``file_not_utf8`` when the
        table is not UTF-8 text (nothing is written); see
        `_updated_fp_lib_table_text` for the conflict codes.
    @throws OSError When the table or its backup cannot be written; the table
        is then left (or put back) as it was, and the backup history unchanged.
        If even putting it back fails (a hard-linked table truncated by a full
        disk), the pre-change copy is kept as ``<table>.kicadgen-backup``
        instead of being deleted (review 2026-10-29, L1).
    @note Every changing write refreshes ``<table>.kicadgen-backup`` (bounded
        history, see `TABLE_BACKUP_RETENTION`) and replaces the table
        atomically and durably (fsync before the rename); the temporary file
        never survives a failed replace.
    @note Order: the pre-change copy is staged, the table is written, and only
        then the history rotates. A failed write leaves the history as it was;
        a failed rotation puts the previous table back, so no change is ever
        left without its backup.
    @note A table that is a symbolic link is updated THROUGH the link: the
        real file is rewritten and the backup history sits next to it, so the
        link survives and both names show the new table. A hard-linked table
        is overwritten in place after its backup is staged (`_publish_scratch`).
        A dangling link is refused (``target_link_unresolved``).
    """
    # Resolved first so the scratch file, the backups and the final rename
    # all work on the real file, never on the link (review 2026-10-27, A1).
    table = _write_target(Path(table_path))
    original_bytes: bytes | None = None
    if table.is_file():
        original_bytes = table.read_bytes()
        # Line endings kept as stored: only the added row may differ.
        original = _decode_utf8(original_bytes, table, keep_newlines=True)
    elif create_missing:
        original = _EMPTY_TABLE
    else:
        raise _global_table_missing(table)
    updated = _updated_fp_lib_table_text(
        original, library_name, uri, table, replace_entry=replace_entry)
    if updated == original:
        return False
    table.parent.mkdir(parents=True, exist_ok=True)
    fresh = (_stage_table_backup(table, original_bytes)
             if original_bytes is not None else None)
    # A BOM the file started with is written back: the rewrite keeps every
    # byte except the added or repointed row.
    bom = (UTF8_BOM if original_bytes is not None
           and original_bytes.startswith(UTF8_BOM) else b"")
    updated_bytes = bom + updated.encode("utf-8")
    try:
        try:
            _atomic_write_bytes(table, updated_bytes)
        except BaseException:
            # A hard-linked table is overwritten in place, which is not
            # crash-atomic: a failure midway leaves it truncated under every
            # name. Put the staged copy back before anything removes it
            # (review 2026-10-29, L1).
            if fresh is not None:
                _put_table_back(table, fresh, original_bytes)
            raise
        if fresh is not None:
            try:
                _publish_table_backup(table, fresh)
            except BaseException:
                # The new table has no backup of its predecessor: undo it. A
                # failed undo must not mask the rotation error; the install
                # rollback compares the table with its own copy and retries.
                _put_table_back(table, fresh, original_bytes)
                raise
    finally:
        if fresh is not None and fresh.exists():
            _settle_staged_backup(table, fresh, (updated_bytes, original_bytes))
    return True


def _file_holds(path: Path, payload: bytes) -> bool:
    """Whether `path` can be read and holds exactly `payload` (unreadable -> False)."""
    try:
        return path.read_bytes() == payload
    except OSError:
        return False


def _put_table_back(table: Path, fresh: Path, original: bytes) -> None:
    """@brief Best effort: make `table` hold its pre-change bytes again after a failed update.

    @param table Real table file (links resolved).
    @param fresh Staged pre-change copy (`_stage_table_backup`); never consumed here.
    @param original The same bytes, for the comparison and the atomic rewrite.
    @note A table that already holds `original` is left alone (a failed
        atomic replace never touched it). A hard-linked table is rewritten in
        place from `fresh`, so both names get the content back and no extra
        scratch file is needed on a disk that may be full. A failure is
        swallowed: the caller re-raises the original error and
        `_settle_staged_backup` keeps `fresh` because the table matches
        neither state.
    """
    if _file_holds(table, original):
        return
    try:
        if _hard_linked(table):
            _write_in_place(fresh, table)
        else:
            _atomic_write_bytes(table, original)
    except OSError:
        pass


def _settle_staged_backup(table: Path, fresh: Path, states: tuple[bytes, ...]) -> None:
    """@brief Dispose of the staged pre-change copy left after `update_fp_lib_table`.

    @param table Real table file.
    @param fresh Staged pre-change copy that still exists.
    @param states Contents for which `fresh` is no longer needed: the new
        table and the original one.
    @note SAFETY BOUNDARY (review 2026-10-29, L1): `fresh` is removed ONLY
        when the table verifiably holds one of `states`. Otherwise it is the
        only complete copy of the user's table: it is published as
        ``<table>.kicadgen-backup`` (the history shifts as after any
        change), and when even that fails it stays under its scratch name.
        Errors are swallowed: this runs in a ``finally`` and must not hide
        the error being raised.
    """
    if any(_file_holds(table, state) for state in states):
        _unlink_quietly(fresh)
        return
    try:
        _publish_table_backup(table, fresh)
    except (OSError, KiCadLibraryError):
        pass


def _updated_fp_lib_table_text(
        original: str, library_name: str, uri: str,
        table_path: Path | None = None, *, replace_entry: bool = False) -> str:
    """@brief Render a table update without touching the filesystem.

    @details A row already registered under ``library_name`` with the same
    URI and the ``KiCad`` type is kept verbatim (options, description and
    ``hidden`` flag included). A row pointing elsewhere is a conflict: it is
    repointed only with ``replace_entry``, and even then only its ``type``
    and ``uri`` children change, plus its ``options`` when the type changes
    (see `_repoint_entry`). A DISABLED row is a conflict whatever it
    points to (review 2026-10-29, L2): with ``replace_entry`` it is repointed
    when needed and its ``(disabled)`` child is removed, every other child
    kept.
    @throws KiCadLibraryError ``invalid_table`` for a non-table file,
        ``duplicate_library_entry`` when the nickname already appears more than
        once, ``library_entry_disabled`` (same context as the next code) when
        the existing row is disabled and ``replace_entry`` is False,
        ``library_entry_conflict`` (context ``library``, ``old_uri``,
        ``new_uri``, ``old_type``, ``type_changes``, ``path``) when the
        existing row points to a different location and ``replace_entry`` is
        False. ``type_changes`` is True when the row's type is not ``KiCad``:
        confirming then also clears its options.
    @note Why a disabled row is not kept: KiCad ignores it, so the installed
        footprint could not be placed («library not enabled») while the
        install reported success. Re-enabling a row the user switched off is
        a change of their table, so it needs the same explicit confirmation
        as a repoint. A ``hidden`` row is still loaded by KiCad (only left
        out of the chooser) and stays as it is.
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
        # Same folder under another spelling (junction, SUBST or mapped
        # drive) is the same place; the row is kept as the user wrote it.
        same_place = same_type and _same_place(entry.uri, uri)
        if entry.disabled:
            if not replace_entry:
                raise KiCadLibraryError(
                    f"The library table {location} maps '{library_name}' to "
                    f"{entry.uri!r} (type {entry.library_type!r}) but the row is "
                    "disabled, so KiCad would not load the installed footprint; "
                    f"installing would enable it pointing to {uri!r}.",
                    code="library_entry_disabled", library=library_name,
                    old_uri=entry.uri or "", new_uri=uri,
                    old_type=entry.library_type or "",
                    type_changes=not same_type, path=location,
                )
            block = original[start:end]
            if not same_place:
                block = _repoint_entry(block, uri, same_type)
            block = _remove_direct_children(block, "disabled")
            return original[:start] + block + original[end:]
        if same_place:
            return original
        if not replace_entry:
            raise KiCadLibraryError(
                f"The library table {location} already maps '{library_name}' to "
                f"{entry.uri!r} (type {entry.library_type!r}); installing would "
                f"repoint it to {uri!r}.",
                code="library_entry_conflict", library=library_name,
                old_uri=entry.uri or "", new_uri=uri,
                old_type=entry.library_type or "",
                type_changes=not same_type, path=location,
            )
        block = _repoint_entry(original[start:end], uri, same_type)
        return original[:start] + block + original[end:]
    closing = original.rstrip().rfind(")")
    if closing < 0:
        raise KiCadLibraryError(
            f"Invalid footprint library table: {location}",
            code="invalid_table", path=location,
        )
    replacement = _library_entry(library_name, uri)
    # Same line ending as the file (CRLF written on Windows by an editor or
    # an older tool) and the same text after the closing parenthesis (final
    # newline or none), so the rewrite changes only the added row.
    newline = "\r\n" if "\r\n" in original else "\n"
    return (original[:closing].rstrip() + newline + "  " + replacement
            + newline + original[closing:])


def _repoint_entry(block: str, uri: str, same_type: bool) -> str:
    """@brief Point one ``lib`` row at `uri` as a native ``KiCad`` library.

    @param block Text of the row.
    @param uri New location.
    @param same_type Whether the row already has the ``KiCad`` type.
    @return The row with ``type`` and ``uri`` set; when the type changes, an
        existing ``options`` child is emptied, every other child kept.
    @note Options are plugin-specific (a GitHub or legacy plugin reads keys a
        KiCad ``.pretty`` folder does not know): kept across a type change
        they described a library that no longer exists (review 2026-11-03).
        Under the same type they still apply and are kept.
    """
    block = _replace_entry_child(block, "type", "KiCad")
    block = _replace_entry_child(block, "uri", uri)
    if not same_type and _direct_child_ranges(block, "options"):
        block = _replace_entry_child(block, "options", "")
    return block


def _fsync_file(path: Path) -> None:
    """@brief Flush an existing file of ours to disk.

    @param path Scratch or rollback copy written by this module (writable).
    @throws OSError When the file cannot be opened for writing or flushed.
    @note Opened for update because Windows only commits handles opened with
        write access; the content is not modified.
    """
    with open(path, "r+b") as stream:
        os.fsync(stream.fileno())


def _target_is_folder(target: Path) -> KiCadLibraryError:
    """@brief Error for a folder sitting where an installed file must go.

    @param target The user's path (footprint, model or table) that is a folder.
    @return KiCadLibraryError ``install_target_is_folder`` (context ``path``).
    """
    return KiCadLibraryError(
        f"A folder is in the place of the file {target}; the installation "
        "cannot replace a folder. Rename or move it, then retry.",
        code="install_target_is_folder", path=target,
    )


def _install_write_refused(error: OSError, root: Path,
                           aliases: dict[Path, Path] | None = None) -> KiCadLibraryError:
    """@brief Error for an install the operating system refused, after a full rollback.

    @param error The PermissionError (or IsADirectoryError) raised by the write.
    @param root Destination folder, named when the error names no file.
    @param aliases Internal paths (scratch files, rollback copies, the
        rollback folder) mapped to the user's file they stand for.
    @return KiCadLibraryError ``install_target_is_folder`` when the real path
        is a folder, otherwise ``install_write_refused``; both name the user's
        file, never a scratch file or the rollback folder.
    """
    refused = _refused_path(error, root)
    aliases = aliases or {}
    if refused in aliases:
        refused = aliases[refused]
    else:
        for internal, real in aliases.items():
            if internal in refused.parents:
                # Something inside the rollback folder: name the destination.
                refused = real
                break
    if refused.is_dir() and refused != root:
        return _target_is_folder(refused)
    return KiCadLibraryError(
        f"Unable to write {refused}: it is read-only or held open by another "
        f"program ({error}). The installation was undone.",
        code="install_write_refused", path=refused, error=error,
    )


def _restore_file(backup: Path, target: Path) -> None:
    """Put a rollback copy back in place atomically and durably.

    A rename is tried first (same volume, cannot run out of space); when it is
    impossible the copy goes through a sibling temporary file. Either way the
    target is replaced in one step and the backup is never deleted first.
    The backup is flushed to disk before the rename (`_fsync_file`): it was
    written by a plain copy, and a power loss right after the rename could
    otherwise leave the restored file empty.

    A symbolic-link target is restored through the link (`_write_target`) and
    a hard-linked one in place (`_write_in_place`), so every name of the file
    shows the restored content; the backup is the pre-change copy itself and
    is not deleted before the write succeeded (review 2026-10-27, A1).
    """
    target = _write_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _fsync_file(backup)
    if _hard_linked(target):
        _write_in_place(backup, target)
        return
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
    """@brief Undo a failed install; return one message per step that failed.

    @param installed Targets this install really changed, in order: replaced
        by a staged file, or (WRL) deleted. Targets never reached are absent.
    @param backups Rollback copy of every target that existed before.
    @param table Library table of the install.
    @param table_backup Rollback copy of the table, None when it did not exist.
    @param table_existed Whether the table existed before the install.
    @return Messages of the steps that failed; empty for a complete rollback.
    @note Only what was really changed is put back. Restoring every backup
        and the table regardless reported a false ``rollback_incomplete``
        whenever an untouched file could not be replaced (the very read-only
        file that had made the install fail) and left the rollback folder,
        scratch files and empty folders behind. The table is restored only
        when its content differs from the copy taken before the install.
    @note Every step is attempted independently, so a locked file does not
        prevent the other files and the table from being restored.
    @note A restore can fail with KiCadLibraryError as well as OSError:
        `_restore_file` resolves the target through `_write_target`, which
        refuses a link that became dangling (``target_link_unresolved``).
        Both are recorded as a failed step; catching only OSError let that
        error escape, skipping the remaining restores and the
        ``rollback_incomplete`` report (review 2026-11-03).
    """
    failures: list[str] = []
    for target in installed:
        if target in backups:
            try:
                _restore_file(backups[target], target)
            except (OSError, KiCadLibraryError) as error:
                failures.append(f"{target} <- {backups[target]}: {error}")
            continue
        try:
            if target.exists():
                target.unlink()
        except OSError as error:
            failures.append(f"{target}: {error}")
    try:
        if table_backup is not None:
            if not _same_content(table, table_backup):
                _restore_file(table_backup, table)
        elif not table_existed and table.exists():
            table.unlink()
    except (OSError, KiCadLibraryError) as error:
        failures.append(f"{table}: {error}")
    return failures


def _same_content(first: Path, second: Path) -> bool:
    """@brief Tell whether two files exist and hold the same bytes.

    @param first File compared (missing or unreadable -> False).
    @param second Reference copy.
    @return True only when both files can be read and are byte-identical.
    """
    try:
        return first.read_bytes() == second.read_bytes()
    except OSError:
        return False


#: Name prefix of the rollback folder an install creates in its destination;
#: followed by 32 hex characters.
ROLLBACK_DIR_PREFIX = ".kicadgen-rollback-"
#: File written into a rollback folder that could not be removed although it
#: is no longer needed (successful install, complete rollback). Only folders
#: holding it are ever swept: a folder kept by ``rollback_incomplete`` holds
#: the only copies of the user's files and has no marker.
ROLLBACK_DISPOSABLE_MARKER = "kicadgen-rollback-disposable"
_ROLLBACK_DIR_NAME = re.compile(re.escape(ROLLBACK_DIR_PREFIX) + r"[0-9a-f]{32}\Z")


def _dispose_rollback_dir(rollback_dir: Path) -> Path | None:
    """@brief Remove a rollback folder that is no longer needed, or mark it.

    @param rollback_dir Rollback folder of an install that succeeded or was
        completely undone.
    @return None when the folder is gone; the folder itself when it could not
        be removed (a copy held open by a scanner), after writing
        `ROLLBACK_DISPOSABLE_MARKER` into it so a later install sweeps it.
    @note The result used to be ignored: the folder stayed in the library
        root forever and nothing said so (review 2026-11-03).
    """
    if _remove_tree_quietly(rollback_dir):
        return None
    try:
        (rollback_dir / ROLLBACK_DISPOSABLE_MARKER).write_bytes(b"")
    except OSError:
        # Without the marker the folder is never swept: kept, never lost.
        pass
    return rollback_dir


def rollback_left_of(error: BaseException) -> Path | None:
    """@brief The rollback folder a FAILED install could not remove, if any.

    @param error Exception raised by `install_component_library`.
    @return The folder in its ``rollback_left`` context (a
        `KiCadLibraryError` after a COMPLETE rollback whose folder stayed,
        marked disposable); None for any other error.
    @note No ``kicad.error.*`` text names this folder (review 2026-11-03,
        D8): the GUI appends ``kicad.result_rollback_left`` (placeholder
        ``path``), the text of a successful install, to the error message.
        ``rollback_incomplete`` keeps its folder in ``rollback_dir`` instead,
        named by its own message.
    """
    context = getattr(error, "context", None)
    left = context.get("rollback_left") if isinstance(context, dict) else None
    return Path(left) if left else None


def _sweep_stale_rollback_dirs(root: Path, now: float | None = None) -> list[Path]:
    """@brief Remove disposable rollback folders older than `STALE_SCRATCH_AGE_S`.

    @param root Library root that receives ``<library>.pretty``.
    @param now Current time (tests); None reads the clock.
    @return The folders removed.
    @note SAFETY BOUNDARY: only a folder named like ours AND holding
        `ROLLBACK_DISPOSABLE_MARKER` whose time is older than the limit is
        removed. A folder without the marker may be the one kept by
        ``rollback_incomplete`` (the only copies of the user's files) or one
        left by an older version, which cannot be told apart: it is kept.
        Failures are ignored, as for `_remove_stale_scratch`.
    """
    limit = (time.time() if now is None else now) - STALE_SCRATCH_AGE_S
    removed: list[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    for entry in entries:
        if not _ROLLBACK_DIR_NAME.match(entry.name):
            continue
        try:
            # A junction passes is_dir(follow_symlinks=False): any link named
            # like ours is skipped, it points to a folder that is not ours.
            if _is_reparse_point(entry) or not entry.is_dir(follow_symlinks=False):
                continue
            marker = Path(entry.path) / ROLLBACK_DISPOSABLE_MARKER
            if marker.stat().st_mtime > limit:
                continue
        except OSError:
            continue
        if _remove_tree_quietly(Path(entry.path)):
            removed.append(Path(entry.path))
    return removed


def _is_directory_link(path: Path) -> bool:
    """Whether the folder `path` is a symbolic link or a Windows junction (rmdir would remove the link)."""
    try:
        if os.path.islink(path):
            return True
        is_junction = getattr(os.path, "isjunction", None)     # Python 3.12+
        if is_junction is not None and is_junction(path):
            return True
        # Older Pythons: a junction resolves elsewhere than its own path. A
        # false positive (a linked parent folder) only keeps the folder.
        return path.resolve() != Path(os.path.abspath(path))
    except OSError:
        return True


def _is_link(path: Path) -> bool:
    """Whether `path` is a symbolic link or a hard-linked file (for `InstallPlan.linked`)."""
    return _is_symlink(path) or _hard_linked(path)


def _expand_project_uri(uri: str, project_dir: Path | None) -> str:
    """Replace ``${KIPRJMOD}`` by `project_dir` (when known) for a location comparison."""
    if project_dir is None:
        return uri
    return re.sub(r"\$\{KIPRJMOD\}|\$\(KIPRJMOD\)",
                  lambda _match: project_dir.as_posix(), uri)


def find_shadowing_entry(
        other_table: str | os.PathLike[str], library_name: str,
        library_location: str | os.PathLike[str], *, other_scope: str,
        project_dir: str | os.PathLike[str] | None = None) -> LibraryShadowing | None:
    """@brief The row of the other scope's table that clashes with an install.

    @param other_table Table of the OTHER scope: the global fp-lib-table for
        a project install, the project's fp-lib-table for a global install.
    @param library_name Nickname being installed.
    @param library_location Absolute folder of the ``.pretty`` the install
        registers (``${KIPRJMOD}`` already expanded).
    @param other_scope ``"global"`` or ``"project"``, the scope of `other_table`.
    @param project_dir Project folder that ``${KIPRJMOD}`` stands for when the
        other row uses it; None leaves the variable unexpanded.
    @return A `LibraryShadowing` when the table has an ENABLED row under the
        same nickname pointing somewhere else (or of another type); None when
        the table or the row is missing, the row is disabled (KiCad does not
        load it) or it points to the same folder.
    @throws KiCadLibraryError ``invalid_table``/``malformed_file``/
        ``file_not_utf8``/``duplicate_library_entry`` from `find_library_entry`.
    @throws OSError When the table cannot be read.
    @note Variables other than ``${KIPRJMOD}`` are not expanded: a row reached
        through another variable is reported, and the user decides.
    """
    entry = find_library_entry(other_table, library_name)
    if entry is None or entry.disabled:
        return None
    project = Path(project_dir) if project_dir is not None else None
    same_type = (entry.library_type or "").casefold() == "kicad"
    if same_type and _same_place(entry.uri or "", Path(library_location).as_posix(),
                                 project):
        return None
    return LibraryShadowing(
        table_path=Path(other_table), scope=other_scope, library=library_name,
        uri=entry.uri or "", library_type=entry.library_type or "")


#: Documents whose KiCad lock file (`kicad_lock_file`) means the project is open.
_PROJECT_DOCUMENT_SUFFIXES = (".kicad_pro", ".kicad_pcb", ".kicad_sch")


def project_lock_files(project_dir: str | os.PathLike[str]) -> tuple[Path, ...]:
    """@brief KiCad lock files present in one project folder.

    @param project_dir Folder of a KiCad project.
    @return The ``~<name>.kicad_pro.lck``, ``~<name>.kicad_pcb.lck`` and
        ``~<name>.kicad_sch.lck`` files found there (`kicad_lock_file`),
        sorted; empty when there are none or the folder cannot be listed.
    @note A lock left by a crashed KiCad looks the same as a live one; the
        result is a warning for the user, never a refusal.
    """
    try:
        entries = list(os.scandir(project_dir))
    except OSError:
        return ()
    locks = []
    for entry in entries:
        name = entry.name
        if (name.startswith("~") and name.endswith(".lck")
                and name[1:-4].endswith(_PROJECT_DOCUMENT_SUFFIXES)):
            locks.append(Path(entry.path))
    return tuple(sorted(locks))


def plan_component_install(
        artifacts: ComponentArtifacts, destination_root: str | os.PathLike[str],
        *, library_name: str = "KiCadGen", project_library: bool = True,
        table_path: str | os.PathLike[str] | None = None,
        other_table_path: str | os.PathLike[str] | None = None,
        kicad_version: str | None = None,
        detected_kicad_version: str | None = None) -> InstallPlan:
    """@brief Pre-flight of `install_component_library`: what it would touch.

    @param artifacts Validated generated artifacts of one component.
    @param destination_root Folder receiving ``<library>.pretty`` and
        ``<library>.3dshapes``.
    @param library_name Library nickname registered in the table.
    @param project_library Portable ``${KIPRJMOD}`` library when True.
    @param table_path Table to update; defaults to the project table.
    @param other_table_path Library table of the OTHER scope, only read, to
        find a row that this install would shadow or be shadowed by
        (`InstallPlan.shadowing`): the global fp-lib-table for a project
        install, the project's fp-lib-table for a global install. None
        skips the check.
    @param kicad_version KiCad settings version that receives the install
        (`GlobalTableLocation.version`); None when unknown. One older than
        `MINIMUM_KICAD_VERSION` sets `InstallPlan.kicad_too_old`.
    @param detected_kicad_version Version of the KiCad found on this
        computer (`system_dependencies.check_kicad_version(...).version`),
        for a project install whose table belongs to no KiCad version; None
        when unknown. One older than the minimum sets
        `InstallPlan.detected_kicad_too_old`, a warning only: the project
        may be opened by a newer KiCad elsewhere.
    @return The targets, the files that would be replaced or removed, the
        linked ones, the table, the clashing row of the other scope, the
        lock files of an open project and the version verdict (see
        `InstallPlan`). Nothing is written.
    @throws KiCadLibraryError ``invalid_library_name``,
        ``install_target_is_folder`` (a folder where a file must go).
    @note The install itself calls this, so the confirmation the GUI asks on
        the plan and the install's own checks cannot disagree: a dangling
        link, a hard link or a WRL to remove is the same here and there.
    @note The destination folder is made absolute with ``os.path.abspath``,
        not ``Path.resolve``: resolving turned a mapped network drive or a
        SUBST drive into its UNC or underlying path, which was then written
        into the global table and the model references although the user
        never chose it (review 2026-11-03). Links are still followed where
        it matters, for each written file (`_write_target`).
    """
    if not _SAFE_LIBRARY_NAME.fullmatch(library_name):
        raise KiCadLibraryError(
            "Library name may contain only letters, numbers, '.', '_', '+' and '-'.",
            code="invalid_library_name",
        )
    root = Path(os.path.abspath(Path(destination_root).expanduser()))
    pretty_dir = root / f"{library_name}.pretty"
    shapes_dir = root / f"{library_name}.3dshapes"
    target_footprint = pretty_dir / f"{artifacts.name}.kicad_mod"
    target_step = shapes_dir / f"{artifacts.name}.step"
    target_wrl_path = shapes_dir / f"{artifacts.name}.wrl"
    targets = (target_footprint, target_step, target_wrl_path)
    for target in targets:
        if target.is_dir() and not _is_symlink(target):
            # Overwriting cannot help: a file never replaces a folder. Saying
            # "read-only or held open" sent the user looking for the wrong cause.
            raise _target_is_folder(target)
    # A dangling symbolic link fails ``exists()`` but is still something in
    # the place of the file: it counts as existing so the user is asked
    # before it is touched (review 2026-10-27, A1).
    existing = [path for path in targets if path.exists() or _is_symlink(path)]
    with_model = artifacts.has_step
    # The WRL is removed, not replaced, when the source has none (A3); the
    # STEP too when no model is shipped (USER DECISION 2026-11-12): the
    # installed footprint will not reference it, and a model of an earlier
    # install would not match the new pads.
    dropped = set() if with_model else {target_step}
    if not with_model or artifacts.wrl_path is None:
        dropped.add(target_wrl_path)
    removed = tuple(path for path in existing if path in dropped)
    replaced = tuple(path for path in existing if path not in removed)
    destination_table = Path(table_path) if table_path else root / "fp-lib-table"
    linked = tuple(path for path in (*existing, destination_table) if _is_link(path))
    library_uri = (f"${{KIPRJMOD}}/{library_name}.pretty"
                   if project_library else pretty_dir.as_posix())
    shadowing: LibraryShadowing | None = None
    shadowing_error: str | None = None
    if other_table_path is not None and not _destination_row_already_there(
            destination_table, library_name, library_uri,
            root if project_library else None):
        # ${KIPRJMOD} names the project: the destination for a project
        # install, the folder of the project table for a global one.
        project_dir = root if project_library else Path(other_table_path).parent
        try:
            shadowing = find_shadowing_entry(
                other_table_path, library_name, pretty_dir,
                other_scope="global" if project_library else "project",
                project_dir=project_dir)
        except (KiCadLibraryError, OSError) as exc:
            # The other scope's table is only read for a warning: an
            # unreadable one must not block an install that does not touch it.
            shadowing_error = str(exc)
    too_old = (kicad_version is not None
               and kicad_version_supported(kicad_version) is False)
    return InstallPlan(
        targets=targets, replaced=replaced, removed=removed, linked=linked,
        table_path=destination_table, table_exists=destination_table.is_file(),
        library_uri=library_uri, shadowing=shadowing,
        shadowing_error=shadowing_error,
        open_locks=project_lock_files(root) if project_library else (),
        kicad_version=kicad_version, kicad_too_old=too_old,
        detected_kicad_version=detected_kicad_version,
        detected_kicad_too_old=(detected_kicad_version is not None and
                                kicad_version_supported(detected_kicad_version) is False),
        with_model=with_model,
    )


def _destination_row_already_there(
        table: Path, library_name: str, library_uri: str,
        project_dir: Path | None) -> bool:
    """@brief Whether the destination table already maps the nickname to this place.

    @param table Table the install updates.
    @param library_name Nickname being installed.
    @param library_uri URI the install registers.
    @param project_dir Folder ``${KIPRJMOD}`` stands for (project install);
        None for the global table.
    @return True when an ENABLED ``KiCad`` row of that nickname already
        points to the same place (`_same_place`); False otherwise, and when
        the table is missing or unreadable.
    @note Used to skip the shadowing check of a RE-install (review
        2026-11-03, D7): the row is already there, so the clash with the
        other scope existed, and was asked about, at the first install;
        repeating the question at every update taught the user to dismiss it.
        Limit: a row added to the other scope AFTER that first install is
        not reported on a re-install either.
    """
    try:
        entry = find_library_entry(table, library_name)
    except (KiCadLibraryError, OSError):
        return False
    return (entry is not None and not entry.disabled
            and (entry.library_type or "").casefold() == "kicad"
            and _same_place(entry.uri, library_uri, project_dir))


def install_component_library(
        artifacts: ComponentArtifacts, destination_root: str | os.PathLike[str],
        *, library_name: str = "KiCadGen", project_library: bool = True,
        table_path: str | os.PathLike[str] | None = None,
        overwrite: bool = False,
        replace_library_entry: bool = False,
        kicad_version: str | None = None) -> LibraryInstallResult:
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
        nickname but a different URI/type, and enable a disabled one, when
        True (explicit confirmation).
    @param kicad_version KiCad settings version that receives the install
        (`GlobalTableLocation.version` for a global install); None when
        unknown, which skips the check.
    @return Installed paths and the ``library:footprint`` identifier;
        ``step_path``/``wrl_path`` of the result are None when that file was
        not installed.
    @throws KiCadLibraryError ``kicad_too_old`` (context ``version``,
        ``minimum``, ``path``) when `kicad_version` is older than
        `MINIMUM_KICAD_VERSION`; ``files_exist``, ``global_table_missing``,
        ``library_entry_conflict``, ``library_entry_disabled``,
        ``duplicate_library_entry`` and footprint
        parsing errors are raised before anything is written.
        ``rollback_incomplete`` (context ``rollback_dir``, ``error``,
        ``failures``) means the install failed AND some files could not be
        restored: the backups are kept in ``rollback_dir``.
        ``install_write_refused`` (context ``path``, ``error``) means the
        operating system refused a write (read-only table or installed file,
        file held open, protected folder) and the rollback was complete:
        ``path`` is the real file, never a scratch name. Any other failure
        is re-raised unchanged after a complete rollback.
    @note CONTRACT CHANGED 2026-10-15: a refused write used to be re-raised as
        a bare PermissionError, which the GUI showed as «unexpected OS error»
        naming a ``.kicadgen.tmp`` scratch path the user never sees.
        ``file_not_utf8`` (table or footprint not saved as UTF-8) is raised
        before anything is written.
    @note Rollback restores, from copies kept in a rollback folder, only the
        component files this install really replaced and the table only when
        its content changed; it removes files and empty folders the install
        created, and deletes the rollback folder only when every step
        succeeded. Each rollback step runs even if an earlier one failed.
    @note Copies never inherit the read-only attribute of their source (see
        `_copy_writable`), so a read-only artifact does not make the
        installed files, or the rollback copies, impossible to replace later.
    @note The installed copy is named ``<artifacts.name>`` inside, like its
        file (see name_footprint); the source footprint is not modified.
    @note USER DECISION 2026-11-12: without a STEP (`InstallPlan.with_model`
        False, e.g. no FreeCAD) the footprint is installed with NO model
        node and no STEP/WRL is copied. A ``<name>.step``/``.wrl`` an earlier
        install WITH a model left in ``<library>.3dshapes`` is deleted (it is
        in ``removed``, so ``files_exist`` asks first, and the rollback puts
        it back), never left referenced. A later install with a STEP adds the
        model and its reference again.
    @note Linked targets (review 2026-10-27, A1): a target or table that is a
        symbolic link is written through the link (`_write_target`), a
        hard-linked one in place (`_publish_scratch`), so the other name of
        the file sees the new content and the rollback restores both. A
        dangling link counts as an existing file for ``files_exist`` and,
        once overwriting is allowed, is refused as ``target_link_unresolved``
        before anything is written. The returned paths are the user's paths.
    @note A rollback folder that cannot be removed after a successful or a
        completely undone install is marked disposable
        (`ROLLBACK_DISPOSABLE_MARKER`) and reported (`rollback_left` of the
        result, context ``rollback_left`` of the error); every install first
        sweeps the disposable ones older than `STALE_SCRATCH_AGE_S`
        (`_sweep_stale_rollback_dirs`). The folder kept by
        ``rollback_incomplete`` holds the only copies and is never swept.
    """
    plan = plan_component_install(
        artifacts, destination_root, library_name=library_name,
        project_library=project_library, table_path=table_path,
        kicad_version=kicad_version)
    if plan.kicad_too_old:
        # Refused before anything is written: KiCad would refuse every file.
        raise _kicad_too_old(kicad_version or "?", plan.table_path)
    target_footprint, target_step, target_wrl_path = plan.targets
    root = target_footprint.parents[1]
    pretty_dir = target_footprint.parent
    shapes_dir = target_step.parent
    with_model = plan.with_model
    #: True once this install deleted the earlier STEP (it may then remove the emptied folder).
    model_removed = False
    # The model files really shipped: none without a STEP (USER DECISION
    # 2026-11-12), the WRL only next to its STEP.
    source_wrl = artifacts.wrl_path if with_model else None
    target_wrl = target_wrl_path if source_wrl is not None else None
    existing = [*plan.replaced, *plan.removed]
    if existing and not overwrite:
        raise KiCadLibraryError(
            "Component files already exist:\n" + "\n".join(map(str, existing)),
            code="files_exist", files="\n".join(map(str, existing)),
        )

    destination_table = plan.table_path
    table_existed = plan.table_exists
    # The table link is resolved BEFORE the missing-table check: a dangling
    # link is not a file either, and was reported as «start KiCad once»
    # (global_table_missing) instead of target_link_unresolved naming the
    # broken link (review 2026-10-29, L5).
    _write_target(destination_table)
    if not table_existed and not project_library:
        # A global table holding only KiCadGen would make KiCad skip its
        # first-run setup of the standard libraries.
        raise _global_table_missing(destination_table)
    # Every target is resolved through its link BEFORE the first write: a
    # dangling link is refused here, and the scratch files, rollback copies
    # and renames below all work on the real files (review 2026-10-27, A1).
    real = {target: _write_target(target) for target in plan.targets}
    real_footprint, real_step, real_wrl = (real[t] for t in plan.targets)
    # A non-UTF-8 table is refused here, before the first write.
    table_original = (_decode_utf8(destination_table.read_bytes(),
                                   destination_table, keep_newlines=True)
                      if table_existed else _EMPTY_TABLE)
    library_uri = plan.library_uri
    # Validate the table update and the footprint before the first write.
    _updated_fp_lib_table_text(
        table_original, library_name, library_uri, destination_table,
        replace_entry=replace_library_entry)
    model_reference = (
        f"${{KIPRJMOD}}/{library_name}.3dshapes/{artifacts.name}.step"
        if project_library else target_step.as_posix()
    )
    named = name_footprint(_read_utf8(artifacts.footprint_path), artifacts.name)
    if with_model:
        rendered = attach_3d_model(named, model_reference, artifacts.model_offset)
    else:
        # Validated as with a model, so a bad offset is refused either way.
        _checked_offset(artifacts.model_offset)
        rendered = detach_3d_model(named)

    # Folders created here are removed again by a failed install when empty.
    # Without a model the .3dshapes folder is neither needed nor created.
    created_dirs = [
        directory
        for directory in (*reversed(root.parents), root, pretty_dir,
                          *((shapes_dir,) if with_model else ()))
        if not directory.exists()
    ]
    # Scratch files are created (exclusively) inside the library folders, so
    # the list fills only once those folders exist; a failure at any step
    # removes exactly the scratch files created so far.
    staged: list[tuple[Path, Path]] = []
    _sweep_stale_rollback_dirs(root)
    rollback_dir = root / f"{ROLLBACK_DIR_PREFIX}{uuid.uuid4().hex}"
    # Internal path -> the user's file it stands for, so an error never names
    # a scratch file or the rollback folder (review 2026-10-15, L5).
    aliases: dict[Path, Path] = {rollback_dir: root}
    try:
        pretty_dir.mkdir(parents=True, exist_ok=True)
        if with_model:
            shapes_dir.mkdir(parents=True, exist_ok=True)
        # Scratch files are siblings of the REAL files (same volume as the
        # final rename), so a linked target is never replaced by a plain file.
        staged_footprint = _temporary_sibling(real_footprint)
        staged.append((staged_footprint, real_footprint))
        if with_model:
            staged_step = _temporary_sibling(real_step)
            staged.append((staged_step, real_step))
        if target_wrl is not None:
            staged_wrl = _temporary_sibling(real_wrl)
            staged.append((staged_wrl, real_wrl))
        aliases.update({temporary: target for temporary, target in staged})
        # Every staged file is on disk before its rename publishes it: the
        # rollback folder is deleted after a successful install, so a power
        # loss could otherwise leave an empty .kicad_mod/.step that the table
        # already references (review 2026-10-15, M5).
        _write_durably(staged_footprint, rendered.encode("utf-8"))
        if with_model:
            _copy_writable(artifacts.step_path, staged_step)
            _fsync_file(staged_step)
        if target_wrl is not None and source_wrl is not None:
            _copy_writable(source_wrl, staged_wrl)
            _fsync_file(staged_wrl)
        rollback_dir.mkdir()
    except BaseException as exc:
        for temporary, _target in staged:
            _unlink_quietly(temporary)
        _remove_created_directories(created_dirs)
        if isinstance(exc, (PermissionError, IsADirectoryError)):
            raise _install_write_refused(exc, root, aliases) from exc
        raise

    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    table_backup: Path | None = None
    try:
        for target in real.values():
            if target.is_file():
                backup = rollback_dir / f"{len(backups)}-{target.name}"
                aliases[backup] = target
                _copy_writable(target, backup)
                backups[target] = backup
        if table_existed:
            table_backup = rollback_dir / "table-backup"
            aliases[table_backup] = destination_table
            _copy_writable(destination_table, table_backup)
        for temporary, target in staged:
            # Retried like every other atomic write: an antivirus or KiCad's
            # file watcher holding the target for an instant must not undo
            # the whole installation (review 2026-10-15, M5). A hard-linked
            # target is overwritten in place: its rollback copy was taken
            # just above (`_publish_scratch`).
            _publish_scratch(temporary, target)
            installed.append(target)
        if not with_model and real_step.is_file():
            # No model shipped (USER DECISION 2026-11-12): the STEP of an
            # earlier install no longer matches the footprint, which does
            # not reference it any more. Backed up above, restored by the
            # rollback, reported in LibraryInstallResult.removed.
            real_step.unlink()
            installed.append(real_step)
            model_removed = True
        if source_wrl is None and real_wrl.is_file():
            # Removal, not replacement: reported in LibraryInstallResult.removed
            # so the GUI can say so (review 2026-10-27, A3). Unlinking one
            # name of a hard-linked file leaves the other name untouched.
            real_wrl.unlink()
            installed.append(real_wrl)
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
        left = _dispose_rollback_dir(rollback_dir)
        _remove_created_directories(created_dirs)
        if isinstance(exc, (PermissionError, IsADirectoryError)):
            refused = _install_write_refused(exc, root, aliases)
            if left is not None:
                refused.context["rollback_left"] = left
            raise refused from exc
        if isinstance(exc, KiCadLibraryError) and left is not None:
            exc.context.setdefault("rollback_left", left)
        raise
    for temporary, _target in staged:
        _unlink_quietly(temporary)
    if model_removed and not _is_directory_link(shapes_dir):
        # This reinstallation without STEP removed the earlier model: the
        # library folder it left EMPTY is removed too (user, 2026-11-12).
        # Only a real folder: a junction or symbolic link would itself be
        # removed by rmdir whatever it points to, breaking the models of the
        # other footprints reached through it (review 2026-11-12). A folder
        # this install did not empty is left alone. Done after the commit,
        # so the rollback never needs it; rmdir refuses a non-empty folder.
        try:
            shapes_dir.rmdir()
        except OSError:
            pass
    left = _dispose_rollback_dir(rollback_dir)
    return LibraryInstallResult(
        f"{library_name}:{artifacts.name}", target_footprint,
        target_step if with_model else None, target_wrl, destination_table,
        replaced=plan.replaced, removed=plan.removed, rollback_left=left,
    )


#: Seconds `kicad-cli version` may take before the path folder is used
#: instead. The command prints one line and exits; the bound only keeps a
#: hung executable from blocking the caller, it is not a performance target.
KICAD_CLI_VERSION_TIMEOUT_S = 10


#: Outputs of ``kicad-cli version`` already obtained in this process, keyed
#: by (absolute path, modification time ns, size) of the executable. The
#: command is run from the GUI thread (global installation) and costs about
#: 0.3 s, up to the timeout when the executable hangs; an executable whose
#: file did not change prints the same version, a reinstalled one changes
#: its key and is asked again.
_KICAD_CLI_VERSION_CACHE: dict[tuple[str, int, int], str | None] = {}


def _run_kicad_cli_version(executable: Path) -> str | None:
    """@brief Stdout of ``kicad-cli version``, or None when it cannot be obtained.

    @param executable Path of kicad-cli.
    @return The command output; None when the file is missing, the command
        fails or times out.
    @note Cached per executable path and file identity for the process
        lifetime (`_KICAD_CLI_VERSION_CACHE`), failures included: a hung
        executable must not block the window again at every installation.
    """
    try:
        stat = executable.stat()
    except OSError:
        return None
    if not executable.is_file():
        return None
    key = (str(executable.resolve()), stat.st_mtime_ns, stat.st_size)
    if key in _KICAD_CLI_VERSION_CACHE:
        return _KICAD_CLI_VERSION_CACHE[key]
    try:
        proc = subprocess.run(
            [str(executable), "version"], capture_output=True, text=True,
            errors="replace", timeout=KICAD_CLI_VERSION_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        output = None
    else:
        output = proc.stdout if proc.returncode == 0 else None
    _KICAD_CLI_VERSION_CACHE[key] = output
    return output


def kicad_settings_version(text: str | None) -> str | None:
    """@brief Extract the ``<major>.<minor>`` settings folder name from ``kicad-cli version`` output.

    @param text Output of the command (e.g. ``10.0.6`` or a nightly
        ``10.99.0-123-g1234abcd``); None or empty when unavailable.
    @return ``"10.0"``, ``"10.99"``... as KiCad names its settings folder;
        None when the first non-blank line does not start with a version.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            match = re.match(r"v?(\d+)\.(\d+)", line)
            return f"{int(match.group(1))}.{int(match.group(2))}" if match else None
    return None


#: Oldest KiCad (``<major>.<minor>``) that reads the files the generators
#: write. Derived from the format constant `GENERATOR_VERSION`, the release
#: whose file format (`KICAD_VERSION`) is written, never from a release name
#: typed here: when the generators move to a newer format, this follows.
#: An older KiCad refuses a footprint of a newer format version as a whole.
MINIMUM_KICAD_VERSION = ".".join(GENERATOR_VERSION.split(".")[:2])


def kicad_version_supported(version: str | None) -> bool | None:
    """@brief Whether a KiCad of `version` reads the files the generators write.

    @param version ``<major>.<minor>`` (or longer) KiCad version, e.g. the
        settings folder of `resolve_global_fp_lib_table` or the output of
        `kicad_settings_version`.
    @return True when it is at least `MINIMUM_KICAD_VERSION`, False when
        older, None when unknown (not a dotted number).
    @note A development series numbered below the minimum (a ``9.99``
        nightly) counts as older: whether it already reads the newer format
        depends on its build date, which the version does not tell.
    """
    return version_at_least(version, MINIMUM_KICAD_VERSION)


def _kicad_too_old(version: str, table: Path | None = None) -> KiCadLibraryError:
    """@brief Error for an installation into a KiCad older than the file format.

    @param version KiCad version that would receive the files.
    @param table Its library table, named in the message when known.
    @return KiCadLibraryError ``kicad_too_old`` (context ``version``,
        ``minimum``, ``path``).
    """
    where = f" ({table})" if table is not None else ""
    return KiCadLibraryError(
        f"KiCad {version}{where} cannot read the generated files: they use "
        f"the file format of KiCad {MINIMUM_KICAD_VERSION}. Install KiCad "
        f"{MINIMUM_KICAD_VERSION} or newer. No file was changed.",
        code="kicad_too_old", version=version, minimum=MINIMUM_KICAD_VERSION,
        path=table if table is not None else "",
    )


def resolve_global_fp_lib_table(
        kicad_cli_path: str | os.PathLike[str],
        *, environ: dict[str, str] | None = None,
        platform: str | None = None,
        version_runner=None) -> GlobalTableLocation:
    """@brief Resolve the per-user global fp-lib-table for the installed KiCad.

    @details Mirrors KiCad's settings path: ``$KICAD_CONFIG_HOME/<version>``
    when that variable is set and not empty, otherwise the platform default
    (``%APPDATA%/kicad/<version>``, ``~/Library/Preferences/kicad/<version>``
    or ``$XDG_CONFIG_HOME/kicad/<version>``).
    @param kicad_cli_path Path of kicad-cli. The settings version is the
        ``<major>.<minor>`` printed by ``kicad-cli version`` (a nightly
        ``10.99.x`` gives ``10.99``, which its install folder may not show);
        when the command gives nothing usable, the ``<major>.<minor>`` folder
        in the path is used, as before.
    @param environ Environment used instead of ``os.environ`` (tests).
    @param platform Platform used instead of ``sys.platform`` (tests).
    @param version_runner Callable taking the executable Path and returning
        the command's stdout or None; defaults to running ``kicad-cli
        version`` (read-only). Injected by tests that have no KiCad.
    @return The table path (the file may not exist yet), the settings version
        and where that version came from (`GlobalTableLocation`), so a caller
        can name them BEFORE writing (review 2026-10-27, D1).
    @throws KiCadLibraryError ``version_unknown`` when no version is found.
    """
    executable = Path(kicad_cli_path)
    runner = _run_kicad_cli_version if version_runner is None else version_runner
    version = kicad_settings_version(runner(executable))
    version_source = "kicad-cli"
    if version is None:
        version = next(
            (part for part in reversed(executable.parts)
             if re.fullmatch(r"\d+\.\d+", part)),
            None,
        )
        version_source = "folder"
    if version is None:
        raise KiCadLibraryError(
            "Unable to determine the installed KiCad version.",
            code="version_unknown",
        )
    values = os.environ if environ is None else environ
    override = (values.get("KICAD_CONFIG_HOME") or "").strip()
    if override:
        table = Path(override).expanduser() / version / "fp-lib-table"
        return GlobalTableLocation(table, version, version_source)
    current_platform = sys.platform if platform is None else platform
    if current_platform == "win32":
        base = Path(values.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif current_platform == "darwin":
        base = Path.home() / "Library" / "Preferences"
    else:
        base = Path(values.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return GlobalTableLocation(
        base / "kicad" / version / "fp-lib-table", version, version_source)


def global_fp_lib_table_path(
        kicad_cli_path: str | os.PathLike[str],
        *, environ: dict[str, str] | None = None,
        platform: str | None = None,
        version_runner=None) -> Path:
    """@brief Path of the per-user global fp-lib-table (see `resolve_global_fp_lib_table`).

    @param kicad_cli_path Path of kicad-cli.
    @param environ Environment used instead of ``os.environ`` (tests).
    @param platform Platform used instead of ``sys.platform`` (tests).
    @param version_runner Version command replacement (tests).
    @return Path of the table; the file may not exist yet.
    @throws KiCadLibraryError ``version_unknown`` when no version is found.
    """
    return resolve_global_fp_lib_table(
        kicad_cli_path, environ=environ, platform=platform,
        version_runner=version_runner).path
