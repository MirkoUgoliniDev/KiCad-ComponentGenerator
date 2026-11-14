"""@brief Seating plane of a module drawn with male headers underneath.

User-confirmed criterion (2026-11-13): KiCad puts the model's Z=0 on the host
board's top face. With pin headers drawn under a module, what rests on the
host is the BOTTOM face of the header's black plastic spacer: the module sits
on the spacer, the long tails go down through the host. The STEP used to keep
Z=0 on the module's PCB underside, so the 2.5 mm spacer sank into the host
(diagnosis proved with kicad-cli: a model offset z +2.5 fixed it).

The fix keeps the box contract of header_geometry (boxes relative to the
module underside, tests/test_male_header_pins.py) and adds a separate lift,
header_seating_lift(), that the FreeCAD script applies to EVERY object and
every stored anchor before any export. Checked here without FreeCAD: the pure
geometry, the lift helper of the script with fake FreeCAD types, and the
order of the steps in the script.
"""
import ast
from pathlib import Path
from unittest.mock import Mock

import pytest

from kicadgen.cad3d import step_generator as step
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.header_geometry import (LONG_TAIL_MM, REFERENCE_PITCH_MM,
                                            SPACER_HEIGHT_MM, header_seating,
                                            header_seating_lift, male_header_boxes_by_row,
                                            spacer_height)
from kicadgen.cad3d.step_generator import header_seating_plan
from kicadgen.generators.castellated_module import (CONTACT_HALF_AND_THROUGH,
                                                    CONTACT_HALF_HOLE,
                                                    CastellatedModuleParams, CastellatedSide)
from kicadgen.generators.pin_header import PinHeaderParams

SCRIPT = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"
BOARD_TOP = 1.6


def _row(count, pitch, y=0.0, first=1):
    """Pins (number, x, y) of one straight row at the given pitch."""
    return [(first + n, n * pitch, y) for n in range(count)]


def _lifted(boxes, lift):
    """Boxes translated by `lift` along Z, as the FreeCAD script does."""
    return [dict(box, z=box["z"] + lift) for box in boxes]


def _role(boxes, role):
    """Boxes of one role ("spacer" or "pin")."""
    return [box for box in boxes if box["role"] == role]


@pytest.mark.parametrize("pitch", [2.54, 2.0])
def test_after_the_lift_the_spacer_bottom_is_the_seating_plane(pitch):
    """@brief Positive: spacer bottom at Z=0, module underside on the spacer, tails below."""
    pins = _row(4, pitch)
    lift = header_seating_lift(pins, [pitch] * len(pins))
    boxes = _lifted(male_header_boxes_by_row(pins, BOARD_TOP, pitches=[pitch] * len(pins)), lift)
    scale = pitch / REFERENCE_PITCH_MM
    for spacer in _role(boxes, "spacer"):
        assert spacer["z"] == pytest.approx(0.0), "the spacer rests on the host's top face"
        # The module underside (Z=0 before the lift) now sits on the spacer.
        assert spacer["z"] + spacer["dz"] == pytest.approx(lift)
    for pin in _role(boxes, "pin"):
        assert pin["z"] == pytest.approx(-LONG_TAIL_MM * scale), "the long tail crosses the host"


def test_the_lift_scales_with_the_pitch_like_the_drawn_spacer():
    """@brief Positive: lift == spacer height of the catalogue, scaled by the pitch."""
    assert header_seating_lift(_row(3, 2.54), None) == pytest.approx(SPACER_HEIGHT_MM)
    assert header_seating_lift(_row(3, 2.0), None) == pytest.approx(SPACER_HEIGHT_MM * 2.0 / 2.54)
    assert header_seating_lift(_row(3, 1.27), [1.27] * 3) == pytest.approx(spacer_height(1.27))
    assert spacer_height(2.0) / spacer_height(2.54) == pytest.approx(2.0 / 2.54)


