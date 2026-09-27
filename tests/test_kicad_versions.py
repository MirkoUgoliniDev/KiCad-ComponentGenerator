"""@brief The newest KiCad installation is chosen by numeric version, not by text."""

import glob as glob_module
from pathlib import Path

import pytest

from kicadgen import kicad_versions
from kicadgen.cad3d import kicad_models
from kicadgen.services import system_dependencies as dependencies


def test_versions_are_ordered_numerically_with_non_numeric_last():
    names = ["9.0", "nightly", "10.0", "8.0", "10.0.1"]
    assert kicad_versions.newest_version_first(names) == [
        "10.0.1", "10.0", "9.0", "8.0", "nightly"]
    assert kicad_versions.parse_version("10.0") == (10, 0)
    assert kicad_versions.parse_version("nightly") is None


def _fake_program_files(tmp_path, monkeypatch, versions, program="kicad-cli"):
    """Install fake KiCad version folders under tmp_path and hide the real disk."""
    for version in versions:
        folder = tmp_path / "KiCad" / version / "bin"
        folder.mkdir(parents=True)
        (folder / f"{program}.exe").write_bytes(b"")
    real_glob = glob_module.glob
    root = str(tmp_path)
    monkeypatch.setenv("ProgramFiles", root)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.delenv("KICAD_PCBNEW_PATH", raising=False)
    monkeypatch.setattr(dependencies.shutil, "which", lambda name: None)
    monkeypatch.setattr(dependencies.glob, "glob",
                        lambda pattern: [m for m in real_glob(pattern) if m.startswith(root)])


def _version_of(path):
    return Path(path).parent.parent.name


def test_the_highest_numeric_version_is_chosen(tmp_path, monkeypatch):
    _fake_program_files(tmp_path, monkeypatch, ["9.0", "10.0", "8.0"])
    assert _version_of(dependencies.find_kicad_cli()) == "10.0"


def test_pcbnew_follows_the_same_rule(tmp_path, monkeypatch):
    _fake_program_files(tmp_path, monkeypatch, ["9.0", "10.0"], program="pcbnew")
    assert _version_of(dependencies.find_pcbnew()) == "10.0"


def test_a_single_older_version_is_still_chosen(tmp_path, monkeypatch):
    _fake_program_files(tmp_path, monkeypatch, ["9.0"])
    assert _version_of(dependencies.find_kicad_cli()) == "9.0"


def test_a_non_numeric_folder_is_found_but_never_preferred(tmp_path, monkeypatch):
    _fake_program_files(tmp_path, monkeypatch, ["nightly"])
    assert _version_of(dependencies.find_kicad_cli()) == "nightly"


def test_a_non_numeric_folder_loses_to_a_numeric_one(tmp_path, monkeypatch):
    _fake_program_files(tmp_path, monkeypatch, ["nightly", "9.0"])
    assert _version_of(dependencies.find_kicad_cli()) == "9.0"


@pytest.mark.parametrize("versions,first", [
    (["9.0", "10.0", "8.0"], "10.0"),
    (["9.0"], "9.0"),
    (["nightly"], "nightly"),
])
def test_3d_model_roots_start_from_the_newest_version(tmp_path, monkeypatch, versions, first):
    base = tmp_path / "KiCad"
    for version in versions:
        (base / version).mkdir(parents=True)
    monkeypatch.delenv(kicad_models.LIBRARY_ENV, raising=False)
    monkeypatch.setattr(kicad_models, "_INSTALL_BASES", (str(base),))
    monkeypatch.setattr(kicad_models.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    roots = [r for r in kicad_models._candidate_roots() if str(r).startswith(str(base))]
    assert [r.parent.parent.parent.name for r in roots][0] == first
    assert len(roots) == len(versions)
