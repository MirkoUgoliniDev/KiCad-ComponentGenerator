"""@brief Detect external desktop applications used by KiCadGen-Tool."""

from __future__ import annotations

from dataclasses import dataclass
import glob
import os
from pathlib import Path
import shutil

from ..cad3d.step_generator import Cad3DError, find_freecadcmd


@dataclass(frozen=True)
class ExternalApplications:
    """@brief Store detected executable paths for optional external tools."""

    kicad_cli: str | None
    freecadcmd: str | None
    pcbnew: str | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        """@brief Return the display names of applications that were not found."""
        result = []
        if not self.kicad_cli:
            result.append("KiCad")
        if not self.freecadcmd:
            result.append("FreeCAD")
        return tuple(result)


def _existing_file(path: str | os.PathLike[str] | None) -> str | None:
    """@brief Return a normalized executable path only when it is a file."""
    if not path:
        return None
    try:
        candidate = Path(path).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else None
    except OSError:
        return None


def find_kicad_cli() -> str | None:
    """@brief Find kicad-cli using an override, PATH and common installations."""
    override = _existing_file(os.environ.get("KICAD_CLI_PATH"))
    if override:
        return override

    from_path = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    resolved = _existing_file(from_path)
    if resolved:
        return resolved

    patterns = []
    for root in filter(None, (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            r"C:\Program Files",
            r"C:\Program Files (x86)",
    )):
        patterns.extend((
            str(Path(root) / "KiCad" / "*" / "bin" / "kicad-cli.exe"),
            str(Path(root) / "KiCad" / "bin" / "kicad-cli.exe"),
        ))
    patterns.extend((
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    ))

    candidates = {
        resolved_path
        for pattern in patterns
        for match in glob.glob(pattern)
        if (resolved_path := _existing_file(match)) is not None
    }
    return sorted(candidates, reverse=True)[0] if candidates else None


def find_pcbnew(kicad_cli: str | None = None) -> str | None:
    """@brief Find the KiCad PCB Editor used for isolated visual previews."""
    override = _existing_file(os.environ.get("KICAD_PCBNEW_PATH"))
    if override:
        return override

    if kicad_cli:
        sibling = _existing_file(
            Path(kicad_cli).with_name("pcbnew.exe" if os.name == "nt" else "pcbnew"))
        if sibling:
            return sibling

    from_path = shutil.which("pcbnew") or shutil.which("pcbnew.exe")
    resolved = _existing_file(from_path)
    if resolved:
        return resolved

    patterns = []
    for root in filter(None, (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            r"C:\Program Files",
            r"C:\Program Files (x86)",
    )):
        patterns.extend((
            str(Path(root) / "KiCad" / "*" / "bin" / "pcbnew.exe"),
            str(Path(root) / "KiCad" / "bin" / "pcbnew.exe"),
        ))
    patterns.extend((
        "/usr/bin/pcbnew",
        "/usr/local/bin/pcbnew",
        "/Applications/KiCad/KiCad.app/Contents/MacOS/pcbnew",
    ))
    candidates = {
        resolved_path
        for pattern in patterns
        for match in glob.glob(pattern)
        if (resolved_path := _existing_file(match)) is not None
    }
    return sorted(candidates, reverse=True)[0] if candidates else None


def detect_external_applications() -> ExternalApplications:
    """@brief Detect KiCad and FreeCAD without starting either application."""
    kicad_cli = find_kicad_cli()
    try:
        freecadcmd = find_freecadcmd()
    except (Cad3DError, OSError):
        freecadcmd = None
    return ExternalApplications(
        kicad_cli=kicad_cli,
        freecadcmd=_existing_file(freecadcmd),
        pcbnew=find_pcbnew(kicad_cli),
    )
