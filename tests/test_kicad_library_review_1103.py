"""@brief Regression tests of the KiCad delivery fixes of review 2026-11-03.

@details Cross-scope shadowing of the «KiCadGen» nickname, KiCad holding the
destination project open, the minimum KiCad version derived from the file
format, the export history that a truncated file used to wipe, options reset
on a type change, disposable rollback folders, rollback steps that raise the
module's own error, mapped drives kept as typed, preview isolation and the
shared S-expression quoter. Every table is a copy in a temporary folder; the
real KiCad configuration is never read or written.
"""

import json
import os
import pathlib
from pathlib import Path

import pytest

from kicadgen.core.footprint import GENERATOR_VERSION
from kicadgen.kicad_versions import version_at_least
from kicadgen.services import kicad_export_history as history
from kicadgen.services import kicad_library
from kicadgen.services import system_dependencies
from kicadgen.services.kicad_library import (
    GlobalTableLocation,
    KiCadLibraryError,
    create_preview_project,
    discover_component_artifacts,
    find_library_entry,
    install_component_library,
    plan_component_install,
    update_fp_lib_table,
)


FOOTPRINT = """(footprint "Demo"
\t(version 20260206)
\t(generator "kicadgen_tool")
\t(layer "F.Cu")
\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""
STEP_BYTES = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
EMPTY_TABLE = "(fp_lib_table\n  (version 7)\n)\n"


def _artifacts(tmp_path: Path, *, wrl: bool = True):
    """Generated component with footprint, STEP and optionally a WRL."""
    component = tmp_path / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.3dshapes").mkdir()
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    (component / "Demo.3dshapes" / "Demo.step").write_bytes(STEP_BYTES)
    if wrl:
        (component / "Demo.3dshapes" / "Demo.wrl").write_bytes(b"WRL")
    return discover_component_artifacts(component)


def _table(path: Path, *rows: str) -> Path:
    """Write a footprint library table holding `rows`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("(fp_lib_table\n  (version 7)\n" + "".join(
        f"  {row}\n" for row in rows) + ")\n", encoding="utf-8")
    return path


# --- 1. the same nickname in the other scope ---------------------------------

def test_project_install_reports_a_global_row_pointing_elsewhere(tmp_path):
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "Board"
    global_table = _table(
        tmp_path / "cfg" / "fp-lib-table",
        '(lib (name "KiCadGen")(type "KiCad")(uri "D:/Libs/KiCadGen.pretty")(options "")(descr ""))')
    before = global_table.read_bytes()
    plan = plan_component_install(artifacts, project, other_table_path=global_table)
    assert plan.shadowing is not None
    assert plan.shadowing.scope == "global"
    assert plan.shadowing.uri == "D:/Libs/KiCadGen.pretty"
    assert plan.shadowing.table_path == global_table
    assert plan.shadowing_error is None
    # Only read: the other scope's table is never written.
    assert global_table.read_bytes() == before


def test_project_install_ignores_a_global_row_for_the_same_folder_or_disabled(tmp_path):
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "Board"
    same = (project / "KiCadGen.pretty").as_posix()
    same_table = _table(
        tmp_path / "a" / "fp-lib-table",
        f'(lib (name "KiCadGen")(type "KiCad")(uri "{same}")(options "")(descr ""))')
    disabled_table = _table(
        tmp_path / "b" / "fp-lib-table",
        '(lib (name "KiCadGen")(type "KiCad")(uri "D:/Elsewhere.pretty")(disabled))')
    other_name = _table(
        tmp_path / "c" / "fp-lib-table",
        '(lib (name "Other")(type "KiCad")(uri "D:/Elsewhere.pretty"))')
    for table in (same_table, disabled_table, other_name, tmp_path / "missing" / "fp-lib-table"):
        plan = plan_component_install(artifacts, project, other_table_path=table)
        assert plan.shadowing is None, table