@pytest.mark.parametrize("pins, pitches", [
    (_row(5, 2.54), None),
    (_row(5, 2.0), [2.0] * 5),
    # A single pin of a side with no declared pitch (0): the boxes fall back
    # to the measured/reference pitch, and so must the lift.
    (_row(1, 2.54), [0.0]),
    # Two rows with different declared pitches.
    (_row(3, 2.54) + _row(3, 2.0, y=10.0, first=4), [2.54] * 3 + [2.0] * 3),
])
def test_the_lift_is_the_depth_of_the_spacer_actually_drawn(pins, pitches):
    """@brief Structural: the lift equals the lowest drawn spacer face, whatever the pitch rule."""
    boxes = male_header_boxes_by_row(pins, BOARD_TOP, pitches=pitches)
    lowest_spacer = min(box["z"] for box in _role(boxes, "spacer"))
    assert header_seating_lift(pins, pitches) == pytest.approx(-lowest_spacer)


def test_mixed_rows_rest_on_the_tallest_spacer_and_none_sinks():
    """@brief Positive: with two pitches the tallest spacer touches the host, none goes below."""
    pins = _row(3, 2.54) + _row(3, 2.0, y=10.0, first=4)
    pitches = [2.54] * 3 + [2.0] * 3
    lift = header_seating_lift(pins, pitches)
    spacers = _role(_lifted(male_header_boxes_by_row(pins, BOARD_TOP, pitches=pitches), lift),
                    "spacer")
    assert min(spacer["z"] for spacer in spacers) == pytest.approx(0.0)
    assert all(spacer["z"] >= -1e-9 for spacer in spacers)


def test_no_header_drawn_means_no_lift():
    """@brief Negative: a flat-soldered module (no header pins) keeps its underside at Z=0."""
    assert header_seating_lift([], None) == 0.0
    assert header_seating_lift([], []) == 0.0


def test_misaligned_pitches_are_refused():
    """@brief Negative: pitches that do not match the pins are an error, as for the boxes."""
    with pytest.raises(ValueError):
        header_seating_lift(_row(3, 2.54), [2.54])


# -- The FreeCAD script: lift helper with fake FreeCAD types ------------------

class _Vector:
    """Minimal FreeCAD.Vector."""

    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z


class _Rotation:
    """Identity rotation: the lift never turns anything."""


class _Placement:
    """Translation-only FreeCAD.Placement: multiply composes the bases."""

    def __init__(self, base=None, rotation=None):
        self.Base = base or _Vector()
        self.Rotation = rotation or _Rotation()

    def multiply(self, other):
        """Return a new placement, as FreeCAD does (no mutation)."""
        return _Placement(_Vector(self.Base.x + other.Base.x, self.Base.y + other.Base.y,
                                  self.Base.z + other.Base.z))


class _Object:
    """Document object with a Placement."""

    def __init__(self, z):
        self.Placement = _Placement(_Vector(1.0, 2.0, z))


def _script_lift():
    """`_apply_seating_lift` of the script, run with the fake FreeCAD above."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    body = [node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_seating_lift"]
    assert body, "the script defines _apply_seating_lift"
    namespace = {"FreeCAD": type("FreeCAD", (), {
        "Vector": _Vector, "Rotation": _Rotation, "Placement": _Placement})}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<seating-lift>", "exec"), namespace)
    return namespace["_apply_seating_lift"]


def test_the_script_lifts_every_object_and_every_anchor():
    """@brief Positive: each object's placement and each pin-number anchor move by the lift."""
    lift = _script_lift()
    objects = [_Object(0.0), _Object(-8.5), _Object(3.2)]
    anchors = {"Pin1": [1.0, 2.0, -0.025], "Pin2": None}
    lift(objects, anchors, 2.5)
    assert [obj.Placement.Base.z for obj in objects] == pytest.approx([2.5, -6.0, 5.7])
    assert [(obj.Placement.Base.x, obj.Placement.Base.y) for obj in objects] == [(1.0, 2.0)] * 3
    assert anchors["Pin1"] == pytest.approx([1.0, 2.0, 2.475])
    assert anchors["Pin2"] is None


