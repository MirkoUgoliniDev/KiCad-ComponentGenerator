"""@brief Explicit pads whose positions were not read drop only the pad geometry (user, 2026-11-09).

VE12H5-K run of 2026-11-09: the main single-part answer listed every pad with
`x_mm`/`y_mm` null (positions not read) beside a valid body, holes and
pinout; the validation rejected the whole paid answer. Decision of the user
(«Sì, correggi così»): the pad geometry is dropped with a note, the rest is
kept, and the positions stay to review (generation blocked) instead of an
extraction error. A coordinate that is present but malformed is still an
error. Every model answer here is a synthetic Mock: no network, no paid call.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import (
    ExtractionError, _params_from_tool_arguments, single_answer_to_data)

#: Five through-hole pads of a relay-like part, positions in mm from pin 1 (drawing frame).
POSITIONS = {"1": (0.0, 0.0), "2": (15.24, 0.0), "3": (15.24, 7.62), "4": (0.0, 7.62),
             "5": (2.54, 5.08)}
PINOUT = [{"pin": "1", "name": "NC", "function": "nc"},
          {"pin": "2", "name": "A1", "function": "coil"},
          {"pin": "3", "name": "A2", "function": "coil"},
          {"pin": "4", "name": "NO", "function": "no"},
          {"pin": "5", "name": "COM", "function": "common"}]


def _pads(unread=()):
    """The pads with their coordinates, null for the numbers in `unread`."""
    pads = []
    for number, (x, y) in POSITIONS.items():
        missing = number in unread
        pads.append({"number": number, "x_mm": None if missing else x,
                     "y_mm": None if missing else y, "pad_type": "thru_hole",
                     "drill_mm": 1.2, "pad_length_mm": 2.2, "pad_width_mm": 2.2})
    return pads


def _answer(pads):
    """A `report_single_component` answer shaped like the logged one, with synthetic values."""
    return dict(drawing_unit="mm", contacts_view="bottom", confidence="medium",
                body_length_mm=18.0, body_width_mm=9.5, body_height_mm=15.0,
                pin_length_3d_mm=3.0, lead_exit="bottom", pads_origin="custom", pads=pads,
                pinout=copy.deepcopy(PINOUT),
                value_sources={"body_length_mm": "read", "body_width_mm": "read",
                               "body_height_mm": "read", "drill_mm": "read",
                               "pad_diameter_mm": "estimated"})


# -- validation of one answer (no dedicated reading on this path) --------------------

def test_pads_without_any_position_keep_the_rest_of_the_answer_and_block():
    """Positive: every pad unread -> no geometry, body/pinout/provenance kept, generation blocked."""
    params = _params_from_tool_arguments(single_answer_to_data(_answer(_pads(POSITIONS))))
    assert params.package_geometry is None
    assert (params.body_length_mm, params.body_width_mm, params.body_height_mm) == (18.0, 9.5, 15.0)
    assert params.pin_length_3d_mm == 3.0
    assert [entry["pin"] for entry in params.pinout] == ["1", "2", "3", "4", "5"]
    assert params.value_sources["body_length_mm"] == "read"
    assert "Geometria dei pad scartata" in params.notes and "1, 2, 3, 4, 5" in params.notes
    # No grid is promoted to the footprint in place of the dropped pads.
    assert params.rows is None and params.cols is None
    # The generation is blocked: the window refuses a result with a conflict first.
    assert params.pin_map_conflict and "NON lette" in params.pin_map_conflict


def test_one_pad_without_a_position_drops_the_whole_pad_geometry():
    """A footprint of the four read pads would miss a terminal: never written."""
    params = _params_from_tool_arguments(single_answer_to_data(_answer(_pads(unread=("5",)))))
    assert params.package_geometry is None
    assert "pad 5 " in params.notes and "pad 1," not in params.notes
    assert params.pin_map_conflict and "pad 5 " in params.pin_map_conflict


def test_an_absent_coordinate_is_an_unread_one():
    pads = _pads()
    del pads[2]["y_mm"]
    params = _params_from_tool_arguments(single_answer_to_data(_answer(pads)))
    assert params.package_geometry is None and "pad 3 " in params.pin_map_conflict


def test_pads_with_every_position_keep_their_geometry():
    """Negative: the same answer with numeric coordinates is unchanged by the rule."""
    params = _params_from_tool_arguments(single_answer_to_data(_answer(_pads())))
    assert [pin["number"] for pin in params.package_geometry["pins"]] == list(POSITIONS)
    assert "Geometria dei pad scartata" not in params.notes
    assert params.pin_map_conflict is None


@pytest.mark.parametrize("bad", ["2.54", True, float("nan")])
def test_a_malformed_coordinate_is_still_an_error(bad):
    """Contract kept: a value that is present but not a finite number is a broken
    answer, not an unread one (test_invalid_explicit_pads_are_still_an_error)."""
    pads = _pads()
    pads[1]["x_mm"] = bad
    with pytest.raises(ExtractionError, match="Geometria package"):
        _params_from_tool_arguments(single_answer_to_data(_answer(pads)))


# -- the whole single-part analysis, with the dedicated pad reading ---------------------

@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)


def _drawing(tmp_path):
    from PIL import Image
    path = tmp_path / "quote.png"
    Image.new("RGB", (300, 200), "white").save(path)
    return path


#: Dedicated chains that do not close on the body (17.4 mm from edge to edge
#: against a body of 18.0 mm), as the logged reading did: not confirmed.
_CHAINS = {"drawing_unit": "mm", "contacts_view": "unknown", "chains": [
    {"axis": "x", "kind": "consecutive", "stops": ["edge", "1,4", "5", "2,3", "edge"],
     "values": [1.0, 2.54, 12.7, 1.16]},
    {"axis": "y", "kind": "consecutive", "stops": ["1,2", "5", "4,3"], "values": [5.08, 2.54]}]}
_BODY = {"drawing_unit": "mm", "body_length": 18.0, "body_width": 9.5, "body_height": 15.0}


def test_end_to_end_unread_pads_no_longer_reject_the_paid_answer(offline, monkeypatch, tmp_path):
    call = Mock(side_effect=[_answer(_pads(POSITIONS)), _CHAINS, _BODY])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["pinout", "top"]})
    assert call.call_count == 3
    assert call.call_args_list[1].kwargs["tool"]["function"]["name"] == "report_pad_positions"
    assert params.package_geometry is None
    assert params.body_length_mm == pytest.approx(18.0) and len(params.pinout) == 5
    assert "Geometria dei pad scartata" in params.notes
    # The dedicated reading's own block stands, with its reason.
    assert params.pin_map_conflict and "NON confermate" in params.pin_map_conflict
    # The answer as given keeps the pads, so «Rileggi posizioni» can still settle them.
    assert len(params.raw_main["package_geometry"]["pins"]) == 5


# -- audit of 2026-11-09 (M1-M3, L1-L3, H1 contract) -------------------------------------

@pytest.mark.parametrize("field, bad", [("x_mm", "2.54"), ("x_mm", True), ("y_mm", float("nan")),
                                        ("pad_length_mm", -1.0), ("pad_type", "glued"),
                                        ("number", None)])
def test_a_malformed_value_beside_a_null_coordinate_is_still_an_error(field, bad):
    """M1: pad 5 unread AND pad 2 malformed -> the malformed value is reported, never hidden
    by the drop. A malformed field of the unread pad itself is an error too (number=None)."""
    pads = _pads(unread=("5",))
    target = pads[4] if field == "number" else pads[1]
    target[field] = bad
    # A pad without its number is a broken answer SHAPE since review
    # 2026-11-10 (an empty number is refused by the pad contract): the error
    # names it and earns the one corrective retry; any other malformed value
    # stays an invalid geometry.
    expected = "Pad senza numero stampato" if field == "number" else "Geometria package"
    with pytest.raises(ExtractionError, match=expected):
        _params_from_tool_arguments(single_answer_to_data(_answer(pads)))


def test_a_malformed_size_is_reported_even_where_the_program_would_derive_the_land():
    """Review 2026-11-11: the derived land must not hide a malformed size the answer gave."""
    pads = _pads()
    pads[1]["pad_width_mm"] = 0.0
    with pytest.raises(ExtractionError, match="pad_width_mm"):
        _params_from_tool_arguments(single_answer_to_data(_answer(pads)))
    # Negative twin: the size simply absent is the rule's to derive, not an error.
    pads = _pads()
    del pads[1]["pad_width_mm"]
    _params_from_tool_arguments(single_answer_to_data(_answer(pads)))


def test_an_unread_pad_with_a_malformed_present_coordinate_is_an_error():
    """M1: x null but y given as text: the text is a broken value, not «not read»."""
    pads = _pads()
    pads[4]["x_mm"], pads[4]["y_mm"] = None, "5.08"
    with pytest.raises(ExtractionError, match="Geometria package"):
        _params_from_tool_arguments(single_answer_to_data(_answer(pads)))


def test_overlapping_read_pads_beside_an_unread_one_are_named_in_the_block():
    """M1: the read pads are validated together, overlaps included -- and the overlap is NAMED, not hidden.

    Contract changed on 2026-11-11 (review round 4): the overlap used to
    raise and lose the paid answer although the geometry was being dropped
    for pad 5 anyway. The values are well formed and only cannot all be
    true, so they block with their own reason beside the unread position;
    a malformed value beside it stays an error (the tests above).
    """
    pads = _pads(unread=("5",))
    pads[1]["x_mm"], pads[1]["y_mm"] = pads[0]["x_mm"] + 0.5, pads[0]["y_mm"]
    params = _params_from_tool_arguments(single_answer_to_data(_answer(pads)))
    assert params.package_geometry is None
    assert set(params.unread_pads) >= {"5", "1", "2"}
    # These pads' lands are derived by the program: half a millimetre apart
    # there is no room for the two holes, which is how the overlap is named.
    # Contract changed on 2026-11-11 (review round 5, V1-9): pads that share
    # one reason and one pair of values are named together in ONE entry
    # («pad 1, 2: ...»), so the block does not grow with the pad count.
    for text in (params.pin_map_conflict, params.notes):
        assert "pad 1, 2: a questo passo non c'e' posto" in text
    assert "pad 5 senza coordinate" in params.pin_map_conflict
    # Negative twin: without the overlap only pad 5 is unread and nothing names a lack of room.
    clean = _params_from_tool_arguments(single_answer_to_data(_answer(_pads(unread=("5",)))))
    assert clean.unread_pads == ["5"] and "non c'e' posto" not in clean.pin_map_conflict


def _note(params, start):
    """The first line of the result notes that starts with `start`."""
    return next(line for line in params.notes.splitlines() if line.startswith(start))


def test_the_drop_note_of_a_single_part_names_only_what_survives():
    """M2: drill, pad size and pitch live in the single-part pads and fall with them."""
    params = _params_from_tool_arguments(single_answer_to_data(_answer(_pads(POSITIONS))))
    note = _note(params, "Geometria dei pad scartata")
    assert "Si tengono: corpo, pinout;" in note
    assert params.drill_mm is None and params.pitch_mm is None
    for absent in ("foro", "passo", "diametro del pad"):
        assert absent not in note


def test_the_drop_note_of_a_main_answer_names_its_matrix_values():
    """M2, other path: a main-tool answer also sending a grid keeps drill, pad and pitch."""
    data = dict(layout="matrix", drawing_unit="mm", contacts_view="top", confidence="medium",
                rows=1, cols=5, pitch_mm=2.54, pad_diameter_mm=1.7, drill_mm=1.0,
                package_geometry={"family": "unknown", "origin": "custom", "source_view": "top",
                                  "pins": _pads(POSITIONS)})
    params = _params_from_tool_arguments(data)
    note = _note(params, "Geometria dei pad scartata")
    assert "Si tengono: foro, diametro del pad, passo;" in note and "corpo" not in note
    assert params.footprint_pads_dropped() and params.pin_map_conflict


def test_the_drop_note_with_nothing_left_says_so():
    """M2: nothing else survives -> the note says so instead of listing invented survivors."""
    answer = _answer(_pads(POSITIONS))
    for key in ("body_length_mm", "body_width_mm", "body_height_mm", "pinout"):
        answer.pop(key)
    params = _params_from_tool_arguments(single_answer_to_data(answer))
    assert "Si tengono: nient'altro;" in _note(params, "Geometria dei pad scartata")


# -- L2: a castellated module's sides are its footprint --------------------------------

def _castellated(pins):
    """A main-tool castellated answer with one side and optional explicit pads `pins`."""
    return dict(layout="castellated", drawing_unit="mm", contacts_view="top", confidence="medium",
                body_length_mm=20.0, body_width_mm=15.0,
                castellated_sides=[{"edge": "left", "count": 5, "pitch_mm": 2.54, "first_pin": 1,
                                    "contact_style": "half_hole"}],
                package_geometry={"family": "unknown", "origin": "custom", "source_view": "top",
                                  "pins": pins})


def _unread_smd_pin(**changes):
    """One SMD pad without a position."""
    return dict({"number": "1", "x_mm": None, "y_mm": None, "pad_length_mm": 1.0,
                 "pad_width_mm": 1.0, "pad_type": "smd"}, **changes)


def test_castellated_optional_pads_without_positions_do_not_block_the_sides():
    """Positive: the sides stand, the unread optional pads go with a note, nothing is blocked."""
    params = _params_from_tool_arguments(_castellated([_unread_smd_pin()]))
    assert params.castellated_sides and params.castellated_sides[0]["count"] == 5
    assert params.package_geometry is None and params.pin_map_conflict is None
    assert not params.footprint_pads_dropped() and params.unread_pad_geometry is None
    note = _note(params, "Pad espliciti facoltativi scartati")
    assert "lati del modulo" in note and "corpo" in note


def test_castellated_malformed_optional_pads_are_still_an_error():
    """Negative twin: a malformed present value is not «not read», castellated or not."""
    with pytest.raises(ExtractionError, match="Geometria package"):
        _params_from_tool_arguments(_castellated([_unread_smd_pin(pad_length_mm="big")]))


def test_matrix_answer_with_unread_pads_stays_blocked():
    """Negative twin of L2: explicit pads ARE the footprint of a matrix answer (schema)."""
    data = _castellated([_unread_smd_pin()])
    data["layout"] = "matrix"
    data.pop("castellated_sides")
    params = _params_from_tool_arguments(data)
    assert params.footprint_pads_dropped() and params.unread_pads == ["1"]
    assert params.pin_map_conflict and params.rows is None


# -- L1: a grid lacking its spacing ------------------------------------------------------

def _grid_answer(**grid):
    """A single-part answer describing its pads as a 2x2 grid, with `grid` overrides."""
    answer = _answer([])
    answer.pop("pads")
    answer["grid"] = dict(dict(rows=2, cols=2, pitch_mm=2.54, row_spacing_mm=7.62,
                               pad_diameter_mm=2.0, drill_mm=1.0), **grid)
    return answer


@pytest.mark.parametrize("missing, unread", [("pitch_mm", "3, 4"), ("row_spacing_mm", "2, 4")])
def test_a_grid_without_its_spacing_names_the_missing_value(missing, unread):
    """L1: the block and the note name the grid value, not «pads without coordinates»."""
    params = _params_from_tool_arguments(single_answer_to_data(_grid_answer(**{missing: None})))
    assert params.package_geometry is None and params.footprint_pads_dropped()
    assert missing in params.pin_map_conflict and "senza coordinate" not in params.pin_map_conflict
    assert f"pad {unread} " in params.pin_map_conflict
    assert f"griglia senza {missing}" in _note(params, "Geometria dei pad scartata")


def test_a_complete_grid_is_not_dropped():
    """Negative: the same grid with both spacings keeps its pads."""
    params = _params_from_tool_arguments(single_answer_to_data(_grid_answer()))
    assert len(params.package_geometry["pins"]) == 4 and not params.footprint_pads_dropped()


# -- H1 contract for the window ----------------------------------------------------------

def test_the_dropped_pads_are_flagged_for_the_window():
    """The fields the GUI reads to refuse every footprint write (H1)."""
    dropped = _params_from_tool_arguments(single_answer_to_data(_answer(_pads(unread=("5",)))))
    assert dropped.footprint_pads_dropped() and dropped.unread_pads == ["5"]
    assert dropped.package_geometry is None and dropped.pin_map_conflict
    kept = _params_from_tool_arguments(single_answer_to_data(_answer(_pads())))
    assert not kept.footprint_pads_dropped() and kept.unread_pads is None
    assert kept.unread_pad_geometry is None


# -- M3: a pad_layout question restores the dropped pads ---------------------------------

#: Two complete layouts of the five pads (mm, page frame), the second with pad 5 elsewhere.
_LAYOUT_A = dict(POSITIONS)
_LAYOUT_B = dict(POSITIONS, **{"5": (12.7, 5.08)})


def _asked(unread=("5",)):
    """A pad_layout question recorded the way apply_dedicated_pads records it."""
    data = single_answer_to_data(_answer(_pads(unread=unread)))
    pins = data["package_geometry"]["pins"]
    holes = ext._HoleVerdicts(None, None)
    ext._block_or_ask(data, pins, [("chains", _LAYOUT_A, {}, False),
                                   ("dedicated", _LAYOUT_B, {}, False)],
                      holes, 1.0, "Posizioni dei pad NON confermate (prova).", None)
    question, = data["_questions"]
    return _params_from_tool_arguments(data), question


def test_an_answered_pad_layout_restores_the_dropped_pads():
    """Positive (M3): the chosen layout fills the dropped pads, which become the footprint."""
    params, question = _asked()
    assert params.footprint_pads_dropped() and params.package_geometry is None
    assert params.unread_pad_geometry is not None and params.questions
    chosen = question["options"][1]["value"]
    restored = ext.apply_answers(params, {question["key"]: chosen})
    assert restored.package_geometry is not None and not restored.footprint_pads_dropped()
    assert restored.unread_pad_geometry is None and restored.pin_map_conflict is None
    assert restored.questions == []
    placed = {pin["number"]: (pin["x_mm"], pin["y_mm"]) for pin in restored.package_geometry["pins"]}
    assert placed["5"] == pytest.approx(_LAYOUT_B["5"])
    assert restored.package_geometry["needs_review"] is True
    # The input is not modified.
    assert params.package_geometry is None and params.footprint_pads_dropped()


def test_a_candidate_that_leaves_a_pad_unplaced_does_not_restore():
    """Negative: a payload missing pad 5 cannot make a footprint: the question stays open."""
    params, question = _asked()
    chosen = question["options"][0]["value"]
    payload = params.candidates[question["key"]][str(chosen)]
    payload["pins"] = [pin for pin in payload["pins"] if pin["number"] != "5"]
    still = ext.apply_answers(params, {question["key"]: chosen})
    assert still.package_geometry is None and still.footprint_pads_dropped()
    assert still.pin_map_conflict and len(still.questions) == 1


# -- L3: downstream messages name the cause ----------------------------------------------

def test_a_dedicated_reading_against_unread_main_pads_names_the_cause():
    """The main answer read no position: not «fewer than two pads in common»."""
    from test_pad_positions_by_drawn_holes import CHAINS
    data = single_answer_to_data(_answer(_pads(POSITIONS)))
    data["package_geometry"].update(body_length_mm=20.0, body_width_mm=12.0)
    reading = dict(ext.pads_from_chains(CHAINS, 1.0), view="bottom", notes="")
    ext.apply_dedicated_pads(data, reading)
    # CONTRACT CHANGED 2026-11-13 (plan P3, rule change approved by the
    # user: «unconfirmed asks instead of blocking»): a complete chain reading
    # beside a main answer that placed none of its pads is offered in ONE
    # question instead of a hard block (no witness here: nothing refutes it).
    # The cause is still named, now in the question and its note.
    assert "_pin_map_conflict" not in data
    questions = [q for q in data["_questions"] if q["field"] == "pad_layout"]
    assert len(questions) == 1
    for text in (questions[0]["text"], data["_numbering_note"]):
        assert "non ha letto le posizioni dei pad 1, 2, 3, 4, 5" in text
        assert "meno di due pad" not in text


def test_a_failed_dedicated_reading_also_names_the_unread_main_pads():
    """L3, no reading at all: the reason still names the main answer's unread pads."""
    data = single_answer_to_data(_answer(_pads(unread=("5",))))
    ext.apply_dedicated_pads(data, None)
    assert "non ha letto le posizioni dei pad 5" in data["_pin_map_conflict"]


