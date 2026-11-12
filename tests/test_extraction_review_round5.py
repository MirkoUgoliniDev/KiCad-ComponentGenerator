"""@brief Round 5 of the extraction review (2026-11-11): the findings of the independent verification of round 4.

The invariant of round 4 stays -- a valid footprint OR unread geometry with a
block naming pads, values and remedy -- and is completed:

- a malformed value the rules READ is a readable `ExtractionError`, but a
  paid answer is never lost to a field the rules do not read (V1-2);
- a printed slot keeps the axes it was transcribed on beside a land that is
  only estimated, and a value the program changed is never «read» (V1-3);
- the invariant survives `apply_answers`: lands derived while a pad had no
  position are kept apart once a chosen layout places it (V1-4);
- a zero hole or slot side is «not printed», never an error (V1-6);
- one default shape everywhere, never «read» when nobody gave it (V1-7);
- the block does not grow with the pad count (V1-9);
- extreme values never raise anything but a readable error and never leave a
  value a project file cannot store (V1-10);
- a height re-read that returns only a conflict does not take away the
  settled height nor the settled questions (V2-1).

Every rule has a positive and a negative case that differ for a structural
reason (a field read or not, a land read or estimated, a pad placed before
or after, a zero or a negative), never for a number tuned to a part.
Invented parts; no network, no AI call.
"""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from kicadgen.core import tht_estimates
from kicadgen.core.package_geometry import PinSpec, package_from_mapping
from kicadgen.generators.package import ESTIMATED_PADS_LISTED
from kicadgen.generators.pin_header import MIN_PAD_GAP_MM
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import ExtractionError
from tests.test_extraction_review_round4 import (_assert_invariant, _by_number, _main, _pin, _ring, _run,
                                                 _smallest_gap, _smd, _with_layout_question)

RING = tht_estimates.MIN_ANNULAR_RING_MM
ANNULUS = tht_estimates.HOLE_TO_PAD_ANNULUS_MM
NAN, INF = float("nan"), float("inf")


def _storable(params) -> None:
    """Fail when the result holds a value a project file cannot store (NaN, inf, a foreign object)."""
    json.dumps(dataclasses.asdict(params), allow_nan=False)


def _castellated(pins):
    """A castellated main answer with two valid sides and the optional explicit pads `pins`."""
    return dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=20.0, body_width_mm=10.0,
                castellated_sides=[dict(edge="left", count=3, pitch_mm=2.54, first_pin=1, offset_mm=0.5),
                                   dict(edge="top", count=4, pitch_mm=2.54, first_pin=4, offset_mm=1.0)],
                package_geometry={"name": "X", "body_length_mm": 20, "body_width_mm": 10, "pins": pins})


# ---------------------------------------------------------------------------
# V1-2: only the fields the pad rules read can fail an answer
# ---------------------------------------------------------------------------

_UNREAD = [("layers", ["F.Cu", "B.Cu"]), ("nets", ["GND"]), ("position", {"x": 0}), ("notes", ["x"]),
           ("hole_callout", ["3-Ø1.0"]), ("name", {"it": "GND"}), ("side", {"edge": "left"}), ("extra", {"a": [1]})]


@pytest.mark.parametrize("field, value", _UNREAD)
@pytest.mark.parametrize("kind", ["thru_hole", "smd", "castellated"])
def test_a_list_or_object_in_a_field_the_rules_do_not_read_never_loses_the_answer(kind, field, value):
    if kind == "smd":
        pads = [_smd("1", 0.0, **{field: value}), _smd("2", 1.27)]
    else:
        pads = [_pin("1", 0.0, **{field: value}), _pin("2", 2.54)]
    params = _run(_castellated(pads) if kind == "castellated" else _main(pads))
    assert params.package_geometry is not None and params.unread_pads is None
    package_from_mapping(params.package_geometry)
    if kind == "castellated":
        # The optional pads are kept and the valid sides are never lost.
        assert len(params.castellated_sides) == 2
    # The field is gone from the pad, and ONE note names it.
    assert field not in _by_number(params)["1"]
    notes = [line for line in params.notes.splitlines() if line.startswith("Campi dei pad fuori contratto")]
    assert len(notes) == 1 and field in notes[0]
    _storable(params)


