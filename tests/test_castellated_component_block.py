"""@brief The «Componenti» block of a castellated module without photo bodies (2026-11-03).

Without components read from the photo, the module gets one block over its
outline, as tall as the module's total height minus the PCB. That volume is
an estimate, never a measurement: it now carries a note with its provenance,
and it is not built when the caller declares the total height undocumented.
"""
from kicadgen.cad3d import step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.castellated_module import CastellatedModuleParams, CastellatedSide


def _module():
    params = CastellatedModuleParams(
        name="Block", body_length=20.0, body_width=15.0, pcb_thickness=1.0,
        sides=[CastellatedSide(edge="left", count=3, pitch=2.54, first_pin=1)])
    params.validate()
    return params


def _generate(monkeypatch, tmp_path, height, declared=None):
    captured = {}

    def fake_run(name, pins, body, output_dir, **options):
        captured.update(options)
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    body = Body3DParams(length_mm=20.0, width_mm=15.0, height_mm=height)
    if declared is not None:
        body.height_declared = declared
    result = step_generator.generate_castellated_3d_model(_module(), body, tmp_path)
    blocks = [box for box in captured.get("extra_boxes", []) if box.get("name") == "Componenti"]
    notes = [note for note in result["notes"] if "Componenti" in note]
    return blocks, notes


def test_the_estimated_block_says_where_its_height_comes_from(monkeypatch, tmp_path):
    """@brief Positive: a block is built, and its note gives the estimate's provenance."""
    blocks, notes = _generate(monkeypatch, tmp_path, height=3.5)
    assert len(blocks) == 1 and blocks[0]["height_mm"] == 2.5
    assert len(notes) == 1
    assert "stimato" in notes[0] and "2.50" in notes[0] and "3.5" in notes[0]


def test_an_undeclared_total_height_builds_no_block(monkeypatch, tmp_path):
    """@brief Negative: the caller says the height is not documented -> no invented volume."""
    blocks, notes = _generate(monkeypatch, tmp_path, height=5.0, declared=False)
    assert blocks == []
    assert len(notes) == 1 and "non costruito" in notes[0]


def test_a_total_height_equal_to_the_pcb_builds_nothing_and_says_nothing(monkeypatch, tmp_path):
    """@brief Negative: no height above the PCB (the GUI's «missing» value) -> no block, no note."""
    blocks, notes = _generate(monkeypatch, tmp_path, height=1.0)
    assert blocks == [] and notes == []


def test_the_window_records_where_the_total_height_came_from(window):
    """G2 (second review 2026-11-03): a recorded provenance, saved with the project, not «height != 5 mm»."""
    window.field_cast_pcb.setValue(1.0)
    window.field_body_height.setValue(1.6)      # an old PCB thickness left in the field
    window._cast_height_declared = False
    assert window._project_state()["cast_height_declared"] is False
    window._cast_height_declared = True
    assert window._project_state()["cast_height_declared"] is True