# -- «Rileggi posizioni» on a stored answer with every position unread -----------------

def _stored_all_unread():
    """The stored main answer (raw_main) with every pad position null, body 20 x 12 mm."""
    stored = single_answer_to_data(_answer(_pads(POSITIONS)))
    stored["package_geometry"].update(body_length_mm=20.0, body_width_mm=12.0)
    stored.update(body_length_mm=20.0, body_width_mm=12.0)
    return stored


def _chains_reading():
    """A dedicated chain reading closing on the 20 x 12 mm body."""
    from test_pad_positions_by_drawn_holes import CHAINS
    return {"drawing_unit": "mm", "contacts_view": "bottom", "chains": copy.deepcopy(CHAINS)}


def test_reread_settles_all_unread_pads_through_the_drawn_holes(monkeypatch, tmp_path):
    """Positive: the drawn holes confirm the re-reading alone -> its positions become the footprint."""
    from test_pad_positions_by_drawn_holes import FakeHoles, TRUE
    monkeypatch.setattr(ext, "_layout_checker", lambda *args: FakeHoles(TRUE))
    monkeypatch.setattr(ext, "_layout_measurer", lambda images: None)
    params = ext.reread_pad_positions(_stored_all_unread(), [_drawing(tmp_path)],
                                      model="vendor/reader", call=Mock(return_value=_chains_reading()))
    assert params.package_geometry is not None and not params.footprint_pads_dropped()
    assert params.pin_map_conflict is None
    assert sorted(pin["number"] for pin in params.package_geometry["pins"]) == list(POSITIONS)
    assert "fori disegnati" in params.notes


def test_reread_without_a_witness_stays_blocked(monkeypatch, tmp_path):
    """Negative: the same re-reading, holes that confirm nothing -> still no footprint."""
    from test_pad_positions_by_drawn_holes import FakeHoles
    elsewhere = {"1": (0.0, 0.0), "2": (1.0, 0.0), "3": (2.0, 0.0), "4": (3.0, 0.0), "5": (4.0, 0.0)}
    monkeypatch.setattr(ext, "_layout_checker", lambda *args: FakeHoles(elsewhere))
    monkeypatch.setattr(ext, "_layout_measurer", lambda images: None)
    params = ext.reread_pad_positions(_stored_all_unread(), [_drawing(tmp_path)],
                                      model="vendor/reader", call=Mock(return_value=_chains_reading()))
    assert params.package_geometry is None and params.footprint_pads_dropped()
    assert "non ha letto le posizioni" in params.pin_map_conflict
