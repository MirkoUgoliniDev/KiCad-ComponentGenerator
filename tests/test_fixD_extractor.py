"""@brief Offline checks of the datasheet-extractor hardening of 2026-09-25 (audit D1-D3, D8, D9, D11, D13).

Every paid call is a Mock or a lambda: no network, no .env.
"""
import threading
import tracemalloc
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import openrouter
from kicadgen.services.datasheet_extractor import (
    ExtractionCancelled, ExtractionError, _castellated_sides, _params_from_tool_arguments,
    _pin_map_seen_from_top)


def matrix(**extra):
    data = {"layout": "matrix", "rows": 2, "cols": 4, "pitch_mm": 2.54,
            "row_spacing_mm": 7.62, "pad_diameter_mm": 1.7, "drill_mm": 1.0,
            "confidence": "high"}
    data.update(extra)
    return data


# ── D1: mandatory matrix fields ────────────────────────────────────────────

@pytest.mark.parametrize("key,bad", [
    ("rows", True), ("rows", 2.7), ("rows", -2), ("rows", "two"), ("rows", float("nan")),
    ("cols", 0), ("cols", False),
    ("pitch_mm", 0), ("pitch_mm", -2.54), ("pitch_mm", "abc"), ("pitch_mm", float("nan")),
    ("pitch_mm", True), ("row_spacing_mm", 0), ("drill_mm", 0), ("pad_diameter_mm", float("inf")),
])
def test_invalid_mandatory_matrix_field_is_an_extraction_error(key, bad):
    with pytest.raises(ExtractionError, match=key):
        _params_from_tool_arguments(matrix(**{key: bad}))


def test_integral_float_counts_are_accepted_as_integers():
    """Negative of the above: 2.0 is an integer count, not a truncation."""
    params = _params_from_tool_arguments(matrix(rows=2.0, cols=4.0))
    assert (params.rows, params.cols) == (2, 4)
    assert type(params.rows) is int and type(params.cols) is int


@pytest.mark.parametrize("drill", [1.7, 2.0])
def test_drill_not_smaller_than_pad_is_rejected(drill):
    with pytest.raises(ExtractionError, match="drill_mm"):
        _params_from_tool_arguments(matrix(drill_mm=drill))


# ── D1: optional fields are dropped with a note, not fatal ─────────────────

def test_populated_pins_keeps_valid_unique_in_range_numbers():
    params = _params_from_tool_arguments(matrix(populated_pins=[True, 2.9, 2, 2, "x", 9, 3.0]))
    assert params.populated_pins == [2, 3]
    assert "populated_pins" in params.notes and "ripetuti" in params.notes


def test_valid_populated_pins_leave_no_note():
    params = _params_from_tool_arguments(matrix(populated_pins=[1, 2, 8]))
    assert params.populated_pins == [1, 2, 8] and "populated_pins" not in params.notes


@pytest.mark.parametrize("pin_map", [
    "zzz",
    [{"pin": 1, "row": 0}],                                   # missing col
    [{"pin": 1, "row": 0, "col": 9}],                         # outside 4 columns
    [{"pin": 1, "row": 5, "col": 0}],                         # outside 2 rows
    [{"pin": 1, "row": 0, "col": 0}, {"pin": 1, "row": 1, "col": 0}],   # repeated pin
    [{"pin": 1, "row": 0, "col": 0}, {"pin": 2, "row": 0, "col": 0}],   # same position
    [{"pin": True, "row": 0, "col": 0}],
    [{"pin": 1, "row": 0.5, "col": 0}],
])
def test_invalid_pin_map_is_dropped_with_a_loud_note(pin_map):
    """@brief The drawn map is dropped loudly; the view still applies to what remains.

    Contract updated on 2026-09-26 (audit finding 5): the view is a property
    of the DRAWING, not of pin_map. Before, an invalid map on a bottom view
    left columns and body offset unmirrored, i.e. a footprint mirrored with
    respect to the part. Now the fallback column-order numbering and the body
    offset read on that bottom view are both mirrored; on a top view nothing
    is mirrored and no map is invented.
    """
    bottom = _params_from_tool_arguments(matrix(pin_map=pin_map, contacts_view="bottom",
                                                body_offset_x_mm=-1.0, body_length_mm=12.0))
    assert "ATTENZIONE pin_map scartata" in bottom.notes
    # Column order read from below: pin 1 ends up in the last column seen from above.
    assert {e["pin"]: e["col"] for e in bottom.pin_map}[1] == 3
    # Margins swap: left margin seen from above = 12 - 7.62 - 1.0.
    assert bottom.body_offset_x_mm == pytest.approx(-3.38)
    top = _params_from_tool_arguments(matrix(pin_map=pin_map, contacts_view="top",
                                             body_offset_x_mm=-1.0, body_length_mm=12.0))
    assert top.pin_map is None and "ATTENZIONE pin_map scartata" in top.notes
    assert top.body_offset_x_mm == -1.0


