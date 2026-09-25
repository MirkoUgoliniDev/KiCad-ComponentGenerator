"""@brief Verify the rollback, table-conflict and preview-safety rules of fix F.

@details Every test writes only below pytest's temporary directory; the real
KiCad configuration is never touched (global tables are explicit paths).
"""

from pathlib import Path

import pytest

from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import (
    TABLE_BACKUP_RETENTION,
    KiCadLibraryError,
    create_preview_project,
    discover_component_artifacts,
    find_library_entry,
    global_fp_lib_table_path,
    install_component_library,
    kicad_lock_file,
    update_fp_lib_table,
)


FOOTPRINT = """(footprint "Demo"
\t(version 20260206)
\t(generator "kicadgen_tool")
\t(layer "F.Cu")
\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""
PROJECT_URI = "${KIPRJMOD}/KiCadGen.pretty"


def _artifacts(tmp_path: Path, footprint: str = FOOTPRINT):
    """Create one generated component with footprint, STEP and WRL."""
    component = tmp_path / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.3dshapes").mkdir()
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(
        footprint, encoding="utf-8")
    (component / "Demo.3dshapes" / "Demo.step").write_bytes(b"STEP")
    (component / "Demo.3dshapes" / "Demo.wrl").write_bytes(b"WRL")
    return discover_component_artifacts(component)


def _table(path: Path, *rows: str) -> Path:
    """Write a footprint table with the given raw ``lib`` rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"  {row}\n" for row in rows)
    path.write_text(f"(fp_lib_table\n  (version 7)\n{body})\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- F1 rollback

def _installed_with_old_files(tmp_path: Path):
    """Install once, then mark the installed files as the user's old copies."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    first = install_component_library(artifacts, destination)
    first.footprint_path.write_text("old footprint", encoding="utf-8")
    first.step_path.write_bytes(b"OLD STEP")
    first.wrl_path.write_bytes(b"OLD WRL")
    return artifacts, destination, first


def test_failed_restore_keeps_backups_and_runs_every_other_step(
        tmp_path, monkeypatch):
    """@brief A restore failure must not destroy the only copy of old files."""
    artifacts, destination, first = _installed_with_old_files(tmp_path)
    table_before = first.table_path.read_text(encoding="utf-8")

    def fail_table_update(*_args, **_kwargs):
        raise OSError("simulated table failure")

    real_restore = kicad_library._restore_file

    def restore_all_but_step(backup, target):
        if Path(target) == first.step_path:
            raise OSError("simulated locked STEP")
        return real_restore(backup, target)

    monkeypatch.setattr(kicad_library, "update_fp_lib_table", fail_table_update)
    monkeypatch.setattr(kicad_library, "_restore_file", restore_all_but_step)
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, destination, overwrite=True)

    error = caught.value
    assert error.code == "rollback_incomplete"
    assert isinstance(error.__cause__, OSError)
    rollback_dir = Path(error.context["rollback_dir"])
    assert rollback_dir.is_dir(), "the backups must survive a failed restore"
    assert str(rollback_dir) in str(error)
    assert str(first.step_path) in error.context["failures"]
    kept = {path.read_bytes() for path in rollback_dir.iterdir()}
    assert b"OLD STEP" in kept
    # The steps after the failing one still ran.
    assert first.footprint_path.read_text(encoding="utf-8") == "old footprint"
    assert first.wrl_path.read_bytes() == b"OLD WRL"
    assert first.table_path.read_text(encoding="utf-8") == table_before


def test_complete_rollback_reraises_original_error_and_cleans_up(
        tmp_path, monkeypatch):
    """@brief Negative case: nothing fails in the rollback, nothing is kept."""
    artifacts, destination, first = _installed_with_old_files(tmp_path)

    def fail_table_update(*_args, **_kwargs):
        raise OSError("simulated table failure")

    monkeypatch.setattr(kicad_library, "update_fp_lib_table", fail_table_update)
    with pytest.raises(OSError, match="simulated table failure") as caught:
        install_component_library(artifacts, destination, overwrite=True)
    assert not isinstance(caught.value, KiCadLibraryError)
    assert not list(destination.glob(".kicadgen-rollback-*"))
    assert first.step_path.read_bytes() == b"OLD STEP"


# ------------------------------------------------- F2/F3 existing table rows

def test_install_refuses_a_row_pointing_elsewhere_before_writing(tmp_path):
    """@brief Same nickname, other URI: refuse and name both locations."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    table = _table(
        destination / "fp-lib-table",
        '(lib (name "KiCadGen")(type "KiCad")(uri "/shared/KiCadGen.pretty")'
        '(options "")(descr "Team library"))',
    )
    before = table.read_text(encoding="utf-8")
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, destination)
    assert caught.value.code == "library_entry_conflict"
    assert caught.value.context["old_uri"] == "/shared/KiCadGen.pretty"
    assert caught.value.context["new_uri"] == PROJECT_URI
    assert table.read_text(encoding="utf-8") == before
    assert not (destination / "KiCadGen.pretty").exists()
    assert not (destination / "KiCadGen.3dshapes").exists()


