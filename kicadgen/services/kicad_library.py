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
    """
    preview_dir = Path(output_dir or artifacts.component_dir / ".kicad-preview")
    preview_dir.mkdir(parents=True, exist_ok=True)
    model_target = preview_dir / f"{artifacts.name}.step"
    shutil.copy2(artifacts.step_path, model_target)
    if artifacts.wrl_path is not None:
        shutil.copy2(artifacts.wrl_path, preview_dir / f"{artifacts.name}.wrl")

    footprint = _board_footprint(
        artifacts.footprint_path.read_text(encoding="utf-8"),
        f"${{KIPRJMOD}}/{artifacts.name}.step",
    )
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
    board_path = preview_dir / f"{artifacts.name}-preview.kicad_pcb"
    project_path = preview_dir / f"{artifacts.name}-preview.kicad_pro"
    board_path.write_text(board, encoding="utf-8", newline="\n")
    if not project_path.exists():
        project_path.write_text("{}\n", encoding="utf-8", newline="\n")
    return PreviewProject(preview_dir, board_path, project_path)


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


def _library_entry(library_name: str, uri: str) -> str:
    """@brief Serialize one native KiCad footprint-library table entry."""
    return (
        f"(lib (name {_quote(library_name)})(type \"KiCad\")"
        f"(uri {_quote(uri)})(options \"\")"
        "(descr \"Generated by KiCadGen-Tool\"))"
    )


def update_fp_lib_table(
        table_path: str | os.PathLike[str], library_name: str, uri: str) -> bool:
    """@brief Add or update one entry while preserving the rest of fp-lib-table.

    @return True when the table contents changed.
    """
    table = Path(table_path)
    original = (table.read_text(encoding="utf-8") if table.is_file()
                else "(fp_lib_table\n  (version 7)\n)\n")
    updated = _updated_fp_lib_table_text(original, library_name, uri, table)
    if updated == original:
        return False
    table.parent.mkdir(parents=True, exist_ok=True)
    if table.is_file():
        backup = table.with_name(table.name + ".kicadgen-backup")
        if not backup.exists():
            shutil.copy2(table, backup)
    temporary = table.with_name(table.name + ".kicadgen.tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    os.replace(temporary, table)
    return True


def _updated_fp_lib_table_text(
        original: str, library_name: str, uri: str,
        table_path: Path | None = None) -> str:
    """@brief Render a table update without touching the filesystem."""
    if not original.lstrip().startswith("(fp_lib_table"):
        location = table_path or "<memory>"
        raise KiCadLibraryError(
            f"Invalid footprint library table: {location}",
            code="invalid_table", path=location,
        )
    replacement = _library_entry(library_name, uri)
    updated = original
    matching_range = None
    for start, end in _direct_child_ranges(original, "lib"):
        block = original[start:end]
        name_match = re.search(r'\(name\s+"([^"\\]*)"\)', block)
        if name_match and name_match.group(1) == library_name:
            matching_range = (start, end)
            break
    if matching_range is not None:
        start, end = matching_range
        updated = original[:start] + replacement + original[end:]
    else:
        closing = original.rstrip().rfind(")")
        if closing < 0:
            location = table_path or "<memory>"
            raise KiCadLibraryError(
                f"Invalid footprint library table: {location}",
                code="invalid_table", path=location,
            )
        updated = original[:closing].rstrip() + "\n  " + replacement + "\n)\n"
    return updated


def install_component_library(
        artifacts: ComponentArtifacts, destination_root: str | os.PathLike[str],
        *, library_name: str = "KiCadGen", project_library: bool = True,
        table_path: str | os.PathLike[str] | None = None,
        overwrite: bool = False) -> LibraryInstallResult:
    """@brief Install one component into a project or global KiCad library.

    @param project_library Use portable ``${KIPRJMOD}`` paths when True.
    @param table_path Table to update; defaults to the project table.
    @param overwrite Replace files for an existing component when True.
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
    table_original = (destination_table.read_text(encoding="utf-8")
                      if destination_table.is_file()
                      else "(fp_lib_table\n  (version 7)\n)\n")
    library_uri = (f"${{KIPRJMOD}}/{library_name}.pretty"
                   if project_library else pretty_dir.as_posix())
    _updated_fp_lib_table_text(
        table_original, library_name, library_uri, destination_table)

    pretty_dir.mkdir(parents=True, exist_ok=True)
    shapes_dir.mkdir(parents=True, exist_ok=True)
    model_reference = (
        f"${{KIPRJMOD}}/{library_name}.3dshapes/{artifacts.name}.step"
        if project_library else target_step.as_posix()
    )
    rendered = attach_3d_model(
        artifacts.footprint_path.read_text(encoding="utf-8"), model_reference)

    staged_footprint = target_footprint.with_name(target_footprint.name + ".kicadgen.tmp")
    staged_step = target_step.with_name(target_step.name + ".kicadgen.tmp")
    staged_wrl = None
    if target_wrl is not None and artifacts.wrl_path is not None:
        staged_wrl = target_wrl.with_name(target_wrl.name + ".kicadgen.tmp")
    staged = [(staged_footprint, target_footprint), (staged_step, target_step)]
    if staged_wrl is not None and target_wrl is not None:
        staged.append((staged_wrl, target_wrl))
    rollback_dir = root / f".kicadgen-rollback-{uuid.uuid4().hex}"
    try:
        staged_footprint.write_text(rendered, encoding="utf-8", newline="\n")
        shutil.copy2(artifacts.step_path, staged_step)
        if staged_wrl is not None and artifacts.wrl_path is not None:
            shutil.copy2(artifacts.wrl_path, staged_wrl)
        rollback_dir.mkdir()
    except OSError:
        for temporary, _target in staged:
            if temporary.exists():
                temporary.unlink()
        raise
    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    table_existed = destination_table.is_file()
    try:
        for target in targets:
            if target.is_file():
                backup = rollback_dir / f"{len(backups)}-{target.name}"
                shutil.copy2(target, backup)
                backups[target] = backup
        for temporary, target in staged:
            os.replace(temporary, target)
            installed.append(target)
        if artifacts.wrl_path is None and target_wrl_path.is_file():
            target_wrl_path.unlink()
            installed.append(target_wrl_path)
        update_fp_lib_table(destination_table, library_name, library_uri)
    except (OSError, KiCadLibraryError):
        for target in installed:
            if target.exists():
                target.unlink()
        for target, backup in backups.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        if table_existed:
            destination_table.parent.mkdir(parents=True, exist_ok=True)
            destination_table.write_text(
                table_original, encoding="utf-8", newline="\n")
        elif destination_table.exists():
            destination_table.unlink()
        raise
    finally:
        for temporary, _target in staged:
            if temporary.exists():
                temporary.unlink()
        shutil.rmtree(rollback_dir, ignore_errors=True)
    return LibraryInstallResult(
        f"{library_name}:{artifacts.name}", target_footprint, target_step,
        target_wrl, destination_table,
    )


def global_fp_lib_table_path(
        kicad_cli_path: str | os.PathLike[str],
        *, environ: dict[str, str] | None = None,
        platform: str | None = None) -> Path:
    """@brief Resolve the default per-user global fp-lib-table for KiCad."""
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
    current_platform = sys.platform if platform is None else platform
    if current_platform == "win32":
        base = Path(values.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif current_platform == "darwin":
        base = Path.home() / "Library" / "Preferences"
    else:
        base = Path(values.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "kicad" / version / "fp-lib-table"
