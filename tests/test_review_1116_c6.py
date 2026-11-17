"""@brief Fixes of the 2026-11-16 review of the datasheet extractor (findings R6-1 .. R6-9).

Every paid call is a Mock or a function: no network, no .env. Each rule has a
positive case and a negative twin that differ for a structural reason (a
field that is a list or not, a retry that has a usable fallback or not, a
grid inside or beyond the declared bound), never a number tuned to a part.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
import time
from unittest.mock import Mock

import pytest

from kicadgen.core.pad_limits import MAX_PAD_POSITIONS
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import (
    ExtractionCancelled, ExtractionError, _params_from_tool_arguments, _validate_main_response,
    read_axis_chain, read_pad_positions)
from kicadgen.services.openrouter import OpenRouterError, RequestCancelled
from test_single_component_prompt import PADS, _drawing, _single

#: A well-formed chain transcription: two pads 2.54 mm apart.
CHAINS = [{"axis": "x", "stops": ["1", "2"], "values": [2.54]},
          {"axis": "y", "stops": ["1,2", "edge"], "values": [1.0]}]


@pytest.fixture
def offline(monkeypatch):
    """No key or credit lookup: the analysis runs on the faked calls only."""
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)


def _tool_name(kwargs) -> str:
    """The name of the tool a faked call was asked to answer."""
    tool = kwargs.get("tool") or {}
    return (tool.get("function") or {}).get("name") or tool.get("name") or ""


# -- R6-1: a malformed best-effort reading is discarded, never a TypeError ----

@pytest.mark.parametrize("field,value", [("chains", 5), ("chains", "1-2"), ("chains", {"x": 1}),
                                         ("midpoints", 1), ("midpoints", float("inf"))])
def test_a_pad_reading_whose_lists_are_not_lists_is_discarded(tmp_path, field, value):
    answer = {"drawing_unit": "mm", "contacts_view": "top", "chains": CHAINS, field: value}
    reading = read_pad_positions([_drawing(tmp_path)], model="m", call=Mock(return_value=answer))
    assert reading["pads"] == {} and field in reading["discarded"]


@pytest.mark.parametrize("missing", ["chains", "midpoints"])
def test_a_pad_reading_with_lists_or_absent_fields_is_read(tmp_path, missing):
    """Negative: a list, or a field left out (an empty transcription), is no broken shape."""
    answer = {"drawing_unit": "mm", "contacts_view": "top", "chains": CHAINS, "midpoints": []}
    answer[missing] = None
    reading = read_pad_positions([_drawing(tmp_path)], model="m", call=Mock(return_value=answer))
    assert "discarded" not in reading
    assert reading["raw_chains"] == ([] if missing == "chains" else CHAINS)
    if missing == "midpoints":
        assert set(reading["pads"]) == {"1", "2"}


def test_an_axis_reread_whose_chains_are_not_a_list_is_unusable(tmp_path):
    logs = []
    broken = {"drawing_unit": "mm", "chains": 5, "midpoints": []}
    assert read_axis_chain([_drawing(tmp_path)], "x", "problem", model="m",
                           call=Mock(return_value=broken), progress=logs.append) is None
    assert logs and "chains" in logs[-1]
    # Negative: the same re-read with a list of chains is used.
    good = {"drawing_unit": "mm", "chains": CHAINS, "midpoints": []}
    reread = read_axis_chain([_drawing(tmp_path)], "x", "problem", model="m",
                             call=Mock(return_value=good))
    assert reread is not None and [c["axis"] for c in reread["chains"]] == ["x"]


def test_reread_pad_positions_survives_a_malformed_reading(tmp_path):
    """The partial re-read used to raise TypeError on `chains: 5`; now the stored
    pads stay, unconfirmed, and the result says the reading was discarded."""
    stored = ext.single_answer_to_data(_single())
    reread = ext.reread_pad_positions(stored, [_drawing(tmp_path)], model="vendor/reader",
                                      call=Mock(return_value={"drawing_unit": "mm", "chains": 5}))
    assert [p["number"] for p in reread.package_geometry["pins"]] == ["1", "2", "5"]
    assert "chains" in reread.notes


def test_a_malformed_pad_reading_keeps_the_paid_main_answer(offline, monkeypatch, tmp_path):
    """End to end: two paid calls, the second malformed; the analysis still returns."""
    def call(**kwargs):
        name = _tool_name(kwargs)
        if name == "report_single_component":
            return _single()
        if name == "report_pad_positions":
            return {"drawing_unit": "mm", "contacts_view": "bottom", "chains": 5,
                    "midpoints": 1}
        raise OpenRouterError("offline")
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["hires"]})
    assert [p["number"] for p in params.package_geometry["pins"]] == ["1", "2", "5"]


# -- R6-2: a main answer of an unexpected shape is an application error -------

@pytest.mark.parametrize("pinout,kept", [(True, None), (5, None),
                                          ([{"pin": "1", "name": "IN"}], ["1"])])
def test_a_pinout_that_is_not_a_list_is_dropped_not_fatal(offline, monkeypatch, tmp_path, pinout,
                                                         kept):
    """`pinout: true` raised TypeError inside the main call, outside every handler,
    then in the role check after validation; now it is dropped with a note and the
    paid answer is kept. Negative: a list pinout is kept."""
    def call(**kwargs):
        if _tool_name(kwargs) == "report_single_component":
            return _single(pinout=pinout)
        raise OpenRouterError("offline")
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["hires"]})
    assert [p["number"] for p in params.package_geometry["pins"]] == ["1", "2", "5"]
    assert (None if params.pinout is None else [str(e["pin"]) for e in params.pinout]) == kept
    if kept is None:
        assert "pinout" in params.notes


def test_an_unexpected_shape_in_the_main_call_becomes_an_extraction_error(offline, monkeypatch,
                                                                          tmp_path):
    """A conversion step that breaks on the answer's shape: logged, application error."""
    def broken(answer):
        raise TypeError("'bool' object is not iterable")
    monkeypatch.setattr(ext, "single_answer_to_data", broken)
    monkeypatch.setattr(ext, "call_tool", lambda **kw: _single())
    lines = []
    with pytest.raises(ExtractionError, match="Risposta AI non valida") as caught:
        ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                      document_roles={"quote.png": ["hires"]},
                                      progress=lines.append)
    assert caught.value.raw_response is not None
    assert any(line.startswith("AI_RESPONSE_REJECTED ") for line in lines)
    # Follow-up S6: the traceback, scrubbed, tells a program defect from a bad
    # answer -- here the frame of the broken conversion is named.
    (trace,) = [line for line in lines if line.startswith("AI_SHAPE_TRACEBACK ")]
    assert "in broken" in trace and "\n" not in trace


