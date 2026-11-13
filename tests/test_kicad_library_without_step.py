"""@brief Deliver a footprint to KiCad without a STEP model (user decision 2026-11-12).

A user without FreeCAD has a generated footprint but no STEP. The isolated
preview, the project installation and the global installation must still
deliver the footprint, with no ``(model ...)`` node pointing at a missing
file and no stale model left referenced. Each case has a twin WITH a STEP
that must keep today's behaviour, so the tests tell the two apart by the
presence of the model file, not by any number.
"""

from pathlib import Path
import subprocess
import sys

import pytest

from kicadgen.services import kicad_library
from kicadgen.services.kicad_library import (
    PREVIEW_DIR_NAME,
    KiCadLibraryError,
    create_preview_project,
    discover_component_artifacts,
    install_component_library,
    plan_component_install,
)


FOOTPRINT = """(footprint "Demo"
\t(version 20260206)
\t(generator "kicadgen_tool")
\t(layer "F.Cu")
\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""

#: Smallest STEP accepted by discover_component_artifacts (ISO trailer present).
STEP_BYTES = b"ISO-10303-21;\nEND-ISO-10303-21;\n"

#: Global table as KiCad writes it after its first start, with an unrelated row.
GLOBAL_TABLE = ('(fp_lib_table\n  (version 7)\n'
                '  (lib (name "Other")(type "KiCad")(uri "C:/libs/Other.pretty")'
                '(options "")(descr "Keep me"))\n)\n')


def _component(tmp_path: Path, *, step: bool, wrl: bool = False) -> Path:
    """Write a generated component folder, with or without its model files."""
    component = tmp_path / "Demo"
    (component / "Demo.pretty").mkdir(parents=True, exist_ok=True)
    (component / "Demo.pretty" / "Demo.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    shapes = component / "Demo.3dshapes"
    if step or wrl:
        shapes.mkdir(exist_ok=True)
    if step:
        (shapes / "Demo.step").write_bytes(STEP_BYTES)
    if wrl:
        (shapes / "Demo.wrl").write_bytes(b"WRL")
    return component


def _remove_model(component: Path) -> None:
    """Delete the component's model files, as a user without FreeCAD has them."""
    for suffix in (".step", ".wrl"):
        path = component / "Demo.3dshapes" / f"Demo{suffix}"
        if path.exists():
            path.unlink()


def _discover(component: Path):
    """Discover the artifacts the way the GUI does after the decision."""
    return discover_component_artifacts(component, "Demo", require_3d=False)


def _global_table(tmp_path: Path) -> Path:
    """Fake KiCad's global fp-lib-table in a temporary folder (never the real one)."""
    table = tmp_path / "config" / "fp-lib-table"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text(GLOBAL_TABLE, encoding="utf-8")
    return table


# --- discovery -------------------------------------------------------------


def test_discover_without_step_reports_absence_and_keeps_expected_path(tmp_path):
    component = _component(tmp_path, step=False)
    artifacts = _discover(component)
    assert artifacts.has_step is False
    assert artifacts.step_path == component.resolve() / "Demo.3dshapes" / "Demo.step"
    assert artifacts.wrl_path is None
    # Negative twin: require_3d keeps refusing a missing model.
    with pytest.raises(KiCadLibraryError) as error:
        discover_component_artifacts(component, "Demo", require_3d=True)
    assert error.value.code == "step_missing"


def test_discover_with_step_reports_presence(tmp_path):
    artifacts = _discover(_component(tmp_path, step=True, wrl=True))
    assert artifacts.has_step is True
    assert artifacts.wrl_path is not None


def test_wrl_without_step_is_not_an_artifact(tmp_path):
    # The WRL is the mesh twin of the STEP: alone it would never be referenced.
    artifacts = _discover(_component(tmp_path, step=False, wrl=True))
    assert artifacts.has_step is False and artifacts.wrl_path is None


