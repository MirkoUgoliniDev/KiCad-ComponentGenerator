"""@brief FreeCAD and KiCad found wherever their installer put them.

Before, FreeCAD was looked for only in ``C:\\Program Files[ (x86)]\\FreeCAD*``
and KiCad only under the Program Files roots: an installation «for the
current user only» (``%LOCALAPPDATA%\\Programs``) or in a folder the user
chose was reported missing at start-up. Each source is tested with a
positive case and a negative one that differs for a structural reason.
"""
from __future__ import annotations

import glob as glob_module
from pathlib import Path

import pytest

from kicadgen.cad3d import step_generator
from kicadgen.cad3d.step_generator import Cad3DError
from kicadgen.services import installed_programs, system_dependencies


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _only_under(root: Path, monkeypatch, *modules):
    """Restrict globbing to ``root``: the machine's real installations stay invisible."""
    real_glob = glob_module.glob
    for module in modules:
        monkeypatch.setattr(module.glob, "glob",
                            lambda pattern: [m for m in real_glob(pattern) if m.startswith(str(root))])


@pytest.fixture
def bare_environment(tmp_path, monkeypatch):
    """No override, no declared roots: only what each test sets up."""
    for name in ("FREECAD_CMD_PATH", "KICAD_CLI_PATH", "KICAD_PCBNEW_PATH",
                 "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(system_dependencies.shutil, "which", lambda name: None)
    _only_under(tmp_path, monkeypatch, step_generator, system_dependencies)
    return tmp_path


# -- registry entries ---------------------------------------------------------------

def test_install_location_is_trusted_first():
    entry = {"InstallLocation": r"D:\Apps\KiCad\10.0",
             "DisplayIcon": r"C:\Elsewhere\kicad.exe,0"}
    assert installed_programs.install_dir_from_entry(entry) == Path(r"D:\Apps\KiCad\10.0")


def test_without_install_location_the_icon_and_uninstaller_name_the_folder():
    """FreeCAD 1.1 leaves InstallLocation empty (seen on a real machine)."""
    icon = {"InstallLocation": "", "DisplayIcon": r"E:\Tools\FreeCAD 1.1\bin\FreeCAD.exe"}
    assert installed_programs.install_dir_from_entry(icon) == Path(r"E:\Tools\FreeCAD 1.1\bin")
    uninstall = {"UninstallString": r'"E:\Tools\FreeCAD 1.1\Uninstall-FreeCAD.exe" /S'}
    assert installed_programs.install_dir_from_entry(uninstall) == Path(r"E:\Tools\FreeCAD 1.1")
    unquoted = {"UninstallString": r"E:\Tools\KiCad\uninstall.exe /allusers"}
    assert installed_programs.install_dir_from_entry(unquoted) == Path(r"E:\Tools\KiCad")


def test_an_entry_without_any_path_gives_no_folder():
    assert installed_programs.install_dir_from_entry({"DisplayName": "FreeCAD 1.1"}) is None


def test_only_entries_whose_name_starts_with_the_program_are_kept():
    entries = [
        {"DisplayName": "FreeCAD 1.1.4", "InstallLocation": r"D:\FC"},
        # Negative: a different product that merely mentions FreeCAD.
        {"DisplayName": "Addon for FreeCAD", "InstallLocation": r"D:\Addon"},
        {"DisplayName": "FreeCADx", "InstallLocation": r"D:\Other"},
        {"DisplayName": "freecad 0.21", "InstallLocation": r"d:\fc"},  # same folder, other case
    ]
    assert installed_programs.registered_install_dirs(r"FreeCAD\b", entries) == [Path(r"D:\FC")]


def test_freecad_is_found_where_its_registry_entry_points(bare_environment, monkeypatch):
    """Positive: a folder outside every root, declared only by the registry."""
    exe = _touch(bare_environment / "Custom" / "FreeCAD 1.1" / "bin" / "freecadcmd.exe")
    monkeypatch.setattr(installed_programs, "_uninstall_entries", lambda: [{
        "DisplayName": "FreeCAD 1.1.4", "InstallLocation": None,
        "DisplayIcon": str(exe.parent / "FreeCAD.exe"),
        "UninstallString": f'"{exe.parent.parent / "Uninstall-FreeCAD.exe"}"'}])
    assert step_generator.find_freecadcmd() == str(exe)


def test_a_registry_entry_without_freecadcmd_is_not_an_installation(bare_environment, monkeypatch):
    """Negative: a stale entry whose folder was deleted finds nothing."""
    monkeypatch.setattr(installed_programs, "_uninstall_entries", lambda: [{
        "DisplayName": "FreeCAD 1.1.4", "InstallLocation": str(bare_environment / "gone")}])
    with pytest.raises(Cad3DError):
        step_generator.find_freecadcmd()


# -- per-user and declared roots ---------------------------------------------------------

def test_freecad_installed_for_the_current_user_is_found(bare_environment, monkeypatch):
    local = bare_environment / "LocalAppData"
    exe = _touch(local / "Programs" / "FreeCAD 1.1" / "bin" / "freecadcmd.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert step_generator.find_freecadcmd() == str(exe)


def test_kicad_installed_for_the_current_user_is_found(bare_environment, monkeypatch):
    local = bare_environment / "LocalAppData"
    exe = _touch(local / "Programs" / "KiCad" / "10.0" / "bin" / "kicad-cli.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert Path(system_dependencies.find_kicad_cli()) == exe.resolve()


def test_without_any_installation_both_are_missing(bare_environment, monkeypatch):
    """Negative: the same roots, empty."""
    monkeypatch.setenv("LOCALAPPDATA", str(bare_environment / "LocalAppData"))
    with pytest.raises(Cad3DError):
        step_generator.find_freecadcmd()
    assert system_dependencies.find_kicad_cli() is None


def test_program_files_on_another_drive_is_followed(bare_environment, monkeypatch):
    """%ProgramFiles% is what Windows declares, not always C:\\Program Files."""
    root = bare_environment / "D_Program Files"
    exe = _touch(root / "FreeCAD 1.0" / "bin" / "freecadcmd.exe")
    monkeypatch.setenv("ProgramFiles", str(root))
    assert step_generator.find_freecadcmd() == str(exe)


def test_the_newest_version_wins_across_sources(bare_environment, monkeypatch):
    """A per-machine 1.0 and a per-user 1.1: ranked together, 1.1 first."""
    machine = bare_environment / "PF"
    local = bare_environment / "LocalAppData"
    _touch(machine / "FreeCAD 1.0" / "bin" / "freecadcmd.exe")
    newer = _touch(local / "Programs" / "FreeCAD 1.1" / "bin" / "freecadcmd.exe")
    monkeypatch.setenv("ProgramFiles", str(machine))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert step_generator.find_freecadcmd() == str(newer)


def test_roots_are_listed_once():
    """The same folder declared twice (ProgramFiles and ProgramW6432) is one root."""
    keys = [str(root).lower().rstrip("\\") for root in installed_programs.program_roots()]
    assert len(keys) == len(set(keys))


def test_the_same_file_found_twice_counts_once():
    paths = [r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe",
             r"c:\program files\FreeCAD 1.1\bin\..\bin\freecadcmd.exe"]
    assert installed_programs.unique_paths(paths) == paths[:1]
