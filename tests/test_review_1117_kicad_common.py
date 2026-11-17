"""@brief KiCad's 3D library is also found through «Configure Paths» (kicad_common.json).

Review 2026-11-17: the search read the path variables from the process
environment only, while KiCad stores the ones the user sets in «Configure
Paths» in ``<config>/<major.minor>/kicad_common.json`` (``environment.vars``).
No test here needs KiCad: the settings folder, the program roots, the
registry and the Documents folder are all faked under ``tmp_path``.
"""

import json
import os
from pathlib import Path

import pytest

from kicadgen.cad3d import kicad_models as km


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """@brief A machine without KiCad whose settings home is ``tmp_path/config``."""
    from kicadgen import app_paths
    from kicadgen.services import installed_programs
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith("KICAD") and ("3DMODEL" in upper or "3RD_PARTY" in upper):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(km, "_INSTALL_BASES", ())
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [])
    monkeypatch.setattr(installed_programs, "registered_installs", lambda name: [])
    monkeypatch.setattr(app_paths, "_documents_dir", lambda: tmp_path / "nodocs")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path


def _library(folder):
    """@brief Create a folder that looks like a 3D library (one ``*.3dshapes``)."""
    (folder / "Package_SO.3dshapes").mkdir(parents=True)
    return folder


def _settings(machine, version, content, home="config"):
    """@brief Write ``<home>/<version>/kicad_common.json``; `content` is a dict or raw text/bytes."""
    folder = machine / home / version
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / km.KICAD_COMMON_FILENAME
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")
    return path


def _vars(**variables):
    """@brief A kicad_common.json document with the given path variables."""
    return {"environment": {"vars": variables}}


def test_a_path_set_in_configure_paths_names_the_library(machine):
    """@brief Positive: KICAD10_3DMODEL_DIR in 10.0's file; negative: the same file without variables."""
    library = _library(machine / "D" / "libs" / "3d")
    _settings(machine, "10.0", {"environment": {"vars": None}})
    assert km.find_library() is None
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(library)))
    assert km.find_library() == library


def test_the_default_settings_home_is_appdata_and_config_home_replaces_it(machine, monkeypatch):
    """@brief Positive: %APPDATA%/kicad/<ver> on Windows; negative: ignored once KICAD_CONFIG_HOME is set."""
    library = _library(machine / "moved")
    _settings(machine, "9.0", _vars(KICAD9_3DMODEL_DIR=str(library)), home="roaming/kicad")
    monkeypatch.setattr(km.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(machine / "roaming"))
    # The fixture's KICAD_CONFIG_HOME points at an empty folder: as in KiCad,
    # it is the only settings home while it is set.
    assert km.find_library() is None
    monkeypatch.delenv("KICAD_CONFIG_HOME")
    assert km.find_library() == library


def test_the_process_environment_overrides_the_file(machine, monkeypatch):
    """@brief Positive: the same variable in the environment replaces the stored one.

    Negative: with the environment variable gone the stored path is used
    again; a blank environment value does not override.
    """
    stored = _library(machine / "stored")
    live = _library(machine / "live")
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(stored)))
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(live))
    assert km.find_library() == live
    assert stored not in list(km._candidate_roots()), "an overridden variable is dropped, as in KiCad"
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", "  ")
    assert km.find_library() == stored
    monkeypatch.delenv("KICAD10_3DMODEL_DIR")
    assert km.find_library() == stored


def test_a_stored_path_outranks_the_default_install_of_the_same_kicad(machine, monkeypatch):
    """@brief Positive: the file beats the program root AND a registry entry declaring a patch number.

    Negative: the default paths stay in the list after it, and an installed
    KiCad of a newer major still comes first.
    """
    from kicadgen.services import installed_programs
    stored = _library(machine / "stored")
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(stored)))
    root = machine / "PF"
    default = _library(root / "KiCad" / "10.0" / "share" / "kicad" / "3dmodels")
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [root])
    monkeypatch.setattr(installed_programs, "registered_installs",
                        lambda name: [(root / "KiCad" / "10.0" / "bin", "10.0.1")])
    assert km._search_roots(None)[:2] == [stored, default]
    eleven = _library(root / "KiCad" / "11.0" / "share" / "kicad" / "3dmodels")
    assert km._search_roots(None)[:3] == [eleven, stored, default]