@pytest.mark.parametrize("field", ["x_mm", "pad_length_mm", "drill_mm", "drill_length_mm", "number",
                                   "shape", "pad_type", "rotation_deg"])
@pytest.mark.parametrize("layout", ["matrix", "castellated"])
def test_the_same_list_in_a_field_the_rules_read_is_a_readable_error(layout, field):
    """Negative twin: the SAME value in a field of the pad contract is a malformed value the rules would read."""
    pads = [{**_pin("1", 0.0), field: ["F.Cu"]}, _pin("2", 2.54)]
    with pytest.raises(ExtractionError, match="Geometria package non valida") as error:
        _run(_castellated(pads) if layout == "castellated" else _main(pads))
    assert field in str(error.value)


def test_a_name_wrapped_in_a_list_becomes_its_text_and_a_plain_unknown_scalar_is_untouched():
    params = _run(_main([_pin("1", 0.0, name=["GND"], comment="plain text", weight=3),
                         _pin("2", 2.54, name="VCC")]))
    assert _assert_invariant(params) == "footprint"
    pins = _by_number(params)
    assert pins["1"]["name"] == "GND" and pins["2"]["name"] == "VCC"
    # A plain scalar harms nothing: kept as answered and not named in the note.
    assert pins["1"]["comment"] == "plain text" and pins["1"]["weight"] == 3
    note = next(line for line in params.notes.splitlines() if line.startswith("Campi dei pad fuori contratto"))
    assert "name" in note and "comment" not in note and "weight" not in note


def test_an_answer_whose_pads_carry_only_contract_fields_gets_no_note():
    """Negative twin of the note: nothing dropped, nothing said."""
    params = _run(_main([_pin("1", 0.0, name="GND", hole_callout="2-Ø1.0"), _pin("2", 2.54)]))
    assert _assert_invariant(params) == "footprint"
    assert "Campi dei pad fuori contratto" not in params.notes


# ---------------------------------------------------------------------------
# V1-3: a printed slot keeps its transcribed axes beside an ESTIMATED land
# ---------------------------------------------------------------------------

def _horizontal_slot(**land):
    """Pad 1 with a printed slot 3.0 along X by 1.0 along Y, and a round read pad 10 mm away."""
    slot = _pin("1", 0.0, drill_mm=3.0, drill_length_mm=1.0, hole_callout="3.0x1.0", **land)
    other = _pin("2", 10.0, pad_length_mm=1.8, pad_width_mm=1.8,
                 value_sources={"drill_mm": "read", "pad_size": "read"})
    return _main([slot, other])


def test_an_estimated_land_across_a_printed_slot_does_not_turn_it():
    params = _run(_horizontal_slot(pad_length_mm=2.0, pad_width_mm=4.0, shape="oval",
                                   value_sources={"drill_mm": "read", "pad_size": "estimated"}))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["1"]
    # The slot as transcribed, still read; the land derived around it, long side on X.
    assert (pin["drill_mm"], pin["drill_length_mm"]) == (3.0, 1.0)
    assert (pin["pad_length_mm"], pin["pad_width_mm"]) == (3.0 + ANNULUS, 1.0 + ANNULUS)
    assert pin["sources"] == {"drill_mm": "read", "pad_size": "derived", "shape": "derived"}
    assert "lato lungo sul lato lungo" not in params.notes