def test_a_valid_main_answer_writes_no_traceback(offline, monkeypatch, tmp_path):
    """Negative: no exception, no traceback line."""
    def call(**kwargs):
        if _tool_name(kwargs) == "report_single_component":
            return _single()
        raise OpenRouterError("offline")
    monkeypatch.setattr(ext, "call_tool", call)
    lines = []
    ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                  document_roles={"quote.png": ["hires"]}, progress=lines.append)
    assert not any(line.startswith("AI_SHAPE_TRACEBACK") for line in lines)


def test_components_3d_that_are_not_a_list_count_as_none(monkeypatch, tmp_path):
    """The «initial components» log line computed `len(True)` outside every handler."""
    from minimal_pdf import MINIMAL_PDF
    from test_fixD_extractor import matrix
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    for components, expected in ((True, "=0"), ([{"name": "U1"}], "=1")):
        monkeypatch.setattr(ext, "call_tool",
                            lambda components=components, **kw: matrix(components_3d=components))
        lines = []
        ext.extract_pin_header_params([pdf], progress=lines.append)
        (line,) = [l for l in lines if "Analisi principale completata" in l]
        assert line.rstrip(".").endswith(expected)


# -- R6-4: a retry failed at transport level keeps the usable fallback ----------

def _contradicting_grid():
    """A single-part grid whose populated pin 9 has no position: a broken shape
    whose fallback keeps the printed numbers 1, 2 and 9 without positions."""
    return _single(pads=[], grid={"rows": 1, "cols": 2, "pitch_mm": 2.54, "pad_diameter_mm": 1.5,
                                  "drill_mm": 0.8, "populated_pins": [1, 2, 9]})


def _flattened_pad():
    """A broken shape WITHOUT a usable fallback (one pad flattened to the top level)."""
    return dict(drawing_unit="mm", contacts_view="bottom", confidence="high", pads="1",
                pads_origin="custom", x_mm=0.0, y_mm=0.0, drill_mm=1.2, pad_type="thru_hole",
                pad_length_mm=2.2, pad_width_mm=2.2, shape="circle")


