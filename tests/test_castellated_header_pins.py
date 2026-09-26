"""@brief Male headers on a castellated module go only into real through-holes.

User decision (2026-09-26): with "show pins" on, a castellated module gets a
male strip soldered under every contact that has a CLOSED through-hole
(contact style through_hole, or half_and_through with a declared drill); a
half-hole alone cannot hold a strip, so those contacts get no pin and a note
says why when the board mixes the two kinds.
"""
import ast
import copy
from pathlib import Path

import pytest

from kicadgen.cad3d import step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.header_geometry import (REFERENCE_PITCH_MM, male_header_boxes,
                                            male_header_boxes_by_row)
from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, CastellatedModuleParams,
    CastellatedSide, pad_positions,
)

SCRIPT = Path(step_generator.__file__).with_name("freecad_build_body.py")


def _module(left_style, right_style, **changes):
    params = CastellatedModuleParams(
        name="Mixed", body_length=20.0, body_width=15.0,
        sides=[CastellatedSide(edge="left", count=3, pitch=2.54, first_pin=1,
                               contact_style=left_style),
               CastellatedSide(edge="right", count=3, pitch=2.0, first_pin=4,
                               contact_style=right_style)])
    for key, value in changes.items():
        setattr(params, key, value)
    params.validate()
    return params


def _generate(params, monkeypatch, tmp_path, **body_extra):
    """What generate_castellated_3d_model hands to FreeCAD, without FreeCAD."""
    captured = {}

    def fake_run(name, pins, body, output_dir, **options):
        captured.update(options, pins=pins)
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    result = step_generator.generate_castellated_3d_model(
        params, Body3DParams(length_mm=params.body_length, width_mm=params.body_width,
                             height_mm=3.0, **body_extra), tmp_path)
    return captured, result


def _header_note(result):
    return [n for n in result["notes"] if "Pin 3D" in n]


# ── Regola pura ─────────────────────────────────────────────────────

def test_through_hole_contacts_get_a_pin_at_the_through_hole_centre():
    params = _module(CONTACT_THROUGH_HOLE, CONTACT_HALF_HOLE)
    pins, pitches, skipped = step_generator.castellated_header_pins(params)
    left = [p for p in pad_positions(params) if p.edge == "left"]
    assert pins == [[p.number, *params.thru_hole_centre(p)] for p in left]
    assert pitches == [2.54] * 3
    assert skipped == ["4", "5", "6"], "i mezzi fori non reggono uno strip"


def test_half_and_through_uses_the_closed_hole_not_the_half_hole():
    params = _module(CONTACT_HALF_AND_THROUGH, CONTACT_HALF_AND_THROUGH)
    pins, pitches, skipped = step_generator.castellated_header_pins(params)
    assert skipped == []
    placements = list(pad_positions(params))
    for pin, placement in zip(pins, placements):
        hole = params.thru_hole_centre(placement)
        assert pin[1:] == [*hole]
        # Il centro del mezzo foro sta a half_hole_inset dal bordo: il pin no.
        assert abs(abs(pin[1]) - (params.body_length / 2 - params.half_hole_inset)) > 0.5
    assert pitches == [2.54] * 3 + [2.0] * 3, "ogni lato col proprio passo"


def test_half_and_through_without_a_drill_is_half_hole_only():
    """@brief Negativo: stesso stile, ma senza foro chiuso dichiarato non c'e' pin."""
    params = _module(CONTACT_HALF_AND_THROUGH, CONTACT_HALF_AND_THROUGH, thru_hole_drill=0.0)
    pins, pitches, skipped = step_generator.castellated_header_pins(params)
    assert pins == [] and pitches == []
    assert skipped == ["1", "2", "3", "4", "5", "6"]


# ── Payload verso FreeCAD ───────────────────────────────────────────

def test_mixed_module_sends_a_male_header_and_explains_the_missing_pins(monkeypatch, tmp_path):
    params = _module(CONTACT_THROUGH_HOLE, CONTACT_HALF_HOLE)
    captured, result = _generate(params, monkeypatch, tmp_path)
    assert [p[0] for p in captured["pins"]] == ["1", "2", "3"]
    assert captured["extra_payload"]["pin_style"] == "male_header"
    assert captured["extra_payload"]["header_pitches"] == [2.54] * 3
    note = _header_note(result)
    assert len(note) == 1 and "4, 5, 6" in note[0]