def test_valid_bottom_view_pin_map_is_mirrored():
    entries = _pin_map_seen_from_top([{"pin": 1, "row": 0, "col": 0},
                                      {"pin": 2, "row": 1, "col": 3}], 4, "bottom", rows=2)
    assert entries == [{"pin": 1, "row": 0, "col": 3}, {"pin": 2, "row": 1, "col": 0}]


def test_pin_map_errors_are_extraction_errors_not_raw_exceptions():
    for bad in ("zzz", [{"pin": 1, "row": 0}], [None]):
        with pytest.raises(ExtractionError):
            _pin_map_seen_from_top(bad, 4, "top")


@pytest.mark.parametrize("key", ["pcb_thickness_mm", "pin_length_3d_mm", "body_offset_y_mm"])
def test_invalid_optional_numbers_are_dropped_with_a_note(key):
    params = _params_from_tool_arguments(matrix(**{key: "abc"}))
    assert getattr(params, key) is None and key in params.notes


def test_invalid_confidence_becomes_low():
    assert _params_from_tool_arguments(matrix(confidence="certain")).confidence == "low"
    assert _params_from_tool_arguments(matrix(confidence="medium")).confidence == "medium"


# ── D3: castellated sides ──────────────────────────────────────────────────

def test_two_sides_on_the_same_edge_are_rejected():
    with pytest.raises(ExtractionError, match="piu' volte"):
        _castellated_sides([dict(edge="left", count=2, pitch_mm=2.54),
                            dict(edge="LEFT", count=3, pitch_mm=2.54)])


def test_sides_on_different_edges_are_accepted():
    sides = _castellated_sides([dict(edge="left", count=2, pitch_mm=2.54),
                                dict(edge="right", count=3, pitch_mm=2.54)])
    assert [s["edge"] for s in sides] == ["left", "right"]


def test_a_side_longer_than_its_edge_is_rejected():
    # left/right run along the board width (10 mm): 5 contacts at 2.54 span 10.16 mm.
    with pytest.raises(ExtractionError, match="bordo"):
        _castellated_sides([dict(edge="left", count=5, pitch_mm=2.54)],
                           body_length=40, body_width=10)


def test_the_same_side_fits_the_longer_edge():
    """Negative: the same row on a top edge (40 mm long) fits."""
    sides = _castellated_sides([dict(edge="top", count=5, pitch_mm=2.54)],
                               body_length=40, body_width=10)
    assert sides[0]["count"] == 5


def test_a_huge_count_never_materializes_its_pad_range():
    tracemalloc.start()
    try:
        sides = _castellated_sides([dict(edge="top", count=10**9, pitch_mm=2.54),
                                    dict(edge="bottom", count=3, pitch_mm=2.54)])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sides[1]["first_pin"] == 10**9 + 1
    assert peak < 5_000_000
    with pytest.raises(ExtractionError, match="bordo"):
        _castellated_sides([dict(edge="top", count=10**9, pitch_mm=2.54)],
                           body_length=40, body_width=20)