def _main_then(first, retry):
    """A faked call: `first` for the first main call, `retry` (raised) for the second,
    every dedicated reading failing at transport level (best effort)."""
    seen = []

    def call(**kwargs):
        if _tool_name(kwargs) == "report_single_component":
            seen.append(1)
            if len(seen) == 1:
                return first
            raise retry
        raise OpenRouterError("offline")
    call.seen = seen
    return call


def test_a_failed_retry_keeps_the_usable_part_of_the_first_answer(offline, monkeypatch, tmp_path):
    call = _main_then(_contradicting_grid(), OpenRouterError("HTTP 502"))
    monkeypatch.setattr(ext, "call_tool", call)
    lines = []
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["hires"]},
                                           progress=lines.append)
    assert len(call.seen) == 2
    assert any("HTTP 502" in line and "parte utilizzabile" in line for line in lines)
    # The printed numbers survive; none of them got a grid coordinate.
    assert {"1", "2", "9"} <= set(params.unread_pads or ())


def test_a_failed_retry_without_a_fallback_still_stops(offline, monkeypatch, tmp_path):
    """Negative: a broken shape with nothing usable is not invented into an answer."""
    monkeypatch.setattr(ext, "call_tool", _main_then(_flattened_pad(), OpenRouterError("HTTP 502")))
    with pytest.raises(ExtractionError, match="HTTP 502"):
        ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                      document_roles={"quote.png": ["hires"]})


def test_a_cancelled_retry_never_falls_back(offline, monkeypatch, tmp_path):
    """Negative: the user's stop during the retry stays a cancellation."""
    monkeypatch.setattr(ext, "call_tool",
                        _main_then(_contradicting_grid(), RequestCancelled("stop")))
    with pytest.raises(ExtractionCancelled):
        ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                      document_roles={"quote.png": ["hires"]})


# -- R6-3: a failed castellated recount keeps the first answer, blocked --------

def _side(edge, count, first_pin, names=None):
    """One castellated side at 2.54 mm pitch, with `names` pin names when given."""
    entry = dict(edge=edge, count=count, pitch_mm=2.54, first_pin=first_pin,
                 contact_style="half_and_through")
    if names is not None:
        entry["pin_names"] = [f"P{i}" for i in range(names)]
    return entry


def _castellated(sides, length=63.3, width=25.4):
    """A castellated main answer around `sides`."""
    return dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=length, body_width_mm=width, castellated_sides=sides)


@pytest.mark.parametrize("failure", [OpenRouterError("HTTP 502"), "unusable"])
def test_a_failed_recount_keeps_the_first_answer_with_the_footprint_blocked(failure):
    """Names against count contradict, the contacts fit their edge: the first
    answer is kept, the cause is said and the footprint is blocked."""
    data = _castellated([_side("top", 22, 1, names=21)])
    call = (Mock(side_effect=failure) if isinstance(failure, Exception)
            else Mock(return_value={"castellated_sides": []}))
    lines = []
    _validate_main_response(data, [], call, None, None, False, lines.append)
    assert call.call_count == 1
    params = _params_from_tool_arguments(data)
    assert [s["count"] for s in params.castellated_sides] == [22]
    assert params.pin_map_conflict and "count=22 ma pin_names ha 21" in params.pin_map_conflict
    if isinstance(failure, Exception):
        assert "HTTP 502" in params.pin_map_conflict
    assert any("ripeti l'analisi" in line for line in lines)


def test_a_successful_recount_does_not_block_the_footprint():
    """Negative: a usable recount replaces the side and nothing is blocked."""
    data = _castellated([_side("top", 22, 1, names=21)])
    call = Mock(return_value=dict(castellated_sides=[_side("top", 22, 1, names=22)]))
    _validate_main_response(data, [], call, None, None, False, None)
    assert not _params_from_tool_arguments(data).pin_map_conflict


def test_a_failed_recount_of_sides_that_cannot_fit_stops_with_both_causes():
    """Negative of the keep: 28 contacts at 2.54 mm do not fit 63.3 mm, so the kept
    answer cannot be drawn; the error names the recount's cause too."""
    data = _castellated([_side("top", 28, 1)])
    with pytest.raises(ExtractionError, match="HTTP 502.*non è utilizzabile"):
        _validate_main_response(data, [], Mock(side_effect=OpenRouterError("HTTP 502")), None,
                                None, False, None)