def test_explicit_replacement_repoints_only_type_and_uri(tmp_path):
    """@brief With confirmation the row is repointed, flags and descr kept."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    table = _table(
        destination / "fp-lib-table",
        '(lib (name "Other")(type "KiCad")(uri "/o.pretty")(options "")(descr ""))',
        '(lib (name "KiCadGen")(type "Legacy")(uri "/shared/KiCadGen.pretty")'
        '(options "keep=1")(descr "Team library")(hidden))',
    )
    install_component_library(artifacts, destination, replace_library_entry=True)
    entry = find_library_entry(table, "KiCadGen")
    assert entry.uri == PROJECT_URI and entry.library_type == "KiCad"
    assert entry.hidden
    text = table.read_text(encoding="utf-8")
    assert '(options "keep=1")' in text and '(descr "Team library")' in text
    assert find_library_entry(table, "Other").uri == "/o.pretty"


def test_same_uri_keeps_the_row_verbatim_with_flags(tmp_path):
    """@brief Unchanged URI: the user's options and disabled flag survive."""
    table = _table(
        tmp_path / "fp-lib-table",
        f'(lib (name "KiCadGen")(type "KiCad")(uri "{PROJECT_URI}")'
        '(options "x")(descr "mine")(disabled))',
    )
    before = table.read_text(encoding="utf-8")
    assert not update_fp_lib_table(table, "KiCadGen", PROJECT_URI)
    assert table.read_text(encoding="utf-8") == before
    assert find_library_entry(table, "KiCadGen").disabled


def test_same_global_path_with_other_separators_is_not_a_conflict(tmp_path):
    """@brief Backslashes and a trailing slash spell the same directory."""
    library = (tmp_path / "Libs" / "KiCadGen.pretty").as_posix()
    written = library.replace("/", "\\") + "\\"
    table = _table(
        tmp_path / "fp-lib-table",
        f'(lib (name "KiCadGen")(type "KiCad")(uri "{written.replace(chr(92), chr(92) * 2)}")'
        '(options "")(descr ""))',
    )
    assert find_library_entry(table, "KiCadGen").uri == written
    assert not update_fp_lib_table(table, "KiCadGen", library)


def test_unquoted_nickname_is_recognised_and_not_duplicated(tmp_path):
    """@brief ``(name KiCadGen)`` is the same row as ``(name "KiCadGen")``."""
    table = _table(
        tmp_path / "fp-lib-table",
        f'(lib (name KiCadGen)(type KiCad)(uri "{PROJECT_URI}")(options "")(descr ""))',
    )
    assert not update_fp_lib_table(table, "KiCadGen", PROJECT_URI)
    with pytest.raises(KiCadLibraryError) as caught:
        update_fp_lib_table(table, "KiCadGen", "/elsewhere.pretty")
    assert caught.value.code == "library_entry_conflict"


