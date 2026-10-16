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
import stat
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
        *, require_3d: bool = True) -> ComponentArtifacts:
    """@brief Locate the generated footprint and matching STEP/WRL files.

    @param component_dir Working directory of one generated component.
    @param name Expected component name; defaults to the directory name.
    @param require_3d Require a STEP model in addition to the footprint.
    @return Validated artifact paths.
    @throws KiCadLibraryError If required files are missing or ambiguous, or
        if the STEP file lacks the ISO 10303-21 trailer (code
        ``step_incomplete``), even when ``require_3d`` is False.
    @throws OSError If the STEP file exists but cannot be read.
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
    if step.is_file() and not step_file_is_complete(step):
        # Never ship a truncated model: it is newer than the footprint, so the
        # "older STEP" warning cannot catch it, and KiCad would show it broken.
        raise KiCadLibraryError(
            f"STEP model is incomplete (no END-ISO-10303-21; trailer, the "
            f"export was probably interrupted): {step}",
            code="step_incomplete", path=step,
        )
    return ComponentArtifacts(
        component_name, root, footprint, step,
        wrl if wrl.is_file() else None,
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
    renamed = footprint_text[:match.start(2)] + _quote(name) + footprint_text[match.end(2):]
    for start, end in _direct_child_ranges(renamed, "property"):
        block = renamed[start:end]
        arguments = _node_arguments(block)
        if arguments[:1] != ["Value"]:
            continue
        value = _VALUE_HEAD.match(block)
        if value is not None and len(arguments) > 1 and arguments[1] == old:
            block = block[:value.start(2)] + _quote(name) + block[value.end(2):]
            renamed = renamed[:start] + block + renamed[end:]
        break
    return renamed


def attach_3d_model(footprint_text: str, model_reference: str) -> str:
    """@brief Replace root model nodes with one normalized STEP reference.

    @param footprint_text Text of a .kicad_mod footprint.
    @param model_reference Model path written in the `(model ...)` node, e.g.
        a `${KIPRJMOD}` or absolute STEP reference.
    @return The footprint text with exactly one model node, with zero offset
        and rotation and unit scale.
    """
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
    return _append_root_child(
        result, f"\t(at {_mm(_PREVIEW_ORIGIN[0])} {_mm(_PREVIEW_ORIGIN[1])})")


def _mm(value: float) -> str:
    """Board coordinate in mm as KiCad writes it: 1 um resolution, no trailing zeros."""
    text = f"{round(float(value), 4):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


#: Name of the preview folder the program creates inside a component folder.
#: Everything in it is generated (board, project, model copies), never user
#: content, which is what allows create_preview_project to unlock its files.
PREVIEW_DIR_NAME = ".kicad-preview"

#: Position of the footprint on the preview board, mm.
_PREVIEW_ORIGIN = (100.0, 100.0)

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


def create_preview_project(
        artifacts: ComponentArtifacts,
        output_dir: str | os.PathLike[str] | None = None) -> PreviewProject:
    """@brief Create an isolated board that can be opened in KiCad for review.

    @details The generated board embeds the footprint and references a local copy
    of the STEP model through ``${KIPRJMOD}``. No KiCad library table is changed.
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
    footprint = _board_footprint(
        footprint_text, f"${{KIPRJMOD}}/{artifacts.name}.step",
    )
    x0, y0, x1, y1 = preview_board_outline(footprint_text)
    lock_path = kicad_lock_file(board_path)
    if lock_path.exists():
        raise KiCadLibraryError(
            f"The preview board is open in KiCad (lock file {lock_path}). "
            "Close it in KiCad before refreshing the preview.",
            code="preview_locked", path=lock_path, board=board_path,
        )
    outline_uuid = uuid.uuid4()
    layer_rows = "".join(
        f"\t\t({ordinal} {_quote(name)} {kind}"
        + (f" {_quote(user_name)}" if user_name else "") + ")\n"
        for ordinal, name, kind, user_name in PREVIEW_BOARD_LAYERS)
    board = (
        f"(kicad_pcb (version {KICAD_VERSION}) (generator \"kicadgen_tool\")\n"
        f"\t(generator_version \"{GENERATOR_VERSION}\")\n"
        "\t(general (thickness 1.6))\n"
        "\t(paper \"A4\")\n"
        "\t(layers\n"
        f"{layer_rows}"
        "\t)\n"
        "\t(setup (pad_to_mask_clearance 0))\n"
        f"{footprint.rstrip()}\n"
        f"\t(gr_rect (start {_mm(x0)} {_mm(y0)}) (end {_mm(x1)} {_mm(y1)})\n"
        "\t\t(stroke (width 0.05) (type default)) (fill none)\n"
        f"\t\t(layer \"Edge.Cuts\") (uuid \"{outline_uuid}\"))\n"
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
                        board_path, project_path):
                if own.is_file():
                    _clear_readonly(own)
        _atomic_copy(artifacts.step_path, preview_dir / f"{artifacts.name}.step")
        if artifacts.wrl_path is not None:
            _atomic_copy(artifacts.wrl_path, preview_dir / f"{artifacts.name}.wrl")
        _atomic_write_text(board_path, board)
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
    return PreviewProject(preview_dir, board_path, project_path)


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