def _counts_and_numbering_conflict(names, recount):
    """Top edge with 28 contacts on 80 mm (they fit) and `names` pin names, plus a
    right edge whose first_pin 25 overlaps 1..28: a numbering conflict between
    sides. `recount` is the first call's outcome (raised when an exception); the
    numbering repair then repeats the conflict, so the side-numbering question
    is asked."""
    sides = [_side("top", 28, 1, names=names), _side("right", 5, 25)]
    data = _castellated(sides, length=80.0)
    data["pin_numbers_printed"] = True
    effects = ([recount] if recount is not None else []) + [dict(castellated_sides=sides)]
    _validate_main_response(data, [], Mock(side_effect=effects), None, None, False, None)
    params = _params_from_tool_arguments(data)
    (question,) = [q for q in params.questions if q["field"] == "side_numbering"]
    return params, question


@pytest.mark.parametrize("answer", [ext.SIDE_CONFLICT_MOVE, ext.SIDE_CONFLICT_CONVENTION])
def test_a_numbering_answer_never_lifts_the_block_of_a_failed_recount(answer):
    """Follow-up D1: the counts a failed recount left contradicted are a block of
    their own cause; answering the side-numbering question lifts only its own."""
    params, question = _counts_and_numbering_conflict(22, OpenRouterError("HTTP 502"))
    # CONTRACT CHANGED (review G): the numbering conflict is held under its own cause too.
    assert set(params.held_blocks) == {ext.HELD_SIDE_NUMBERING, ext.HELD_SIDE_COUNTS}
    assert "HTTP 502" in params.pin_map_conflict and "25, 26, 27, 28" in params.pin_map_conflict
    after = ext.apply_answers(params, {question["key"]: answer})
    assert after.pin_map_conflict and "HTTP 502" in after.pin_map_conflict
    assert "25, 26, 27, 28" not in after.pin_map_conflict
    assert set(after.held_blocks) == {ext.HELD_SIDE_COUNTS}


def test_a_numbering_answer_unblocks_when_no_other_cause_holds():
    """Reverse: the counts are coherent (no recount), so the same answer unblocks."""
    params, question = _counts_and_numbering_conflict(28, None)
    # CONTRACT CHANGED (review G): the conflict is the only held cause.
    assert set(params.held_blocks) == {ext.HELD_SIDE_NUMBERING} and params.pin_map_conflict
    after = ext.apply_answers(params, {question["key"]: ext.SIDE_CONFLICT_MOVE})
    assert after.pin_map_conflict is None


@pytest.mark.parametrize("held", [{ext.HELD_GRID_SIZE: "grid block"}, None])
def test_a_pad_reread_keeps_only_the_held_blocks_of_the_settled_result(tmp_path, held):
    """A partial pad re-read replaces the scope's block with its own (here none: the
    reading confirms the pads); a held block of another cause stays, and without
    one the merged result is unblocked."""
    wrong = [dict(p, x_mm=x, y_mm=y) for p, (x, y) in zip(PADS, [(0.0, 0.0), (2.54, 0.0),
                                                                (5.08, 0.0)])]
    stored = ext.single_answer_to_data(_single(pads=wrong))
    agreeing = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["1", "2", "5"], "values": [2.54, 2.54]},
        {"axis": "y", "stops": ["1,2,5", "edge"], "values": [3.0]}]}
    reread = ext.reread_pad_positions(stored, [_drawing(tmp_path)], model="vendor/reader",
                                      call=Mock(return_value=agreeing))
    assert reread.pin_map_conflict is None
    settled = _params_from_tool_arguments(dict(stored))
    settled.held_blocks = held
    settled.pin_map_conflict = "old pad block" + ("\ngrid block" if held else "")
    merged = ext.merge_reread(settled, reread)
    assert merged.pin_map_conflict == ("grid block" if held else None)


def test_a_cancelled_recount_is_a_cancellation():
    """An aborted recount request is the user's stop, not a recount failure."""
    data = _castellated([_side("top", 22, 1, names=21)])
    with pytest.raises(ExtractionCancelled):
        _validate_main_response(data, [], Mock(side_effect=RequestCancelled("stop")), None,
                                None, False, None)


# -- R6-5: a grid beyond the declared bound is refused before it is enumerated --