def test_numbering_matches_the_step_by_step_convention():
    sides = _castellated_sides([dict(edge="top", count=3, pitch_mm=2.54),
                                dict(edge="bottom", count=2, pitch_mm=2.54, first_pin=2),
                                dict(edge="left", count=2, pitch_mm=2.54, first_pin=6)])
    # top cannot start at 1 (2..3 taken): 4..5 is free but 6 is taken, so 8.
    assert [s["first_pin"] for s in sides] == [8, 2, 6]


def test_pin_names_of_the_wrong_length_are_dropped_with_a_note():
    notes = []
    sides = _castellated_sides([dict(edge="left", count=3, pitch_mm=2.54, pin_names=["A", "B"])],
                               notes=notes)
    assert "pin_names" not in sides[0] and "nomi" in notes[0]
    matching = _castellated_sides([dict(edge="left", count=2, pitch_mm=2.54, pin_names=["A", "B"])])
    assert matching[0]["pin_names"] == ["A", "B"]


# ── End-to-end: the paid chain, faked ──────────────────────────────────────

@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    path = tmp_path / "drawing.pdf"
    path.write_bytes(b"%PDF-1.4 offline")
    return path


def test_d2_repair_compares_normalized_sides(offline, monkeypatch):
    """The raw list holds a dropped entry and an upper-case edge; the correction is valid."""
    sides = ["not a side", dict(edge="TOP", count=2, pitch_mm=2.54, first_pin=1),
             dict(edge="bottom", count=2, pitch_mm=2.54, first_pin=1)]
    corrected = [dict(edge="top", count=2, pitch_mm=2.54, first_pin=1),
                 dict(edge="bottom", count=2, pitch_mm=2.54, first_pin=3)]
    call = Mock(side_effect=[dict(layout="castellated", castellated_sides=sides),
                             dict(castellated_sides=corrected),
                             {"sides": []}])
    monkeypatch.setattr(ext, "call_tool", call)
    result = ext.extract_pin_header_params([offline])
    assert call.call_count == 3
    assert call.call_args_list[2].kwargs["tool"]["function"]["name"] == "observe_contact_styles"
    assert [(s["edge"], s["first_pin"]) for s in result.castellated_sides] == [("top", 1), ("bottom", 3)]


def test_d2_a_correction_that_changes_counts_is_still_rejected(offline, monkeypatch):
    sides = [dict(edge="top", count=2, pitch_mm=2.54, first_pin=1),
             dict(edge="bottom", count=2, pitch_mm=2.54, first_pin=1)]
    corrected = [dict(edge="top", count=2, pitch_mm=2.54, first_pin=1),
                 dict(edge="bottom", count=3, pitch_mm=2.54, first_pin=3)]
    monkeypatch.setattr(ext, "call_tool", Mock(side_effect=[
        dict(layout="castellated", castellated_sides=sides), dict(castellated_sides=corrected)]))
    with pytest.raises(ExtractionError, match="conteggi"):
        ext.extract_pin_header_params([offline])


def test_rejected_main_answer_is_kept_in_the_log_and_the_error(offline, monkeypatch):
    answer = matrix(rows=True)
    # A fresh dict per call: the extractor sanitizes the answer in place, the
    # preserved copy must be the answer exactly as it arrived.
    monkeypatch.setattr(ext, "call_tool", lambda **kw: dict(answer))
    lines = []
    with pytest.raises(ExtractionError) as caught:
        ext.extract_pin_header_params([offline], progress=lines.append)
    assert caught.value.raw_response == answer
    rejected = [line for line in lines if line.startswith("AI_RESPONSE_REJECTED ")]
    assert len(rejected) == 1 and '"rows": true' in rejected[0]


def test_a_valid_main_answer_writes_no_rejection_line(offline, monkeypatch):
    monkeypatch.setattr(ext, "call_tool", lambda **kw: matrix())
    lines = []
    ext.extract_pin_header_params([offline], progress=lines.append)
    assert not any(line.startswith("AI_RESPONSE_REJECTED") for line in lines)