def test_a_read_land_that_holds_the_slot_only_turned_turns_it_and_the_hole_is_no_longer_read():
    """Negative twin: the SAME land, but READ -- two readings that agree once the axes are exchanged."""
    params = _run(_horizontal_slot(pad_length_mm=2.0, pad_width_mm=4.0, shape="oval",
                                   value_sources={"drill_mm": "read", "pad_size": "read"}))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["1"]
    assert (pin["drill_mm"], pin["drill_length_mm"], pin["pad_length_mm"], pin["pad_width_mm"]) == (
        1.0, 3.0, 2.0, 4.0)
    # The program exchanged the axes: the hole is not «read», and the note says to verify.
    assert pin["sources"]["drill_mm"] == "derived" and pin["sources"]["pad_size"] == "read"
    assert "Pad 1: asola stampata messa col lato lungo sul lato lungo" in params.notes


def test_a_read_land_that_holds_the_slot_as_transcribed_changes_nothing():
    params = _run(_horizontal_slot(pad_length_mm=4.0, pad_width_mm=3.5, shape="oval",
                                   value_sources={"drill_mm": "read", "pad_size": "read"}))
    pin = _by_number(params)["1"]
    assert (pin["drill_mm"], pin["drill_length_mm"]) == (3.0, 1.0) and pin["sources"]["drill_mm"] == "read"
    assert "lato lungo sul lato lungo" not in params.notes


# ---------------------------------------------------------------------------
# V1-4: the invariant survives apply_answers
# ---------------------------------------------------------------------------

def _row_with_an_unread_pad(pitch, lead=0.5, count=4):
    """`count` leaded pads in a row at `pitch`, the last one without a position; lands estimated, lead read."""
    pads = [_pin(str(i + 1), None if i == count - 1 else i * pitch, None if i == count - 1 else 0.0,
                 drill_mm="DEL", pad_length_mm=1.0, pad_width_mm=1.0,
                 lead={"shape": "round", "diameter_mm": lead},
                 value_sources={"lead": "read", "pad_size": "estimated"}) for i in range(count)]
    return _run(_main(pads))


def _placed(pitch, count=4):
    """The layout that puts pad i at i * pitch."""
    return {str(i + 1): i * pitch for i in range(count)}


def test_lands_derived_while_a_pad_had_no_position_are_kept_apart_when_it_is_placed():
    dropped = _row_with_an_unread_pad(1.5)
    assert _assert_invariant(dropped) == "unread" and dropped.unread_pads == ["4"]
    answered = _with_layout_question(dropped, _placed(1.5))
    assert _assert_invariant(answered) == "footprint" and answered.pin_map_conflict is None
    # Pad 4's land was derived when it had no neighbour: 1.55 mm, 0.075 mm from pad 3.
    assert _smallest_gap(answered) >= MIN_PAD_GAP_MM - 1e-5
    pins = _by_number(answered)
    assert pins["4"]["pad_length_mm"] < 1.55 and _ring(pins["4"]) >= RING - 1e-6
    assert pins["4"]["sources"]["pad_size"] == "estimated"
    assert "piazzola ricavata dal foro ridotta" in answered.notes.splitlines()[-2]
    # The record of the lands the program wrote never reaches a footprint.
    assert ext._DERIVED_LANDS_KEY not in answered.package_geometry
    _storable(answered)


def test_the_same_restore_at_a_pitch_with_room_changes_no_land():
    """Negative twin: at 2.54 mm the derived lands clear each other; nothing is reduced or said."""
    dropped = _row_with_an_unread_pad(2.54)
    before = {number: pin["pad_length_mm"] for number, pin in _by_number(dropped).items()}
    answered = _with_layout_question(dropped, _placed(2.54))
    assert _assert_invariant(answered) == "footprint"
    assert {number: pin["pad_length_mm"] for number, pin in _by_number(answered).items()} == before
    assert "piazzola ricavata dal foro ridotta" not in answered.notes[len(dropped.notes):]