@pytest.mark.parametrize("rows,cols", [(1e30, 2), (3000, 3000), (MAX_PAD_POSITIONS + 1, 1)])
def test_a_single_part_grid_beyond_the_bound_is_a_broken_shape(rows, cols):
    start = time.monotonic()
    with pytest.raises(ext.AnswerShapeError, match=str(MAX_PAD_POSITIONS)):
        ext.single_answer_to_data(_single(pads=[], grid={
            "rows": rows, "cols": cols, "pitch_mm": 2.54, "row_spacing_mm": 2.54,
            "pad_diameter_mm": 1.5, "drill_mm": 0.8}))
    assert time.monotonic() - start < 1.0


def test_a_single_part_grid_at_the_bound_is_kept():
    """Negative: exactly MAX_PAD_POSITIONS positions are a grid, not an error."""
    data = ext.single_answer_to_data(_single(pads=[], grid={
        "rows": 1, "cols": MAX_PAD_POSITIONS, "pitch_mm": 0.1, "pad_diameter_mm": 0.08,
        "drill_mm": 0.05}))
    assert len(data["package_geometry"]["pins"]) == MAX_PAD_POSITIONS


@pytest.mark.parametrize("rows,cols", [(1e30, 4), (3000, 3000)])
def test_a_main_matrix_beyond_the_bound_keeps_the_answer_and_blocks(rows, cols):
    """Follow-up B#6: the counts are dropped at once, the rest of the paid answer
    kept, and the footprint (the grid) blocked by its own cause."""
    from test_fixD_extractor import matrix
    start = time.monotonic()
    data = matrix(rows=rows, cols=cols, body_length_mm=20.0)
    _validate_main_response(data, [], Mock(), None, None, False, None)
    for params in (_params_from_tool_arguments(data),
                   _params_from_tool_arguments(matrix(rows=rows, cols=cols, body_length_mm=20.0))):
        assert params.rows is None and params.cols is None
        assert params.body_length_mm == pytest.approx(20.0) and params.pitch_mm == 2.54
        assert str(MAX_PAD_POSITIONS) in params.pin_map_conflict
        assert set(params.held_blocks) == {ext.HELD_GRID_SIZE}
    assert time.monotonic() - start < 1.0


def test_an_informative_grid_beyond_the_bound_is_dropped_without_a_block():
    """Negative: explicit pads are the footprint, so the absurd grid beside them
    is dropped with a note and blocks nothing."""
    data = ext.single_answer_to_data(_single())
    data.update(rows=3000, cols=3000)
    params = _params_from_tool_arguments(data)
    assert params.rows is None and not params.held_blocks
    assert str(MAX_PAD_POSITIONS) in params.notes


def test_a_main_matrix_inside_the_bound_is_validated():
    """Negative: an ordinary 2 x 4 header passes both checks."""
    from test_fixD_extractor import matrix
    data = matrix()
    _validate_main_response(data, [], Mock(), None, None, False, None)
    params = _params_from_tool_arguments(data)
    assert (params.rows, params.cols) == (2, 4)


def test_the_pin_field_of_a_grid_beyond_the_bound_is_unusable():
    from test_fixD_extractor import matrix
    params = _params_from_tool_arguments(matrix())
    assert len(ext._pin_field_positions(params)) == 8
    params.rows = 10 ** 30
    start = time.monotonic()
    assert ext._pin_field_positions(params) == []
    assert time.monotonic() - start < 1.0


# -- R6-6: rejected and unreadable answers are redacted in the AI log ----------

SECRET = "sk-or-v1-" + "a1b2c3d4" * 4


def test_a_rejected_answer_is_logged_without_credentials():
    lines = []
    error = ExtractionError("x")
    ext._log_rejected_response({"rows": True, "notes": f"key {SECRET}"}, error, lines.append)
    (line,) = [l for l in lines if l.startswith("AI_RESPONSE_REJECTED ")]
    assert SECRET not in line
    # Negative: the answer's own values are still there to compare with a retry,
    # and the exception keeps the answer exactly as it arrived.
    assert '"rows": true' in line and error.raw_response["notes"].endswith(SECRET)


def test_an_unreadable_answer_is_logged_without_credentials():
    from kicadgen.services.openrouter import ToolArgumentsError
    lines = []
    raw = '{"x": , "k": "Bearer ' + SECRET + '"}'
    ext._log_unreadable_arguments(ToolArgumentsError("bad", raw=raw), lines.append, 1)
    assert SECRET not in lines[0]
    # Negative: the text is not digested, so the parse error stays explainable.
    assert lines[0].startswith('AI_RESPONSE_UNREADABLE attempt=1 {"x": , "k": "Bearer ')


