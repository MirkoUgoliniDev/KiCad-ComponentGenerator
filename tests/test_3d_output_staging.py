"""@brief A 3D run never leaves a stale or truncated model that looks current.

Contract under test (kicadgen.cad3d.step_generator._run_freecad and the
FreeCAD child): the previous STEP/WRL/preview JSON are deleted before the
child starts; each export is written under a staging name and renamed only
once complete; a failed or cancelled run leaves none of the three; a WRL whose
export failed is absent, not the old one. On the delivery side,
kicad_library.discover_component_artifacts refuses a STEP without the
ISO 10303-21 trailer.

No test needs FreeCAD except the last one: a Python child plays freecadcmd.
"""

import ast
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from kicadgen.cad3d import process_log, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.step_generator import Cad3DError, staging_path
from kicadgen.services.kicad_library import (
    KiCadLibraryError, discover_component_artifacts, step_file_is_complete,
)

CAD_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"
NAME = "Demo"

#: Minimal STEP accepted by colorize_step (two solids) and by the trailer check.
COMPLETE_STEP = (
    "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n"
    "#1 = ( GEOMETRIC_REPRESENTATION_CONTEXT(3) );\n"
    "#10 = MANIFOLD_SOLID_BREP('Body',#11);\n"
    "#11 = CLOSED_SHELL('',());\n"
    "ENDSEC;\nEND-ISO-10303-21;\n"
)
TRUNCATED_STEP = COMPLETE_STEP[: COMPLETE_STEP.index("#11")]

#: Fake freecadcmd. It honours the staging protocol of the real child unless
#: `direct` is set (a child that writes straight to the final names), and
#: stops after the phase named by `fail_at`.
_CHILD = """
import json, os, sys
data = json.load(open(sys.argv[-1], encoding="utf-8"))
staging = data.get("staging_paths", {{}}) if not {direct} else {{}}
def write(key, final, text):
    target = staging.get(key, final)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    if target != final:
        os.replace(target, final)
if {fail_at!r} == "step":
    with open(staging.get("step", data["step_path"]), "w", encoding="utf-8") as fh:
        fh.write({truncated!r})
    print("Traceback: killed during Import.export", flush=True)
    sys.exit(1)
write("step", data["step_path"], {complete!r})
print("KICADGEN_STEP_OK:" + data["step_path"], flush=True)
write("mesh", data["mesh_path"], json.dumps({{"parts": [{{"color": "#112233", "solids": 1}}]}}))
print("KICADGEN_MESH_OK:" + data["mesh_path"], flush=True)
if {fail_at!r} == "wrl":
    with open(staging.get("wrl", data["wrl_path"]), "w", encoding="utf-8") as fh:
        fh.write("#VRML V2.0 utf8 half")
    print("KICADGEN_WRL_FAILED:disk full", flush=True)
    sys.exit(0)
write("wrl", data["wrl_path"], "#VRML V2.0 utf8 NEW")
print("KICADGEN_WRL_OK:" + data["wrl_path"], flush=True)
"""


def _arm_child(monkeypatch, tmp_path, *, fail_at="", direct=False):
    """@brief Replace freecadcmd with the Python child above."""
    script = tmp_path / "fake_freecad.py"
    script.write_text(_CHILD.format(fail_at=fail_at, direct=direct,
                                    truncated=TRUNCATED_STEP, complete=COMPLETE_STEP),
                      encoding="utf-8")
    monkeypatch.setattr(step_generator, "_SCRIPT_PATH", script)


def _outputs(out_dir):
    """@brief Final paths of the three shipped outputs of NAME."""
    shapes = out_dir / f"{NAME}.3dshapes"
    return (shapes / f"{NAME}.step", shapes / f"{NAME}.wrl", shapes / f"{NAME}.preview.json")


def _seed_old_outputs(out_dir):
    """@brief Leave a complete, older model set, as a previous run would."""
    step, wrl, mesh = _outputs(out_dir)
    step.parent.mkdir(parents=True, exist_ok=True)
    step.write_text(COMPLETE_STEP.replace("'Body'", "'OLD'"), encoding="utf-8")
    wrl.write_text("#VRML V2.0 utf8 OLD", encoding="utf-8")
    mesh.write_text('{"parts": [], "old": true}', encoding="utf-8")


def _staged_leftovers(out_dir):
    return sorted(p.name for p in (out_dir / f"{NAME}.3dshapes").glob("*kicadgen-partial*"))


def _run(out_dir, **kwargs):
    return step_generator._run_freecad(
        NAME, [], Body3DParams(10.0, 8.0, 2.0), out_dir,
        freecadcmd_path=sys.executable, **kwargs)


# -- run contract ------------------------------------------------------------

