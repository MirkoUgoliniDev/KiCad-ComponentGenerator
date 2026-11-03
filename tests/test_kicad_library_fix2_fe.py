"""Second-review fixes of the KiCad library, version check and export history.

D1 same folder under another spelling, D2/D4 KiCad version in the component
check and a newer installed KiCad, D3 junctions never followed by the
rollback sweep, D7 no repeated shadowing warning on a re-install, D8 the
rollback folder of a failed install, D11 two instances on a corrupt history.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kicadgen.services import component_check as check
from kicadgen.services import kicad_export_history as history
from kicadgen.services import kicad_library
from kicadgen.services import system_dependencies
from kicadgen.services.kicad_library import (
    KiCadLibraryError,
    discover_component_artifacts,
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


def _artifacts(tmp_path: Path):
    """Generated component with footprint and STEP."""
    component = tmp_path / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.3dshapes").mkdir()
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    (component / "Demo.3dshapes" / "Demo.step").write_bytes(STEP_BYTES)
    return discover_component_artifacts(component)


def _table(path: Path, *rows: str) -> Path:
    """Write a footprint library table holding `rows`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("(fp_lib_table\n  (version 7)\n" + "".join(
        f"  {row}\n" for row in rows) + ")\n", encoding="utf-8")
    return path


def _junction(link: Path, target: Path) -> None:
    """Create a directory junction (no admin rights needed) or skip."""
    if sys.platform != "win32":
        pytest.skip("junctions exist on Windows only")
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError) as exc:
        pytest.skip(f"cannot create a junction here: {exc}")


# --- D1: the same folder through a junction -------------------------------------

def test_reinstall_through_a_junction_is_not_a_conflict(tmp_path):
    artifacts = _artifacts(tmp_path)
    real = tmp_path / "RealLibs"
    real.mkdir()
    link = tmp_path / "LinkedLibs"
    _junction(link, real)
    table = _table(tmp_path / "cfg" / "fp-lib-table")
    install_component_library(artifacts, real, project_library=False, table_path=table)
    before = table.read_text(encoding="utf-8")
    # Same folder, other spelling: no conflict, row kept as the user wrote it.
    install_component_library(artifacts, link, project_library=False,
                              table_path=table, overwrite=True)
    assert table.read_text(encoding="utf-8") == before
    assert (real / "KiCadGen.pretty").as_posix() in before
    # Negative: a really different folder is still a conflict.
    with pytest.raises(KiCadLibraryError) as caught:
        install_component_library(artifacts, tmp_path / "Other", project_library=False,
                                  table_path=table)
    assert caught.value.code == "library_entry_conflict"


def test_same_place_through_a_monkeypatched_resolve(tmp_path, monkeypatch):
    mapped = tmp_path / "Mapped"
    real = tmp_path / "Real"
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        # A SUBST/mapped drive: the mapped spelling resolves to the real one.
        if str(self).startswith(str(mapped)):
            return real / self.relative_to(mapped)
        return real_resolve(self, strict=strict)
    monkeypatch.setattr(Path, "resolve", resolve)
    table = _table(tmp_path / "fp-lib-table",
                   f'(lib (name "KiCadGen")(type "KiCad")(uri "{(real / "K.pretty").as_posix()}"))')
    before = table.read_bytes()
    update_fp_lib_table(table, "KiCadGen", (mapped / "K.pretty").as_posix())
    assert table.read_bytes() == before
    # Negative: another folder under the mapped drive is a conflict.
    with pytest.raises(KiCadLibraryError):
        update_fp_lib_table(table, "KiCadGen", (mapped / "Other.pretty").as_posix())


def test_kiprjmod_row_is_never_equated_with_an_absolute_project_uri(tmp_path):
    # Portability: a project row written as an absolute path is NOT the
    # ${KIPRJMOD} row the install registers, even if it is the same folder.
    project = tmp_path / "Board"
    table = _table(project / "fp-lib-table",
                   f'(lib (name "KiCadGen")(type "KiCad")(uri "{(project / "KiCadGen.pretty").as_posix()}"))')
    with pytest.raises(KiCadLibraryError) as caught:
        update_fp_lib_table(table, "KiCadGen", "${KIPRJMOD}/KiCadGen.pretty")
    assert caught.value.code == "library_entry_conflict"