def test_global_install_reports_the_project_row_under_kiprjmod(tmp_path):
    artifacts = _artifacts(tmp_path)
    library_root = tmp_path / "GlobalLibs"
    global_table = _table(tmp_path / "cfg" / "fp-lib-table")
    project_table = _table(
        tmp_path / "Board" / "fp-lib-table",
        '(lib (name "KiCadGen")(type "KiCad")(uri "${KIPRJMOD}/KiCadGen.pretty"))')
    plan = plan_component_install(
        artifacts, library_root, project_library=False, table_path=global_table,
        other_table_path=project_table)
    assert plan.shadowing is not None and plan.shadowing.scope == "project"
    # Negative: the global library IS the project's folder -> no clash.
    same = plan_component_install(
        artifacts, tmp_path / "Board", project_library=False, table_path=global_table,
        other_table_path=project_table)
    assert same.shadowing is None


def test_an_unreadable_other_table_does_not_block_the_plan(tmp_path):
    artifacts = _artifacts(tmp_path)
    broken = tmp_path / "cfg" / "fp-lib-table"
    broken.parent.mkdir()
    broken.write_text("not a table", encoding="utf-8")
    plan = plan_component_install(artifacts, tmp_path / "Board", other_table_path=broken)
    assert plan.shadowing is None
    assert plan.shadowing_error


# --- 2. KiCad holds the destination project open ------------------------------

def test_project_plan_lists_kicad_lock_files(tmp_path):
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "Board"
    project.mkdir()
    (project / "Board.kicad_pro").write_text("{}", encoding="utf-8")
    assert not plan_component_install(artifacts, project).kicad_open
    # A lock of an unrelated document is not KiCad holding the project.
    (project / "~notes.txt.lck").write_text("", encoding="utf-8")
    assert not plan_component_install(artifacts, project).kicad_open
    lock = kicad_library.kicad_lock_file(project / "Board.kicad_pcb")
    lock.write_text("", encoding="utf-8")
    plan = plan_component_install(artifacts, project)
    assert plan.kicad_open and plan.open_locks == (lock,)


def test_global_plan_does_not_look_for_project_locks(tmp_path):
    artifacts = _artifacts(tmp_path)
    root = tmp_path / "Libs"
    root.mkdir()
    kicad_library.kicad_lock_file(root / "X.kicad_sch").write_text("", encoding="utf-8")
    plan = plan_component_install(
        artifacts, root, project_library=False,
        table_path=_table(tmp_path / "cfg" / "fp-lib-table"))
    assert plan.open_locks == ()


# --- 3. minimum KiCad version -------------------------------------------------

def test_minimum_version_follows_the_format_constant():
    assert kicad_library.MINIMUM_KICAD_VERSION == ".".join(GENERATOR_VERSION.split(".")[:2])


def test_version_at_least_compares_numerically():
    assert version_at_least("10.0", "10.0") is True
    assert version_at_least("10.0.6", "10.0") is True
    assert version_at_least("10.99", "10.0") is True
    assert version_at_least("9.0", "10.0") is False
    assert version_at_least("9.99", "10.0") is False
    assert version_at_least("10", "10.0") is True
    assert version_at_least(None, "10.0") is None
    assert version_at_least("nightly", "10.0") is None


@pytest.mark.parametrize("output, supported", [
    ("9.0.7\n", False), ("10.0.1\n", True), ("10.99.0-12-gabc\n", True), (None, None)])
def test_check_kicad_version_with_a_fake_runner(output, supported):
    check = system_dependencies.check_kicad_version(
        "kicad-cli.exe", version_runner=lambda _path: output)
    assert check.supported is supported
    assert check.minimum == kicad_library.MINIMUM_KICAD_VERSION