def test_a_layout_with_no_room_for_the_hole_is_not_applied_and_the_block_says_why():
    """Hole 0.75 with its minimum ring and the gap needs 1.25 mm: at 0.9 mm no layout of these pads fits."""
    dropped = _row_with_an_unread_pad(0.9)
    answered = _with_layout_question(dropped, _placed(0.9))
    assert _assert_invariant(answered) == "unread" and answered.questions
    assert answered.unread_pad_geometry == dropped.unread_pad_geometry
    for text in (answered.pin_map_conflict, answered.notes):
        assert "Disposizione dei pad scelta non applicabile" in text and "non c'e' posto per il foro" in text
    # The block that was there stays: it names the unread position.
    assert dropped.pin_map_conflict in answered.pin_map_conflict
    # Negative twin: the same pads placed where there IS room are restored.
    wide = _with_layout_question(dropped, _placed(2.54))
    assert _assert_invariant(wide) == "footprint" and "non applicabile" not in wide.notes


def test_a_dropped_geometry_without_the_record_keeps_what_could_not_be_settled():
    """An older result (no record of the derived lands) carrying `unsettled_pads` is never restored."""
    pads = [_pin(n, x, drill_mm="DEL", pad_length_mm=1.2, pad_width_mm=1.2,
                 lead={"shape": "round", "diameter_mm": 1.0},
                 value_sources={"lead": "read", "pad_size": "read"}) for n, x in (("1", 0.0), ("2", 5.08))]
    dropped = _run(_main(pads))
    assert dropped.unread_pad_geometry["unsettled_pads"] == ["1", "2"]
    older = copy.deepcopy(dropped)
    older.unread_pad_geometry.pop(ext._DERIVED_LANDS_KEY, None)
    for params in (dropped, older):
        answered = _with_layout_question(params, {"1": 0.0, "2": 7.62})
        assert answered.package_geometry is None and answered.questions


def test_a_layout_that_puts_the_pads_of_a_valid_footprint_on_each_other_is_refused():
    params = _run(_main([_pin("1", 0.0), _pin("2", 2.54)]))
    assert _assert_invariant(params) == "footprint"
    answered = _with_layout_question(params, {"1": 0.0, "2": 0.5})
    assert answered.package_geometry == params.package_geometry and answered.questions
    assert "non applicabile" in answered.pin_map_conflict and "pad sovrapposti" in answered.pin_map_conflict
    # Negative twin: a layout that keeps them apart is written.
    moved = _with_layout_question(params, {"1": 0.0, "2": 5.08})
    assert _by_number(moved)["2"]["x_mm"] == 5.08 and not moved.questions and moved.pin_map_conflict is None
    package_from_mapping(moved.package_geometry)


# ---------------------------------------------------------------------------
# V1-6: a zero hole or slot side is «not printed»
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("zero", [0, 0.0])
def test_a_zero_slot_side_on_a_round_printed_hole_is_no_slot(zero):
    params = _run(_main([_pin("1", 0.0, drill_length_mm=zero, value_sources={"drill_mm": "read"}),
                         _pin("2", 2.54, value_sources={"drill_mm": "read"})]))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["1"]
    assert pin["drill_mm"] == 1.0 and pin.get("drill_length_mm") is None
    assert pin["sources"]["drill_mm"] == "read" and "asola" not in params.notes


def test_a_real_slot_side_that_was_not_printed_is_dropped_and_named():
    """Negative twin of the zero: a positive side is a slot the answer claims, and an unprinted slot is said."""
    params = _run(_main([_pin("1", 0.0, drill_length_mm=2.0, pad_width_mm=3.0,
                              value_sources={"drill_mm": "estimated"}), _pin("2", 5.08)]))
    assert "asola non stampata" in params.notes


@pytest.mark.parametrize("level", ["pad", "part"])
def test_a_zero_hole_with_a_declared_terminal_is_derived_from_it(level):
    """CONTRACT CHANGED (user, 2026-11-11, round 6 (b)): the hole still comes from the terminal,
    but a terminal only the main answer read is read ONCE, so the hole is «estimated», no longer
    «derived»; with a second agreeing reading it is «derived» (test_extraction_review_round6)."""
    lead ={"lead": {"shape": "round", "diameter_mm": 0.6}, "value_sources": {"lead": "read"}}
    pads = [_pin(n, x, drill_mm=0, **(copy.deepcopy(lead) if level == "pad" else {}))
            for n, x in (("1", 0.0), ("2", 2.54))]
    top = {} if level == "pad" else {"lead_diameter_mm": 0.6, "value_sources": {"lead_diameter_mm": "read"}}
    params = _run(_main(pads, **top))
    assert _assert_invariant(params) == "footprint"
    hole = tht_estimates.estimated_hole({"shape": "round", "diameter_mm": 0.6})
    for pin in params.package_geometry["pins"]:
        assert pin["drill_mm"] == hole and pin["sources"]["drill_mm"] == "estimated"
    assert "foro 0 nella risposta" in params.notes
    assert "terminale letto una sola volta" in params.notes