@pytest.mark.parametrize("direct", [False, True], ids=["staged", "direct-write"])
def test_failed_run_leaves_no_3d_file_at_all(monkeypatch, tmp_path, direct):
    """Negative: a child that dies half-way through the STEP export (staged
    or, worst case, written straight to the final name) leaves neither a
    truncated STEP nor the old STEP/WRL/preview."""
    out = tmp_path / "out"
    _seed_old_outputs(out)
    _arm_child(monkeypatch, tmp_path, fail_at="step", direct=direct)
    with pytest.raises(Cad3DError):
        _run(out)
    assert [p.exists() for p in _outputs(out)] == [False, False, False]
    assert _staged_leftovers(out) == []


def test_successful_run_publishes_all_three_new_outputs(monkeypatch, tmp_path):
    """Positive: a complete run replaces every old output and leaves no
    staging file; the STEP is complete (colorized in place, atomically)."""
    out = tmp_path / "out"
    _seed_old_outputs(out)
    _arm_child(monkeypatch, tmp_path)
    result = _run(out)
    step, wrl, mesh = _outputs(out)
    assert result["step_path"] == step and result["wrl_path"] == wrl
    assert result["mesh_path"] == mesh
    assert "'OLD'" not in step.read_text(encoding="utf-8")
    assert "STYLED_ITEM" in step.read_text(encoding="utf-8")
    assert step_file_is_complete(step)
    assert wrl.read_text(encoding="utf-8").endswith("NEW")
    assert "old" not in mesh.read_text(encoding="utf-8")
    assert _staged_leftovers(out) == []


def test_wrl_failure_leaves_no_wrl_not_the_old_one(monkeypatch, tmp_path):
    """Negative for the WRL alone: the STEP ships, the old WRL does not."""
    out = tmp_path / "out"
    _seed_old_outputs(out)
    _arm_child(monkeypatch, tmp_path, fail_at="wrl")
    result = _run(out)
    step, wrl, mesh = _outputs(out)
    assert result["wrl_path"] is None and not wrl.exists()
    assert step.is_file() and mesh.is_file()
    assert _staged_leftovers(out) == []


def test_wrl_without_success_marker_is_removed(monkeypatch, tmp_path):
    """A WRL present under its final name but never confirmed by the child
    (direct writer that then reports a failure) is not a result."""
    out = tmp_path / "out"
    _arm_child(monkeypatch, tmp_path, fail_at="wrl", direct=True)
    result = _run(out)
    assert result["wrl_path"] is None and not _outputs(out)[1].exists()


def test_cancel_during_export_removes_the_partial_step(monkeypatch, tmp_path):
    """Negative, interactive path: the user cancels while the child is
    writing the STEP; the old outputs and the partial STEP are all gone."""
    out = tmp_path / "out"
    _seed_old_outputs(out)
    step = _outputs(out)[0]

    def killed_run_logged(command, log_path, **kwargs):
        # The child published a (truncated) STEP just before being killed.
        step.write_text(TRUNCATED_STEP, encoding="utf-8")
        staging_path(step).write_text(TRUNCATED_STEP, encoding="utf-8")
        raise process_log.ProcessCancelled("cancelled")

    monkeypatch.setattr(process_log, "run_logged", killed_run_logged)
    with pytest.raises(Cad3DError):
        _run(out, progress=lambda line: None)
    assert [p.exists() for p in _outputs(out)] == [False, False, False]
    assert _staged_leftovers(out) == []


def test_cancel_before_launch_keeps_the_previous_outputs(tmp_path):
    """Nothing was run, so nothing is discarded: the older-STEP warning of the
    KiCad tab still covers this case."""
    import threading
    out = tmp_path / "out"
    _seed_old_outputs(out)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cad3DError):
        _run(out, cancel=cancel)
    assert all(p.exists() for p in _outputs(out))