def test_an_environment_variable_outranks_a_patch_release_in_the_registry(machine, monkeypatch):
    """@brief Positive: KICAD10 in the environment beats the registered 10.0.1 default.

    Negative: the nightly 10.99, which reads KICAD11 variables, still comes
    before a KICAD10 variable.
    """
    from kicadgen.services import installed_programs
    live = _library(machine / "live")
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(live))
    install = machine / "PF" / "KiCad" / "10.0"
    default = _library(install / "share" / "kicad" / "3dmodels")
    monkeypatch.setattr(installed_programs, "registered_installs",
                        lambda name: [(install, "10.0.1")])
    assert km._search_roots(None)[:2] == [live, default]
    nightly = _library(machine / "N" / "KiCad" / "10.99" / "share" / "kicad" / "3dmodels")
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [machine / "N"])
    assert km._search_roots(None)[:3] == [nightly, live, default]


def test_versions_stay_ordered_across_settings_folders(machine):
    """@brief Positive: 10.0's stored path before 9.0's; negative: removing 10.0's leaves 9.0's first."""
    nine = _library(machine / "nine")
    ten = _library(machine / "ten")
    _settings(machine, "9.0", _vars(KICAD9_3DMODEL_DIR=str(nine)))
    ten_file = _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(ten)))
    assert km._search_roots(None)[:2] == [ten, nine]
    ten_file.unlink()
    assert km._search_roots(None) == [nine]


def test_a_variable_of_another_major_in_a_settings_folder_is_not_used(machine):
    """@brief Negative: KICAD9_3DMODEL_DIR left in 10.0's file; positive: the same value under KICAD10."""
    library = _library(machine / "library")
    _settings(machine, "10.0", _vars(KICAD9_3DMODEL_DIR=str(library),
                                     KICAD10_SYMBOL_DIR=str(library)))
    assert km.find_library() is None
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(library)))
    assert km.find_library() == library


def test_a_nightly_settings_folder_reads_the_next_major(machine):
    """@brief Positive: 9.99 reads KICAD10_3DMODEL_DIR; negative: KICAD9_3DMODEL_DIR there is not its variable."""
    library = _library(machine / "library")
    _settings(machine, "9.99", _vars(KICAD9_3DMODEL_DIR=str(library)))
    assert km.find_library() is None
    _settings(machine, "9.99", _vars(KICAD10_3DMODEL_DIR=str(library)))
    assert km.find_library() == library


def test_the_unversioned_variable_in_the_file_ranks_below_the_versioned_one(machine, monkeypatch):
    """@brief Positive: KICAD_3DMODEL_DIR stored alone is used; with the versioned one it comes second.

    Negative: the program's own override in the environment (same name)
    replaces the stored one.
    """
    plain = _library(machine / "plain")
    versioned = _library(machine / "versioned")
    _settings(machine, "10.0", _vars(KICAD_3DMODEL_DIR=str(plain)))
    assert km.find_library() == plain
    _settings(machine, "10.0", _vars(KICAD_3DMODEL_DIR=str(plain),
                                     KICAD10_3DMODEL_DIR=str(versioned)))
    assert km._search_roots(None) == [versioned, plain]
    explicit = _library(machine / "explicit")
    monkeypatch.setenv(km.LIBRARY_ENV, str(explicit))
    assert km._search_roots(None) == [explicit, versioned]