def test_global_location_reports_whether_it_is_supported(tmp_path):
    def location(version):
        return kicad_library.resolve_global_fp_lib_table(
            tmp_path / "kicad-cli.exe", environ={"APPDATA": str(tmp_path)},
            platform="win32", version_runner=lambda _path: version)
    assert location("9.0.7").supported is False
    assert location("10.0.2").supported is True
    assert isinstance(location("10.0.2"), GlobalTableLocation)


def test_global_install_into_an_older_kicad_is_refused_before_any_write(tmp_path):
    artifacts = _artifacts(tmp_path)
    root = tmp_path / "Libs"
    table = _table(tmp_path / "cfg" / "9.0" / "fp-lib-table")
    before = table.read_bytes()
    plan = plan_component_install(
        artifacts, root, project_library=False, table_path=table, kicad_version="9.0")
    assert plan.kicad_too_old
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(
            artifacts, root, project_library=False, table_path=table,
            kicad_version="9.0")
    assert caught.value.code == "kicad_too_old"
    assert caught.value.context["minimum"] == kicad_library.MINIMUM_KICAD_VERSION
    assert table.read_bytes() == before
    assert not root.exists()
    # Positive: the supported version installs.
    result = install_component_library(
        artifacts, root, project_library=False, table_path=table,
        kicad_version="10.0")
    assert result.footprint_path.is_file()


# --- 4. export history ---------------------------------------------------------