def test_shadowing_ignores_a_global_row_reaching_the_project_through_a_junction(tmp_path):
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "Board"
    (project / "KiCadGen.pretty").mkdir(parents=True)
    link = tmp_path / "BoardLink"
    _junction(link, project)
    global_table = _table(
        tmp_path / "cfg" / "fp-lib-table",
        f'(lib (name "KiCadGen")(type "KiCad")(uri "{(link / "KiCadGen.pretty").as_posix()}"))')
    assert plan_component_install(artifacts, project, other_table_path=global_table).shadowing is None
    elsewhere = _table(tmp_path / "cfg2" / "fp-lib-table",
                       '(lib (name "KiCadGen")(type "KiCad")(uri "D:/Libs/KiCadGen.pretty"))')
    assert plan_component_install(artifacts, project, other_table_path=elsewhere).shadowing is not None


# --- D3: junctions are never followed by the sweep ------------------------------

def test_sweep_skips_a_junction_named_like_a_rollback_folder(tmp_path):
    root = tmp_path / "Board"
    root.mkdir()
    victim = tmp_path / "UserData"
    victim.mkdir()
    (victim / "precious.kicad_mod").write_bytes(b"keep")
    (victim / kicad_library.ROLLBACK_DISPOSABLE_MARKER).write_bytes(b"")
    stale = 1_000_000.0
    os.utime(victim / kicad_library.ROLLBACK_DISPOSABLE_MARKER, (stale, stale))
    link = root / (kicad_library.ROLLBACK_DIR_PREFIX + "d" * 32)
    _junction(link, victim)
    # Positive control: a real old marked folder of ours is swept.
    ours = root / (kicad_library.ROLLBACK_DIR_PREFIX + "e" * 32)
    ours.mkdir()
    (ours / kicad_library.ROLLBACK_DISPOSABLE_MARKER).write_bytes(b"")
    os.utime(ours / kicad_library.ROLLBACK_DISPOSABLE_MARKER, (stale, stale))
    removed = kicad_library._sweep_stale_rollback_dirs(root)
    assert removed == [ours]
    assert (victim / "precious.kicad_mod").read_bytes() == b"keep"


def test_remove_tree_refuses_a_junction_and_does_not_enter_one_inside(tmp_path):
    victim = tmp_path / "UserData"
    victim.mkdir()
    (victim / "precious.step").write_bytes(b"keep")
    link = tmp_path / "Link"
    _junction(link, victim)
    assert kicad_library._remove_tree_quietly(link) is False
    assert (victim / "precious.step").is_file()
    ours = tmp_path / "Ours"
    (ours / "sub").mkdir(parents=True)
    (ours / "sub" / "copy.step").write_bytes(b"copy")
    _junction(ours / "inner", victim)
    assert kicad_library._remove_tree_quietly(ours) is True
    assert not ours.exists()
    assert (victim / "precious.step").read_bytes() == b"keep"


# --- D7: no repeated shadowing question on a re-install --------------------------

def test_reinstall_does_not_repeat_the_shadowing_warning(tmp_path):
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "Board"
    global_table = _table(tmp_path / "cfg" / "fp-lib-table",
                          '(lib (name "KiCadGen")(type "KiCad")(uri "D:/Libs/KiCadGen.pretty"))')
    first = plan_component_install(artifacts, project, other_table_path=global_table)
    assert first.shadowing is not None
    install_component_library(artifacts, project)
    again = plan_component_install(artifacts, project, other_table_path=global_table)
    assert again.shadowing is None
    # Negative: a project table mapping KiCadGen elsewhere still warns.
    other = tmp_path / "Board2"
    _table(other / "fp-lib-table",
           '(lib (name "KiCadGen")(type "KiCad")(uri "${KIPRJMOD}/Elsewhere.pretty"))')
    assert plan_component_install(
        artifacts, other, other_table_path=global_table).shadowing is not None


# --- D2/D4: KiCad version ----------------------------------------------------------

def test_project_plan_carries_the_detected_kicad_version(tmp_path):
    artifacts = _artifacts(tmp_path)
    old = plan_component_install(artifacts, tmp_path / "Board", detected_kicad_version="9.0")
    assert old.detected_kicad_too_old and not old.kicad_too_old
    new = plan_component_install(artifacts, tmp_path / "Board", detected_kicad_version="10.0")
    assert not new.detected_kicad_too_old
    unknown = plan_component_install(artifacts, tmp_path / "Board")
    assert unknown.detected_kicad_version is None and not unknown.detected_kicad_too_old


def _runner(versions):
    return lambda path: versions.get(Path(path).name)