def test_d8_cancel_before_the_first_paid_call(offline, monkeypatch):
    call = Mock(return_value=matrix())
    monkeypatch.setattr(ext, "call_tool", call)
    stop = threading.Event()
    stop.set()
    with pytest.raises(ExtractionCancelled):
        ext.extract_pin_header_params([offline], cancel=stop)
    call.assert_not_called()


def test_d8_cancel_between_paid_calls(offline, monkeypatch):
    """Cancelled during the main call: the numbering repair is never paid."""
    stop = threading.Event()
    sides = [dict(edge="top", count=2, pitch_mm=2.54, first_pin=1),
             dict(edge="bottom", count=2, pitch_mm=2.54, first_pin=1)]

    def main_call(**kw):
        stop.set()
        return dict(layout="castellated", castellated_sides=sides)
    call = Mock(side_effect=main_call)
    monkeypatch.setattr(ext, "call_tool", call)
    with pytest.raises(ExtractionCancelled):
        ext.extract_pin_header_params([offline], cancel=stop.is_set, progress=lambda m: None)
    assert call.call_count == 1


def test_d8_no_cancel_channel_keeps_the_previous_behaviour(offline, monkeypatch):
    monkeypatch.setattr(ext, "call_tool", lambda **kw: matrix())
    assert ext.extract_pin_header_params([offline], cancel=threading.Event()).rows == 2


def _castellated_answer():
    return dict(layout="castellated", body_length_mm=40, body_width_mm=20,
                castellated_sides=[dict(edge="left", count=2, pitch_mm=2.54)],
                components_3d=[dict(name="R1", x_mm=0, y_mm=0, length_mm=2, width_mm=1,
                                    height_mm=.5)])


@pytest.mark.parametrize("failure", [KeyError("box"), TypeError("bad row"), RuntimeError("x")])
def test_d9_any_photo_inventory_failure_becomes_a_note(offline, monkeypatch, failure):
    from kicadgen.services import photo_inventory as inv
    monkeypatch.setattr(ext, "call_tool", lambda **kw: _castellated_answer())
    monkeypatch.setattr(inv, "extract_inventory", Mock(side_effect=failure))
    lines = []
    result = ext.extract_pin_header_params([offline], progress=lines.append,
                                           photo_cache_dir=offline.parent / "cache")
    assert "Inventario fotografico fallito" in result.notes
    assert any(line.startswith("AI_PHOTO_INVENTORY_TRACEBACK ") and type(failure).__name__ in line
               for line in lines)
    assert result.castellated_sides[0]["edge"] == "left"


def test_d9_a_cancellation_inside_the_inventory_is_not_downgraded(offline, monkeypatch):
    from kicadgen.services import photo_inventory as inv
    monkeypatch.setattr(ext, "call_tool", lambda **kw: _castellated_answer())
    monkeypatch.setattr(inv, "extract_inventory",
                        Mock(side_effect=ExtractionCancelled("stop")))
    with pytest.raises(ExtractionCancelled):
        ext.extract_pin_header_params([offline], photo_cache_dir=offline.parent / "cache")


def test_d11_file_too_large_message_is_a_template(monkeypatch, tmp_path):
    path = tmp_path / "big.pdf"
    path.write_bytes(b"%PDF")
    monkeypatch.setattr(ext, "MAX_FILE_BYTES", 1)
    with pytest.raises(ExtractionError, match=r"big\.pdf troppo grande \(0\.0 MB, max 0 MB\)"):
        ext._file_to_content_block(path)


def test_d13_usage_is_reset_before_any_validation(monkeypatch):
    openrouter.reset_usage()
    openrouter._record_usage({"usage": {"cost": 1.0}})
    assert openrouter.usage_totals().calls == 1
    with pytest.raises(ExtractionError):
        ext.extract_pin_header_params([], photo_detector="ai")
    assert openrouter.usage_totals().calls == 0