@pytest.mark.parametrize("pad_type", ["thru_hole", "np_thru_hole"])
def test_a_zero_hole_without_a_terminal_is_an_unread_hole_never_an_exception(pad_type):
    """Negative twin: nothing to derive the hole from -- unread geometry with the hole's own block."""
    params = _run(_main([_pin("1", 0.0, drill_mm=0, pad_type=pad_type), _pin("2", 2.54)]))
    assert _assert_invariant(params) == "unread" and params.unread_pads == ["1"]
    assert "Foro dei pad 1 non stampato e terminale non dichiarato" in params.pin_map_conflict


@pytest.mark.parametrize("field", ["drill_mm", "drill_length_mm"])
@pytest.mark.parametrize("value", [-1, NAN, "0", False])
def test_a_malformed_hole_on_a_holed_pad_is_still_an_error(field, value):
    """Negative twin of the zero: only the NUMBER zero means «not printed»."""
    with pytest.raises(ExtractionError, match=field):
        _run(_main([_pin("1", 0.0, **{field: value}), _pin("2", 2.54)]))


# ---------------------------------------------------------------------------
# V1-7: one default shape, never «read» when nobody gave it
# ---------------------------------------------------------------------------

def _between_read_pads(pitch, **middle):
    """A leaded pad without a printed hole between two read pads Ø1.8 at `pitch`."""
    read = dict(pad_length_mm=1.8, pad_width_mm=1.8, value_sources={"pad_size": "read", "drill_mm": "read"})
    pad = _pin("2", pitch, drill_mm="DEL", pad_length_mm=1.2, pad_width_mm=1.2, rotation_deg=45,
               lead={"shape": "round", "diameter_mm": 0.8}, **middle)
    return _main([_pin("1", 0.0, **copy.deepcopy(read)), pad, _pin("3", 2 * pitch, **copy.deepcopy(read))])


def test_a_land_written_for_a_pad_without_a_shape_is_measured_as_it_is_written():
    """A circle was assumed while the footprint drew a roundrect turned 45 degrees: 0.083 mm from its neighbours."""
    params = _run(_between_read_pads(2.1, shape="DEL", value_sources={"lead": "read", "shape": "read"}))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["2"]
    # The shape the program decided is written, and is not «read».
    assert pin["shape"] in ("circle", PinSpec.__dataclass_fields__["shape"].default)
    assert pin["sources"]["shape"] != "read"
    assert _smallest_gap(params) >= MIN_PAD_GAP_MM - 1e-5


def test_a_shape_the_answer_gives_and_calls_read_stays_and_is_measured_too():
    """Negative twin: the shape IS in the answer, so its word counts and the land keeps it."""
    params = _run(_between_read_pads(2.1, shape="rect", value_sources={"lead": "read", "shape": "read"}))
    assert _assert_invariant(params) == "footprint"
    pin = _by_number(params)["2"]
    assert pin["shape"] == "rect" and pin["sources"]["shape"] == "read"
    assert _smallest_gap(params) >= MIN_PAD_GAP_MM - 1e-5


