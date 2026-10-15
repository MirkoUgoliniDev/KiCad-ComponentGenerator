"""@brief Robustness of the KiCad library installer (audit 2026-10-15, item 6).

@details Covers six defects of ``kicadgen.services.kicad_library``:

- a non-UTF-8 ``fp-lib-table`` raised a bare UnicodeDecodeError, which escapes
  the GUI handlers (they catch KiCadLibraryError and OSError) and closed the
  application;
- ``shutil.copy2`` carried the Windows read-only attribute onto every copy,
  so a read-only source broke the next preview/overwrite and left scratch
  files that could not be deleted; read-only table backups written that way
  blocked every later backup rotation;
- the install rollback restored every backup and the table even when they
  had never been touched, reporting a false ``rollback_incomplete``;
- the table backups were rotated before the table write, so a failed write
  still shifted (and discarded) the history;
- the table write was not fsync'ed;
- a preview whose target is read-only raised a bare PermissionError, and an
  install refused by the system ended with one naming a scratch file.

Every test writes only below pytest's temporary directory; the user's real
KiCad tables are never involved (tables are explicit paths). Tests that rely
on the Windows meaning of the read-only attribute (``os.replace`` and
``os.remove`` refused) are skipped elsewhere; each defect also has a
platform-independent test driven by an injected failure.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import time

import pytest

from kicadgen import fsutil
from kicadgen.services import kicad_library
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, LANGUAGES
from kicadgen.services.kicad_library import (
    TABLE_BACKUP_RETENTION,
    KiCadLibraryError,
    create_preview_project,
    discover_component_artifacts,
    find_library_entry,
    install_component_library,
    update_fp_lib_table,
)


FOOTPRINT = """(footprint "Demo"
\t(version 20260206)
\t(generator "kicadgen_tool")
\t(layer "F.Cu")
\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""
#: Smallest STEP accepted by discover_component_artifacts (trailer present).
STEP_BYTES = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
#: A table naming a folder with an accented character, as a Windows editor
#: saving in the ANSI code page (cp1252) would write it.
ACCENTED_TABLE = (
    '(fp_lib_table\n  (version 7)\n'
    '  (lib (name "Mio")(type "KiCad")(uri "C:/Progetti/Società/Mio.pretty")'
    '(options "")(descr ""))\n)\n'
)

windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the read-only attribute blocks replace/remove only on Windows")


def _artifacts(tmp_path: Path, *, wrl: bool = True):
    """Create one generated component (footprint, STEP, optional WRL)."""
    component = tmp_path / "src" / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.3dshapes").mkdir()
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(
        FOOTPRINT, encoding="utf-8")
    (component / "Demo.3dshapes" / "Demo.step").write_bytes(STEP_BYTES)
    if wrl:
        (component / "Demo.3dshapes" / "Demo.wrl").write_bytes(b"WRL")
    return discover_component_artifacts(component)


def _read_only(path: Path) -> None:
    """Set the read-only attribute (Windows) / clear the write bits (POSIX)."""
    os.chmod(path, stat.S_IREAD)


def _writable(path: Path) -> bool:
    """Tell whether the owner write bit (the Windows attribute) is set."""
    return bool(os.stat(path).st_mode & stat.S_IWRITE)


@pytest.fixture(autouse=True)
def _unlock_everything(tmp_path):
    """Clear read-only bits after each test so pytest can remove tmp_path."""
    yield
    for path in tmp_path.rglob("*"):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD
                     | (stat.S_IEXEC if path.is_dir() else 0))
        except OSError:
            pass