def _remove_tree_quietly(directory: Path) -> bool:
    """@brief Remove a folder of ours (rollback copies), read-only files included.

    @param directory Folder created by this module.
    @return True when the folder is gone.
    @note ``shutil.rmtree(ignore_errors=True)`` silently left the folder
        behind when one copy was read-only; files are unlocked first here.
    """
    if not directory.exists():
        return True
    for current, folders, files in os.walk(directory, topdown=False):
        for name in files:
            try:
                _remove_clearing_readonly(Path(current) / name)
            except OSError:
                pass
        for name in folders:
            try:
                os.rmdir(Path(current) / name)
            except OSError:
                pass
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


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write bytes durably through a sibling scratch file and an atomic replace."""
    temporary = _temporary_sibling(target)
    try:
        _write_durably(temporary, payload)
        replace_file(temporary, target)
    finally:
        _unlink_quietly(temporary)


def _atomic_write_text(target: Path, text: str) -> None:
    """Write UTF-8 text (LF newlines) durably through a scratch file and a replace."""
    _atomic_write_bytes(target, text.encode("utf-8"))


def _atomic_copy(source: Path, target: Path) -> None:
    """Copy a file (writable, see `_copy_writable`) durably through a scratch file and a replace."""
    temporary = _temporary_sibling(target)
    try:
        _copy_writable(source, temporary)
        _fsync_file(temporary)
        replace_file(temporary, target)
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
    """
    history = _table_backup_history(table)
    for path in history:
        if path.exists():
            _clear_readonly(path)
    moved: list[tuple[Path, Path]] = []
    oldest = history[-1]
    # The first shift overwrites the oldest copy. It is first moved aside, so
    # a rotation failing halfway can put it back: an undo that restores only
    # the shifted copies lost it (review 2026-10-15, L1).
    aside: Path | None = None
    try:
        if oldest.exists() and history[-2].exists():
            scratch = _temporary_sibling(oldest)
            try:
                replace_file(oldest, scratch)
            except BaseException:
                # Still empty: it must never be "restored" over the oldest copy.
                _unlink_quietly(scratch)
                raise
            aside = scratch
        for index in range(len(history) - 1, 0, -1):
            if history[index - 1].exists():
                replace_file(history[index - 1], history[index])
                moved.append((history[index - 1], history[index]))
        replace_file(fresh, history[0])
    except BaseException as exc:
        failures = _undo_backup_moves(moved, aside, oldest)
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