def test_no_pad_reports_as_read_a_shape_the_answer_does_not_give():
    shapeless = _smd("1", 0.0, value_sources={"shape": "read", "pad_size": "read"})
    del shapeless["shape"]
    pads = [shapeless,
            _smd("2", 1.27, value_sources={"shape": "read", "pad_size": "read"}),
            _pin("3", 5.0, shape="DEL", value_sources={"shape": "read", "pad_size": "read", "drill_mm": "read"}),
            _pin("4", 8.0, value_sources={"shape": "read", "pad_size": "read", "drill_mm": "read"})]
    pins = _by_number(_run(_main(pads)))
    assert "shape" not in pins["1"]["sources"] and pins["1"]["sources"]["pad_size"] == "read"
    assert pins["3"]["sources"]["shape"] == "estimated" and pins["3"]["shape"]
    # Negative twins: the pads that DO give their shape keep the declared word.
    assert pins["2"]["sources"]["shape"] == "read" and pins["4"]["sources"]["shape"] == "read"


# ---------------------------------------------------------------------------
# V1-9: the block does not grow with the pad count
# ---------------------------------------------------------------------------

def _crowded(count, leads):
    """`count` leaded pads 1.0 mm apart; pad i takes lead `leads[i % len(leads)]`."""
    pads = [_pin(str(i + 1), float(i), drill_mm="DEL", pad_length_mm=3.0, pad_width_mm=3.0,
                 lead={"shape": "round", "diameter_mm": leads[i % len(leads)]},
                 value_sources={"lead": "read"}) for i in range(count)]
    return _run(_main(pads))


def test_pads_that_share_one_reason_and_one_pair_of_values_are_one_entry_of_the_block():
    few, many = _crowded(4, [0.5]), _crowded(200, [0.5])
    assert _assert_invariant(few) == "unread" and _assert_invariant(many) == "unread"
    assert "pad 1-4: a questo passo non c'e' posto per il foro 0.75 mm" in few.pin_map_conflict
    assert "pad 1-200: a questo passo non c'e' posto per il foro 0.75 mm" in many.pin_map_conflict
    # Fifty times the pads, the same text but for the range.
    assert len(many.pin_map_conflict) - len(few.pin_map_conflict) == len("1-200") - len("1-4")
    assert len(many.unread_pads) == 200


def test_pads_with_different_values_keep_their_own_entries_up_to_a_bound():
    """Negative twin: different holes are different facts -- named apart, and counted beyond the bound."""
    two = _crowded(4, [0.5, 0.6])
    assert "pad 1, 3: a questo passo" in two.pin_map_conflict and "pad 2, 4: a questo passo" in two.pin_map_conflict
    leads = [round(0.30 + 0.05 * i, 2) for i in range(ESTIMATED_PADS_LISTED + 3)]
    many = _crowded(len(leads), leads)
    assert many.pin_map_conflict.count("a questo passo non c'e' posto") == ESTIMATED_PADS_LISTED
    assert "e altri 3 pad" in many.pin_map_conflict


def test_a_long_list_of_scattered_pads_is_bounded_and_counted():
    numbers = [str(n) for n in range(1, 60, 2)]
    text = ext._bounded_pad_list(numbers)
    assert text.startswith("1, 3, 5") and text.endswith(f"e altri {len(numbers) - ESTIMATED_PADS_LISTED}")
    # Negative twin: a run is one entry and needs no tail.
    assert ext._bounded_pad_list([str(n) for n in range(1, 60)]) == "1-59"


# ---------------------------------------------------------------------------
# V1-10: extreme values
# ---------------------------------------------------------------------------

def _undrilled():
    """Two through-hole pads without a printed hole."""
    return [_pin("1", 0.0, drill_mm="DEL"), _pin("2", 2.54, drill_mm="DEL")]


def test_a_part_lead_beyond_what_kicad_can_represent_is_dropped_and_said():
    params = _run(_main(_undrilled(), lead_diameter_mm=1e308))
    assert params.lead_diameter_mm is None and "lead_diameter_mm scartato" in params.notes
    # No terminal is left: the holes are unread, with their block.
    assert _assert_invariant(params) == "unread"
    _storable(params)
    # Negative twin: a real lead is kept and gives the holes.
    real = _run(_main(_undrilled(), lead_diameter_mm=0.6, value_sources={"lead_diameter_mm": "read"}))
    assert real.lead_diameter_mm == 0.6 and _assert_invariant(real) == "footprint"