# -- R6-7: a cancelled photo review is a cancellation --------------------------

def test_a_cancelled_photo_review_is_an_extraction_cancelled(monkeypatch, tmp_path):
    from kicadgen.services import photo_inventory
    from minimal_pdf import MINIMAL_PDF

    def stop(*args, **kwargs):
        raise photo_inventory.ReviewCancelled("review cancelled")
    monkeypatch.setattr(photo_inventory, "extract_inventory", stop)
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    monkeypatch.setattr(ext, "call_tool", lambda **kw: _castellated(
        [_side("top", 22, 1, names=22), _side("bottom", 22, 23, names=22)]))
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    with pytest.raises(ExtractionCancelled):
        ext.extract_pin_header_params([pdf])

    # Negative: an inventory that fails is optional, never a cancellation.
    def fail(*args, **kwargs):
        raise ValueError("inventory broken")
    monkeypatch.setattr(photo_inventory, "extract_inventory", fail)
    assert ext.extract_pin_header_params([pdf]) is not None


# -- contact-style recheck: an aborted request is the user's stop --------------

def test_an_aborted_contact_style_recheck_is_a_cancellation(monkeypatch):
    side = {"edge": "top"}
    monkeypatch.setattr(ext, "_needs_contact_style_recheck", lambda *a: True)
    monkeypatch.setattr(ext, "_sides_missing_style", lambda data: {"top": side})
    with pytest.raises(ExtractionCancelled):
        ext._recheck_missing_contact_styles({}, [], Mock(side_effect=RequestCancelled("stop")),
                                            "m")
    # Negative: a failed check leaves the choice to the user, the analysis goes on.
    lines = []
    ext._recheck_missing_contact_styles({}, [], Mock(side_effect=OpenRouterError("HTTP 502")),
                                        "m", progress=lines.append)
    assert "contact_style" not in side and lines


# -- follow-up E2: every block is of its own cause ----------------------------

def _same_model_pad_question(tmp_path):
    """A pad re-read by the main model: one recommended pad-layout question, no block."""
    wrong = [dict(p, x_mm=x, y_mm=y) for p, (x, y) in zip(PADS, [(0.0, 0.0), (2.54, 0.0),
                                                                (5.08, 0.0)])]
    stored = ext.single_answer_to_data(_single(pads=wrong))
    agreeing = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["1", "2", "5"], "values": [2.54, 2.54]},
        {"axis": "y", "stops": ["1,2,5", "edge"], "values": [3.0]}]}
    params = ext.reread_pad_positions(stored, [_drawing(tmp_path)], model="vendor/main",
                                      call=Mock(return_value=agreeing), same_model=True)
    (question,) = [q for q in params.questions if q["field"] == "pad_layout"]
    return params, question


def test_a_pad_layout_answer_lifts_only_the_pad_causes(tmp_path):
    """The applied pad-layout answer settles the pad causes (positions, holes, fit);
    a partial pin map no convention completes is another cause and stays."""
    params, question = _same_model_pad_question(tmp_path)
    params.held_blocks = {ext.HELD_PIN_MAP_COMPLETION: "completion block",
                          ext.HELD_PAD_FIT: "fit block"}
    params.pin_map_conflict = "completion block\nfit block"
    after = ext.apply_answers(params, {question["key"]: question["options"][0]["value"]})
    assert after.pin_map_conflict == "completion block"
    assert set(after.held_blocks) == {ext.HELD_PIN_MAP_COMPLETION}


def test_a_pad_layout_answer_unblocks_when_only_pad_causes_hold(tmp_path):
    """Negative: with only pad causes held, the same answer unblocks."""
    params, question = _same_model_pad_question(tmp_path)
    params.held_blocks = {ext.HELD_PAD_FIT: "fit block"}
    params.pin_map_conflict = "fit block"
    after = ext.apply_answers(params, {question["key"]: question["options"][0]["value"]})
    assert after.pin_map_conflict is None and not after.held_blocks


def test_a_numbering_answer_never_lifts_a_pad_cause():
    """The side-numbering answer settles its own conflict, never a pad cause."""
    params, question = _counts_and_numbering_conflict(28, None)
    params.held_blocks = {ext.HELD_PAD_HOLES: "hole block"}
    params.pin_map_conflict += "\nhole block"
    after = ext.apply_answers(params, {question["key"]: ext.SIDE_CONFLICT_MOVE})
    assert after.pin_map_conflict == "hole block"