def test_truncated_step_is_refused_even_without_require_3d(tmp_path):
    component = _component(tmp_path, step=True)
    (component / "Demo.3dshapes" / "Demo.step").write_bytes(b"ISO-10303-21;\nDATA;\n")
    with pytest.raises(KiCadLibraryError) as error:
        _discover(component)
    assert error.value.code == "step_incomplete"


# --- preview ---------------------------------------------------------------


def test_preview_without_step_has_no_model_node_and_copies_nothing(tmp_path):
    component = _component(tmp_path, step=False)
    preview = create_preview_project(_discover(component))
    board = preview.board_path.read_text(encoding="utf-8")
    assert "(model " not in board
    assert '(pad "1"' in board and "(at 100 100)" in board
    assert not (preview.directory / "Demo.step").exists()
    assert not (preview.directory / "Demo.wrl").exists()
    assert not (component / "fp-lib-table").exists()


def test_preview_with_step_keeps_model_node_and_copy(tmp_path):
    component = _component(tmp_path, step=True, wrl=True)
    preview = create_preview_project(_discover(component))
    board = preview.board_path.read_text(encoding="utf-8")
    assert board.count("(model ") == 1
    assert '(model "${KIPRJMOD}/Demo.step"' in board
    assert (preview.directory / "Demo.step").read_bytes() == STEP_BYTES
    assert (preview.directory / "Demo.wrl").read_bytes() == b"WRL"


def test_preview_without_step_removes_stale_model_of_earlier_preview(tmp_path):
    component = _component(tmp_path, step=True, wrl=True)
    first = create_preview_project(_discover(component))
    assert (first.directory / "Demo.step").is_file()
    _remove_model(component)
    second = create_preview_project(_discover(component))
    assert first.directory.name == PREVIEW_DIR_NAME
    assert not (second.directory / "Demo.step").exists()
    assert not (second.directory / "Demo.wrl").exists()
    assert "(model " not in second.board_path.read_text(encoding="utf-8")


def test_preview_in_caller_folder_never_deletes_files(tmp_path):
    # Only the program's own preview folder is cleaned: a caller-chosen one
    # may hold user content of the same name.
    component = _component(tmp_path, step=False)
    folder = tmp_path / "chosen"
    folder.mkdir()
    (folder / "Demo.step").write_bytes(b"USER FILE")
    preview = create_preview_project(_discover(component), folder)
    assert (folder / "Demo.step").read_bytes() == b"USER FILE"
    assert "(model " not in preview.board_path.read_text(encoding="utf-8")


def test_preview_drops_model_node_of_a_hand_edited_footprint(tmp_path):
    component = _component(tmp_path, step=False)
    footprint = component / "Demo.pretty" / "Demo.kicad_mod"
    footprint.write_text(FOOTPRINT.rstrip()[:-1]
                         + '\t(model "C:/old/Demo.step")\n)\n', encoding="utf-8")
    preview = create_preview_project(_discover(component))
    assert "(model " not in preview.board_path.read_text(encoding="utf-8")
    # The generated file in the project folder is never modified.
    assert '(model "C:/old/Demo.step")' in footprint.read_text(encoding="utf-8")


# --- project installation --------------------------------------------------


def test_project_install_without_step_has_no_model_and_no_shapes(tmp_path):
    component = _component(tmp_path, step=False)
    source_before = (component / "Demo.pretty" / "Demo.kicad_mod").read_bytes()
    project = tmp_path / "Board"
    artifacts = _discover(component)
    plan = plan_component_install(artifacts, project)
    assert plan.with_model is False and plan.removed == () and plan.replaced == ()
    result = install_component_library(artifacts, project)
    installed = result.footprint_path.read_text(encoding="utf-8")
    assert "(model " not in installed
    assert '(footprint "Demo"' in installed
    assert result.step_path is None and result.wrl_path is None
    assert not (project / "KiCadGen.3dshapes").exists()
    table = result.table_path.read_text(encoding="utf-8")
    assert '(uri "${KIPRJMOD}/KiCadGen.pretty")' in table
    assert (component / "Demo.pretty" / "Demo.kicad_mod").read_bytes() == source_before