@pytest.fixture
def no_retry(monkeypatch):
    """Make a refused replace fail at once instead of retrying for a second."""
    monkeypatch.setattr(fsutil, "REPLACE_ATTEMPTS", 1)
    monkeypatch.setattr(kicad_library, "replace_file",
                        lambda s, t, **k: fsutil.replace_file(s, t, attempts=1))


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Map every path below `root` to its bytes (None for folders)."""
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


# ------------------------------------------------- a. non-UTF-8 library table

def test_non_utf8_global_table_is_a_translated_error_and_writes_nothing(tmp_path):
    """@brief A cp1252 table is refused with ``file_not_utf8`` before any write."""
    artifacts = _artifacts(tmp_path)
    table = tmp_path / "cfg" / "fp-lib-table"
    table.parent.mkdir()
    table.write_bytes(ACCENTED_TABLE.encode("cp1252"))
    before = _snapshot(tmp_path / "cfg")
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, tmp_path / "lib",
                                  project_library=False, table_path=table)
    assert caught.value.code == "file_not_utf8"
    assert Path(caught.value.context["path"]) == table
    assert not (tmp_path / "lib").exists(), "nothing may be created"
    assert _snapshot(tmp_path / "cfg") == before, "no backup, no scratch file"


def test_utf8_table_with_the_same_accented_path_still_installs(tmp_path):
    """@brief Negative case: the same table saved as UTF-8 is accepted as is."""
    artifacts = _artifacts(tmp_path)
    table = tmp_path / "cfg" / "fp-lib-table"
    table.parent.mkdir()
    table.write_text(ACCENTED_TABLE, encoding="utf-8")
    install_component_library(artifacts, tmp_path / "lib",
                              project_library=False, table_path=table)
    text = table.read_text(encoding="utf-8")
    assert "C:/Progetti/Società/Mio.pretty" in text
    assert '(name "KiCadGen")' in text


def test_non_utf8_table_is_refused_by_the_reader_and_the_updater(tmp_path):
    """@brief find_library_entry and update_fp_lib_table map the decode error too."""
    table = tmp_path / "fp-lib-table"
    table.write_bytes(ACCENTED_TABLE.encode("cp1252"))
    original = table.read_bytes()
    for call in (lambda: find_library_entry(table, "Mio"),
                 lambda: update_fp_lib_table(table, "KiCadGen", "/x.pretty")):
        with pytest.raises(KiCadLibraryError) as caught:
            call()
        assert caught.value.code == "file_not_utf8"
        assert not isinstance(caught.value, UnicodeDecodeError)
    assert table.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fp-lib-table"]


def test_new_error_strings_exist_in_all_four_languages():
    """@brief Every new error code is translated with the same placeholders."""
    import string
    for code in ("file_not_utf8", "preview_write_refused", "install_write_refused",
                 "install_target_is_folder", "table_backup_rotation_incomplete"):
        entry = DEFAULT_TRANSLATIONS[f"kicad.error.{code}"]
        assert set(entry) >= set(LANGUAGES), code
        fields = {language: {name for _t, name, _f, _c in
                             string.Formatter().parse(entry[language]) if name}
                  for language in LANGUAGES}
        assert len({frozenset(f) for f in fields.values()}) == 1, fields
        for language in LANGUAGES:
            entry[language].format(path="P", error="E", failures="F")


def test_new_error_strings_are_in_the_bundled_sqlite_catalogue():
    """@brief The repository database carries the new keys (reseeded)."""
    import sqlite3
    path = Path(__file__).resolve().parents[1] / "data" / "analysis_profiles.sqlite3"
    if not path.is_file():
        pytest.skip("no repository profile database in this checkout")
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        for code in ("file_not_utf8", "preview_write_refused", "install_write_refused",
                 "install_target_is_folder", "table_backup_rotation_incomplete"):
            key = f"kicad.error.{code}"
            rows = dict(db.execute(
                "SELECT language, text FROM translations WHERE key=?", (key,)))
            assert set(rows) >= set(LANGUAGES), key
            for language in LANGUAGES:
                assert rows[language] == DEFAULT_TRANSLATIONS[key][language]
    finally:
        db.close()


# ----------------------------------------------- b. read-only attribute copies

def test_read_only_source_gives_writable_preview_and_installed_copies(tmp_path):
    """@brief Copies never inherit read-only; previews and overwrites keep working."""
    artifacts = _artifacts(tmp_path)
    _read_only(artifacts.step_path)
    _read_only(artifacts.wrl_path)
    preview_dir = tmp_path / "preview"
    first = create_preview_project(artifacts, preview_dir)
    assert _writable(first.directory / "Demo.step")
    assert _writable(first.directory / "Demo.wrl")
    create_preview_project(artifacts, preview_dir)        # refresh succeeds
    project = tmp_path / "project"
    result = install_component_library(artifacts, project)
    assert _writable(result.step_path) and _writable(result.wrl_path)
    install_component_library(artifacts, project, overwrite=True)
    leftovers = [p for p in tmp_path.rglob("*")
                 if p.name.endswith(".kicadgen.tmp") or ".kicadgen-rollback-" in p.name]
    assert leftovers == []
    # The source itself is left read-only: the copy changes, the original not.
    assert not _writable(artifacts.step_path)


def test_copies_keep_the_source_timestamp(tmp_path):
    """@brief Negative case: dropping the attribute keeps the modification time."""
    artifacts = _artifacts(tmp_path)
    past = time.time() - 3 * 24 * 3600
    os.utime(artifacts.step_path, (past, past))
    result = install_component_library(artifacts, tmp_path / "project")
    assert abs(result.step_path.stat().st_mtime - past) < 2


@windows_only
def test_read_only_table_backups_left_by_older_versions_do_not_block_rotation(tmp_path):
    """@brief Our own read-only backups are unlocked before the history shifts."""
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    backup = tmp_path / "fp-lib-table.kicadgen-backup"
    history = [backup] + [Path(f"{backup}.{i}")
                          for i in range(1, TABLE_BACKUP_RETENTION)]
    for index, path in enumerate(history):
        path.write_text(f"old {index}", encoding="utf-8")
        _read_only(path)
    assert update_fp_lib_table(table, "KiCadGen", "/x.pretty")
    assert backup.read_text(encoding="utf-8") == "(fp_lib_table\n  (version 7)\n)\n"
    assert history[1].read_text(encoding="utf-8") == "old 0"
    assert all(_writable(path) for path in history)


@windows_only
def test_read_only_stale_scratch_file_is_removed(tmp_path):
    """@brief A read-only scratch file of ours, a day old, no longer survives."""
    target = tmp_path / "Demo.step"
    stale = tmp_path / "Demo.step.0123abcd.kicadgen.tmp"
    stale.write_bytes(b"x")
    old = time.time() - 2 * kicad_library.STALE_SCRATCH_AGE_S
    os.utime(stale, (old, old))
    _read_only(stale)
    assert kicad_library._remove_stale_scratch(target) == [stale]
    assert not stale.exists()


@windows_only
def test_unrelated_read_only_file_is_never_unlocked(tmp_path):
    """@brief Negative case: a read-only file that is not ours keeps its attribute."""
    target = tmp_path / "Demo.step"
    foreign = tmp_path / "Demo.step.user-notes.tmp"
    foreign.write_bytes(b"x")
    old = time.time() - 2 * kicad_library.STALE_SCRATCH_AGE_S
    os.utime(foreign, (old, old))
    _read_only(foreign)
    assert kicad_library._remove_stale_scratch(target) == []
    assert foreign.exists() and not _writable(foreign)


# ------------------------------------------------------ c. rollback precision

def _failing_replace(monkeypatch, refused_name: str):
    """Make every os.replace onto a target named `refused_name` fail."""
    real_replace = os.replace

    def replace(source, target, *args, **kwargs):
        if Path(target).name == refused_name:
            raise PermissionError(13, "simulated refusal", str(source), None, str(target))
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(kicad_library.os, "replace", replace)


def test_untouched_target_is_not_restored_and_rollback_is_complete(
        tmp_path, monkeypatch, no_retry):
    """@brief A refused WRL replace restores only the files really replaced."""
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "project"
    first = install_component_library(artifacts, project)
    first.footprint_path.write_text("old footprint", encoding="utf-8")
    first.step_path.write_bytes(b"OLD STEP")
    first.wrl_path.write_bytes(b"OLD WRL")
    before = _snapshot(project)
    _failing_replace(monkeypatch, "Demo.wrl")
    # CONTRACT CHANGED 2026-10-15: after a complete rollback a refused write was
    # re-raised as a bare PermissionError, shown by the GUI as «unexpected OS
    # error» naming a .kicadgen.tmp scratch path; it is now install_write_refused
    # naming the real file, with the PermissionError as its cause.
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, project, overwrite=True)
    assert caught.value.code == "install_write_refused", \
        "nothing really failed in the rollback: no rollback_incomplete"
    assert Path(caught.value.context["path"]) == first.wrl_path
    assert isinstance(caught.value.__cause__, PermissionError)
    assert _snapshot(project) == before, "no rollback folder, no scratch, old files back"


def test_refused_table_write_rolls_back_cleanly_and_keeps_backup_history(
        tmp_path, monkeypatch, no_retry):
    """@brief c+d: the table is not restored when unwritten; history untouched."""
    artifacts = _artifacts(tmp_path)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    table = cfg / "fp-lib-table"
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    install_component_library(artifacts, tmp_path / "lib1",
                              project_library=False, table_path=table)
    install_component_library(artifacts, tmp_path / "lib2", project_library=False,
                              table_path=table, replace_library_entry=True)
    cfg_before = _snapshot(cfg)
    _failing_replace(monkeypatch, "fp-lib-table")
    # CONTRACT CHANGED 2026-10-15: a bare PermissionError naming the table's
    # scratch file used to escape; now install_write_refused names the table.
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, tmp_path / "lib3", project_library=False,
                                  table_path=table, replace_library_entry=True)
    assert caught.value.code == "install_write_refused"
    assert Path(caught.value.context["path"]) == table, "the table, not its scratch file"
    assert _snapshot(cfg) == cfg_before, "table and its backups unchanged"
    assert not (tmp_path / "lib3").exists(), "created folders removed"


def test_written_table_is_restored_by_the_rollback(tmp_path):
    """@brief Negative case: a table that WAS rewritten is put back."""
    table = tmp_path / "fp-lib-table"
    table.write_text("new", encoding="utf-8")
    backup = tmp_path / "table-backup"
    backup.write_text("old", encoding="utf-8")
    failures = kicad_library._rollback_install([], {}, table, backup, True)
    assert failures == []
    assert table.read_text(encoding="utf-8") == "old"


def test_replaced_target_whose_restore_fails_is_still_reported(tmp_path, monkeypatch):
    """@brief Negative case: a real restore failure still yields a failure line."""
    target = tmp_path / "Demo.step"
    target.write_bytes(b"NEW")
    backup = tmp_path / "0-Demo.step"
    backup.write_bytes(b"OLD")

    def refuse(_backup, _target):
        raise OSError("locked")

    monkeypatch.setattr(kicad_library, "_restore_file", refuse)
    failures = kicad_library._rollback_install(
        [target], {target: backup}, tmp_path / "fp-lib-table", None, True)
    assert len(failures) == 1 and str(target) in failures[0]


@windows_only
def test_read_only_wrl_target_gives_a_complete_rollback(tmp_path, no_retry):
    """@brief The audit reproduction: real read-only attribute on the old WRL."""
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "project"
    first = install_component_library(artifacts, project)
    first.footprint_path.write_text("old footprint", encoding="utf-8")
    _read_only(first.wrl_path)
    before = _snapshot(project)
    # CONTRACT CHANGED 2026-10-15: was a bare PermissionError (see above).
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, project, overwrite=True)
    assert caught.value.code == "install_write_refused"
    assert Path(caught.value.context["path"]) == first.wrl_path
    assert _snapshot(project) == before


def test_install_other_os_errors_are_not_disguised(tmp_path, monkeypatch):
    """@brief Negative case: a non-permission failure keeps its own type."""
    artifacts = _artifacts(tmp_path)

    def full_disk(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(kicad_library.os, "replace", full_disk)
    with pytest.raises(OSError) as caught:
        install_component_library(artifacts, tmp_path / "project")
    assert not isinstance(caught.value, KiCadLibraryError)
    assert not (tmp_path / "project").exists()


# ------------------------------------- d. rotation only after a successful write

def test_failed_table_write_does_not_rotate_the_backups(tmp_path, monkeypatch, no_retry):
    """@brief update_fp_lib_table: a refused write leaves the history as it was."""
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    assert update_fp_lib_table(table, "A", "/a.pretty")
    assert update_fp_lib_table(table, "B", "/b.pretty")
    before = _snapshot(tmp_path)
    _failing_replace(monkeypatch, "fp-lib-table")
    with pytest.raises(PermissionError):
        update_fp_lib_table(table, "C", "/c.pretty")
    assert _snapshot(tmp_path) == before


def test_successful_write_still_rotates_the_backups(tmp_path):
    """@brief Negative case: a write that succeeds shifts the history once."""
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    assert update_fp_lib_table(table, "A", "/a.pretty")
    state_a = table.read_text(encoding="utf-8")
    assert update_fp_lib_table(table, "B", "/b.pretty")
    backup = tmp_path / "fp-lib-table.kicadgen-backup"
    assert backup.read_text(encoding="utf-8") == state_a
    assert Path(f"{backup}.1").read_text(encoding="utf-8") == \
        "(fp_lib_table\n  (version 7)\n)\n"


def test_failed_rotation_after_the_write_restores_the_table(tmp_path, monkeypatch):
    """@brief Without a fresh backup the change is undone, never left unbacked."""
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    assert update_fp_lib_table(table, "A", "/a.pretty")
    state_a = table.read_bytes()
    _failing_replace(monkeypatch, "fp-lib-table.kicadgen-backup")
    # CONTRACT CHANGED 2026-10-15 (review pass 2, L1): every replace onto
    # .kicadgen-backup is refused, so moving the shifted .1 back fails too.
    # That undo failure used to be swallowed (bare OSError); it is now
    # reported as table_backup_rotation_incomplete naming the misplaced copy.
    with pytest.raises(KiCadLibraryError) as caught:
        update_fp_lib_table(table, "B", "/b.pretty")
    assert caught.value.code == "table_backup_rotation_incomplete"
    assert "fp-lib-table.kicadgen-backup.1" in caught.value.context["failures"]
    assert table.read_bytes() == state_a


# --------------------------------------------------------- e. durable write

def test_table_write_is_fsynced_before_the_replace(tmp_path, monkeypatch):
    """@brief The new table content reaches the disk before it is published."""
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor):
        events.append("fsync")
        return real_fsync(descriptor)

    def replace(source, target, *args, **kwargs):
        if Path(target) == table:
            events.append("replace-table")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(kicad_library.os, "fsync", fsync)
    monkeypatch.setattr(kicad_library.os, "replace", replace)
    assert update_fp_lib_table(table, "KiCadGen", "/x.pretty")
    assert "replace-table" in events
    assert "fsync" in events[:events.index("replace-table")]


# ---------------------------------------------- f. read-only preview target

def test_preview_with_a_refused_target_is_a_clear_error(tmp_path, monkeypatch, no_retry):
    """@brief A refused replace in the preview names the file, not a bare OSError."""
    artifacts = _artifacts(tmp_path)
    preview_dir = tmp_path / "preview"
    create_preview_project(artifacts, preview_dir)
    _failing_replace(monkeypatch, "Demo.step")
    with pytest.raises(KiCadLibraryError) as caught:
        create_preview_project(artifacts, preview_dir)
    assert caught.value.code == "preview_write_refused"
    assert Path(caught.value.context["path"]) == preview_dir / "Demo.step"
    assert isinstance(caught.value.__cause__, PermissionError)
    assert not list(preview_dir.glob("*.kicadgen.tmp"))


def test_preview_other_os_errors_are_not_disguised(tmp_path, monkeypatch):
    """@brief Negative case: a non-permission failure keeps its own type."""
    artifacts = _artifacts(tmp_path)

    def full_disk(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(kicad_library, "replace_file", full_disk)
    with pytest.raises(OSError) as caught:
        create_preview_project(artifacts, tmp_path / "preview")
    assert not isinstance(caught.value, KiCadLibraryError)


@windows_only
def test_read_only_files_of_the_own_preview_folder_are_unlocked(tmp_path, no_retry):
    """@brief The audit reproduction in ``.kicad-preview``: refreshed, not refused."""
    artifacts = _artifacts(tmp_path)
    first = create_preview_project(artifacts)
    assert first.directory.name == kicad_library.PREVIEW_DIR_NAME
    owned = [first.directory / "Demo.step", first.directory / "Demo.wrl",
             first.board_path, first.project_path]
    for path in owned:
        _read_only(path)
    second = create_preview_project(artifacts)
    assert second.board_path == first.board_path
    assert all(_writable(path) for path in owned)
    assert not list(first.directory.glob("*.kicadgen.tmp"))


@windows_only
def test_preview_with_a_real_read_only_target(tmp_path, no_retry):
    """@brief Negative case: a caller-chosen folder is not unlocked, only reported."""
    artifacts = _artifacts(tmp_path)
    preview_dir = tmp_path / "preview"
    create_preview_project(artifacts, preview_dir)
    _read_only(preview_dir / "Demo.step")
    with pytest.raises(KiCadLibraryError) as caught:
        create_preview_project(artifacts, preview_dir)
    assert caught.value.code == "preview_write_refused"
    assert Path(caught.value.context["path"]) == preview_dir / "Demo.step"


# =========================================================== review pass 2
# M5 durable installed files, L1 exact rotation undo, L2 BOM, L3 line
# endings, L4 no unlocking through links, L5 real path and matching reason.

def test_installed_files_are_fsynced_before_they_replace_the_targets(tmp_path, monkeypatch):
    """@brief M5: each staged file is on disk before the rename publishes it."""
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "project"
    install_component_library(artifacts, project)
    synced: set[int] = set()
    published: list[tuple[str, bool]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor):
        synced.add(os.fstat(descriptor).st_ino)
        return real_fsync(descriptor)

    def replace(source, target, *args, **kwargs):
        if Path(target).name in ("Demo.kicad_mod", "Demo.step", "Demo.wrl"):
            published.append((Path(target).name, os.stat(source).st_ino in synced))
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(kicad_library.os, "fsync", fsync)
    monkeypatch.setattr(kicad_library.os, "replace", replace)
    install_component_library(artifacts, project, overwrite=True)
    assert sorted(published) == [("Demo.kicad_mod", True), ("Demo.step", True),
                                 ("Demo.wrl", True)]


def test_a_transient_lock_on_a_target_is_retried_not_rolled_back(tmp_path, monkeypatch):
    """@brief M5: one refused replace (antivirus, file watcher) is retried."""
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "project"
    install_component_library(artifacts, project)
    real_replace = os.replace
    refusals = []

    def replace_once_refused(source, target, *args, **kwargs):
        if Path(target).name == "Demo.step" and not refusals:
            refusals.append(target)
            raise PermissionError(32, "simulated transient lock", str(source), None, str(target))
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(kicad_library.os, "replace", replace_once_refused)
    install_component_library(artifacts, project, overwrite=True)
    assert refusals, "the refusal really happened"
    assert (project / "KiCadGen.3dshapes" / "Demo.step").read_bytes() == STEP_BYTES


def _three_backups(tmp_path: Path) -> tuple[Path, list[Path]]:
    """Create a table with a full backup history ``backup0..2``."""
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    history = kicad_library._table_backup_history(table)
    for index, path in enumerate(history):
        path.write_text(f"backup{index}", encoding="utf-8")
    return table, history


def test_rotation_failing_halfway_restores_the_exact_history(tmp_path, monkeypatch):
    """@brief L1: the oldest copy survives a rotation that fails in the middle."""
    table, history = _three_backups(tmp_path)
    real = kicad_library.replace_file

    def flaky(source, target, **kwargs):
        if Path(source) == history[0] and Path(target) == history[1]:
            raise PermissionError(13, "simulated", str(source))
        return real(source, target, **kwargs)

    monkeypatch.setattr(kicad_library, "replace_file", flaky)
    with pytest.raises(OSError):
        update_fp_lib_table(table, "KiCadGen", "/y", create_missing=False)
    assert [p.read_text(encoding="utf-8") for p in history] == \
        ["backup0", "backup1", "backup2"]
    assert "KiCadGen" not in table.read_text(encoding="utf-8")
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_rotation_whose_undo_fails_is_reported(tmp_path, monkeypatch):
    """@brief L1 negative: an undo that cannot be done is not silent."""
    table, history = _three_backups(tmp_path)
    real = kicad_library.replace_file

    def broken(source, target, **kwargs):
        # The shift into the first place fails, and so does moving back the
        # copy already shifted from .1 to .2.
        if Path(source) == history[0] and Path(target) == history[1]:
            raise PermissionError(13, "simulated", str(source))
        if Path(source) == history[2] and Path(target) == history[1]:
            raise PermissionError(13, "simulated undo", str(source))
        return real(source, target, **kwargs)

    monkeypatch.setattr(kicad_library, "replace_file", broken)
    with pytest.raises(KiCadLibraryError) as caught:
        update_fp_lib_table(table, "KiCadGen", "/y", create_missing=False)
    assert caught.value.code == "table_backup_rotation_incomplete"
    assert str(history[1]) in caught.value.context["failures"]
    assert "KiCadGen" not in table.read_text(encoding="utf-8")


BOM = b"\xef\xbb\xbf"
CRLF_TABLE = (b'(fp_lib_table\r\n  (version 7)\r\n  (lib (name "A")(type "KiCad")'
              b'(uri "x")(options "")(descr ""))\r\n)\r\n')


def test_a_table_with_a_utf8_bom_is_accepted_and_keeps_it(tmp_path):
    """@brief L2: the BOM is not an invalid table; the rewrite keeps it."""
    table = tmp_path / "fp-lib-table"
    table.write_bytes(BOM + CRLF_TABLE)
    assert find_library_entry(table, "A").uri == "x"
    assert update_fp_lib_table(table, "KiCadGen", "/y", create_missing=False)
    data = table.read_bytes()
    assert data.startswith(BOM) and not data[3:].startswith(BOM)
    assert find_library_entry(table, "KiCadGen").uri == "/y"


def test_a_table_without_bom_does_not_gain_one(tmp_path):
    """@brief L2 negative: no BOM is added to a plain UTF-8 table."""
    table = tmp_path / "fp-lib-table"
    table.write_bytes(CRLF_TABLE)
    assert update_fp_lib_table(table, "KiCadGen", "/y", create_missing=False)
    assert not table.read_bytes().startswith(BOM)


def test_a_crlf_table_keeps_its_line_endings_and_changes_only_the_new_row(tmp_path):
    """@brief L3: only the added row differs, written with the file's CRLF."""
    table = tmp_path / "fp-lib-table"
    table.write_bytes(CRLF_TABLE)
    assert update_fp_lib_table(table, "KiCadGen", "/y", create_missing=False)
    data = table.read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b""), "no bare LF introduced"
    lines = data.split(b"\r\n")
    original = CRLF_TABLE.split(b"\r\n")
    assert lines[:3] == original[:3] and lines[-2:] == original[-2:]
    assert len(lines) == len(original) + 1 and b"KiCadGen" in lines[3]


