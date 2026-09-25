"""@brief Verify isolated KiCad previews and safe library installation."""

from pathlib import Path

import pytest

from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import (
    KiCadLibraryError,
    attach_3d_model,
    create_preview_project,
    discover_component_artifacts,
    global_fp_lib_table_path,
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


def _artifacts(tmp_path: Path):
    component = tmp_path / "Demo"
    pretty = component / "Demo.pretty"
    shapes = component / "Demo.3dshapes"
    pretty.mkdir(parents=True)
    shapes.mkdir()
    (pretty / "Demo.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    (shapes / "Demo.step").write_bytes(b"STEP")
    (shapes / "Demo.wrl").write_bytes(b"WRL")
    return discover_component_artifacts(component)


def test_discover_requires_matching_footprint_and_step(tmp_path):
    component = tmp_path / "Demo"
    (component / "Demo.pretty").mkdir(parents=True)
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(
        FOOTPRINT, encoding="utf-8")
    with pytest.raises(KiCadLibraryError, match="STEP model not found"):
        discover_component_artifacts(component)


def test_attach_3d_model_replaces_existing_root_model():
    original = attach_3d_model(FOOTPRINT, "old.step")
    updated = attach_3d_model(original, "${KIPRJMOD}/Models/Demo.step")
    assert updated.count("\n\t(model ") == 1
    assert "old.step" not in updated
    assert '"${KIPRJMOD}/Models/Demo.step"' in updated
    assert "(offset (xyz 0 0 0))" in updated


def test_preview_project_is_isolated_and_embeds_model_reference(tmp_path):
    artifacts = _artifacts(tmp_path)
    preview = create_preview_project(artifacts)
    board = preview.board_path.read_text(encoding="utf-8")
    assert preview.directory == artifacts.component_dir / ".kicad-preview"
    assert preview.project_path.is_file()
    assert (preview.directory / "Demo.step").read_bytes() == b"STEP"
    assert "(kicad_pcb" in board
    assert '(model "${KIPRJMOD}/Demo.step"' in board
    assert "(at 100 100)" in board
    assert not (artifacts.component_dir / "fp-lib-table").exists()


def test_project_install_creates_portable_library_and_table(tmp_path):
    artifacts = _artifacts(tmp_path)
    project = tmp_path / "BoardProject"
    result = install_component_library(artifacts, project)
    footprint = result.footprint_path.read_text(encoding="utf-8")
    table = result.table_path.read_text(encoding="utf-8")
    assert result.identifier == "KiCadGen:Demo"
    assert '${KIPRJMOD}/KiCadGen.3dshapes/Demo.step' in footprint
    assert result.step_path.read_bytes() == b"STEP"
    assert result.wrl_path.read_bytes() == b"WRL"
    assert '(name "KiCadGen")' in table
    assert '(uri "${KIPRJMOD}/KiCadGen.pretty")' in table


def test_global_install_uses_absolute_paths_and_requested_table(tmp_path):
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "GlobalLibraries"
    table = tmp_path / "config" / "fp-lib-table"
    # Contract changed (fix F4): a global install no longer creates a missing
    # global table, because KiCad would then skip its first-run library setup.
    # The table therefore exists here, as it does after KiCad's first start.
    table.parent.mkdir(parents=True)
    table.write_text("(fp_lib_table\n  (version 7)\n)\n", encoding="utf-8")
    result = install_component_library(
        artifacts, destination, project_library=False, table_path=table)
    footprint = result.footprint_path.read_text(encoding="utf-8")
    assert result.step_path.resolve().as_posix() in footprint
    assert result.footprint_path.parent.resolve().as_posix() in table.read_text(
        encoding="utf-8")


def test_install_refuses_existing_component_without_overwrite(tmp_path):
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    install_component_library(artifacts, destination)
    with pytest.raises(KiCadLibraryError, match="already exist"):
        install_component_library(artifacts, destination)
    result = install_component_library(artifacts, destination, overwrite=True)
    assert result.footprint_path.is_file()


def test_library_table_update_preserves_entries_and_does_not_duplicate(tmp_path):
    table = tmp_path / "fp-lib-table"
    table.write_text(
        '(fp_lib_table\n  (version 7)\n'
        '  (lib (name "Existing")(type "KiCad")(uri "/old.pretty")'
        '(options "")(descr "Keep me"))\n)\n',
        encoding="utf-8",
    )
    assert update_fp_lib_table(table, "KiCadGen", "${KIPRJMOD}/KiCadGen.pretty")
    first = table.read_text(encoding="utf-8")
    assert "Existing" in first and "Keep me" in first
    assert first.count('(name "KiCadGen")') == 1
    assert not update_fp_lib_table(table, "KiCadGen", "${KIPRJMOD}/KiCadGen.pretty")
    assert table.read_text(encoding="utf-8") == first
    assert table.with_name("fp-lib-table.kicadgen-backup").is_file()


def test_install_restores_existing_component_when_table_update_fails(
        tmp_path, monkeypatch):
    """@brief Roll back replaced files if the final table write cannot complete."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    first = install_component_library(artifacts, destination)
    first.footprint_path.write_text("old footprint", encoding="utf-8")
    first.step_path.write_bytes(b"OLD STEP")
    first.wrl_path.write_bytes(b"OLD WRL")
    table_before = first.table_path.read_text(encoding="utf-8")

    def fail_table_update(*_args, **_kwargs):
        raise OSError("simulated table failure")

    monkeypatch.setattr(kicad_library, "update_fp_lib_table", fail_table_update)
    with pytest.raises(OSError, match="simulated table failure"):
        install_component_library(artifacts, destination, overwrite=True)

    assert first.footprint_path.read_text(encoding="utf-8") == "old footprint"
    assert first.step_path.read_bytes() == b"OLD STEP"
    assert first.wrl_path.read_bytes() == b"OLD WRL"
    assert first.table_path.read_text(encoding="utf-8") == table_before
    # A fully successful rollback no longer needs its copies.
    assert not list(destination.glob(".kicadgen-rollback-*"))


def test_install_cleans_staging_files_when_copy_fails(tmp_path, monkeypatch):
    """@brief Leave no partial component when artifact staging fails."""
    artifacts = _artifacts(tmp_path)
    destination = tmp_path / "Project"
    real_copy = kicad_library.shutil.copy2

    def fail_step_copy(source, target, *args, **kwargs):
        if Path(source) == artifacts.step_path:
            raise OSError("simulated copy failure")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(kicad_library.shutil, "copy2", fail_step_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        install_component_library(artifacts, destination)

    assert not list(destination.rglob("*.kicadgen.tmp"))
    assert not list(destination.glob(".kicadgen-rollback-*"))
    assert not (destination / "KiCadGen.pretty" / "Demo.kicad_mod").exists()


def test_global_table_path_uses_detected_kicad_version(tmp_path):
    """@brief Resolve the Windows global table without hard-coding a version."""
    executable = Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe")
    result = global_fp_lib_table_path(
        executable, environ={"APPDATA": str(tmp_path)}, platform="win32")
    assert result == tmp_path / "kicad" / "10.0" / "fp-lib-table"


def test_global_table_path_rejects_an_unknown_version(tmp_path):
    """@brief Refuse global writes when the KiCad version cannot be resolved."""
    with pytest.raises(KiCadLibraryError, match="installed KiCad version"):
        global_fp_lib_table_path(
            tmp_path / "kicad-cli.exe", environ={}, platform="win32")
