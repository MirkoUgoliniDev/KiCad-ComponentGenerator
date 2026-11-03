"""@brief Detect external desktop applications used by KiCadGen-Tool."""

from __future__ import annotations

from dataclasses import dataclass
import glob
import os
from pathlib import Path
import shutil

from ..cad3d.step_generator import Cad3DError, find_freecadcmd
from . import installed_programs
from ..kicad_versions import newest_version_first


@dataclass(frozen=True)
class ExternalApplications:
    """@brief Store detected executable paths for optional external tools."""

    kicad_cli: str | None
    freecadcmd: str | None
    pcbnew: str | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        """@brief Return the display names of applications that were not found.

        @return Tuple of names among "KiCad" and "FreeCAD".
        """
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


def _find_installed(program: str) -> str | None:
    """@brief Find a KiCad program in the usual installation folders.

    @param program Executable name without extension (``kicad-cli``, ``pcbnew``).
    @return The program of the highest installed KiCad version, or None.
    @note Windows installs one folder per version (``KiCad/10.0/bin``); the
        newest is chosen by numeric version, not by text (``9.0`` sorts after
        ``10.0`` as text). Unversioned locations are kept as a fallback.
    @note Besides the program roots (per machine and per user,
        `installed_programs.program_roots`), the folders declared by KiCad's
        «Uninstall» registry entries are searched, so an installation in a
        folder the user chose is found too.
    """
    patterns = []
    for root in installed_programs.program_roots():
        patterns.extend((
            str(Path(root) / "KiCad" / "*" / "bin" / f"{program}.exe"),
            str(Path(root) / "KiCad" / "bin" / f"{program}.exe"),
        ))
    patterns.extend((
        f"/usr/bin/{program}",
        f"/usr/local/bin/{program}",
        f"/Applications/KiCad/KiCad.app/Contents/MacOS/{program}",
    ))
    matches = [match for pattern in patterns for match in glob.glob(pattern)]
    matches.extend(installed_programs.executables_in(
        installed_programs.registered_install_dirs(r"KiCad\b"), f"{program}.exe"))
    candidates = {
        resolved_path
        for match in matches
        if (resolved_path := _existing_file(match)) is not None
    }
    # The version folder is the parent of ``bin``.
    ordered = newest_version_first(candidates, lambda path: Path(path).parent.parent.name)
    return ordered[0] if ordered else None


def find_kicad_cli() -> str | None:
    """@brief Find kicad-cli using an override, PATH and common installations.

    @return Path of the executable (KICAD_CLI_PATH first), or None.
    """
    override = _existing_file(os.environ.get("KICAD_CLI_PATH"))
    if override:
        return override

    from_path = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    resolved = _existing_file(from_path)
    if resolved:
        return resolved

    return _find_installed("kicad-cli")


def find_pcbnew(kicad_cli: str | None = None) -> str | None:
    """@brief Find the KiCad PCB Editor used for isolated visual previews.

    @param kicad_cli Path of kicad-cli, whose sibling pcbnew is tried first
        after the KICAD_PCBNEW_PATH override; None skips that step.
    @return Path of the pcbnew executable, or None.
    """
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

    return _find_installed("pcbnew")


@dataclass(frozen=True)
class KiCadVersionCheck:
    """@brief Verdict on whether an installed kicad-cli reads the generated files.

    @note ``version`` is the ``<major>.<minor>`` printed by ``kicad-cli
        version`` (None when the command gave nothing usable), ``minimum``
        the oldest KiCad that reads the file format the generators write
        (`kicad_library.MINIMUM_KICAD_VERSION`) and ``supported`` the
        comparison: None when the version is unknown, so the caller can say
        «not verified» instead of a pass or a refusal.
    @note ``newer_cli`` and ``newer_version`` name a supported KiCad
        installed in the usual folders (`_find_installed`) when the checked
        one is too old: ``find_kicad_cli`` takes ``KICAD_CLI_PATH`` and PATH
        first, so a KiCad 9 on PATH hid an installed KiCad 10 (review
        2026-11-03, D4). None when there is none, or when the check passed or
        is unknown.
    """

    version: str | None
    minimum: str
    supported: bool | None
    newer_cli: str | None = None
    newer_version: str | None = None


def _same_executable(first: str | os.PathLike[str], second: str | os.PathLike[str]) -> bool:
    """Whether two executable paths name the same file (case-insensitive where the OS is)."""
    try:
        if os.path.samefile(first, second):
            return True
    except (OSError, ValueError):
        pass
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def check_kicad_version(kicad_cli: str | os.PathLike[str],
                        *, version_runner=None,
                        installed_finder=None) -> KiCadVersionCheck:
    """@brief Compare the version of a kicad-cli with the generated file format.

    @param kicad_cli Path of kicad-cli (from `find_kicad_cli`).
    @param version_runner Callable taking the executable Path and returning
        the stdout of ``kicad-cli version`` or None; defaults to running the
        command (read-only, cached per executable). Injected by tests.
    @param installed_finder Callable returning the kicad-cli of the newest
        installed KiCad or None; defaults to ``_find_installed("kicad-cli")``.
        Asked only when `kicad_cli` is too old. Injected by tests.
    @return The version found, the minimum, the verdict and, for a too old
        KiCad, a newer supported installation (`KiCadVersionCheck`).
    @note Detection (`find_kicad_cli`) prefers the newest installation but
        returns a KiCad 9 when it is the only one; the files are written in
        a newer format that it refuses. This check is what tells, so a
        verification or an installation does not report KiCad as usable
        (review 2026-11-03).
    """
    # Imported here: kicad_library pulls in the footprint serializer, which
    # the start-up detection of FreeCAD and KiCad does not need.
    from . import kicad_library
    runner = (kicad_library._run_kicad_cli_version
              if version_runner is None else version_runner)
    version = kicad_library.kicad_settings_version(runner(Path(kicad_cli)))
    supported = kicad_library.kicad_version_supported(version)
    newer_cli = newer_version = None
    if supported is False:
        finder = ((lambda: _find_installed("kicad-cli"))
                  if installed_finder is None else installed_finder)
        candidate = finder()
        if candidate and not _same_executable(candidate, kicad_cli):
            candidate_version = kicad_library.kicad_settings_version(
                runner(Path(candidate)))
            if kicad_library.kicad_version_supported(candidate_version):
                newer_cli, newer_version = str(candidate), candidate_version
    return KiCadVersionCheck(
        version=version, minimum=kicad_library.MINIMUM_KICAD_VERSION,
        supported=supported, newer_cli=newer_cli, newer_version=newer_version)


def detect_external_applications() -> ExternalApplications:
    """@brief Detect KiCad and FreeCAD without starting either application.

    @return ExternalApplications with the paths found (None when missing).
    """
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