def test_project_install_with_step_after_one_without_adds_the_model(tmp_path):
    component = _component(tmp_path, step=False)
    project = tmp_path / "Board"
    install_component_library(_discover(component), project)
    _component(tmp_path, step=True, wrl=True)
    artifacts = _discover(component)
    plan = plan_component_install(artifacts, project)
    assert plan.with_model is True and plan.removed == ()
    result = install_component_library(artifacts, project, overwrite=True)
    installed = result.footprint_path.read_text(encoding="utf-8")
    assert installed.count("(model ") == 1
    assert "${KIPRJMOD}/KiCadGen.3dshapes/Demo.step" in installed
    assert result.step_path.read_bytes() == STEP_BYTES
    assert result.wrl_path.read_bytes() == b"WRL"


def test_project_reinstall_without_step_removes_the_earlier_model(tmp_path):
    component = _component(tmp_path, step=True, wrl=True)
    project = tmp_path / "Board"
    first = install_component_library(_discover(component), project)
    old_step, old_wrl = first.step_path, first.wrl_path
    _remove_model(component)
    artifacts = _discover(component)
    plan = plan_component_install(artifacts, project)
    assert set(plan.removed) == {old_step, old_wrl}
    assert plan.replaced == (first.footprint_path,)
    # The deletion is confirmed like any overwrite.
    with pytest.raises(KiCadLibraryError) as error:
        install_component_library(artifacts, project)
    assert error.value.code == "files_exist"
    assert old_step.is_file()
    result = install_component_library(artifacts, project, overwrite=True)
    assert "(model " not in result.footprint_path.read_text(encoding="utf-8")
    assert not old_step.exists() and not old_wrl.exists()
    assert set(result.removed) == {old_step, old_wrl}
    assert result.step_path is None and result.wrl_path is None


def test_rollback_without_step_restores_the_earlier_model(tmp_path, monkeypatch):
    component = _component(tmp_path, step=True, wrl=True)
    project = tmp_path / "Board"
    first = install_component_library(_discover(component), project)
    footprint_before = first.footprint_path.read_bytes()
    table_before = first.table_path.read_bytes()
    _remove_model(component)

    def fail_table_update(*_args, **_kwargs):
        raise OSError("simulated table failure")

    monkeypatch.setattr(kicad_library, "update_fp_lib_table", fail_table_update)
    with pytest.raises(OSError, match="simulated table failure"):
        install_component_library(_discover(component), project, overwrite=True)
    assert first.footprint_path.read_bytes() == footprint_before
    assert first.step_path.read_bytes() == STEP_BYTES
    assert first.wrl_path.read_bytes() == b"WRL"
    assert first.table_path.read_bytes() == table_before
    assert not list(project.rglob("*.kicadgen.tmp"))
    assert not list(project.glob(".kicadgen-rollback-*"))


def test_failed_first_install_without_step_leaves_nothing(tmp_path, monkeypatch):
    component = _component(tmp_path, step=False)
    project = tmp_path / "Board"

    def fail_table_update(*_args, **_kwargs):
        raise OSError("simulated table failure")

    monkeypatch.setattr(kicad_library, "update_fp_lib_table", fail_table_update)
    with pytest.raises(OSError, match="simulated table failure"):
        install_component_library(_discover(component), project)
    assert not project.exists()


# --- global installation ---------------------------------------------------