@pytest.mark.parametrize("lead", [{"shape": "round", "diameter_mm": 1e308},
                                  {"shape": "flat", "width_mm": 1e308, "thickness_mm": 1e308},
                                  {"shape": "flat", "width_mm": 1e200, "thickness_mm": 0.5}])
def test_a_pad_lead_beyond_what_kicad_can_represent_is_an_invalid_terminal(lead):
    params = _run(_main([_pin("1", 0.0, drill_mm="DEL", lead=lead), _pin("2", 2.54)]))
    assert "Pad 1: terminale dichiarato non valido" in params.notes
    assert _assert_invariant(params) == "unread" and params.unread_pads == ["1"]
    assert "lead" not in _by_number(params)["1"]
    _storable(params)


@pytest.mark.parametrize("unit", [["mm"], {"unit": "mm"}, ("inch",), 5, True])
def test_a_drawing_unit_that_is_not_text_is_an_unknown_unit_never_an_exception(unit):
    params = _run(_main([_pin("1", 0.0), _pin("2", 2.54)], drawing_unit=unit))
    assert _assert_invariant(params) == "footprint"
    assert "DUBBIO unita' di misura" in params.notes and "non riconosciuta" in params.notes
    assert _by_number(params)["2"]["x_mm"] == 2.54


def test_a_declared_unit_still_converts():
    """Negative twin: a known unit, as text, is applied."""
    params = _run(_main([_pin("1", 0.0, pad_length_mm=0.07, pad_width_mm=0.07, drill_mm=0.04),
                         _pin("2", 0.1, pad_length_mm=0.07, pad_width_mm=0.07, drill_mm=0.04)],
                        drawing_unit="inch"))
    assert _by_number(params)["2"]["x_mm"] == pytest.approx(2.54) and "non riconosciuta" not in params.notes


@pytest.mark.parametrize("value", [NAN, INF, -INF])
def test_a_non_finite_value_ends_as_its_field_requires(value):
    def two(**extra):
        """Two pads, the first one carrying `extra` over its own fields."""
        return _main([{**_pin("1", 0.0), **extra}, _pin("2", 2.54)])

    # Informative or unread fields: dropped, the footprint stands and can be stored.
    for field in ("name", "roundrect_ratio", "side", "hole_callout", "anything"):
        params = _run(two(**{field: value}))
        assert _assert_invariant(params) == "footprint", field
        assert field not in _by_number(params)["1"] or _by_number(params)["1"][field] is None
        _storable(params)
    # Fields the rules read: a readable error naming the field.
    for field in ("number", "rotation_deg", "x_mm", "pad_width_mm", "drill_mm"):
        with pytest.raises(ExtractionError, match=field):
            _run(two(**{field: value}))


def test_finite_values_in_the_same_fields_are_kept():
    """Negative twin: the same fields with real values pass untouched."""
    params = _run(_main([_pin(7, 0.0, name="GND", rotation_deg=90.0, roundrect_ratio=0.25), _pin("2", 2.54)]))
    assert _assert_invariant(params) == "footprint"
    pin = params.package_geometry["pins"][0]
    assert (str(pin["number"]), pin["name"], pin["rotation_deg"], pin["roundrect_ratio"]) == ("7", "GND", 90.0, 0.25)


# ---------------------------------------------------------------------------
# V2-1: a height re-read that returns only a conflict takes nothing away
# ---------------------------------------------------------------------------

_HEIGHT_QUESTION = {"key": "body_height_mm:7.5|9", "field": "body_height_mm",
                    "options": [{"value": 7.5}, {"value": 9.0}]}
_PAD_QUESTION = {"key": "pad_layout:x", "field": "pad_layout", "options": [{"value": "main"}]}
_CONFLICT = "Quote verticali in disaccordo fra le due letture: 7.5 e 9."