@pytest.mark.parametrize("content", [
    "{ not json",
    b"\xff\xfe\x00broken",
    "[]",
    "null",
    json.dumps({"environment": None}),
    json.dumps({"environment": []}),
    json.dumps({"environment": {"vars": ["KICAD10_3DMODEL_DIR"]}}),
    json.dumps({"environment": {"vars": "KICAD10_3DMODEL_DIR"}}),
    json.dumps({"environment": {"vars": {"KICAD10_3DMODEL_DIR": 7}}}),
    json.dumps({"environment": {"vars": {"KICAD10_3DMODEL_DIR": None}}}),
    json.dumps({"environment": {"vars": {"KICAD10_3DMODEL_DIR": ["D:/libs"]}}}),
    json.dumps({"environment": {"vars": {"KICAD10_3DMODEL_DIR": "   "}}}),
    json.dumps({"environment": {"vars": {"KICAD10_3DMODEL_DIR": "${OTHER}/3d"}}}),
    json.dumps({"vars": {"KICAD10_3DMODEL_DIR": "D:/libs"}}),
])
def test_an_unusable_settings_file_is_skipped_without_raising(machine, content):
    """@brief Negative: every broken shape gives no library; positive: the good 9.0 beside it still counts."""
    _settings(machine, "10.0", content)
    assert km.find_library() is None
    assert list(km._settings_model_folders()) == []
    good = _library(machine / "good")
    _settings(machine, "9.0", _vars(KICAD9_3DMODEL_DIR=str(good)))
    assert km.find_library() == good


def test_a_stored_path_to_a_missing_folder_and_odd_settings_folders_are_skipped(machine):
    """@brief Negative: a path that does not exist, a folder named «nightly», a file where a folder is expected.

    Positive: the next candidate is still found.
    """
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(machine / "gone")))
    library = _library(machine / "library")
    _settings(machine, "nightly", _vars(KICAD10_3DMODEL_DIR=str(library)))
    (machine / "config" / "8.0").write_text("not a folder", encoding="utf-8")
    (machine / "config" / "7.0").mkdir()
    (machine / "config" / "7.0" / km.KICAD_COMMON_FILENAME).mkdir()
    assert km.find_library() is None
    assert km._search_roots(None) == []
    _settings(machine, "9.0", _vars(KICAD9_3DMODEL_DIR=str(library)))
    assert km.find_library() == library


def test_no_settings_home_at_all_is_not_an_error(machine, monkeypatch):
    """@brief Negative: a settings home that does not exist; positive: an oversized file is not parsed."""
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(machine / "absent"))
    assert km._settings_model_folders() == []
    assert km.find_library() is None
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(machine / "config"))
    library = _library(machine / "library")
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(library)))
    assert km.find_library() == library
    monkeypatch.setattr(km, "_KICAD_COMMON_MAX_BYTES", 8)
    assert km.find_library() is None


def test_the_settings_are_read_once_per_listing_scope(machine, monkeypatch):
    """@brief Positive: one read inside a scope; negative: outside a scope every search reads again."""
    library = _library(machine / "library")
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(library)))
    reads = []
    real = km._settings_path_variables
    monkeypatch.setattr(km, "_settings_path_variables",
                        lambda folder: reads.append(folder) or real(folder))
    with km.listing_scope():
        assert km.find_library() == library
        assert km.find_library() == library
    assert len(reads) == 1
    km.find_library()
    km.find_library()
    assert len(reads) == 3


# ── Second round: footprints from their own source, stale settings, slow probes, absolute paths ──

def _install(machine, monkeypatch, version="10.0", footprints=True, root="PF"):
    """@brief Fake an installed KiCad under a program root; returns (3D library, footprints folder)."""
    from kicadgen.services import installed_programs
    share = machine / root / "KiCad" / version / "share" / "kicad"
    library = _library(share / "3dmodels")
    if footprints:
        (share / "footprints" / "Package_SO.pretty").mkdir(parents=True)
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [machine / root])
    return library, share / "footprints"


def _pretty(folder):
    """@brief Create a folder that looks like a footprint root (one ``*.pretty``)."""
    (folder / "Package_SO.pretty").mkdir(parents=True)
    return folder


def test_a_moved_3d_library_does_not_hide_the_installed_footprints(machine, monkeypatch):
    """@brief Positive: 3D library moved to a folder without footprints, the installed ones still found.

    Negative: an arbitrary folder that is not this machine's library gets
    no footprints from the installed KiCad; without footprints anywhere the
    answer is None.
    """
    from kicadgen.services import library_footprints
    assert km.footprints_root() is None
    assert library_footprints.footprints_root() is None
    default, footprints = _install(machine, monkeypatch)
    moved = _library(machine / "D" / "libs" / "3d")
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(moved)))
    assert km.find_library() == moved
    assert not (moved.parent / "footprints").exists()
    assert km.footprints_root() == footprints
    assert km.footprints_root(moved) == footprints
    assert library_footprints.footprints_root() == footprints
    stranger = _library(machine / "stranger" / "3dmodels")
    assert km.footprints_root(stranger) is None
    assert km.footprints_root(default) == footprints, "the sibling folder of an installation"


