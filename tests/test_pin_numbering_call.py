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
def test_an_unusable_answer_leaves_the_main_numbering(tmp_path, answer):
    logs = []
    reading = read_pin_numbering([_drawing(tmp_path)], rows=2, cols=8, model="m",
                                 call=Mock(return_value=answer), progress=logs.append)
    assert reading is None


def test_a_failed_call_is_best_effort(tmp_path):
    logs = []
    reading = read_pin_numbering([_drawing(tmp_path)], rows=2, cols=8, model="m",
                                 call=Mock(side_effect=ext.OpenRouterError("boom")),
                                 progress=logs.append)
    assert reading is None and any("non riuscita" in line for line in logs)


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
    data = {"pin_map": list(U_NUMBERING), "contacts_view": "top"}
    apply_dedicated_numbering(data, _reading(U_NUMBERING, view="bottom"))
    assert data["contacts_view"] == "top"


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