def test_the_blocks_of_the_pad_rules_are_held_by_cause():
    """A pad read without its position: the block is held under its cause and
    listed in the text the window reads."""
    pads = [dict(p, x_mm=None) if p["number"] == "5" else p for p in PADS]
    params = _params_from_tool_arguments(ext.single_answer_to_data(_single(pads=pads)))
    assert set(params.held_blocks) == {ext.HELD_PAD_POSITIONS}
    assert params.pin_map_conflict == params.held_blocks[ext.HELD_PAD_POSITIONS]
    # Negative: a part whose pads are all placed holds no pad cause.
    clean = _params_from_tool_arguments(ext.single_answer_to_data(_single()))
    assert not set(clean.held_blocks or ()) & set(ext.PAD_LAYOUT_CAUSES)


# -- follow-up E1: a pad re-read merges the held causes ------------------------

def test_a_pad_reread_replaces_the_pad_causes_and_keeps_the_others(tmp_path):
    """The settled pad causes are the re-read's scope (replaced by its own, here
    none); another held cause stays; a cause the re-read holds is carried over."""
    params, _question = _same_model_pad_question(tmp_path)
    reread = params
    reread.held_blocks = {ext.HELD_PAD_HOLES: "new hole block"}
    reread.pin_map_conflict = "new hole block"
    settled = copy.deepcopy(reread)
    settled.partial_reread = None
    settled.held_blocks = {ext.HELD_PAD_FIT: "old fit block", ext.HELD_GRID_SIZE: "grid block"}
    settled.pin_map_conflict = "old fit block\ngrid block"
    merged = ext.merge_reread(settled, reread)
    assert merged.held_blocks == {ext.HELD_GRID_SIZE: "grid block",
                                  ext.HELD_PAD_HOLES: "new hole block"}
    assert merged.pin_map_conflict.splitlines() == ["new hole block", "grid block"]


# -- R2-1 twin: runs sharing an edge are paired by their order -----------------

def _run(edge, count, first_pin, offset):
    """One castellated run; two runs may share an edge (`CastellatedSide.side_of`)."""
    return dict(edge=edge, count=count, pitch_mm=2.54, first_pin=first_pin, offset_mm=offset,
                reverse=False)


def test_a_side_choice_writes_each_run_of_an_edge_its_own_numbering():
    data = {"castellated_sides": [_run("top", 4, 1, 0.0), _run("top", 4, 5, 20.0),
                                  _run("bottom", 8, 9, 0.0)], "notes": ""}
    outcome = {"sides": [dict(_run("top", 4, 13, 0.0), reverse=True),
                         dict(_run("top", 4, 9, 20.0), reverse=True),
                         _run("bottom", 8, 1, 0.0)], "pinout": None, "note": "chosen"}
    assert ext._write_side_choice(data, outcome) is True
    assert [(s["first_pin"], s["reverse"]) for s in data["castellated_sides"]] == [
        (13, True), (9, True), (1, False)]


def test_a_side_choice_with_a_different_number_of_runs_is_not_written():
    """Negative: one run in the choice against two on the edge -- pairing would be
    a guess, so nothing is written and the caller keeps the question open."""
    sides = [_run("top", 4, 1, 0.0), _run("top", 4, 5, 20.0)]
    data = {"castellated_sides": copy.deepcopy(sides), "notes": ""}
    outcome = {"sides": [_run("top", 8, 9, 0.0)], "pinout": None, "note": "chosen"}
    assert ext._write_side_choice(data, outcome) is False
    assert data["castellated_sides"] == sides and data["notes"] == ""
    assert "superiore" in ext._unpaired_choice_reason(data, outcome)


# -- review G: «keep blocked» and the readings' disagreement are held causes ----

