"""@brief Top-face writing and bottom-face pin numbers of a single THT part (user, 2026-10-10).

The pin numbers follow the lead positions like the castellated silk labels
follow the contacts; the writing is a free text saved with the project. No
FreeCAD: the solid builder is replaced by a capture.
"""
import math
from pathlib import Path

import pytest

from kicadgen.cad3d import step_generator
from kicadgen.cad3d.annotations import FaceTextAnnotation
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.pin_header import PinHeaderParams
from tests.test_project_wizard import select_board

TEL8_MAP = {1: (0, 7), 7: (0, 1), 8: (0, 0), 9: (1, 0), 10: (1, 1), 16: (1, 7)}


def _tel8():
    params = PinHeaderParams(name="TEL8", rows=2, cols=8, pitch=2.54, row_spacing=10.16,
                             pin_map=dict(TEL8_MAP), populated_pins=set(TEL8_MAP))
    body = Body3DParams(23.8, 13.7, 8.0, offset_x_mm=-3.0, offset_y_mm=-1.77,
                        pin_diameter_mm=0.5, pin_numbers_3d=True)
    return params, body


# -- annotation contract ---------------------------------------------------------

def test_anchored_text_needs_both_finite_coordinates():
    FaceTextAnnotation(face="bottom", text="1", x_mm=1.0, y_mm=2.0).validate()
    FaceTextAnnotation(face="top", text="X").validate()
    with pytest.raises(ValueError, match="x_mm e y_mm"):
        FaceTextAnnotation(face="bottom", text="1", x_mm=1.0).validate()
    with pytest.raises(ValueError, match="x_mm e y_mm"):
        FaceTextAnnotation(face="bottom", text="1", x_mm=math.nan, y_mm=0.0).validate()


# -- placement rule ---------------------------------------------------------------

def test_two_row_part_prints_each_number_between_the_rows_inside_the_body():
    params, body = _tel8()
    labels = step_generator.pin_number_face_texts(params, body)
    assert [t.text for t in labels] == ["1", "7", "8", "9", "10", "16"]
    centre_y = body.offset_y_mm + body.width_mm / 2
    for label, (pin, x, y) in zip(labels, step_generator.pin_positions(params)):
        assert label.face == "bottom" and label.x_mm == pytest.approx(x)
        # Towards the centre, clear of the lead, never on the far side of it.
        assert (label.y_mm - y) * (centre_y - y) > 0
        assert abs(label.y_mm - y) > body.pin_diameter_mm / 2
        assert 0.3 <= label.size_mm <= step_generator.PIN_LABEL_MAX_SIZE_MM
        assert body.offset_x_mm <= label.x_mm <= body.offset_x_mm + body.length_mm
        assert body.offset_y_mm <= label.y_mm <= body.offset_y_mm + body.width_mm
        label.validate()


def test_single_row_part_moves_the_numbers_off_the_row_not_between_the_leads():
    """One row: the sparse axis is still Y (no second row), never X between neighbours."""
    params = PinHeaderParams(name="SIP", rows=1, cols=3, pitch=2.54)
    body = Body3DParams(10.0, 4.0, 5.0, offset_x_mm=-1.5, offset_y_mm=-2.0,
                        pin_diameter_mm=0.5, pin_numbers_3d=True)
    labels = step_generator.pin_number_face_texts(params, body)
    assert [t.text for t in labels] == ["1", "2", "3"]
    for label, (_, x, y) in zip(labels, step_generator.pin_positions(params)):
        assert label.x_mm == pytest.approx(x) and label.y_mm != pytest.approx(y)
        assert label.y_mm - label.size_mm / 2 >= body.offset_y_mm - 1e-6
        assert label.y_mm + label.size_mm / 2 <= body.offset_y_mm + body.width_mm + 1e-6


def test_two_columns_one_lead_each_move_the_numbers_along_x():
    """Negative: a 2x1 grid (two leads apart on Y, one per column... i.e. rows=2, cols=1)
    has its sparse axis on X, so the labels leave the lead sideways."""
    params = PinHeaderParams(name="RAD", rows=2, cols=1, row_spacing=5.0)
    body = Body3DParams(8.0, 10.0, 5.0, offset_x_mm=-4.0, offset_y_mm=-2.5,
                        pin_diameter_mm=0.6, pin_numbers_3d=True)
    labels = step_generator.pin_number_face_texts(params, body)
    for label, (_, x, y) in zip(labels, step_generator.pin_positions(params)):
        assert label.y_mm == pytest.approx(y) and label.x_mm != pytest.approx(x)


