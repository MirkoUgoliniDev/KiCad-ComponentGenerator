"""@brief Fixes of the KiCad delivery review of 2026-10-29 (L1-L5).

@details Every test writes only below pytest's temporary directory; KiCad's
real configuration is never touched (global tables are explicit paths).
Hard links are made with ``os.link`` (no privilege needed on NTFS); symbolic
links are faked through the module's own link probes, as in
tests/test_review_1027_kicad_library.py, because an ordinary Windows account
may not create one.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from kicadgen import fsutil
from kicadgen.services import component_check, kicad_library
from kicadgen.services.kicad_library import (
    KiCadLibraryError,
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
STEP_BYTES = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
EMPTY_TABLE = "(fp_lib_table\n  (version 7)\n)\n"
PROJECT_URI = "${KIPRJMOD}/KiCadGen.pretty"


def _artifacts(base: Path):
    """Create one generated component under `base`."""
    component = base / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.3dshapes").mkdir()
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    (component / "Demo.3dshapes" / "Demo.step").write_bytes(STEP_BYTES)
    return discover_component_artifacts(component)


def _hard_link_or_skip(target: Path, link: Path) -> None:
    """Create a hard link, or skip when the volume does not support one."""
    try:
        os.link(target, link)
    except OSError as exc:
        pytest.skip(f"hard links unavailable here: {exc}")


def _hard_linked_table(tmp_path: Path) -> tuple[Path, Path]:
    """A table with a second hard-linked name (a dotfiles checkout)."""
    table = tmp_path / "cfg" / "fp-lib-table"
    table.parent.mkdir()
    table.write_text(EMPTY_TABLE, encoding="utf-8")
    other = tmp_path / "dotfiles-table"
    _hard_link_or_skip(table, other)
    return table, other


def _failing_copy(monkeypatch, *, fail_calls: set[int]) -> list[int]:
    """Make ``shutil.copyfileobj`` of the module write a partial table, then fail.

    @param fail_calls 1-based call numbers that fail; the others copy normally.
    @return Call counter (one element).
    """
    real_copy = shutil.copyfileobj
    calls = [0]

    def copy(source, target, *args):
        calls[0] += 1
        if calls[0] in fail_calls:
            target.write(b"(fp_lib_table\n  (vers")
            raise OSError(28, "No space left on device")
        return real_copy(source, target, *args)

    monkeypatch.setattr(kicad_library.shutil, "copyfileobj", copy)
    return calls


def _scratch_files(folder: Path) -> list[Path]:
    """Our scratch files left in `folder`."""
    return [p for p in folder.iterdir() if p.name.endswith(kicad_library.TEMPORARY_SUFFIX)]


# ------------------------------------------------- L1 hard-linked table write

def test_failed_in_place_write_puts_the_table_back_under_both_names(tmp_path, monkeypatch):
    """@brief L1: a partial in-place write is undone from the staged copy before it is removed."""
    table, other = _hard_linked_table(tmp_path)
    _failing_copy(monkeypatch, fail_calls={1})
    with pytest.raises(OSError):
        update_fp_lib_table(table, "KiCadGen", "C:/x/KiCadGen.pretty", create_missing=False)
    assert table.read_text(encoding="utf-8") == EMPTY_TABLE
    assert other.read_text(encoding="utf-8") == EMPTY_TABLE, "the other name was restored too"
    assert os.stat(table).st_nlink == 2, "the link survived"
    assert not _scratch_files(table.parent)


def test_unrecoverable_in_place_write_keeps_the_original_as_backup(tmp_path, monkeypatch):
    """@brief L1: when even the restore fails, the pre-change copy survives as .kicadgen-backup."""
    table, _other = _hard_linked_table(tmp_path)
    _failing_copy(monkeypatch, fail_calls=set(range(1, 100)))
    with pytest.raises(OSError):
        update_fp_lib_table(table, "KiCadGen", "C:/x/KiCadGen.pretty", create_missing=False)
    assert table.read_text(encoding="utf-8") != EMPTY_TABLE, "the disk stayed full"
    backup = table.with_name("fp-lib-table.kicadgen-backup")
    assert backup.read_text(encoding="utf-8") == EMPTY_TABLE, "the only good copy was kept"


def test_hard_linked_table_normal_update_is_unchanged(tmp_path):
    """@brief L1 negative: a successful in-place write updates both names and rotates the backup."""
    table, other = _hard_linked_table(tmp_path)
    assert update_fp_lib_table(table, "KiCadGen", "C:/x/KiCadGen.pretty", create_missing=False)
    assert "KiCadGen" in table.read_text(encoding="utf-8")
    assert other.read_bytes() == table.read_bytes()
    assert table.with_name("fp-lib-table.kicadgen-backup").read_text(encoding="utf-8") == EMPTY_TABLE
    assert not _scratch_files(table.parent)


def test_plain_table_failed_replace_leaves_the_table_untouched(tmp_path, monkeypatch):
    """@brief L1 negative: an atomic replace that fails never touched the table; nothing is rewritten."""
    table = tmp_path / "fp-lib-table"
    table.write_text(EMPTY_TABLE, encoding="utf-8")
    writes: list[Path] = []
    real_write = kicad_library._atomic_write_bytes

    def refuse(target, payload):
        writes.append(target)
        if len(writes) == 1:
            raise PermissionError(13, "refused", str(target))
        return real_write(target, payload)

    monkeypatch.setattr(kicad_library, "_atomic_write_bytes", refuse)
    with pytest.raises(PermissionError):
        update_fp_lib_table(table, "KiCadGen", "/x.pretty")
    assert table.read_text(encoding="utf-8") == EMPTY_TABLE
    assert len(writes) == 1, "a table still holding the original is not rewritten"
    assert not table.with_name("fp-lib-table.kicadgen-backup").exists()
    assert not _scratch_files(tmp_path)


# ------------------------------------------------------- L2 disabled table row

def _disabled_project(tmp_path: Path, uri: str = PROJECT_URI) -> tuple[Path, Path]:
    """A project whose table has a disabled «KiCadGen» row pointing to `uri`."""
    project = tmp_path / "proj"
    project.mkdir()
    table = project / "fp-lib-table"
    table.write_text(
        "(fp_lib_table\n  (version 7)\n"
        f'  (lib (name "KiCadGen")(type "KiCad")(uri "{uri}")(options "keep=1")'
        '(descr "old")(disabled))\n'
        '  (lib (name "Other")(type "KiCad")(uri "/o.pretty")(options "")(descr "")(disabled))\n)\n',
        encoding="utf-8")
    return project, table


def test_disabled_row_with_the_same_uri_needs_confirmation(tmp_path):
    """@brief L2: a disabled matching row is a conflict; nothing is installed without confirmation."""
    project, table = _disabled_project(tmp_path)
    before = table.read_bytes()
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(_artifacts(tmp_path / "src"), project)
    assert caught.value.code == "library_entry_disabled"
    context = caught.value.context
    assert context["library"] == "KiCadGen"
    assert context["old_uri"] == PROJECT_URI and context["new_uri"] == PROJECT_URI
    assert table.read_bytes() == before
    assert not (project / "KiCadGen.pretty").exists()


def test_confirmed_install_enables_the_row_and_keeps_its_fields(tmp_path):
    """@brief L2: confirming removes only ``(disabled)`` of that row; other rows untouched."""
    project, table = _disabled_project(tmp_path)
    install_component_library(_artifacts(tmp_path / "src"), project,
                              replace_library_entry=True)
    entry = find_library_entry(table, "KiCadGen")
    assert not entry.disabled and entry.uri == PROJECT_URI
    text = table.read_text(encoding="utf-8")
    assert '(options "keep=1")' in text and '(descr "old")' in text
    assert find_library_entry(table, "Other").disabled, "an unrelated disabled row stays disabled"


def test_disabled_row_elsewhere_is_repointed_and_enabled_on_confirmation(tmp_path):
    """@brief L2: a disabled row pointing elsewhere uses the same code, then repoint + enable."""
    project, table = _disabled_project(tmp_path, "/shared/KiCadGen.pretty")
    with pytest.raises(KiCadLibraryError) as caught:
        update_fp_lib_table(table, "KiCadGen", PROJECT_URI)
    assert caught.value.code == "library_entry_disabled"
    assert caught.value.context["old_uri"] == "/shared/KiCadGen.pretty"
    assert update_fp_lib_table(table, "KiCadGen", PROJECT_URI, replace_entry=True)
    entry = find_library_entry(table, "KiCadGen")
    assert entry.uri == PROJECT_URI and not entry.disabled


def test_enabled_and_hidden_rows_are_still_kept_verbatim(tmp_path):
    """@brief L2 negative: an enabled (even hidden) matching row is no conflict and is not rewritten."""
    table = tmp_path / "fp-lib-table"
    table.write_text(
        "(fp_lib_table\n  (version 7)\n"
        f'  (lib (name "KiCadGen")(type "KiCad")(uri "{PROJECT_URI}")(options "")'
        '(descr "mine")(hidden))\n)\n', encoding="utf-8")
    before = table.read_bytes()
    assert not update_fp_lib_table(table, "KiCadGen", PROJECT_URI)
    assert not update_fp_lib_table(table, "KiCadGen", PROJECT_URI, replace_entry=True)
    assert table.read_bytes() == before


# ------------------------------------------------- L3 transient refusal retry

def test_in_place_open_retries_a_transient_refusal(tmp_path, monkeypatch):
    """@brief L3: a target held for an instant is opened on a later attempt."""
    source = tmp_path / "source"
    source.write_bytes(b"NEW")
    target = tmp_path / "target"
    target.write_bytes(b"OLD")
    real_open = open
    refusals = [2]
    pauses: list[float] = []

    def flaky_open(path, mode="r", *args, **kwargs):
        if Path(path) == target and refusals[0]:
            refusals[0] -= 1
            raise PermissionError(13, "held open", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    monkeypatch.setattr(kicad_library, "_retry_pause", pauses.append)
    kicad_library._write_in_place(source, target)
    assert target.read_bytes() == b"NEW"
    assert pauses == [fsutil.REPLACE_FIRST_PAUSE_S, fsutil.REPLACE_FIRST_PAUSE_S * 2]


def test_in_place_open_gives_up_after_the_bounded_attempts(tmp_path, monkeypatch):
    """@brief L3 negative: a lasting refusal is raised after REPLACE_ATTEMPTS; the target is untouched."""
    source = tmp_path / "source"
    source.write_bytes(b"NEW")
    target = tmp_path / "target"
    target.write_bytes(b"OLD")
    real_open = open
    attempts = [0]

    def refused_open(path, mode="r", *args, **kwargs):
        if Path(path) == target:
            attempts[0] += 1
            raise PermissionError(13, "read-only", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refused_open)
    monkeypatch.setattr(kicad_library, "_retry_pause", lambda _seconds: None)
    with pytest.raises(PermissionError):
        kicad_library._write_in_place(source, target)
    assert attempts[0] == fsutil.REPLACE_ATTEMPTS
    assert target.read_bytes() == b"OLD"


def test_retry_transient_does_not_retry_other_errors():
    """@brief L3 negative: only PermissionError is transient; a full disk is raised at once."""
    calls = [0]

    def full_disk():
        calls[0] += 1
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError):
        fsutil.retry_transient(full_disk, sleep=lambda _s: None)
    assert calls[0] == 1


# ------------------------------------------------------ L4 footprint with BOM

def _check_footprint(tmp_path: Path, payload: bytes, monkeypatch):
    """Run the component check on a footprint with `payload`, without KiCad CLI."""
    monkeypatch.setattr(component_check, "find_kicad_cli", lambda: None)
    root = tmp_path / "comp"
    (root / "Demo.pretty").mkdir(parents=True)
    (root / "Demo.3dshapes").mkdir()
    (root / "Demo.pretty" / "Demo.kicad_mod").write_bytes(payload)
    (root / "Demo.3dshapes" / "Demo.step").write_bytes(STEP_BYTES)
    rows = [{"pin": "1", "name": "A", "electrical_type": "input",
             "electrical_type_reviewed": True}]
    return component_check.verify_component(root, "Demo", rows)


def _footprint_checks(results):
    """The «Footprint su disco» («footprint on disk») results."""
    return [r for r in results if r.title == "Footprint su disco"]


def test_footprint_with_bom_is_named_and_still_checked(tmp_path, monkeypatch):
    """@brief L4: a BOM is reported by name and the pads are still read."""
    results = _check_footprint(tmp_path, kicad_library.UTF8_BOM + FOOTPRINT.encode("utf-8"),
                               monkeypatch)
    checks = _footprint_checks(results)
    assert [c.status for c in checks] == ["WARN", "OK"]
    assert "BOM" in checks[0].detail and "EF BB BF" in checks[0].detail
    assert not any("non valido" in r.detail for r in results)
    pin_pad = [r for r in results if r.title == "Corrispondenza pin-pad"]
    assert pin_pad and pin_pad[0].status == "OK"


def test_footprint_without_bom_has_no_bom_warning(tmp_path, monkeypatch):
    """@brief L4 negative: a plain UTF-8 footprint gets only the OK row."""
    results = _check_footprint(tmp_path, FOOTPRINT.encode("utf-8"), monkeypatch)
    assert [c.status for c in _footprint_checks(results)] == ["OK"]


def test_footprint_that_is_not_a_footprint_is_still_refused(tmp_path, monkeypatch):
    """@brief L4 negative: a BOM in front of a non-footprint is still an error."""
    results = _check_footprint(
        tmp_path, kicad_library.UTF8_BOM + b"(symbol \"x\")\n", monkeypatch)
    assert [c.status for c in _footprint_checks(results)] == ["WARN", "ERROR"]


# ------------------------------------------- L5 dangling global-table link

def _fake_dangling_link(monkeypatch, link: Path) -> None:
    """Make the module see `link` as a dangling symbolic link."""
    def is_symlink(path: Path) -> bool:
        return Path(path) == link

    def resolve_link(path: Path) -> Path:
        raise FileNotFoundError(2, "dangling", str(path))

    monkeypatch.setattr(kicad_library, "_is_symlink", is_symlink)
    monkeypatch.setattr(kicad_library, "_resolve_link", resolve_link)


def test_dangling_global_table_link_is_named_as_a_broken_link(tmp_path, monkeypatch):
    """@brief L5: a dangling link in place of the global table is target_link_unresolved."""
    link = tmp_path / "cfg" / "fp-lib-table"
    link.parent.mkdir()
    _fake_dangling_link(monkeypatch, link)
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(_artifacts(tmp_path / "src"), tmp_path / "lib",
                                  project_library=False, table_path=link)
    assert caught.value.code == "target_link_unresolved"
    assert Path(caught.value.context["path"]) == link
    assert not (tmp_path / "lib").exists()


def test_really_missing_global_table_still_asks_to_start_kicad(tmp_path):
    """@brief L5 negative: no file and no link is still global_table_missing."""
    table = tmp_path / "cfg" / "fp-lib-table"
    table.parent.mkdir()
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(_artifacts(tmp_path / "src"), tmp_path / "lib",
                                  project_library=False, table_path=table)
    assert caught.value.code == "global_table_missing"