def test_truncated_history_is_set_aside_not_overwritten(tmp_path):
    target = tmp_path / "kicad_exports.json"
    truncated = b'[{"component": "Old", "kind": "preview"'
    target.write_bytes(truncated)
    assert history.record_export(component="New", kind="preview", source="s",
                                 destination="d", path=target)
    kept = list(tmp_path.glob("kicad_exports.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == truncated
    records = json.loads(target.read_text(encoding="utf-8"))
    assert [record["component"] for record in records] == ["New"]


def test_history_is_not_written_when_the_bad_file_cannot_be_moved(tmp_path, monkeypatch):
    target = tmp_path / "kicad_exports.json"
    target.write_bytes(b"{ broken")

    def refuse(*_args):
        raise PermissionError("held open")
    monkeypatch.setattr(history.os, "rename", refuse)
    assert not history.record_export(component="New", kind="preview", source="s",
                                     destination="d", path=target)
    assert target.read_bytes() == b"{ broken"


def test_valid_history_keeps_its_records(tmp_path):
    target = tmp_path / "kicad_exports.json"
    target.write_text(json.dumps([{"component": "Old"}]), encoding="utf-8")
    assert history.record_export(component="New", kind="preview", source="s",
                                 destination="d", path=target)
    assert [r["component"] for r in history.load_history(target)] == ["New", "Old"]
    assert not list(tmp_path.glob("*.corrupt-*"))


# --- 5. repointing a row of another type ---------------------------------------

def test_repoint_of_another_type_clears_its_options(tmp_path):
    table = _table(tmp_path / "fp-lib-table",
                   '(lib (name "KiCadGen")(type "Legacy")(uri "/old.mod")'
                   '(options "legacy_key=1")(descr "Mine"))')
    with pytest.raises(KiCadLibraryError) as caught:
        update_fp_lib_table(table, "KiCadGen", "/new.pretty")
    assert caught.value.context["type_changes"] is True
    assert caught.value.context["old_type"] == "Legacy"
    update_fp_lib_table(table, "KiCadGen", "/new.pretty", replace_entry=True)
    text = table.read_text(encoding="utf-8")
    assert '(type "KiCad")' in text and '(options "")' in text
    assert "legacy_key" not in text and '(descr "Mine")' in text


def test_repoint_of_the_same_type_keeps_its_options(tmp_path):
    table = _table(tmp_path / "fp-lib-table",
                   '(lib (name "KiCadGen")(type "KiCad")(uri "/old.pretty")'
                   '(options "keep=1")(descr "Mine"))')
    with pytest.raises(KiCadLibraryError) as caught:
        update_fp_lib_table(table, "KiCadGen", "/new.pretty")
    assert caught.value.context["type_changes"] is False
    update_fp_lib_table(table, "KiCadGen", "/new.pretty", replace_entry=True)
    assert '(options "keep=1")' in table.read_text(encoding="utf-8")


# --- 6. rollback folders -------------------------------------------------------

def test_rollback_folder_left_behind_is_reported_and_marked(tmp_path, monkeypatch):
    artifacts = _artifacts(tmp_path)
    original = kicad_library._remove_tree_quietly

    def stuck(directory):
        if directory.name.startswith(kicad_library.ROLLBACK_DIR_PREFIX):
            return False
        return original(directory)
    monkeypatch.setattr(kicad_library, "_remove_tree_quietly", stuck)
    result = install_component_library(artifacts, tmp_path / "Board")
    assert result.rollback_left is not None
    assert (result.rollback_left / kicad_library.ROLLBACK_DISPOSABLE_MARKER).is_file()


def test_only_old_marked_rollback_folders_are_swept(tmp_path):
    root = tmp_path / "Board"
    root.mkdir()
    old_marked = root / (kicad_library.ROLLBACK_DIR_PREFIX + "a" * 32)
    young_marked = root / (kicad_library.ROLLBACK_DIR_PREFIX + "b" * 32)
    old_unmarked = root / (kicad_library.ROLLBACK_DIR_PREFIX + "c" * 32)
    foreign = root / ".kicadgen-rollback-mine"
    for folder in (old_marked, young_marked, old_unmarked, foreign):
        folder.mkdir()
        (folder / "0-Demo.step").write_bytes(b"copy")
    for folder in (old_marked, young_marked, foreign):
        (folder / kicad_library.ROLLBACK_DISPOSABLE_MARKER).write_bytes(b"")
    stale = 1_000_000.0
    for folder in (old_marked, foreign):
        os.utime(folder / kicad_library.ROLLBACK_DISPOSABLE_MARKER, (stale, stale))
    os.utime(old_unmarked / "0-Demo.step", (stale, stale))
    removed = kicad_library._sweep_stale_rollback_dirs(root)
    assert removed == [old_marked]
    assert young_marked.is_dir() and old_unmarked.is_dir() and foreign.is_dir()


# --- 7. a rollback step raising the module's own error -------------------------

def test_rollback_continues_after_a_restore_raising_kicad_library_error(
        tmp_path, monkeypatch):
    first, second = tmp_path / "a.kicad_mod", tmp_path / "b.step"
    backups = {first: tmp_path / "0-a", second: tmp_path / "1-b"}
    restored = []

    def fake_restore(backup, target):
        if target == first:
            raise KiCadLibraryError("dangling", code="target_link_unresolved")
        restored.append(target)
    monkeypatch.setattr(kicad_library, "_restore_file", fake_restore)
    table = tmp_path / "fp-lib-table"
    table.write_text(EMPTY_TABLE, encoding="utf-8")
    table_backup = tmp_path / "table-backup"
    table_backup.write_text("(fp_lib_table (version 7))", encoding="utf-8")
    failures = kicad_library._rollback_install(
        [first, second], backups, table, table_backup, True)
    assert second in restored and table in restored
    assert len(failures) == 1 and "dangling" in failures[0]


def test_install_reports_rollback_incomplete_when_a_restore_raises(tmp_path, monkeypatch):
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "Board"
    install_component_library(artifacts, project)

    def table_fails(*_args, **_kwargs):
        raise OSError("disk full")

    def restore_fails(_backup, _target):
        raise KiCadLibraryError("dangling", code="target_link_unresolved")
    monkeypatch.setattr(kicad_library, "update_fp_lib_table", table_fails)
    monkeypatch.setattr(kicad_library, "_restore_file", restore_fails)
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, project, overwrite=True)
    assert caught.value.code == "rollback_incomplete"
    assert Path(caught.value.context["rollback_dir"]).is_dir()


# --- 8. mapped drives are written as typed --------------------------------------

def test_destination_is_not_resolved_to_its_underlying_path(tmp_path, monkeypatch):
    artifacts = _artifacts(tmp_path)
    root = tmp_path / "Mapped"
    table = _table(tmp_path / "cfg" / "fp-lib-table")
    real_resolve = pathlib.Path.resolve

    def unc_resolve(self, strict=False):
        # What Windows does for a mapped or SUBST drive: another spelling.
        if str(self).startswith(str(root)):
            return Path("//server/share") / self.relative_to(root)
        return real_resolve(self, strict=strict)
    monkeypatch.setattr(pathlib.Path, "resolve", unc_resolve)
    plan = plan_component_install(
        artifacts, root, project_library=False, table_path=table)
    assert plan.library_uri == (root / "KiCadGen.pretty").as_posix()
    result = install_component_library(
        artifacts, root, project_library=False, table_path=table)
    assert "//server" not in table.read_text(encoding="utf-8")
    assert "//server" not in result.footprint_path.read_text(encoding="utf-8")


# --- 9 and 11. preview isolation and stale WRL ----------------------------------

def test_preview_creates_no_table_and_leaves_existing_tables_untouched(tmp_path):
    artifacts = _artifacts(tmp_path)
    project_table = _table(
        artifacts.component_dir / "fp-lib-table",
        '(lib (name "Keep")(type "KiCad")(uri "/keep.pretty"))')
    global_table = _table(
        tmp_path / "cfg" / "10.0" / "fp-lib-table",
        '(lib (name "KiCadGen")(type "KiCad")(uri "/global.pretty"))')
    before = (project_table.read_bytes(), global_table.read_bytes())
    preview = create_preview_project(artifacts)
    assert not (preview.directory / "fp-lib-table").exists()
    assert not list(preview.directory.rglob("*fp-lib-table*"))
    assert (project_table.read_bytes(), global_table.read_bytes()) == before


def test_preview_drops_a_wrl_the_component_no_longer_has(tmp_path):
    artifacts = _artifacts(tmp_path)
    preview = create_preview_project(artifacts)
    assert (preview.directory / "Demo.wrl").is_file()
    (artifacts.component_dir / "Demo.3dshapes" / "Demo.wrl").unlink()
    preview = create_preview_project(discover_component_artifacts(artifacts.component_dir))
    assert not (preview.directory / "Demo.wrl").exists()
    assert (preview.directory / "Demo.step").is_file()


def test_preview_outside_the_own_folder_keeps_an_unrelated_wrl(tmp_path):
    artifacts = _artifacts(tmp_path, wrl=False)
    chosen = tmp_path / "MyReview"
    chosen.mkdir()
    (chosen / "Demo.wrl").write_bytes(b"user file")
    create_preview_project(artifacts, chosen)
    assert (chosen / "Demo.wrl").read_bytes() == b"user file"


# --- 10. shared quoter ------------------------------------------------------------

def test_table_uri_with_control_characters_is_escaped_and_reads_back(tmp_path):
    table = _table(tmp_path / "fp-lib-table")
    uri = "C:/odd\nname\t\"q\"/KiCadGen.pretty"
    update_fp_lib_table(table, "KiCadGen", uri)
    text = table.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if "KiCadGen" in line)
    assert "\\n" in row and "\\t" in row
    assert find_library_entry(table, "KiCadGen").uri == uri


def test_preview_board_strings_are_quoted_by_the_shared_quoter(tmp_path):
    artifacts = _artifacts(tmp_path)
    board = create_preview_project(artifacts).board_path.read_text(encoding="utf-8")
    assert '(generator "kicadgen_tool")' in board
    assert f'(generator_version "{GENERATOR_VERSION}")' in board
    assert '(layer "Edge.Cuts")' in board
    assert '(0 "F.Cu" signal)' in board
