"""@brief Fixes of the KiCad library review of 2026-10-27 (A1, A2, A3, D1).

@details Every test writes only below pytest's temporary directory; no real
KiCad configuration is touched. Symbolic links cannot be created by an
ordinary Windows account (WinError 1314): the hard-link cases use ``os.link``
and the symbolic-link behaviour is exercised through the module's own link
probes (`_is_symlink`, `_resolve_link`) faked by monkeypatch, with the real
symbolic-link variants skipped when the account may not create one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import (
    TABLE_BACKUP_RETENTION,
    KiCadLibraryError,
    discover_component_artifacts,
    global_fp_lib_table_path,
    install_component_library,
    plan_component_install,
    resolve_global_fp_lib_table,
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


def _artifacts(base: Path, *, wrl: bool = True):
    """Create one generated component under `base`, with or without a WRL."""
    component = base / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.3dshapes").mkdir()
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    (component / "Demo.3dshapes" / "Demo.step").write_bytes(STEP_BYTES)
    if wrl:
        (component / "Demo.3dshapes" / "Demo.wrl").write_bytes(b"WRL")
    return discover_component_artifacts(component)


def _global_table(tmp_path: Path) -> Path:
    """An explicit, scratch global table (never KiCad's real one)."""
    table = tmp_path / "cfg" / "fp-lib-table"
    table.parent.mkdir()
    table.write_text(EMPTY_TABLE, encoding="utf-8")
    return table


def _inode(path: Path) -> int:
    """File identity of `path`, links followed."""
    return os.stat(path).st_ino


def _snapshot(folder: Path) -> dict[str, bytes]:
    """Every file below `folder` with its content, for before/after comparisons."""
    return {str(p.relative_to(folder)): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _hard_link_or_skip(target: Path, link: Path) -> None:
    """Create a hard link, or skip when the volume does not support one."""
    try:
        os.link(target, link)
    except OSError as exc:
        pytest.skip(f"hard links unavailable here: {exc}")


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Create a symbolic link, or skip when the account may not create one."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links unavailable (WinError 1314 for an ordinary account): {exc}")


def _fake_symlink(monkeypatch, link: Path, real: Path | None) -> None:
    """Make the module see `link` as a symbolic link to `real` (None: dangling).

    Only the module's own link probes are replaced, never ``os`` or
    ``pathlib``: the code under test takes exactly the branch a real link
    would make it take, without the privilege Windows asks for.
    """
    def is_symlink(path: Path) -> bool:
        return Path(path) == link

    def resolve_link(path: Path) -> Path:
        if real is None:
            raise FileNotFoundError(2, "dangling", str(path))
        return real

    monkeypatch.setattr(kicad_library, "_is_symlink", is_symlink)
    monkeypatch.setattr(kicad_library, "_resolve_link", resolve_link)


# ---------------------------------------------------------------- A1 hard links

def test_hard_linked_table_keeps_its_inode_and_both_names_see_the_row(tmp_path):
    """@brief A1: a dotfiles-managed table (hard link) is updated in place."""
    table = _global_table(tmp_path)
    other_name = table.with_name("dotfiles-fp-lib-table")
    _hard_link_or_skip(table, other_name)
    before = _inode(table)
    result = install_component_library(
        _artifacts(tmp_path / "src"), tmp_path / "lib",
        project_library=False, table_path=table)
    assert result.table_path == table
    assert _inode(table) == before and _inode(other_name) == before
    assert "KiCadGen" in table.read_text(encoding="utf-8")
    assert other_name.read_bytes() == table.read_bytes(), "the other name sees the new row"
    assert table.with_name("fp-lib-table.kicadgen-backup").read_text(encoding="utf-8") == EMPTY_TABLE
    assert not [p for p in table.parent.iterdir() if p.name.endswith(".tmp")]


def test_hard_linked_table_rollback_restores_both_names(tmp_path, monkeypatch):
    """@brief A1: an undone update puts the old content under both names, same inode."""
    table = _global_table(tmp_path)
    other_name = table.with_name("dotfiles-fp-lib-table")
    _hard_link_or_skip(table, other_name)
    before = _inode(table)

    def rotation_fails(_table, _fresh):
        raise OSError("simulated rotation failure")

    monkeypatch.setattr(kicad_library, "_publish_table_backup", rotation_fails)
    with pytest.raises(OSError):
        update_fp_lib_table(table, "KiCadGen", "/x.pretty", create_missing=False)
    assert table.read_text(encoding="utf-8") == EMPTY_TABLE
    assert other_name.read_text(encoding="utf-8") == EMPTY_TABLE
    assert _inode(table) == before and _inode(other_name) == before


def test_hard_linked_installed_model_is_overwritten_in_place_and_rolled_back(
        tmp_path, monkeypatch):
    """@brief A1: an installed STEP kept under another name follows the install and its undo."""
    artifacts = _artifacts(tmp_path / "src")
    project = tmp_path / "project"
    first = install_component_library(artifacts, project)
    first.step_path.write_bytes(b"OLD STEP")
    other_name = tmp_path / "shared-Demo.step"
    _hard_link_or_skip(first.step_path, other_name)
    before = _inode(first.step_path)

    def table_fails(*_args, **_kwargs):
        raise OSError("simulated table failure")

    monkeypatch.setattr(kicad_library, "update_fp_lib_table", table_fails)
    with pytest.raises(OSError):
        install_component_library(artifacts, project, overwrite=True)
    assert other_name.read_bytes() == b"OLD STEP" and first.step_path.read_bytes() == b"OLD STEP"
    assert _inode(first.step_path) == before and _inode(other_name) == before
    assert not list(project.glob(".kicadgen-rollback-*"))

    monkeypatch.undo()
    result = install_component_library(artifacts, project, overwrite=True)
    assert result.replaced == (first.footprint_path, first.step_path, first.wrl_path)
    assert other_name.read_bytes() == STEP_BYTES, "the other name sees the new model"
    assert _inode(first.step_path) == before


def test_a_plain_file_still_goes_through_the_atomic_replace(tmp_path, monkeypatch):
    """@brief A1 negative: without a second name the rename publishes, never the in-place write."""
    table = _global_table(tmp_path)
    renames: list[Path] = []
    real_replace = kicad_library.replace_file

    def counting_replace(source, target, **kwargs):
        renames.append(Path(target))
        return real_replace(source, target, **kwargs)

    def never(*_args):
        raise AssertionError("in-place write used on a plain file")

    monkeypatch.setattr(kicad_library, "replace_file", counting_replace)
    monkeypatch.setattr(kicad_library, "_write_in_place", never)
    result = install_component_library(
        _artifacts(tmp_path / "src"), tmp_path / "lib",
        project_library=False, table_path=table)
    assert table in renames and result.step_path in renames
    assert not kicad_library._hard_linked(table)


def test_hard_link_detection_distinguishes_one_name_from_two(tmp_path):
    """@brief A1: the in-place branch is chosen by the link count, nothing else."""
    single = tmp_path / "single"
    single.write_bytes(b"x")
    assert not kicad_library._hard_linked(single)
    assert not kicad_library._hard_linked(tmp_path / "missing")
    assert not kicad_library._hard_linked(tmp_path)
    _hard_link_or_skip(single, tmp_path / "second-name")
    assert kicad_library._hard_linked(single)


# ------------------------------------------------------------ A1 symbolic links

def test_dangling_link_counts_as_existing_and_is_refused_once_overwriting(
        tmp_path, monkeypatch):
    """@brief A1: a dangling link is asked about, then refused with its own code."""
    artifacts = _artifacts(tmp_path / "src")
    project = tmp_path / "project"
    dangling = project / "KiCadGen.3dshapes" / "Demo.wrl"
    _fake_symlink(monkeypatch, dangling, None)
    plan = plan_component_install(artifacts, project)
    assert plan.replaced == (dangling,) and plan.removed == ()
    assert plan.linked == (dangling,)
    with pytest.raises(KiCadLibraryError) as refused:
        install_component_library(artifacts, project)
    assert refused.value.code == "files_exist"
    assert str(dangling) in refused.value.context["files"]
    with pytest.raises(KiCadLibraryError) as unresolved:
        install_component_library(artifacts, project, overwrite=True)
    assert unresolved.value.code == "target_link_unresolved"
    assert Path(unresolved.value.context["path"]) == dangling
    assert not project.exists(), "refused before the first write"


def test_linked_table_is_rewritten_through_the_link(tmp_path, monkeypatch):
    """@brief A1: the real table behind a link gets the row, its backups and no plain-file replacement.

    @note The link is faked: ``link`` is a stand-in file with the same bytes
        as ``real`` (a true symbolic link reads the same). The install must
        rewrite ``real`` and leave the stand-in untouched: with a real link,
        a rename onto the link path is exactly what destroyed the link.
    """
    real = tmp_path / "dotfiles" / "fp-lib-table"
    real.parent.mkdir()
    real.write_text(EMPTY_TABLE, encoding="utf-8")
    link = _global_table(tmp_path)
    _fake_symlink(monkeypatch, link, real)
    plan = plan_component_install(
        _artifacts(tmp_path / "src"), tmp_path / "lib",
        project_library=False, table_path=link)
    assert plan.linked == (link,)
    install_component_library(
        _artifacts(tmp_path / "src2"), tmp_path / "lib",
        project_library=False, table_path=link)
    assert "KiCadGen" in real.read_text(encoding="utf-8")
    assert real.with_name("fp-lib-table.kicadgen-backup").read_text(encoding="utf-8") == EMPTY_TABLE
    assert link.read_text(encoding="utf-8") == EMPTY_TABLE, "no plain file written over the link"
    assert not [p for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]


def test_real_symbolic_link_to_the_table_survives_the_update(tmp_path):
    """@brief A1 with a true symbolic link (skipped where the account may not create one)."""
    real = tmp_path / "dotfiles" / "fp-lib-table"
    real.parent.mkdir()
    real.write_text(EMPTY_TABLE, encoding="utf-8")
    link = tmp_path / "cfg" / "fp-lib-table"
    link.parent.mkdir()
    _symlink_or_skip(link, real)
    assert update_fp_lib_table(link, "KiCadGen", "/x.pretty", create_missing=False)
    assert link.is_symlink(), "the link survived"
    assert "KiCadGen" in real.read_text(encoding="utf-8")
    assert link.read_bytes() == real.read_bytes()


def test_real_dangling_symbolic_link_is_refused(tmp_path):
    """@brief A1 with a true dangling link (skipped where the account may not create one)."""
    artifacts = _artifacts(tmp_path / "src")
    project = tmp_path / "project"
    (project / "KiCadGen.3dshapes").mkdir(parents=True)
    _symlink_or_skip(project / "KiCadGen.3dshapes" / "Demo.wrl", tmp_path / "gone.wrl")
    with pytest.raises(KiCadLibraryError) as refused:
        install_component_library(artifacts, project)
    assert refused.value.code == "files_exist"
    with pytest.raises(KiCadLibraryError) as unresolved:
        install_component_library(artifacts, project, overwrite=True)
    assert unresolved.value.code == "target_link_unresolved"


# ------------------------------------------------------- A2 backup compaction

def _history(table: Path) -> list[Path]:
    """The backup slots of `table`, newest first."""
    return kicad_library._table_backup_history(table)


def _contents(paths: list[Path]) -> list[str | None]:
    """Content of each slot, None for a missing one."""
    return [p.read_text(encoding="utf-8") if p.exists() else None for p in paths]


def test_gap_in_the_history_is_compacted_and_three_states_are_kept(tmp_path):
    """@brief A2: a missing first slot no longer discards the oldest copy."""
    table = _global_table(tmp_path)
    history = _history(table)
    history[1].write_text("one", encoding="utf-8")
    history[2].write_text("two", encoding="utf-8")
    foreign = [table.with_name("fp-lib-table.kicadgen-backup.3"),
               table.with_name("fp-lib-table.kicadgen-backup.2.notes.txt")]
    for path in foreign:
        path.write_text("foreign", encoding="utf-8")
    assert update_fp_lib_table(table, "A", "/a.pretty", create_missing=False)
    assert _contents(history) == [EMPTY_TABLE, "one", "two"]
    assert all(p.read_text(encoding="utf-8") == "foreign" for p in foreign)
    assert not [p for p in table.parent.iterdir() if p.name.endswith(".tmp")]


def test_gap_in_the_middle_of_the_history_is_closed(tmp_path):
    """@brief A2: slots are filled in order whatever slot was missing."""
    table = _global_table(tmp_path)
    history = _history(table)
    history[0].write_text("zero", encoding="utf-8")
    history[2].write_text("two", encoding="utf-8")
    assert update_fp_lib_table(table, "A", "/a.pretty", create_missing=False)
    assert _contents(history) == [EMPTY_TABLE, "zero", "two"]


def test_full_history_rotation_is_unchanged(tmp_path):
    """@brief A2 negative: the ordinary shift keeps the newest three, oldest discarded."""
    table = _global_table(tmp_path)
    history = _history(table)
    for index, path in enumerate(history):
        path.write_text(f"backup{index}", encoding="utf-8")
    assert update_fp_lib_table(table, "A", "/a.pretty", create_missing=False)
    assert _contents(history) == [EMPTY_TABLE, "backup0", "backup1"]
    assert len(history) == TABLE_BACKUP_RETENTION
    assert not [p for p in table.parent.iterdir() if p.name.endswith(".tmp")]


def test_failed_compaction_restores_the_gapped_history_exactly(tmp_path, monkeypatch):
    """@brief A2: an undo after a failure halfway puts the gapped history back as it was."""
    table = _global_table(tmp_path)
    history = _history(table)
    history[1].write_text("one", encoding="utf-8")
    history[2].write_text("two", encoding="utf-8")
    real = kicad_library.replace_file

    def flaky(source, target, **kwargs):
        if Path(target) == history[0]:
            raise PermissionError(13, "simulated", str(source))
        return real(source, target, **kwargs)

    monkeypatch.setattr(kicad_library, "replace_file", flaky)
    with pytest.raises(OSError):
        update_fp_lib_table(table, "A", "/a.pretty", create_missing=False)
    assert _contents(history) == [None, "one", "two"]
    assert table.read_text(encoding="utf-8") == EMPTY_TABLE
    assert not [p for p in table.parent.iterdir() if p.name.endswith(".tmp")]


# ------------------------------------------------- A3 replaced versus removed

def test_a_wrl_the_source_lost_is_reported_as_removed_not_replaced(tmp_path, monkeypatch):
    """@brief A3: the plan and the result both say the WRL is deleted."""
    with_wrl = _artifacts(tmp_path / "src")
    project = tmp_path / "project"
    first = install_component_library(with_wrl, project)
    without_wrl = _artifacts(tmp_path / "src2", wrl=False)
    plan = plan_component_install(without_wrl, project)
    assert plan.removed == (first.wrl_path,)
    assert plan.replaced == (first.footprint_path, first.step_path)

    def table_fails(*_args, **_kwargs):
        raise OSError("simulated table failure")

    monkeypatch.setattr(kicad_library, "update_fp_lib_table", table_fails)
    with pytest.raises(OSError):
        install_component_library(without_wrl, project, overwrite=True)
    assert first.wrl_path.read_bytes() == b"WRL", "the removal is rolled back"
    monkeypatch.undo()
    result = install_component_library(without_wrl, project, overwrite=True)
    assert result.removed == (first.wrl_path,) and result.wrl_path is None
    assert result.replaced == (first.footprint_path, first.step_path)
    assert not first.wrl_path.exists()


def test_a_source_with_a_wrl_replaces_it_and_removes_nothing(tmp_path):
    """@brief A3 negative: with a WRL in the source every existing file is replaced."""
    artifacts = _artifacts(tmp_path / "src")
    project = tmp_path / "project"
    first = install_component_library(artifacts, project)
    assert first.replaced == () and first.removed == ()
    second = install_component_library(artifacts, project, overwrite=True)
    assert second.removed == ()
    assert second.replaced == (first.footprint_path, first.step_path, first.wrl_path)


# ------------------------------------------- D1 global table named before the write

def test_resolved_global_table_names_version_and_its_source(tmp_path):
    """@brief D1: the table path and version are available before any write."""
    executable = tmp_path / "KiCad" / "9.0" / "bin" / "kicad-cli.exe"
    environ = {"KICAD_CONFIG_HOME": str(tmp_path / "cfg")}
    from_cli = resolve_global_fp_lib_table(
        executable, environ=environ, version_runner=lambda _p: "10.0.6\n")
    assert (from_cli.version, from_cli.version_source) == ("10.0", "kicad-cli")
    assert from_cli.path == tmp_path / "cfg" / "10.0" / "fp-lib-table"
    from_folder = resolve_global_fp_lib_table(
        executable, environ=environ, version_runner=lambda _p: None)
    assert (from_folder.version, from_folder.version_source) == ("9.0", "folder")
    assert global_fp_lib_table_path(
        executable, environ=environ, version_runner=lambda _p: "10.0.6") == from_cli.path
    with pytest.raises(KiCadLibraryError) as unknown:
        resolve_global_fp_lib_table(
            tmp_path / "bin" / "kicad-cli.exe", environ=environ,
            version_runner=lambda _p: "garbage")
    assert unknown.value.code == "version_unknown"