def test_an_lf_table_without_final_newline_keeps_that_state(tmp_path):
    """@brief L3 negative: LF stays LF, and no final newline is invented."""
    table = tmp_path / "fp-lib-table"
    table.write_bytes(b"(fp_lib_table\n  (version 7)\n)")
    assert update_fp_lib_table(table, "KiCadGen", "/y")
    data = table.read_bytes()
    assert b"\r" not in data and data.endswith(b")") and b"KiCadGen" in data


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Create a file symlink, or skip when the account may not create one."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")


@windows_only
def test_clear_readonly_never_unlocks_through_a_link(tmp_path):
    """@brief L4: a read-only link to a user file leaves the user file locked."""
    user_file = tmp_path / "user.step"
    user_file.write_bytes(b"user")
    _read_only(user_file)
    link = tmp_path / "Demo.step"
    _symlink_or_skip(link, user_file)
    try:
        os.chmod(link, stat.S_IREAD, follow_symlinks=False)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"cannot mark the link itself read-only: {exc}")
    kicad_library._clear_readonly(link)
    assert not _writable(user_file)


@pytest.mark.parametrize("mode, attributes", [
    (stat.S_IFLNK | stat.S_IREAD, 0),            # symbolic link
    (stat.S_IFREG | stat.S_IREAD, 0x400),        # FILE_ATTRIBUTE_REPARSE_POINT
], ids=["symlink", "reparse_point"])
def test_clear_readonly_never_chmods_a_link(tmp_path, monkeypatch, mode, attributes):
    """@brief L4 (any platform): no chmod on an entry the link check flags.

    @note The entry is described by a faked ``os.stat`` so the test runs
        without the privilege Windows requires to create symbolic links; the
        real-link variant above is skipped in that case.
    """
    entry = tmp_path / "Demo.step"
    entry.write_bytes(b"x")
    calls = []
    fake = os.stat_result((mode, 0, 0, 1, 0, 0, 1, 0, 0, 0),
                          {"st_file_attributes": attributes})
    monkeypatch.setattr(kicad_library.os, "stat", lambda *a, **k: fake)
    monkeypatch.setattr(kicad_library.os, "chmod", lambda *a, **k: calls.append(a))
    kicad_library._clear_readonly(entry)
    assert calls == []