def test_all_through_holes_need_no_explanation(monkeypatch, tmp_path):
    params = _module(CONTACT_THROUGH_HOLE, CONTACT_THROUGH_HOLE)
    captured, result = _generate(params, monkeypatch, tmp_path)
    assert len(captured["pins"]) == 6
    assert _header_note(result) == []


@pytest.mark.parametrize("styles, body_extra", [
    ((CONTACT_THROUGH_HOLE, CONTACT_HALF_HOLE), {"show_pins_3d": False}),
    ((CONTACT_HALF_HOLE, CONTACT_HALF_HOLE), {}),
])
def test_without_pins_to_show_the_payload_is_the_old_one(styles, body_extra, monkeypatch, tmp_path):
    """@brief Negativi: pin spenti, o nessun foro passante -> niente pin, stile, nota."""
    params = _module(*styles)
    captured, result = _generate(params, monkeypatch, tmp_path, **body_extra)
    assert captured["pins"] == []
    assert "pin_style" not in captured["extra_payload"]
    assert "header_pitches" not in captured["extra_payload"]
    assert _header_note(result) == []


# ── Geometria dello strip per righe con passo proprio ───────────────

def test_each_row_keeps_its_declared_pitch():
    # Due file a passo 2.54 i cui pin d'angolo distano solo 1.0 mm: quella
    # distanza non e' un passo.
    pins = [(1, 0.0, 0.0), (2, 2.54, 0.0), (3, 0.0, 1.0), (4, 0.0, 3.54)]
    inferred = [b for b in male_header_boxes(pins, 1.6) if b["role"] == "spacer"]
    assert inferred[0]["dx"] == pytest.approx(1.0), "senza passi dichiarati si stima"
    rows = [b for b in male_header_boxes_by_row(pins, 1.6, pitches=[2.54] * 4)
            if b["role"] == "spacer"]
    assert {round(b["dx"], 6) for b in rows} == {2.54}
    assert male_header_boxes_by_row(pins, 1.6) == male_header_boxes(pins, 1.6)
    with pytest.raises(ValueError):
        male_header_boxes_by_row(pins, 1.6, pitches=[2.54])


def test_a_single_contact_side_without_pitch_uses_the_reference_header():
    params = CastellatedModuleParams(
        name="One", body_length=10.0, body_width=10.0,
        sides=[CastellatedSide(edge="left", count=1, pitch=0.0,
                               contact_style=CONTACT_THROUGH_HOLE)])
    params.validate()
    _pins, pitches, _skipped = step_generator.castellated_header_pins(params)
    assert pitches == [REFERENCE_PITCH_MM]


# ── Lo script FreeCAD ribalta i pin insieme ai contatti ─────────────

@pytest.fixture(scope="module")
def flip_y():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_flip_y")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "_FB_SWAP" for t in n.targets))]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<flip>", "exec"), namespace)
    return namespace["_flip_y"]


def test_pins_stay_on_their_holes_after_the_y_flip(flip_y, monkeypatch, tmp_path):
    params = _module(CONTACT_HALF_AND_THROUGH, CONTACT_THROUGH_HOLE)
    captured, _ = _generate(params, monkeypatch, tmp_path)
    data = copy.deepcopy({
        "body": {"offset_x_mm": -10.0, "offset_y_mm": -7.5, "width_mm": 15.0},
        "pins": captured["pins"], "edge_contacts": captured["edge_contacts"],
        "notches": captured["notches"],
    })
    flipped = flip_y(data)
    holes = {}
    for contact in flipped["edge_contacts"]:
        through = contact.get("through_hole") or contact
        holes[contact["number"]] = (through["x"], through["y"])
    for number, x, y in flipped["pins"]:
        assert (x, y) == pytest.approx(holes[number])


def test_the_script_draws_rows_with_the_declared_pitches():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'male_header_boxes_by_row(' in source
    assert 'pitches=data.get("header_pitches")' in source
