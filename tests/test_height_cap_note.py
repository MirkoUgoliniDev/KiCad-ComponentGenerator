"""@brief «Altezza ridotta» is reported only for bodies built as approximate shapes (66c, 2026-09-26).

A body that receives a KiCad library model is imported at the model's own
height, so a note saying it was squeezed would be false.
"""
from kicadgen.cad3d import kicad_models, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.castellated_module import (CastellatedModuleParams, CastellatedSide,
                                                    ComponentBox)


def _generate(monkeypatch, tmp_path, attach):
    captured = {}

    def fake_run(name, pins, body, output_dir, **options):
        captured.update(options)
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    monkeypatch.setattr(kicad_models, "attach_models", attach)
    params = CastellatedModuleParams(
        name="Scheda", body_length=30.0, body_width=20.0, pcb_thickness=1.0,
        sides=[CastellatedSide(edge="left", count=2, pitch=2.54, first_pin=1)],
        components=[ComponentBox(x=-5, y=0, length=6, width=4, height=5.0, name="ALTO1"),
                    ComponentBox(x=6, y=0, length=6, width=4, height=5.0, name="ALTO2"),
                    ComponentBox(x=0, y=6, length=3, width=2, height=1.0, name="BASSO")])
    params.validate()
    result = step_generator.generate_castellated_3d_model(
        params, Body3DParams(length_mm=30.0, width_mm=20.0, height_mm=3.0), tmp_path)
    notes = [n for n in result["notes"] if "ridotta a" in n]
    return captured["extra_payload"]["components"], notes


def test_a_body_with_a_library_model_gets_no_height_note(monkeypatch, tmp_path):
    def attach(components, **kwargs):
        # Il modello si abbina per riga di origine, non per nome.
        components[0]["model_path"] = str(tmp_path / "model.step")
        return 1

    components, notes = _generate(monkeypatch, tmp_path, attach)
    assert len(notes) == 1 and "ALTO2" in notes[0] and "ALTO1" not in notes[0]
    # La forma approssimata (e il ripiego di un import fallito) resta limitata.
    assert [c["height_mm"] for c in components] == [2.0, 2.0, 1.0]


def test_without_library_models_every_squeezed_body_is_reported(monkeypatch, tmp_path):
    """@brief Negativo: senza modelli, ogni corpo ridotto ha la sua nota; quelli bassi no."""
    components, notes = _generate(monkeypatch, tmp_path, lambda components, **kwargs: 0)
    assert len(notes) == 2
    assert any("ALTO1" in n for n in notes) and any("ALTO2" in n for n in notes)
    assert not any("BASSO" in n for n in notes)