def test_global_install_without_step_then_with_step(tmp_path):
    component = _component(tmp_path, step=False)
    destination = tmp_path / "GlobalLibraries"
    table = _global_table(tmp_path)
    result = install_component_library(
        _discover(component), destination, project_library=False, table_path=table)
    assert "(model " not in result.footprint_path.read_text(encoding="utf-8")
    assert result.step_path is None
    assert not (destination / "KiCadGen.3dshapes").exists()
    text = table.read_text(encoding="utf-8")
    assert 'Keep me' in text and result.footprint_path.parent.as_posix() in text

    _component(tmp_path, step=True)
    again = install_component_library(
        _discover(component), destination, project_library=False,
        table_path=table, overwrite=True)
    installed = again.footprint_path.read_text(encoding="utf-8")
    assert installed.count("(model ") == 1
    assert again.step_path.as_posix() in installed
    assert again.step_path.read_bytes() == STEP_BYTES


def test_global_reinstall_without_step_removes_the_earlier_model(tmp_path):
    component = _component(tmp_path, step=True)
    destination = tmp_path / "GlobalLibraries"
    table = _global_table(tmp_path)
    first = install_component_library(
        _discover(component), destination, project_library=False, table_path=table)
    _remove_model(component)
    result = install_component_library(
        _discover(component), destination, project_library=False,
        table_path=table, overwrite=True)
    assert "(model " not in result.footprint_path.read_text(encoding="utf-8")
    assert not first.step_path.exists()
    assert result.removed == (first.step_path,)
    assert 'Keep me' in table.read_text(encoding="utf-8")


def test_reinstall_without_step_removes_the_emptied_model_folder(tmp_path):
    """@brief Positive: the only model goes away -> the empty .3dshapes folder goes too."""
    component = _component(tmp_path, step=True)
    project = tmp_path / "Board"
    first = install_component_library(_discover(component), project)
    shapes = first.step_path.parent
    _remove_model(component)
    install_component_library(_discover(component), project, overwrite=True)
    assert not shapes.exists()


def test_reinstall_without_step_keeps_a_model_folder_with_other_models(tmp_path):
    """@brief Negative: another component's model still in .3dshapes -> folder and model stay."""
    component = _component(tmp_path, step=True)
    project = tmp_path / "Board"
    first = install_component_library(_discover(component), project)
    shapes = first.step_path.parent
    other = shapes / "Other.step"
    other.write_bytes(STEP_BYTES)
    _remove_model(component)
    install_component_library(_discover(component), project, overwrite=True)
    assert shapes.is_dir() and other.is_file()


def test_new_table_rows_name_the_program_kicadgen(tmp_path):
    """@brief Positive: a new row says «Generated by KiCadGen»; negative: never the old «KiCadGen-Tool»."""
    component = _component(tmp_path, step=False)
    result = install_component_library(_discover(component), tmp_path / "Board")
    table = result.table_path.read_text(encoding="utf-8")
    assert '(descr "Generated by KiCadGen")' in table
    assert "KiCadGen-Tool" not in table


def test_a_first_install_without_step_keeps_an_existing_empty_model_folder(tmp_path):
    """@brief Negative: an empty .3dshapes this install did not empty (made by the user) stays."""
    component = _component(tmp_path, step=False)
    project = tmp_path / "Board"
    shapes = project / "KiCadGen.3dshapes"
    shapes.mkdir(parents=True)
    result = install_component_library(_discover(component), project)
    assert result.step_path is None and shapes.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction")
def test_reinstall_without_step_never_removes_a_junction_model_folder(tmp_path):
    """@brief Negative: .3dshapes is a junction to a shared folder -> the link and the shared models stay."""
    component = _component(tmp_path, step=True)
    project = tmp_path / "Board"
    project.mkdir()
    shared = tmp_path / "shared-models"
    shared.mkdir()
    link = project / "KiCadGen.3dshapes"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(shared)],
                          capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("mklink /J not available: " + made.stderr)
    first = install_component_library(_discover(component), project)
    assert (shared / "Demo.step").is_file()
    # Only our model is behind the link: once it is removed the folder looks
    # empty, the case where rmdir would delete the junction itself.
    _remove_model(component)
    install_component_library(_discover(component), project, overwrite=True)
    assert not first.step_path.exists()
    assert link.exists() and shared.is_dir(), "the junction itself must never be removed"