def test_colorize_failure_keeps_the_complete_uncoloured_step(monkeypatch, tmp_path):
    """The in-place colouring goes through a staging file: if publishing it
    fails, the STEP exported by FreeCAD is intact and a warning says so."""
    out = tmp_path / "out"
    _arm_child(monkeypatch, tmp_path)

    def refuse(source, target, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(step_generator, "replace_file", refuse)
    result = _run(out)
    text = _outputs(out)[0].read_text(encoding="utf-8")
    assert text == COMPLETE_STEP
    assert result["warnings"]
    assert _staged_leftovers(out) == []


def test_undeletable_old_output_stops_the_run(monkeypatch, tmp_path):
    """An old file that cannot be removed would survive a failure looking
    current: the run is refused instead of started."""
    out = tmp_path / "out"
    _seed_old_outputs(out)
    _arm_child(monkeypatch, tmp_path)
    real_unlink = Path.unlink

    def locked(self, *args, **kwargs):
        if self.suffix == ".wrl" and "partial" not in self.name:
            raise PermissionError("in use")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(Cad3DError) as caught:
        _run(out)
    assert "in use" in str(caught.value)
    step, wrl, mesh = _outputs(out)
    # The child never ran (it would have written a new STEP); every other
    # old output was still removed.
    assert not step.exists() and not mesh.exists() and wrl.exists()


# -- child-side publishing helpers -------------------------------------------

def _child_helpers():
    """@brief Execute the staging helpers of freecad_build_body without FreeCAD."""
    tree = ast.parse((CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8"))
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name in {"_staging_path", "_publish", "_discard"}]
    namespace = {"os": os, "time": time}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<build_body>", "exec"), namespace)
    return namespace


def test_child_staging_name_matches_the_parent_and_keeps_the_extension(tmp_path):
    helpers = _child_helpers()
    final = tmp_path / "X.step"
    assert helpers["_staging_path"]({}, "step", final) == str(staging_path(final))
    assert helpers["_staging_path"]({}, "step", final).endswith(".step")
    given = {"staging_paths": {"step": "given.step"}}
    assert helpers["_staging_path"](given, "step", final) == "given.step"


def test_child_publish_replaces_the_final_file_atomically(tmp_path):
    helpers = _child_helpers()
    final = tmp_path / "X.wrl"
    final.write_text("OLD", encoding="utf-8")
    staged = staging_path(final)
    staged.write_text("NEW", encoding="utf-8")
    helpers["_publish"](str(staged), str(final))
    assert final.read_text(encoding="utf-8") == "NEW" and not staged.exists()


def test_child_exports_go_through_staging_names():
    """Structural check: no export call in main() targets a final name."""
    source = (CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8")
    assert "Import.export(styled, step_staged)" in source
    assert "Part.export(objects, step_staged)" in source
    assert "_write_vrml(wrl_staged, tessellated)" in source
    assert "open(mesh_staged, \"w\"" in source
    assert "Import.export(styled, step_path)" not in source
    assert "_write_vrml(wrl_path, tessellated)" not in source


# -- delivery side -------------------------------------------------------------

def _component(tmp_path, step_text):
    root = tmp_path / NAME
    (root / f"{NAME}.pretty").mkdir(parents=True)
    (root / f"{NAME}.pretty" / f"{NAME}.kicad_mod").write_text(
        f'(footprint "{NAME}")\n', encoding="utf-8")
    (root / f"{NAME}.3dshapes").mkdir()
    (root / f"{NAME}.3dshapes" / f"{NAME}.step").write_text(step_text, encoding="utf-8")
    return root


def test_truncated_step_is_refused_with_its_own_code(tmp_path):
    root = _component(tmp_path, TRUNCATED_STEP)
    with pytest.raises(KiCadLibraryError) as caught:
        discover_component_artifacts(root)
    assert caught.value.code == "step_incomplete"
    assert "END-ISO-10303-21" in str(caught.value)


@pytest.mark.parametrize("step_text", [
    COMPLETE_STEP,
    COMPLETE_STEP.rstrip("\n"),
    COMPLETE_STEP.replace("\n", "\r\n") + "\r\n\r\n",
    COMPLETE_STEP.replace("DATA;\n", "DATA;\n" + "/* padding */\n" * 2000),
], ids=["lf", "no-final-newline", "crlf-trailing-blank", "larger-than-tail"])
def test_complete_step_is_accepted(tmp_path, step_text):
    root = _component(tmp_path, step_text)
    assert discover_component_artifacts(root).step_path.name == f"{NAME}.step"


def test_trailer_followed_by_more_data_is_refused(tmp_path):
    """The trailer must be the END of the file, not merely present: a file
    cut while appending after an earlier trailer is still incomplete."""
    root = _component(tmp_path, COMPLETE_STEP + "ISO-10303-21;\nHEADER;\n")
    with pytest.raises(KiCadLibraryError):
        discover_component_artifacts(root)


# -- real FreeCAD ------------------------------------------------------------

def _freecad():
    try:
        return step_generator.find_freecadcmd()
    except Exception:  # noqa: BLE001 - absent FreeCAD means skip
        return None


@pytest.mark.skipif(_freecad() is None, reason="FreeCAD not installed")
def test_real_freecad_run_publishes_complete_outputs(tmp_path):
    out = tmp_path / "out"
    _seed_old_outputs(out)
    result = step_generator._run_freecad(NAME, [], Body3DParams(10.0, 8.0, 2.0), out)
    step, wrl, mesh = _outputs(out)
    assert result["step_path"] == step and step_file_is_complete(step)
    assert "'OLD'" not in step.read_text(encoding="utf-8", errors="replace")
    assert result["wrl_path"] == wrl and "OLD" not in wrl.read_text(encoding="utf-8")
    assert result["mesh_path"] == mesh
    assert _staged_leftovers(out) == []