def test_similar_names_and_descriptions_do_not_match(tmp_path):
    """@brief Negative case: only the exact nickname atom identifies a row."""
    table = _table(
        tmp_path / "fp-lib-table",
        '(lib (name "KiCadGen2")(type "KiCad")(uri "/a.pretty")(options "")'
        '(descr "(name \\"KiCadGen\\")"))',
        '(lib (name "Ki\\"Cad")(type "KiCad")(uri "/b.pretty")(options "")(descr ""))',
    )
    assert update_fp_lib_table(table, "KiCadGen", PROJECT_URI)
    assert find_library_entry(table, "KiCadGen").uri == PROJECT_URI
    assert find_library_entry(table, "KiCadGen2").uri == "/a.pretty"
    assert find_library_entry(table, 'Ki"Cad').uri == "/b.pretty"


def test_duplicate_nickname_is_refused_without_writing(tmp_path):
    """@brief An ambiguous table is never edited."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    table = _table(
        destination / "fp-lib-table",
        f'(lib (name "KiCadGen")(type "KiCad")(uri "{PROJECT_URI}")(options "")(descr ""))',
        f'(lib (name KiCadGen)(type "KiCad")(uri "{PROJECT_URI}")(options "")(descr ""))',
    )
    before = table.read_text(encoding="utf-8")
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, destination)
    assert caught.value.code == "duplicate_library_entry"
    assert table.read_text(encoding="utf-8") == before
    assert not (destination / "KiCadGen.pretty").exists()


# ------------------------------------------------------ F4 missing global table

def test_global_install_refuses_a_missing_global_table(tmp_path):
    """@brief KiCad must create its own global table before we add a row."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "GlobalLibraries"
    table = tmp_path / "config" / "fp-lib-table"
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(
            artifacts, destination, project_library=False, table_path=table)
    assert caught.value.code == "global_table_missing"
    assert Path(caught.value.context["path"]) == table
    assert not table.exists() and not table.parent.exists()
    assert not destination.exists()


def test_project_install_still_creates_a_missing_project_table(tmp_path):
    """@brief Negative case: a project table is ours to create."""
    artifacts = _artifacts(tmp_path)
    result = install_component_library(artifacts, tmp_path / "Project")
    assert find_library_entry(result.table_path, "KiCadGen").uri == PROJECT_URI


# ------------------------------------------------- F5 temporary files/folders