def test_no_pins_no_labels():
    params = PinHeaderParams(name="E", rows=1, cols=2, pitch=2.54, populated_pins=set())
    assert step_generator.pin_number_face_texts(params, Body3DParams(5, 5, 5)) == []


# -- what reaches FreeCAD ---------------------------------------------------------

def _captured_texts(monkeypatch, tmp_path, params, body, face_texts):
    captured = {}

    def fake_run(name, pins, body_, output_dir, **options):
        captured.update(options, pins=pins)
        return {"step_path": tmp_path / "x.step", "warnings": [], "notes": []}

    monkeypatch.setattr(step_generator, "_run_freecad", fake_run)
    step_generator.generate_single_tht_3d_model(params, body, tmp_path, face_texts=face_texts)
    return captured["face_texts"]


def test_pin_numbers_are_appended_to_the_top_writing_only_when_chosen(monkeypatch, tmp_path):
    params, body = _tel8()
    top = FaceTextAnnotation(face="top", text="TEL 8-2411WI")
    texts = _captured_texts(monkeypatch, tmp_path, params, body, [top])
    assert texts[0] is top and [t.text for t in texts[1:]] == ["1", "7", "8", "9", "10", "16"]
    body.pin_numbers_3d = False
    assert _captured_texts(monkeypatch, tmp_path, params, body, [top]) == [top]


def test_anchor_travels_in_the_freecad_payload(tmp_path, monkeypatch):
    """The JSON handed to the FreeCAD script carries x_mm/y_mm (None for a centred text)."""
    import json
    payloads = []

    def fake_subprocess(*args, **kwargs):
        raise RuntimeError("stop before FreeCAD")

    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: "freecadcmd")
    original_dump = json.dump

    def capture_dump(obj, fp, *args, **kwargs):
        payloads.append(obj)
        return original_dump(obj, fp, *args, **kwargs)

    monkeypatch.setattr(step_generator.json, "dump", capture_dump)
    monkeypatch.setattr(step_generator.subprocess, "run", fake_subprocess)
    with pytest.raises(Exception):
        step_generator._run_freecad(
            "P", [[1, 0.0, 0.0]], Body3DParams(5, 5, 5), tmp_path,
            face_texts=[FaceTextAnnotation(face="bottom", text="1", x_mm=0.9, y_mm=0.0),
                        FaceTextAnnotation(face="top", text="P")])
    texts = payloads[-1]["face_texts"]
    assert (texts[0]["x_mm"], texts[0]["y_mm"]) == (0.9, 0.0)
    assert (texts[1]["x_mm"], texts[1]["y_mm"]) == (None, None)


# -- GUI: field, option and project state ------------------------------------------

def _select_single_tht(window):
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))


def test_top_writing_is_prefilled_with_the_name_and_an_emptied_field_stays_empty(window):
    _select_single_tht(window)
    window._restore_top_text(None)
    assert window.field_top_text_3d.text() == window._component_name() != ""
    window.field_top_text_3d.setText("")
    saved = window._project_state()
    assert saved["top_text"] == ""
    window.field_top_text_3d.setText("changed")
    window._apply_project_state(saved)
    assert window.field_top_text_3d.text() == ""


def test_top_writing_goes_to_freecad_sized_for_the_body_only_on_a_single_part(window):
    _select_single_tht(window)
    window.group_3d.setChecked(True)
    window.field_body_length.setValue(23.8)
    window.field_top_text_3d.setText("TEL 8-2411WI")
    texts = window._face_texts_for_3d()
    assert [t.face for t in texts] == ["top"] and texts[0].text == "TEL 8-2411WI"
    assert 0.65 * len("TEL 8-2411WI") * texts[0].size_mm <= 0.8 * 23.8 + 1e-6
    window.field_top_text_3d.setText("")
    assert window._face_texts_for_3d() == []
    window.field_top_text_3d.setText("board")
    select_board(window)
    assert window._face_texts_for_3d() == [], "un modulo su PCB non ha una faccia da scrivere"