def test_footprints_without_any_3d_library_are_still_found(machine, monkeypatch):
    """@brief Positive: KiCad installed without its 3D models; negative: a footprints folder with no library in it."""
    from kicadgen.services import installed_programs
    share = machine / "PF" / "KiCad" / "10.0" / "share" / "kicad"
    (share / "footprints").mkdir(parents=True)
    monkeypatch.setattr(installed_programs, "program_roots", lambda: [machine / "PF"])
    assert km.find_library() is None
    assert km.footprints_root() is None
    _pretty(share / "footprints")
    assert km.footprints_root() == share / "footprints"


def test_the_footprint_variable_names_the_footprints(machine, monkeypatch):
    """@brief Positive: KICAD10_FOOTPRINT_DIR stored, then in the environment, beats the installed folder.

    Negative: the variable of another major stored in 10.0's file, and a
    variable naming a folder without libraries, change nothing.
    """
    _default, installed = _install(machine, monkeypatch)
    stored = _pretty(machine / "stored-fp")
    _settings(machine, "10.0", _vars(KICAD9_FOOTPRINT_DIR=str(stored),
                                     KICAD10_FOOTPRINT_DIR=str(machine / "empty-fp")))
    assert km.footprints_root() == installed
    _settings(machine, "10.0", _vars(KICAD10_FOOTPRINT_DIR=str(stored)))
    assert km.footprints_root() == stored
    live = _pretty(machine / "live-fp")
    monkeypatch.setenv("KICAD10_FOOTPRINT_DIR", str(live))
    assert km.footprints_root() == live
    assert stored not in list(km._candidate_footprint_roots())


def test_a_moved_share_tree_brings_its_own_footprints(machine, monkeypatch):
    """@brief Positive: footprints beside the moved 3D library before the installed ones.

    Negative: a stored footprint variable still comes before that sibling.
    """
    _default, installed = _install(machine, monkeypatch)
    moved = _library(machine / "moved" / "3dmodels")
    beside = _pretty(machine / "moved" / "footprints")
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(moved)))
    assert km.footprints_root() == beside
    declared = _pretty(machine / "declared-fp")
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(moved),
                                     KICAD10_FOOTPRINT_DIR=str(declared)))
    assert list(km._candidate_footprint_roots())[:3] == [declared, beside, installed]


def test_a_stale_settings_folder_does_not_beat_an_installed_kicad(machine, monkeypatch):
    """@brief Negative: the path stored for an uninstalled KiCad comes after the installed libraries.

    The installed KiCad has no readable version (folder «current») or is an
    older major than the leftover settings folder. Positive: once a KiCad
    of the stored variable's major is installed, the stored path ranks with
    it and wins.
    """
    stale = _library(machine / "stale")
    _settings(machine, "11.0", _vars(KICAD11_3DMODEL_DIR=str(stale)))
    unversioned, _fp = _install(machine, monkeypatch, version="current", footprints=False)
    assert km._search_roots(None) == [unversioned, stale]
    ten, _fp = _install(machine, monkeypatch, version="10.0", footprints=False)
    assert km._search_roots(None) == [ten, unversioned, stale]
    eleven, _fp = _install(machine, monkeypatch, version="11.0", footprints=False)
    assert km._search_roots(None) == [stale, eleven, ten, unversioned]


def test_a_stale_footprint_variable_is_a_last_resort_too(machine, monkeypatch):
    """@brief Negative: KICAD9_FOOTPRINT_DIR of an uninstalled KiCad 9 after the installed one; positive: used alone."""
    stale = _pretty(machine / "stale-fp")
    _settings(machine, "9.0", _vars(KICAD9_FOOTPRINT_DIR=str(stale)))
    assert km.footprints_root() == stale
    _default, installed = _install(machine, monkeypatch, version="current")
    assert list(km._candidate_footprint_roots())[0] == installed
    assert list(km._candidate_footprint_roots())[-1] == stale