def test_failed_table_replace_leaves_no_temp_file_or_new_folders(
        tmp_path, monkeypatch):
    """@brief A failed atomic replace removes the temp and the empty folders."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    real_replace = kicad_library.os.replace

    def fail_table_replace(source, target):
        if Path(target).name == "fp-lib-table":
            raise OSError("simulated replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(kicad_library.os, "replace", fail_table_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        install_component_library(artifacts, destination)
    assert not destination.exists(), "every folder the install created is gone"


def test_failed_install_keeps_folders_that_already_held_files(
        tmp_path, monkeypatch):
    """@brief Negative case: pre-existing library folders are never removed."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    other = destination / "KiCadGen.pretty" / "Other.kicad_mod"
    other.parent.mkdir(parents=True)
    other.write_text("keep", encoding="utf-8")
    real_replace = kicad_library.os.replace

    def fail_table_replace(source, target):
        if Path(target).name == "fp-lib-table":
            raise OSError("simulated replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(kicad_library.os, "replace", fail_table_replace)
    with pytest.raises(OSError):
        install_component_library(artifacts, destination)
    assert other.read_text(encoding="utf-8") == "keep"
    assert not (destination / "KiCadGen.pretty" / "Demo.kicad_mod").exists()
    assert not list(destination.rglob("*.kicadgen.tmp"))
    assert not (destination / "KiCadGen.3dshapes").exists()


# ------------------------------------------------------------ F6 table backups

def test_each_changing_write_refreshes_the_backup_with_bounded_history(tmp_path):
    """@brief The backup is the state before the latest write, not the first."""
    table = _table(tmp_path / "fp-lib-table")
    states = []
    for index in range(TABLE_BACKUP_RETENTION + 2):
        states.append(table.read_text(encoding="utf-8"))
        assert update_fp_lib_table(table, f"Lib{index}", f"/lib{index}.pretty")
    backup = table.with_name("fp-lib-table.kicadgen-backup")
    assert backup.read_text(encoding="utf-8") == states[-1]
    assert Path(f"{backup}.1").read_text(encoding="utf-8") == states[-2]
    kept = sorted(tmp_path.glob("fp-lib-table.kicadgen-backup*"))
    assert len(kept) == TABLE_BACKUP_RETENTION
    assert not list(tmp_path.glob("*.tmp"))


def test_unchanged_write_does_not_rotate_backups(tmp_path):
    """@brief Negative case: a no-op update leaves the backups alone."""
    table = _table(tmp_path / "fp-lib-table")
    assert update_fp_lib_table(table, "KiCadGen", PROJECT_URI)
    backup = table.with_name("fp-lib-table.kicadgen-backup")
    first_backup = backup.read_text(encoding="utf-8")
    assert not update_fp_lib_table(table, "KiCadGen", PROJECT_URI)
    assert backup.read_text(encoding="utf-8") == first_backup
    assert not Path(f"{backup}.1").exists()


# ------------------------------------------------------ F7 KICAD_CONFIG_HOME

def test_global_table_path_honours_kicad_config_home(tmp_path):
    """@brief KiCad appends only the version to KICAD_CONFIG_HOME."""
    executable = Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe")
    override = tmp_path / "portable-config"
    result = global_fp_lib_table_path(
        executable,
        environ={"KICAD_CONFIG_HOME": str(override), "APPDATA": str(tmp_path)},
        platform="win32")
    assert result == override / "10.0" / "fp-lib-table"


def test_empty_kicad_config_home_falls_back_to_the_platform_default(tmp_path):
    """@brief Negative case: an empty variable is ignored, as KiCad does."""
    executable = Path("/usr/lib/kicad/9.0/bin/kicad-cli")
    result = global_fp_lib_table_path(
        executable,
        environ={"KICAD_CONFIG_HOME": " ", "XDG_CONFIG_HOME": str(tmp_path)},
        platform="linux")
    assert result == tmp_path / "kicad" / "9.0" / "fp-lib-table"


# ---------------------------------------------------------- F8 preview safety

def test_preview_refuses_a_board_open_in_kicad(tmp_path):
    """@brief KiCad's lock file blocks the refresh; nothing is rewritten."""
    artifacts = _artifacts(tmp_path)
    preview = create_preview_project(artifacts)
    board_before = preview.board_path.read_text(encoding="utf-8")
    lock = kicad_lock_file(preview.board_path)
    assert lock.name == f"~{preview.board_path.name}.lck"
    lock.write_text("user@host", encoding="utf-8")
    artifacts.step_path.write_bytes(b"NEW STEP")
    with pytest.raises(KiCadLibraryError) as caught:
        create_preview_project(artifacts)
    assert caught.value.code == "preview_locked"
    assert Path(caught.value.context["path"]) == lock
    assert preview.board_path.read_text(encoding="utf-8") == board_before
    assert (preview.directory / "Demo.step").read_bytes() == b"STEP"
    lock.unlink()
    create_preview_project(artifacts)
    assert (preview.directory / "Demo.step").read_bytes() == b"NEW STEP"
    assert not list(preview.directory.glob("*.tmp"))


def test_preview_validates_the_footprint_before_copying_anything(tmp_path):
    """@brief A malformed footprint leaves no preview folder or STEP copy."""
    artifacts = _artifacts(tmp_path, footprint="(footprint \"Demo\"\n")
    with pytest.raises(KiCadLibraryError):
        create_preview_project(artifacts)
    assert not (artifacts.component_dir / ".kicad-preview").exists()