def test_too_old_cli_names_a_newer_installed_kicad(tmp_path):
    old_cli, new_cli = tmp_path / "old-cli.exe", tmp_path / "new-cli.exe"
    versions = {"old-cli.exe": "9.0.7\n", "new-cli.exe": "10.0.2\n"}
    found = system_dependencies.check_kicad_version(
        old_cli, version_runner=_runner(versions), installed_finder=lambda: str(new_cli))
    assert found.supported is False
    assert found.newer_cli == str(new_cli) and found.newer_version == "10.0"
    # Negatives: the newest installed is the same old one, or also too old.
    same = system_dependencies.check_kicad_version(
        old_cli, version_runner=_runner(versions), installed_finder=lambda: str(old_cli))
    assert same.newer_cli is None
    versions["new-cli.exe"] = "9.0.1\n"
    older = system_dependencies.check_kicad_version(
        old_cli, version_runner=_runner(versions), installed_finder=lambda: str(new_cli))
    assert older.newer_cli is None
    # A supported CLI does not look further.
    def never():
        raise AssertionError("not asked")
    versions["new-cli.exe"] = "10.0.2\n"
    assert system_dependencies.check_kicad_version(
        new_cli, version_runner=_runner(versions), installed_finder=never).newer_cli is None


def _verdict(version, supported, newer_cli=None, newer_version=None):
    return system_dependencies.KiCadVersionCheck(
        version=version, minimum=kicad_library.MINIMUM_KICAD_VERSION,
        supported=supported, newer_cli=newer_cli, newer_version=newer_version)


def _versioned_check(tmp_path, monkeypatch, verdict):
    monkeypatch.setattr(check, "find_kicad_cli", lambda: "old-cli")
    monkeypatch.setattr(check, "check_kicad_version", lambda _cli: verdict)
    used = []

    def run(args, **_kwargs):
        used.append(args[0])
        raise OSError("no real KiCad in this test")
    monkeypatch.setattr(check.subprocess, "run", run)
    results = check.verify_component(tmp_path, "X", [])
    return [r for r in results if r.title == "Versione KiCad"], used


def test_component_check_reports_a_too_old_kicad(tmp_path, monkeypatch):
    rows, used = _versioned_check(tmp_path, monkeypatch, _verdict("9.0", False))
    assert [r.status for r in rows] == ["ERROR"]
    assert "9.0" in rows[0].detail and kicad_library.MINIMUM_KICAD_VERSION in rows[0].detail


def test_component_check_uses_a_newer_installed_kicad(tmp_path, monkeypatch):
    rows, used = _versioned_check(
        tmp_path, monkeypatch, _verdict("9.0", False, "new-cli", "10.0"))
    assert [r.status for r in rows] == ["WARN"]
    assert set(used) <= {"new-cli"}


@pytest.mark.parametrize("verdict", [_verdict("10.0", True), _verdict(None, None)])
def test_component_check_gives_no_version_verdict_when_supported_or_unknown(
        tmp_path, monkeypatch, verdict):
    rows, _used = _versioned_check(tmp_path, monkeypatch, verdict)
    assert rows == []


# --- D8: rollback folder of a failed install -------------------------------------

def test_rollback_left_of_reads_the_error_context(tmp_path):
    left = tmp_path / ".kicadgen-rollback-x"
    error = KiCadLibraryError("x", code="install_write_refused", rollback_left=left)
    assert kicad_library.rollback_left_of(error) == left
    assert kicad_library.rollback_left_of(KiCadLibraryError("x", code="files_exist")) is None
    assert kicad_library.rollback_left_of(OSError("x")) is None


# --- D11: two instances on a corrupt history ---------------------------------------

def test_history_moved_by_another_instance_is_read_again(tmp_path, monkeypatch):
    target = tmp_path / "kicad_exports.json"
    target.write_bytes(b"[ broken")
    real_rename = os.rename

    def other_instance_wins(source, destination):
        # The other window moved the corrupt file and wrote its valid history.
        real_rename(source, str(destination) + "-other")
        Path(source).write_text(json.dumps([{"component": "Other"}]), encoding="utf-8")
        monkeypatch.setattr(history.os, "rename", real_rename)
        raise FileNotFoundError(source)
    monkeypatch.setattr(history.os, "rename", other_instance_wins)
    assert history.record_export(component="Mine", kind="preview", source="s",
                                 destination="d", path=target)
    assert [r["component"] for r in history.load_history(target)] == ["Mine", "Other"]


def test_valid_history_moved_aside_by_a_late_instance_keeps_its_records(tmp_path, monkeypatch):
    target = tmp_path / "kicad_exports.json"
    target.write_bytes(b"[ broken")
    real_rename = os.rename

    def replaced_before_rename(source, destination):
        # Between this read and this rename the other window wrote a valid file.
        Path(source).write_text(json.dumps([{"component": "Other"}]), encoding="utf-8")
        real_rename(source, destination)
    monkeypatch.setattr(history.os, "rename", replaced_before_rename)
    assert history.record_export(component="Mine", kind="preview", source="s",
                                 destination="d", path=target)
    assert [r["component"] for r in history.load_history(target)] == ["Mine", "Other"]