def test_pin_numbers_checkbox_is_a_project_option_shown_for_single_tht_only(window):
    _select_single_tht(window)
    checkbox = window.chk_pin_numbers_3d
    assert not checkbox.isHidden() and checkbox.isChecked(), "on by default: the user asked for them"
    assert window._current_body_params().pin_numbers_3d is True
    revision = window._model_revision
    checkbox.setChecked(False)
    assert not window._pin_numbers_3d() and window._model_revision == revision + 1
    assert window._current_body_params().pin_numbers_3d is False
    saved = window._project_state()
    assert saved["options"]["pin_numbers_3d"] is False
    checkbox.setChecked(True)
    window._apply_project_state(saved)
    assert not checkbox.isChecked() and not window._pin_numbers_3d(), "a saved choice is kept"
    select_board(window)
    assert checkbox.isHidden()
    assert window.top_text_bar_3d.isHidden()


# -- FreeCAD script: anchors follow the Y reflection of the pins --------------------

def test_flip_y_reflects_the_text_anchor_like_the_pins():
    """Same loader as test_castellated_header_pins: the script imports FreeCAD."""
    import ast
    script = Path("kicadgen/cad3d/freecad_build_body.py")
    tree = ast.parse(script.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_flip_y")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "_FB_SWAP" for t in n.targets))]
    namespace = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<flip>", "exec"), namespace)
    data = {"body": {"offset_y_mm": -1.77, "width_mm": 13.7},
            "pins": [[1, 17.78, 0.0]],
            "face_texts": [{"face": "bottom", "text": "1", "x_mm": 17.78, "y_mm": 1.0},
                           {"face": "top", "text": "T", "x_mm": None, "y_mm": None}]}
    namespace["_flip_y"](data)
    assert data["pins"] == [[1, 17.78, -0.0]]
    assert data["face_texts"][0]["y_mm"] == -1.0 and data["face_texts"][0]["x_mm"] == 17.78
    assert data["face_texts"][1]["y_mm"] is None


# -- leads from the underside must stay inside the body (automatic validation) ------

def _tel8_extracted(**extra):
    from kicadgen.services.datasheet_extractor import ExtractedParams
    values = dict(name="TEL8", description="", tags="", confidence="high", notes="",
                  layout="matrix", rows=2, cols=8, pitch_mm=2.54, row_spacing_mm=15.24,
                  pad_diameter_mm=1.5, drill_mm=0.8, body_length_mm=23.8, body_width_mm=13.7,
                  body_height_mm=8.0, lead_exit="bottom",
                  pin_map=[{"pin": 1, "row": 0, "col": 0}, {"pin": 16, "row": 1, "col": 0}],
                  populated_pins=[1, 16])
    values.update(extra)
    return ExtractedParams(**values)


def test_bottom_exit_leads_wider_than_the_body_block_the_automatic_generation(window):
    _select_single_tht(window)
    with pytest.raises(ValueError, match="lead_exit = bottom"):
        window._validate_automatic_input(_tel8_extracted(), require_body_dimensions=False)


def test_side_exit_or_unknown_exit_only_warn(window):
    """Negative: a DIP (side exit) may have its rows outside the moulding."""
    _select_single_tht(window)
    for exit_ in ("side", None):
        window._validate_automatic_input(_tel8_extracted(lead_exit=exit_),
                                         require_body_dimensions=False)


def test_bottom_exit_leads_inside_the_body_pass(window):
    _select_single_tht(window)
    window._validate_automatic_input(_tel8_extracted(row_spacing_mm=10.16),
                                     require_body_dimensions=False)
    with pytest.raises(ValueError, match="lungo Y"):
        # Offset known: the second row at 1.77 + 10.16 = 11.93 fits, but an
        # offset putting row 0 at 5 mm from the edge pushes row 1 outside.
        window._validate_automatic_input(
            _tel8_extracted(row_spacing_mm=10.16, body_offset_y_mm=-5.0),
            require_body_dimensions=False)