def _layout_and_hole_questions(tmp_path, monkeypatch):
    """A single THT part whose two pad readings disagree (a layout question) and
    whose pads print no hole (a pad-hole question), as the SRD relay run gave.
    The layout question gets the last-resort «keep blocked» option and the
    hole question a hole for every pad, as `_ask_pad_holes` offers one when a
    donor exists. Returns (params, layout question, hole question)."""
    import test_terminal_reread as srd
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    disagreeing = dict(srd._pads_reading(terminals=()), chains=[
        {"axis": "x", "kind": "consecutive", "stops": ["1,4,5", "2,3"], "values": [10.0],
         "values_text": ["10.0"]},
        {"axis": "y", "kind": "consecutive", "stops": ["1,2", "4", "5,3"], "values": [6.1, 6.1],
         "values_text": ["6.1", "6.1"]}])
    params, _called = srd._run(tmp_path, monkeypatch, pads=disagreeing, reread={
        "drawing_unit": "mm", "printed_terminals": [], "printed_holes": []})
    layout, hole = None, None
    for question in params.questions:
        if question["field"] == "pad_layout":
            if question["key"].startswith(f"pad_layout:{ext.PAD_HOLE_KIND}"):
                hole = question
            else:
                layout = question
    assert layout is not None and hole is not None
    kept = f"{ext.LAYOUT_BLOCK_KIND}:blocked"
    layout["options"].append({"label": "keep blocked", "value": kept})
    params.candidates[layout["key"]][kept] = {"kind": ext.LAYOUT_BLOCK_KIND}
    pads = hole["options"][0]["value"].partition(":")[2].partition("=")[0]
    donor = f"{ext.PAD_HOLE_KIND}:{pads}=1"
    hole["options"].insert(0, {"label": "hole 1 mm", "value": donor})
    params.candidates[hole["key"]][donor] = {"kind": ext.PAD_HOLE_KIND,
                                             "pads": pads.split(","), "drill_mm": 1.0,
                                             "drill_length_mm": None, "like": None}
    return params, layout, hole, kept, donor


def test_a_hole_answer_never_lifts_a_kept_layout_block(tmp_path, monkeypatch):
    """Review G scenario: «keep blocked» for the layout, then a hole for the pads.
    The holes are settled; the user's choice still blocks the footprint."""
    params, layout, hole, kept, donor = _layout_and_hole_questions(tmp_path, monkeypatch)
    after = ext.apply_answers(params, {layout["key"]: kept})
    assert ext.HELD_LAYOUT_KEPT in after.held_blocks
    after = ext.apply_answers(after, {hole["key"]: donor})
    assert after.pin_map_conflict and ext.HELD_LAYOUT_KEPT in after.held_blocks
    assert ext.HELD_PAD_HOLES not in after.held_blocks


def test_a_hole_answer_alone_settles_only_the_holes(tmp_path, monkeypatch):
    """Negative twin: without «keep blocked», the hole answer lifts the holes; the
    readings' disagreement, if held, stays for the layout question to settle."""
    params, layout, hole, _kept, donor = _layout_and_hole_questions(tmp_path, monkeypatch)
    before = set(params.held_blocks)
    after = ext.apply_answers(params, {hole["key"]: donor})
    assert ext.HELD_PAD_HOLES not in (after.held_blocks or {})
    assert set(after.held_blocks or ()) == before - set(ext.HOLE_ANSWER_CAUSES)
    assert any(q["key"] == layout["key"] for q in after.questions)


def _bare_result():
    """A result with nothing but its required fields."""
    return ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                               layout="matrix")


def test_a_layout_answer_lifts_the_kept_block_and_the_disagreement():
    """The kept block and the readings' disagreement are lifted by an applied layout
    answer (here the policy itself), never by a hole or numbering answer."""
    for causes, kept in ((ext.LAYOUT_ANSWER_CAUSES, set()),
                         (ext.HOLE_ANSWER_CAUSES, {ext.HELD_LAYOUT_KEPT, ext.HELD_PAD_LAYOUT}),
                         (ext.SIDE_NUMBERING_ANSWER_CAUSES,
                          {ext.HELD_LAYOUT_KEPT, ext.HELD_PAD_LAYOUT})):
        result = _bare_result()
        result.held_blocks = {ext.HELD_LAYOUT_KEPT: "kept", ext.HELD_PAD_LAYOUT: "disagree"}
        ext._lift_causes(result, causes)
        assert set(result.held_blocks or ()) == kept


def test_an_old_result_without_causes_is_lifted_by_any_applied_answer():
    """A result built without causes (older, or by the window): its text is lifted
    as before by a layout or numbering answer, kept by nothing else."""
    result = _bare_result()
    result.pin_map_conflict = "legacy block"
    ext._add_to_cause(result, ext.HELD_PAD_HOLES, "hole line")
    assert result.pin_map_conflict == "legacy block\nhole line"
    ext._lift_causes(result, ext.SIDE_NUMBERING_ANSWER_CAUSES)
    assert result.pin_map_conflict == "hole line"