def test_the_script_leaves_a_flat_module_untouched():
    """@brief Negative: lift 0 (no header drawn) keeps the PCB underside and anchors at their Z."""
    lift = _script_lift()
    objects = [_Object(0.0)]
    before = objects[0].Placement
    anchors = {"Pin1": [1.0, 2.0, -0.025]}
    lift(objects, anchors, 0.0)
    assert objects[0].Placement is before
    assert anchors == {"Pin1": [1.0, 2.0, -0.025]}


# -- The FreeCAD script: where the lift is decided and applied ----------------

def _main_source():
    """Source lines of main() in the FreeCAD script."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    main = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    return ast.get_source_segment(SCRIPT.read_text(encoding="utf-8"), main)


def test_the_lift_is_applied_before_every_export():
    """@brief Positive: the lift precedes recompute, STEP, FCStd, tessellation (WRL, preview)."""
    source = _main_source()
    applied = source.index("_apply_seating_lift(")
    for later in ('doc.recompute()', 'Import.export(', '_export_fcstd(',
                  '_StepHeartbeat("tessellation")', '_write_vrml('):
        assert applied < source.index(later), later
    # Every built object, not a selection of them.
    assert "_apply_seating_lift([obj for obj, _ in colored], label_anchors, seating_lift)" in source


def test_only_a_drawn_male_header_sets_the_lift():
    """@brief Negative: the lift starts at 0 and is set only inside the male-header branch."""
    source = _main_source()
    start = source.index("seating_lift = 0.0")
    branch = source.index('if shown_pins and data.get("pin_style") == "male_header":')
    assigned = source.index('seating_lift = float(data.get("header_seating_lift_mm") or 0.0)')
    cylinders = source.index("for pin_num, x, y in shown_pins:")
    assert start < branch < assigned < cylinders
    assert source.count("seating_lift = ") == 2


# -- Audit M3: lift only when EVERY contact is header-mounted -----------------

def _module(left_style, right_style):
    """Castellated module with a 2.54 mm left side and a 2.0 mm right side."""
    params = CastellatedModuleParams(
        name="Seat", body_length=20.0, body_width=15.0,
        sides=[CastellatedSide(edge="left", count=3, pitch=2.54, first_pin=1,
                               contact_style=left_style),
               CastellatedSide(edge="right", count=3, pitch=2.0, first_pin=4,
                               contact_style=right_style)])
    params.validate()
    return params


def _castellated_payload(params, monkeypatch, tmp_path, **body_extra):
    """What generate_castellated_3d_model hands to FreeCAD, and its notes."""
    captured = {}

    def fake_run(name, pins, body, output_dir, **options):
        captured.update(options, pins=pins)
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step, "_run_freecad", fake_run)
    result = step.generate_castellated_3d_model(
        params, Body3DParams(length_mm=params.body_length, width_mm=params.body_width,
                             height_mm=3.0, **body_extra), tmp_path)
    return captured.get("extra_payload", {}), result["notes"]


def _lift_notes(notes):
    """Notes that state a seating decision (lifted or not lifted)."""
    return [note for note in notes if "Modello STEP" in note and "sollevato" in note]


def test_plan_lifts_when_every_contact_is_header_mounted():
    """@brief Positive: no flat contact -> the tallest spacer, and a note with mm and pitch."""
    pins = _row(3, 2.54) + _row(3, 2.0, y=10.0, first=4)
    lift, notes = header_seating_plan(pins, [2.54] * 3 + [2.0] * 3, [])
    assert lift == pytest.approx(SPACER_HEIGHT_MM)
    assert len(notes) == 1
    assert "2.50 mm" in notes[0] and "passo 2.54 mm" in notes[0]


def test_plan_does_not_lift_when_one_contact_is_soldered_flat():
    """@brief Negative: a single flat contact seats the module on its pads -> 0 and a note."""
    pins = _row(3, 2.54)
    lift, notes = header_seating_plan(pins, [2.54] * 3, ["7"])
    assert lift == 0.0
    assert len(notes) == 1 and "non sollevato" in notes[0] and "7" in notes[0]


def test_plan_without_headers_says_nothing():
    """@brief Negative: no header drawn -> no lift and no note (flat module as before)."""
    assert header_seating_plan([], [], ["1", "2"]) == (0.0, [])
    assert header_seating_plan([], None, []) == (0.0, [])


def test_header_seating_names_the_pitch_of_the_tallest_spacer():
    """@brief Positive: the pitch reported is the one whose spacer sets the lift."""
    pins = _row(3, 2.0) + _row(3, 2.54, y=10.0, first=4)
    lift, pitch = header_seating(pins, [2.0] * 3 + [2.54] * 3)
    assert (lift, pitch) == (pytest.approx(spacer_height(2.54)), pytest.approx(2.54))
    assert header_seating([], None) == (0.0, None)


def test_castellated_all_header_mounted_sends_the_lift(monkeypatch, tmp_path):
    """@brief Positive: every contact has a closed hole -> payload lift and the note."""
    payload, notes = _castellated_payload(
        _module(CONTACT_HALF_AND_THROUGH, CONTACT_HALF_AND_THROUGH), monkeypatch, tmp_path)
    assert payload["pin_style"] == "male_header"
    assert payload["header_seating_lift_mm"] == pytest.approx(SPACER_HEIGHT_MM)
    lifted = _lift_notes(notes)
    assert len(lifted) == 1 and "non sollevato" not in lifted[0]


def test_castellated_with_half_hole_contacts_is_not_lifted(monkeypatch, tmp_path):
    """@brief Negative: half-hole-only contacts (no header) -> headers drawn, lift 0, note."""
    payload, notes = _castellated_payload(
        _module(CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE), monkeypatch, tmp_path)
    assert payload["pin_style"] == "male_header", "the headers are still drawn"
    assert payload["header_seating_lift_mm"] == 0.0
    flat = _lift_notes(notes)
    assert len(flat) == 1 and "non sollevato" in flat[0]
    assert "4, 5, 6" in flat[0]


def test_castellated_with_pins_off_sends_no_header_and_no_note(monkeypatch, tmp_path):
    """@brief Negative: pins switched off -> no header payload, no seating note."""
    payload, notes = _castellated_payload(
        _module(CONTACT_HALF_AND_THROUGH, CONTACT_HALF_AND_THROUGH), monkeypatch, tmp_path,
        show_pins_3d=False)
    assert "header_seating_lift_mm" not in payload
    assert _lift_notes(notes) == []


def _populated_payload(monkeypatch, tmp_path, **body_extra):
    """What the populated-board path hands to FreeCAD, and its notes."""
    run = Mock(return_value={"warnings": [], "notes": []})
    monkeypatch.setattr(step, "_run_freecad", run)
    body = Body3DParams(length_mm=40, width_mm=20, height_mm=5, populated_board=True,
                        components=[], **body_extra)
    result = step.generate_3d_model(PinHeaderParams(pitch=2.0), body, tmp_path)
    return run.call_args.kwargs["extra_payload"], result["notes"]


def test_populated_board_rests_on_its_headers(monkeypatch, tmp_path):
    """@brief Positive: every pin of a populated board is header-mounted -> lifted."""
    payload, notes = _populated_payload(monkeypatch, tmp_path)
    assert payload["header_seating_lift_mm"] == pytest.approx(spacer_height(2.0))
    lifted = _lift_notes(notes)
    assert len(lifted) == 1 and "passo 2 mm" in lifted[0]


def test_populated_board_with_pins_off_is_not_lifted(monkeypatch, tmp_path):
    """@brief Negative: pins switched off -> lift 0 and no seating note."""
    payload, notes = _populated_payload(monkeypatch, tmp_path, show_pins_3d=False)
    assert payload["header_seating_lift_mm"] == 0.0
    assert _lift_notes(notes) == []