def _count_probes(monkeypatch, target):
    """@brief Count `Path.is_dir` calls on `target`; returns the list of calls."""
    calls = []
    real = Path.is_dir

    def is_dir(self, *args, **kwargs):
        """Record a probe of the watched folder, then answer as the real method."""
        if os.path.normcase(str(self)) == os.path.normcase(str(target)):
            calls.append(self)
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, "is_dir", is_dir)
    return calls


def test_a_slow_failed_probe_is_paid_once_per_session(machine, monkeypatch):
    """@brief Positive: a folder whose probe fails slowly is probed once; negative: `forget` probes it again."""
    share = machine / "share-down" / "3d"
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(share)))
    calls = _count_probes(monkeypatch, share)
    # Every failed probe counts as slow: the clock is not what is under test.
    monkeypatch.setattr(km, "_SLOW_PROBE_S", 0.0)
    for _ in range(4):
        assert km.find_library() is None
        assert km._search_roots(None) == []
    assert len(calls) == 1
    km.forget_unreachable_roots()
    _library(share)
    assert km.find_library() == share
    assert len(calls) == 2


def test_a_fast_failure_and_a_success_are_never_remembered(machine, monkeypatch):
    """@brief Negative: a missing local folder is probed at every search and found once it exists."""
    later = machine / "later" / "3d"
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(later)))
    calls = _count_probes(monkeypatch, later)
    assert km.find_library() is None
    assert km.find_library() is None
    assert len(calls) == 2
    _library(later)
    assert km.find_library() == later
    monkeypatch.setattr(km, "_SLOW_PROBE_S", 0.0)
    assert km.find_library() == later, "a success is not remembered as unreachable"
    assert len(calls) == 4


def test_an_unreachable_folder_is_probed_again_after_the_retry_budget(machine, monkeypatch, caplog):
    """@brief Positive: past `_UNREACHABLE_RETRY_S` the folder is probed again and, back, it is found.

    Negative: within the budget the slow probe is not repeated, even though
    the folder is back; a second slow failure of the same outage is not
    logged again.
    """
    share = machine / "nas" / "3d"
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(share)))
    calls = _count_probes(monkeypatch, share)
    now = [1000.0]
    monkeypatch.setattr(km, "_monotonic", lambda: now[0])
    # Every failed probe counts as slow: the fake clock does not advance inside a probe.
    monkeypatch.setattr(km, "_SLOW_PROBE_S", 0.0)
    caplog.set_level("WARNING", logger=km.__name__)
    assert km.find_library() is None
    assert len(calls) == 1
    now[0] += km._UNREACHABLE_RETRY_S + 1
    assert km.find_library() is None, "still down: probed again, remembered again"
    assert len(calls) == 2
    warnings = [record for record in caplog.records if str(share) in record.getMessage()]
    assert len(warnings) == 1, "one outage, one line"
    _library(share)
    now[0] += km._UNREACHABLE_RETRY_S - 1
    assert km.find_library() is None, "within the budget the share is not touched"
    assert len(calls) == 2
    now[0] += 2
    assert km.find_library() == share
    assert len(calls) == 3
    remembered = os.path.normcase(os.path.normpath(str(share)))
    assert remembered not in km._UNREACHABLE_ROOTS, "a folder that answered is forgotten"


@pytest.mark.parametrize("stored", ["libs/3d", "./libs/3d", "~/libs/3d", "$HOME/libs/3d",
                                    "%USERPROFILE%/libs/3d", "libs"])
def test_only_absolute_stored_paths_are_accepted(machine, monkeypatch, stored):
    """@brief Negative: a relative or unexpanded value is not resolved against the working folder.

    Positive: the same library named by its absolute path is found.
    """
    work = machine / "work"
    for prefix in (work, work / "~", work / "$HOME", work / "%USERPROFILE%",
                   machine / "home"):
        _library(prefix / "libs" / "3d")
    _library(work / "libs")
    monkeypatch.chdir(work)
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=stored))
    assert Path(stored).is_dir(), "the literal value does name a library from the working folder"
    assert km._settings_model_folders() == []
    assert km.find_library() is None
    absolute = work / stored
    _settings(machine, "10.0", _vars(KICAD10_3DMODEL_DIR=str(absolute)))
    assert km.find_library() == absolute
