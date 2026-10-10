"""@brief Dedicated pin-numbering call (user, 2026-10-10: «separa, vediamo se funziona»).

The main answer reads every quote and invents the numbering; a second call
with the drawing alone reads only the printed numbers. Every paid call is a
Mock: no network, no .env.
"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import (
    ExtractedParams, apply_dedicated_numbering, numbering_documents, read_pin_numbering)

U_NUMBERING = [{"pin": 1, "row": 0, "col": 0}, {"pin": 7, "row": 0, "col": 6},
               {"pin": 8, "row": 0, "col": 7}, {"pin": 9, "row": 1, "col": 7},
               {"pin": 10, "row": 1, "col": 6}, {"pin": 16, "row": 1, "col": 0}]
ZIGZAG = [{"pin": 2 * c + r + 1, "row": r, "col": c} for c in range(8) for r in range(2)]


def _answer(pins, view="bottom"):
    """What the model returns through the tool."""
    return {"contacts_view": view, "pins": pins, "notes": ""}


def _reading(pins, view="bottom"):
    """What `read_pin_numbering` hands to `apply_dedicated_numbering`."""
    return {"view": view, "pins": list(pins), "notes": ""}


# -- which documents ----------------------------------------------------------------

def test_only_image_documents_declared_drawing_or_pinout_are_read(tmp_path):
    paths = [tmp_path / "photo.webp", tmp_path / "quote.png", tmp_path / "pins.png",
             tmp_path / "ds.pdf"]
    roles = ext.normalize_document_roles(
        {"photo.webp": ["hires"], "quote.png": ["top"], "pins.png": ["pinout"],
         "ds.pdf": ["datasheet", "top"]}, [p.name for p in paths])
    assert [p.name for p in numbering_documents(paths, roles)] == ["quote.png", "pins.png"]
    assert numbering_documents(paths, None) == []


# -- the call itself ------------------------------------------------------------------

def _drawing(tmp_path):
    from PIL import Image
    path = tmp_path / "quote.png"
    Image.new("RGB", (300, 200), "white").save(path)
    return path


def test_the_call_sends_the_drawing_alone_with_the_grid_and_validates_the_answer(tmp_path):
    call = Mock(return_value=_answer(U_NUMBERING))
    reading = read_pin_numbering([_drawing(tmp_path)], rows=2, cols=8,
                                 model="deepseek-api/deepseek-flash", call=call)
    assert reading["view"] == "bottom" and reading["pins"] == U_NUMBERING
    kwargs = call.call_args.kwargs
    assert kwargs["tool"]["function"]["name"] == "report_pin_numbering"
    assert kwargs["system_prompt"] == ext.DEFAULT_PIN_NUMBERING_PROMPT
    texts = [b["text"] for b in kwargs["content_blocks"] if b["type"] == "text"]
    assert any("2 file" in t and "8 posizioni" in t for t in texts)
    assert sum(1 for b in kwargs["content_blocks"] if b["type"] == "image_url") == 1


@pytest.mark.parametrize("answer", [
    _answer([{"pin": 1, "row": 0, "col": 9}]),             # outside 8 columns
    _answer([{"pin": 1, "row": 0, "col": 0}, {"pin": 1, "row": 1, "col": 0}]),   # twice
    "not a dict",
])
def test_an_unusable_answer_is_reported_as_discarded_with_its_reason(tmp_path, answer):
    logs = []
    reading = read_pin_numbering([_drawing(tmp_path)], rows=2, cols=8, model="m",
                                 call=Mock(return_value=answer), progress=logs.append)
    assert reading["pins"] == [] and reading["discarded"]
    data = {"pin_map": list(ZIGZAG), "contacts_view": "bottom"}
    apply_dedicated_numbering(data, reading)
    assert data["pin_map"] == ZIGZAG and "risposta scartata" in data["_numbering_note"]


def test_a_failed_call_is_best_effort_and_says_so(tmp_path):
    logs = []
    reading = read_pin_numbering([_drawing(tmp_path)], rows=2, cols=8, model="m",
                                 call=Mock(side_effect=ext.OpenRouterError("boom")),
                                 progress=logs.append)
    assert reading["pins"] == [] and reading["failed"] == "boom"
    assert any("non riuscita" in line for line in logs)
    data = {"pin_map": list(ZIGZAG), "contacts_view": "bottom"}
    apply_dedicated_numbering(data, reading)
    assert data["pin_map"] == ZIGZAG and "chiamata non riuscita: boom" in data["_numbering_note"]


# -- confrontation rules ---------------------------------------------------------------

def test_matching_readings_are_confirmed_and_nothing_changes():
    data = {"pin_map": list(U_NUMBERING), "contacts_view": "bottom"}
    apply_dedicated_numbering(data, _reading(U_NUMBERING))
    assert data["pin_map"] == U_NUMBERING and "_pin_map_conflict" not in data
    assert "confermata" in data["_numbering_note"]


def test_a_different_reading_wins_and_leaves_both_in_the_conflict():
    """TEL8 14:14: zig-zag convention in the main answer, printed numbers in the dedicated one."""
    data = {"pin_map": list(ZIGZAG), "contacts_view": "bottom"}
    apply_dedicated_numbering(data, _reading(U_NUMBERING))
    assert data["pin_map"] == U_NUMBERING
    assert "9@(0,4)" in data["_pin_map_conflict"] and "9@(1,7)" in data["_pin_map_conflict"]


def test_no_number_read_keeps_the_main_numbering_unconfirmed():
    data = {"pin_map": list(ZIGZAG), "contacts_view": "bottom"}
    apply_dedicated_numbering(data, _reading([]))
    assert data["pin_map"] == ZIGZAG and "_pin_map_conflict" not in data
    assert "NON confermata" in data["_numbering_note"]
    apply_dedicated_numbering(data, None)
    assert data["pin_map"] == ZIGZAG


def test_the_dedicated_view_fills_an_unknown_view_only():
    data = {"pin_map": [], "contacts_view": "unknown"}
    apply_dedicated_numbering(data, _reading(U_NUMBERING, view="bottom"))
    assert data["contacts_view"] == "bottom" and data["pin_map"] == U_NUMBERING
    assert "_pin_map_conflict" not in data, "no main map: nothing to disagree with"
    # Same positions but a different declared view: a disagreement, never
    # «confermata» (review A, finding 2); the dedicated view wins and is said.
    data = {"pin_map": list(U_NUMBERING), "contacts_view": "top"}
    apply_dedicated_numbering(data, _reading(U_NUMBERING, view="bottom"))
    assert data["contacts_view"] == "bottom"
    assert "(top)" in data["_pin_map_conflict"] and "(bottom, usata)" in data["_pin_map_conflict"]


def test_a_subset_that_agrees_is_only_a_partial_confirmation():
    """Review A, finding 5: «1» alone printed cannot confirm sixteen positions."""
    data = {"pin_map": list(ZIGZAG), "contacts_view": "bottom"}
    apply_dedicated_numbering(data, _reading([{"pin": 1, "row": 0, "col": 0}]))
    assert data["pin_map"] == ZIGZAG and "_pin_map_conflict" not in data
    assert "solo per 1 pin su 16" in data["_numbering_note"]


# -- end to end: the second call happens for a single matrix part with a drawing -------

_MAIN = dict(layout="matrix", rows=2, cols=8, pitch_mm=2.54, row_spacing_mm=10.16,
             pad_diameter_mm=1.6, drill_mm=0.8, contacts_view="bottom", confidence="high",
             populated_pins=[1, 7, 8, 9, 10, 16], pin_map=list(ZIGZAG),
             body_length_mm=23.8, body_width_mm=13.7, body_height_mm=8.0)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)


def _run(monkeypatch, tmp_path, roles, family="single_tht", second=None):
    drawing = _drawing(tmp_path)
    answers = [dict(_MAIN), second if second is not None else _answer(U_NUMBERING)]
    call = Mock(side_effect=answers)
    monkeypatch.setattr(ext, "call_tool", call)
    logs = []
    params = ext.extract_pin_header_params(
        [drawing], document_roles=roles, component_family=family, progress=logs.append)
    return call, params, logs


def test_end_to_end_the_dedicated_reading_replaces_the_convention(offline, monkeypatch, tmp_path):
    call, params, logs = _run(monkeypatch, tmp_path, {"quote.png": ["top"]})
    assert call.call_count == 2
    assert call.call_args_list[1].kwargs["tool"]["function"]["name"] == "report_pin_numbering"
    # Mirrored once (bottom view): pin 1 at column 7 seen from above.
    by_pin = {e["pin"]: (e["row"], e["col"]) for e in params.pin_map}
    assert by_pin[1] == (0, 7) and by_pin[9] == (1, 0) and by_pin[16] == (1, 7)
    assert sorted(by_pin) == [1, 7, 8, 9, 10, 16]
    assert params.pin_map_conflict and "non coincidono" in params.pin_map_conflict


def test_end_to_end_no_drawing_role_means_no_second_call(offline, monkeypatch, tmp_path):
    call, params, logs = _run(monkeypatch, tmp_path, {"quote.png": ["hires"]})
    assert call.call_count == 1 and params.pin_map_conflict is None


def test_end_to_end_a_populated_board_does_not_ask(offline, monkeypatch, tmp_path):
    call, params, logs = _run(monkeypatch, tmp_path, {"quote.png": ["top"]},
                              family="aggregate_tht")
    assert call.call_count == 1


# -- GUI: the conflict blocks the automatic generation ---------------------------------

def test_the_conflict_blocks_the_automatic_generation(window):
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))
    params = ExtractedParams(
        name="TEL8", description="", tags="", confidence="high", notes="", layout="matrix",
        rows=2, cols=8, pitch_mm=2.54, row_spacing_mm=10.16, pad_diameter_mm=1.6,
        drill_mm=0.8, body_length_mm=23.8, body_width_mm=13.7, body_height_mm=8.0,
        pin_map=list(U_NUMBERING), populated_pins=[1, 7, 8, 9, 10, 16],
        pin_map_conflict="Le due letture della numerazione non coincidono.")
    with pytest.raises(ValueError, match="non coincidono"):
        window._validate_automatic_input(params, require_body_dimensions=False)
    params.pin_map_conflict = None
    window._validate_automatic_input(params, require_body_dimensions=False)


# -- partial maps: anchors complete one convention or block (review A, finding 1) -----

def _matrix(**extra):
    data = dict(layout="matrix", rows=2, cols=8, pitch_mm=2.54, row_spacing_mm=7.62,
                pad_diameter_mm=1.6, drill_mm=0.8, confidence="high")
    data.update(extra)
    return data


def test_a_header_with_only_1_and_16_printed_keeps_all_sixteen_pads():
    """A plain 2x8 header whose drawing prints «1» (top-left) and «16» (bottom-left: a U)."""
    from kicadgen.services.datasheet_extractor import _params_from_tool_arguments
    params = _params_from_tool_arguments(_matrix(
        pin_map=[{"pin": 1, "row": 0, "col": 0}, {"pin": 16, "row": 1, "col": 0}]))
    assert params.pin_map_conflict is None
    assert sorted(e["pin"] for e in params.pin_map) == list(range(1, 17))
    assert "convenzione u" in params.notes and "DERIVATO" in params.notes
    by_pin = {e["pin"]: (e["row"], e["col"]) for e in params.pin_map}
    assert by_pin[8] == (0, 7) and by_pin[9] == (1, 7)


def test_anchors_fitting_several_conventions_block_instead_of_guessing():
    """Negative: «1» alone fits the column and the U convention: no completion."""
    from kicadgen.services.datasheet_extractor import _params_from_tool_arguments
    params = _params_from_tool_arguments(_matrix(pin_map=[{"pin": 1, "row": 0, "col": 0}]))
    assert params.pin_map is None and "piu' convenzioni" in params.pin_map_conflict


def test_anchors_fitting_no_convention_block():
    from kicadgen.services.datasheet_extractor import _params_from_tool_arguments
    params = _params_from_tool_arguments(_matrix(
        pin_map=[{"pin": 1, "row": 0, "col": 0}, {"pin": 16, "row": 0, "col": 3}]))
    assert params.pin_map is None and "nessuna convenzione" in params.pin_map_conflict


def test_a_complete_map_over_the_populated_pins_is_used_as_is():
    """Negative of the partial rule: TEL8, six printed numbers for six pins, no convention."""
    from kicadgen.services.datasheet_extractor import _params_from_tool_arguments
    params = _params_from_tool_arguments(_matrix(
        rows=2, cols=3, row_spacing_mm=10.16,
        pin_map=[{"pin": 1, "row": 0, "col": 0}, {"pin": 7, "row": 0, "col": 1},
                 {"pin": 8, "row": 0, "col": 2}, {"pin": 9, "row": 1, "col": 2},
                 {"pin": 10, "row": 1, "col": 1}, {"pin": 16, "row": 1, "col": 0}],
        populated_pins=[1, 7, 8, 9, 10, 16]))
    assert params.pin_map_conflict is None and params.populated_pins == [1, 7, 8, 9, 10, 16]
    assert "convenzione" not in params.notes


def test_a_dedicated_pin_missing_from_populated_is_added_not_dropped():
    """Review A, finding 4: a printed number is evidence of a pin."""
    from kicadgen.services.datasheet_extractor import _params_from_tool_arguments
    params = _params_from_tool_arguments(_matrix(
        _dedicated_map=True,
        pin_map=[{"pin": 1, "row": 0, "col": 0}, {"pin": 8, "row": 0, "col": 7},
                 {"pin": 9, "row": 1, "col": 7}, {"pin": 16, "row": 1, "col": 0}],
        populated_pins=[1, 8, 9]))
    assert params.populated_pins == [1, 8, 9, 16] and "aggiunti ai pin presenti" in params.notes


def test_a_transparent_drawing_is_composited_on_white_when_enlarged(tmp_path):
    """Review A, finding 3: convert("RGB") alone turned the background black."""
    import base64
    from io import BytesIO
    from PIL import Image
    path = tmp_path / "alpha.png"
    image = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    image.putpixel((10, 10), (0, 0, 0, 255))
    image.save(path)
    url = ext._file_to_content_block(path, "deepseek-api/deepseek-flash")["image_url"]["url"]
    with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as out:
        assert out.size[0] > 300 and out.getextrema()[0][1] == 255, "white background kept"


def test_a_lifted_drill_keeps_a_solderable_ring():
    """Review A, finding 6: pad 0.8 over a 0.75 hole is no pad."""
    from kicadgen.services.datasheet_extractor import _params_from_tool_arguments
    params = _params_from_tool_arguments(_matrix(
        rows=2, cols=4, pad_diameter_mm=0.8, drill_mm=0.5, lead_diameter_mm=0.5,
        value_sources={"drill_mm": "estimated", "pad_diameter_mm": "read"}))
    assert params.drill_mm == 0.75 and params.pad_diameter_mm == 1.55
    assert params.value_sources["pad_diameter_mm"] == "estimated"


def test_pdf_only_drawings_get_the_unconfirmed_note(offline, monkeypatch, tmp_path):
    pdf = tmp_path / "ds.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(ext, "call_tool", Mock(return_value=dict(_MAIN)))
    logs = []
    params = ext.extract_pin_header_params(
        [pdf], document_roles={"ds.pdf": ["datasheet", "top"]}, component_family="single_tht",
        progress=logs.append)
    assert ext.call_tool.call_count == 1
    assert "solo in PDF" in params.notes


def test_the_conflict_blocks_explicit_pads_too(window):
    """VE12H-K 16:47: the pad readings disagreed and the footprint was still written."""
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))
    params = ExtractedParams(
        name="VE", description="", tags="", confidence="high", notes="", layout="matrix",
        body_length_mm=20.5, body_width_mm=10.5, body_height_mm=20.5,
        package_geometry={"family": "unknown", "source_view": "bottom", "pins": [
            {"number": "1", "x_mm": 0.0, "y_mm": 0.0, "pad_length_mm": 2.0, "pad_width_mm": 2.0,
             "pad_type": "thru_hole", "drill_mm": 1.2}]},
        pin_map_conflict="Le due letture delle posizioni dei pad non coincidono.")
    with pytest.raises(ValueError, match="non coincidono"):
        window._validate_automatic_input(params, require_body_dimensions=False)
    params.pin_map_conflict = None
    window._validate_automatic_input(params, require_body_dimensions=False)