def _settled(height=3.0, source=ext.USER_SOURCE):
    """A settled result: a height with its provenance, one height question and one pad question open."""
    params = _run(_main([_pin("1", 0.0), _pin("2", 2.54)], pin_length_3d_mm=3.3))
    sources = dict(params.value_sources or {})
    sources["body_height_mm"] = source
    return dataclasses.replace(params, body_height_mm=height, value_sources=sources, body_conflict=None,
                               questions=[dict(_PAD_QUESTION), dict(_HEIGHT_QUESTION)])


def _reread(settled, **changes):
    """A partial height re-read of `settled` with `changes`; by default it read nothing at all."""
    fields = dict(body_height_mm=None, pin_length_3d_mm=None, body_conflict=None, questions=[], notes="",
                  value_sources={}, partial_reread={"field": "body_height_mm", "body_axes_measured": False,
                                                    "notes": []})
    fields.update(changes)
    return dataclasses.replace(settled, **fields)


def test_a_reread_with_only_a_conflict_keeps_the_settled_height_and_questions_and_records_the_conflict():
    settled = _settled()
    reread = _reread(settled, body_conflict=_CONFLICT,
                     partial_reread={"field": "body_height_mm", "body_axes_measured": False,
                                     "notes": [_CONFLICT]})
    merged = ext.merge_reread(settled, reread)
    assert merged.body_height_mm == 3.0 and merged.value_sources["body_height_mm"] == ext.USER_SOURCE
    assert merged.package_geometry == settled.package_geometry
    # The free choice of a height survives: the settled question is still there, with its options.
    assert merged.questions == settled.questions
    assert merged.body_conflict == _CONFLICT and merged.notes.count(_CONFLICT) == 1
    assert merged.pin_length_3d_mm == 3.3 and merged.partial_reread is None
    # The user can still answer it, without a paid call.
    answered = ext.apply_answers(merged, {"body_height_mm:7.5|9": 7.5})
    assert answered.body_height_mm == 7.5 and answered.body_conflict is None


def test_a_reread_that_reads_a_height_replaces_the_scope_as_before():
    """Negative twin: a positive height IS a decision of the scope -- value, provenance, questions, conflict."""
    settled = _settled()
    reread = _reread(settled, body_height_mm=7.5, value_sources={"body_height_mm": "read"},
                     body_conflict=_CONFLICT)
    merged = ext.merge_reread(settled, reread)
    assert merged.body_height_mm == 7.5 and merged.value_sources["body_height_mm"] == "read"
    assert merged.package_geometry["body_height_mm"] == 7.5
    assert [question["field"] for question in merged.questions] == ["pad_layout"]
    assert merged.body_conflict == _CONFLICT


def test_a_reread_that_asks_a_question_of_the_scope_replaces_the_scope_as_before():
    """Negative twin: a question of the scope's fields is a decision too -- the old question goes with the height."""
    settled = _settled()
    asked = {"key": "body_height_mm:8|9", "field": "body_height_mm", "options": [{"value": 8.0}, {"value": 9.0}]}
    merged = ext.merge_reread(settled, _reread(settled, questions=[asked], body_conflict=_CONFLICT))
    assert merged.body_height_mm is None and "body_height_mm" not in merged.value_sources
    assert [question["key"] for question in merged.questions] == ["pad_layout:x", "body_height_mm:8|9"]


def test_a_reread_that_returns_nothing_changes_nothing():
    settled = _settled()
    merged = ext.merge_reread(settled, _reread(settled))
    assert dataclasses.asdict(merged) == dataclasses.asdict(settled)


def test_a_question_outside_the_scope_does_not_count_as_a_decision_of_the_height():
    """A pad question in the re-read's result is not the scope's: the conflict alone still takes nothing away."""
    settled = _settled()
    reread = _reread(settled, body_conflict=_CONFLICT, questions=[dict(_PAD_QUESTION)])
    merged = ext.merge_reread(settled, reread)
    assert merged.body_height_mm == 3.0 and merged.questions == settled.questions
    assert merged.body_conflict == _CONFLICT