def _undo_backup_moves(moved: list[tuple[Path, Path]], aside: Path | None,
                       oldest: Path) -> list[str]:
    """@brief Reverse the moves of a failed backup rotation, newest move first.

    @param moved (source, destination) of every move that succeeded, in order.
    @param aside Scratch file holding the oldest copy, or None when it was not
        moved aside (or the move aside itself failed).
    @param oldest Path the oldest copy must return to.
    @return One message per copy that could not be put back; empty when the
        history is exactly as before.
    @note At the first failed undo the remaining ones stop: each of them would
        replace a file that still holds a copy not yet moved back. The
        messages name every copy left out of place so none is lost silently.
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
    if aside is not None and aside.exists():
        if failures:
            failures.append(f"oldest backup kept in {aside} (for {oldest})")
        else:
            try:
                replace_file(aside, oldest)
            except OSError as error:
                failures.append(f"{aside} -> {oldest}: {error}")
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
        nickname to a different URI/type (explicit user confirmation).
    @param create_missing Create a new table when the file does not exist;
        pass False for KiCad's global table, which KiCad must create itself.
    @return True when the table contents changed.
    @throws KiCadLibraryError ``global_table_missing`` when the table is
        absent and ``create_missing`` is False, ``file_not_utf8`` when the
        table is not UTF-8 text (nothing is written); see
        `_updated_fp_lib_table_text` for the conflict codes.
    @throws OSError When the table or its backup cannot be written; the table
        is then left (or put back) as it was, and the backup history unchanged.
    @note Every changing write refreshes ``<table>.kicadgen-backup`` (bounded
        history, see `TABLE_BACKUP_RETENTION`) and replaces the table
        atomically and durably (fsync before the rename); the temporary file
        never survives a failed replace.
    @note Order: the pre-change copy is staged, the table is written, and only
        then the history rotates. A failed write leaves the history as it was;
        a failed rotation puts the previous table back, so no change is ever
        left without its backup.
    """
    table = Path(table_path)
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
    try:
        _atomic_write_bytes(table, bom + updated.encode("utf-8"))
        if fresh is not None:
            try:
                _publish_table_backup(table, fresh)
            except BaseException:
                # The new table has no backup of its predecessor: undo it. A
                # failed undo must not mask the rotation error; the install
                # rollback compares the table with its own copy and retries.
                try:
                    _atomic_write_bytes(table, original_bytes)
                except OSError:
                    pass
                raise
    finally:
        if fresh is not None:
            _unlink_quietly(fresh)
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
    # Same line ending as the file (CRLF written on Windows by an editor or
    # an older tool) and the same text after the closing parenthesis (final
    # newline or none), so the rewrite changes only the added row.
    newline = "\r\n" if "\r\n" in original else "\n"
    return (original[:closing].rstrip() + newline + "  " + replacement
            + newline + original[closing:])


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
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _fsync_file(backup)
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
    """
    failures: list[str] = []
    for target in installed:
        if target in backups:
            try:
                _restore_file(backups[target], target)
            except OSError as error:
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
    except OSError as error:
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
    for target in targets:
        if target.is_dir() and not target.is_symlink():
            # Overwriting cannot help: a file never replaces a folder. Saying
            # "read-only or held open" sent the user looking for the wrong cause.
            raise _target_is_folder(target)
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
    # A non-UTF-8 table is refused here, before the first write.
    table_original = (_decode_utf8(destination_table.read_bytes(),
                                   destination_table, keep_newlines=True)
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
        name_footprint(_read_utf8(artifacts.footprint_path), artifacts.name),
        model_reference)

    # Folders created here are removed again by a failed install when empty.
    created_dirs = [
        directory
        for directory in (*reversed(root.parents), root, pretty_dir, shapes_dir)
        if not directory.exists()
    ]
    # Scratch files are created (exclusively) inside the library folders, so
    # the list fills only once those folders exist; a failure at any step
    # removes exactly the scratch files created so far.
    staged: list[tuple[Path, Path]] = []
    rollback_dir = root / f".kicadgen-rollback-{uuid.uuid4().hex}"
    # Internal path -> the user's file it stands for, so an error never names
    # a scratch file or the rollback folder (review 2026-10-15, L5).
    aliases: dict[Path, Path] = {rollback_dir: root}
    try:
        pretty_dir.mkdir(parents=True, exist_ok=True)
        shapes_dir.mkdir(parents=True, exist_ok=True)
        staged_footprint = _temporary_sibling(target_footprint)
        staged.append((staged_footprint, target_footprint))
        staged_step = _temporary_sibling(target_step)
        staged.append((staged_step, target_step))
        if target_wrl is not None:
            staged_wrl = _temporary_sibling(target_wrl)
            staged.append((staged_wrl, target_wrl))
        aliases.update({temporary: target for temporary, target in staged})
        # Every staged file is on disk before its rename publishes it: the
        # rollback folder is deleted after a successful install, so a power
        # loss could otherwise leave an empty .kicad_mod/.step that the table
        # already references (review 2026-10-15, M5).
        _write_durably(staged_footprint, rendered.encode("utf-8"))
        _copy_writable(artifacts.step_path, staged_step)
        _fsync_file(staged_step)
        if target_wrl is not None and artifacts.wrl_path is not None:
            _copy_writable(artifacts.wrl_path, staged_wrl)
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
        for target in targets:
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
            # the whole installation (review 2026-10-15, M5).
            replace_file(temporary, target)
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
        _remove_tree_quietly(rollback_dir)
        _remove_created_directories(created_dirs)
        if isinstance(exc, (PermissionError, IsADirectoryError)):
            raise _install_write_refused(exc, root, aliases) from exc
        raise
    for temporary, _target in staged:
        _unlink_quietly(temporary)
    _remove_tree_quietly(rollback_dir)
    return LibraryInstallResult(
        f"{library_name}:{artifacts.name}", target_footprint, target_step,
        target_wrl, destination_table,
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


def global_fp_lib_table_path(
        kicad_cli_path: str | os.PathLike[str],
        *, environ: dict[str, str] | None = None,
        platform: str | None = None,
        version_runner=None) -> Path:
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
    @return Path of the table; the file may not exist yet.
    @throws KiCadLibraryError ``version_unknown`` when no version is found.
    """
    executable = Path(kicad_cli_path)
    runner = _run_kicad_cli_version if version_runner is None else version_runner
    version = kicad_settings_version(runner(executable))
    if version is None:
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