def test_clear_readonly_still_unlocks_a_regular_file(tmp_path):
    """@brief L4 negative: our own regular read-only file is unlocked."""
    own = tmp_path / "Demo.step.0123abcd.kicadgen.tmp"
    own.write_bytes(b"x")
    _read_only(own)
    kicad_library._clear_readonly(own)
    assert _writable(own)


def test_refused_rollback_copy_names_the_users_file_not_the_rollback_folder(
        tmp_path, monkeypatch):
    """@brief L5: a refusal on an internal copy names the real target."""
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "project"
    first = install_component_library(artifacts, project)
    real_copy = kicad_library._copy_writable

    def refuse_backup(source, target):
        if Path(target).parent.name.startswith(".kicadgen-rollback-"):
            raise PermissionError(13, "simulated", str(target))
        return real_copy(source, target)

    monkeypatch.setattr(kicad_library, "_copy_writable", refuse_backup)
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, project, overwrite=True)
    assert caught.value.code == "install_write_refused"
    path = Path(caught.value.context["path"])
    assert ".kicadgen-rollback-" not in str(path)
    assert path == first.footprint_path


def test_a_folder_in_place_of_a_file_is_described_as_such(tmp_path):
    """@brief L5: a folder named like the WRL is not "read-only or locked"."""
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "project"
    first = install_component_library(artifacts, project)
    first.wrl_path.unlink()
    first.wrl_path.mkdir()
    (first.wrl_path / "keep.txt").write_text("user", encoding="utf-8")
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, project, overwrite=True)
    assert caught.value.code == "install_target_is_folder"
    assert Path(caught.value.context["path"]) == first.wrl_path
    assert (first.wrl_path / "keep.txt").read_text(encoding="utf-8") == "user"
