"""@brief Pad positions added up by the program from the drawing's dimension chains (user, 2026-10-30).

Two readings of a relay drawing placed the coil pins one quote short, the
same way, because the model added the chain up itself; the agreement check
let them through when they agreed and could not choose when they did not.
The model now transcribes the chains and the program adds them up and
checks them against the drawing: chains that contradict each other or do
not close on the body block, whatever the two readings say. A chain reading
is never preferred to a main answer that differs: a closure checks a sum,
which swapped labels or transposed quotes keep (adversarial review).

The drawing used here is invented (a 15 x 9 mm body, four holes), not the
relay: the rules depend on the structure of the chains, never on its quotes.
"""
import math

import pytest

from kicadgen.services.datasheet_extractor import apply_dedicated_pads, pads_from_chains

#: x: outline, 1, {2, 3}, 4, outline; y: outline, {1, 2}, {3, 4}, outline.
CHAINS = [
    {"axis": "x", "stops": ["edge", "1", "2,3", "4", "edge"], "values": [2.0, 3.5, 7.5, 2.0]},
    {"axis": "y", "stops": ["edge", "1,2", "3,4", "edge"], "values": [2.5, 4.0, 2.5]},
]
#: The same holes, from the outline's top-left corner.
TRUE = {"1": (2.0, 2.5), "2": (5.5, 2.5), "3": (5.5, 6.5), "4": (13.0, 6.5)}


def _main(pads, length=15.0, width=9.0, view="bottom"):
    return {"drawing_unit": "mm", "contacts_view": view, "package_geometry": {
        "origin": "custom", "body_length_mm": length, "body_width_mm": width,
        "pins": [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in pads.items()]}}


def _reading(chains, view="bottom", factor=1.0):
    return dict(pads_from_chains(chains, factor), view=view, notes="")


def _distances(pads):
    names = sorted(pads)
    return {(a, b): math.dist(pads[a], pads[b]) for i, a in enumerate(names) for b in names[i + 1:]}


# ── The sums ─────────────────────────────────────────────────────────────

def test_chains_are_added_up_with_shared_stops_and_outline_spans():
    result = pads_from_chains(CHAINS)
    assert result["problems"] == [] and result["unplaced"] == []
    assert _distances(result["pads"]) == pytest.approx(_distances(TRUE))
    assert result["spans"] == {"x": pytest.approx(15.0), "y": pytest.approx(9.0),
                               # Where the outline stops are (2026-10-31: the body is
                               # placed axis by axis, _write_chain_pads).
                               "edges": {"x": pytest.approx((0.0, 15.0)),
                                         "y": pytest.approx((0.0, 9.0))}}


def test_the_declared_unit_is_converted():
    inches = [dict(chain, values=[v / 25.4 for v in chain["values"]]) for chain in CHAINS]
    result = pads_from_chains(inches, 25.4)
    assert _distances(result["pads"]) == pytest.approx(_distances(TRUE))


def test_two_chains_reaching_a_pad_differently_are_a_problem():
    """Positive: a second x chain puts 4 a quote away from the first one."""
    extra = {"axis": "x", "stops": ["1", "4"], "values": [9.0]}
    result = pads_from_chains(CHAINS + [extra])
    assert len(result["problems"]) == 1 and "4" in result["problems"][0]
    # Negative: a redundant chain that agrees is no problem.
    agreeing = {"axis": "x", "stops": ["1", "4"], "values": [11.0]}
    assert pads_from_chains(CHAINS + [agreeing])["problems"] == []


def test_a_pad_tied_on_one_axis_only_is_unplaced_never_guessed():
    chains = CHAINS + [{"axis": "x", "stops": ["4", "5"], "values": [1.0]}]
    result = pads_from_chains(chains)
    assert "5" not in result["pads"] and result["unplaced"] == ["5"]


def test_an_outline_stop_inside_a_chain_or_a_short_value_list_is_refused():
    middle = [{"axis": "x", "stops": ["1", "edge", "2"], "values": [1.0, 1.0]}]
    short = [{"axis": "x", "stops": ["1", "2", "3"], "values": [1.0]}]
    assert len(pads_from_chains(middle)["malformed"]) == 1
    assert len(pads_from_chains(short)["malformed"]) == 1


def test_chains_too_broken_to_place_a_pad_say_why():
    """A quote missing from the only x chain leaves no pad: blocked, and the block names the chain.

    Contract changed on 2026-10-30 (user): an unconfirmed reading blocks
    instead of leaving a note; the reason is still named.
    """
    broken = [dict(CHAINS[0], values=[2.0, 3.5, 2.0]), CHAINS[1]]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(broken))
    assert "catena x n. 1" in data["_pin_map_conflict"]


# ── The comparison with the main answer ──────────────────────────────────

def test_two_readings_wrong_the_same_way_are_blocked_by_the_closure():
    """The old blind spot: one quote misread (4.0 for 7.5) in BOTH readings."""
    wrong = [dict(CHAINS[0], values=[2.0, 3.5, 4.0, 2.0]), CHAINS[1]]
    reading = _reading(wrong)
    data = _main(reading["pads"])
    apply_dedicated_pads(data, reading)
    assert "sommano 11.50 mm" in data["_pin_map_conflict"]
    assert "15.00 mm" in data["_pin_map_conflict"]


@pytest.mark.parametrize("x_chain", [
    # Pad labels 1 and 4 swapped: a wrong pinout, same sums.
    {"axis": "x", "stops": ["edge", "4", "2,3", "1", "edge"], "values": [2.0, 3.5, 7.5, 2.0]},
    # Two quotes transposed: same sums.
    {"axis": "x", "stops": ["edge", "1", "2,3", "4", "edge"], "values": [2.0, 7.5, 3.5, 2.0]},
], ids=["swapped-labels", "transposed-quotes"])
def test_a_closed_chain_reading_never_overrides_a_main_answer_that_differs(x_chain):
    """Negative (adversarial review): a closure checks a SUM, so it cannot pick a reading."""
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading([x_chain, CHAINS[1]]))
    assert "non coincidono" in data["_pin_map_conflict"]
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    assert pins == TRUE and data["package_geometry"]["origin"] == "custom"


def test_without_a_closure_on_both_axes_a_difference_still_blocks():
    """No outline stop on y: a difference blocks as before."""
    open_y = [CHAINS[0], {"axis": "y", "stops": ["1,2", "3,4"], "values": [4.0]}]
    data = _main(dict(TRUE, **{"4": (9.5, 6.5)}))
    apply_dedicated_pads(data, _reading(open_y))
    assert "non coincidono" in data["_pin_map_conflict"]


def test_body_sizes_read_in_another_frame_are_reported_not_blocked():
    """Length and width exchanged (read from other views): both spans match, swapped.

    Not a contradiction of the pads, but a footprint risk (the outline would
    be drawn across the pads): said in the note (adversarial review).
    """
    data = _main(TRUE, length=9.0, width=15.0)
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "_pin_map_conflict" not in data and "confermate" in data["_numbering_note"]
    assert "scambiate" in data["_numbering_note"]
    # Negative: a span matching neither size still blocks.
    data = _main(TRUE, length=9.0, width=16.0)
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "sommano" in data["_pin_map_conflict"]


def test_the_tolerance_grows_with_the_quotes_summed():
    """Ten quotes rounded to one decimal may drift by half a unit each: 0.4 mm is rounding."""
    x = {"axis": "x", "stops": ["edge"] + [str(n) for n in range(1, 10)] + ["edge"],
         "values": [1.54] * 10}
    y = {"axis": "y", "stops": ["edge", ",".join(str(n) for n in range(1, 10)), "edge"],
         "values": [2.0, 2.0]}
    pads = pads_from_chains([x, y])["pads"]
    data = _main(pads, length=15.0, width=4.0)
    apply_dedicated_pads(data, _reading([x, y]))
    assert "_pin_map_conflict" not in data
    # Negative: a whole quote lost is far beyond any rounding.
    data = _main(pads, length=15.0 + 1.54, width=4.0)
    apply_dedicated_pads(data, _reading([x, y]))
    assert "sommano" in data["_pin_map_conflict"]


def test_pads_sharing_a_number_skip_the_chain_checks():
    """Two shield tabs «S» merge into one stop: no false contradiction, said in the note."""
    data = _main(TRUE)
    data["package_geometry"]["pins"] += [{"number": "S", "x_mm": 0.5, "y_mm": 4.5},
                                         {"number": "S", "x_mm": 14.5, "y_mm": 4.5}]
    chains = CHAINS + [{"axis": "x", "stops": ["edge", "S", "1"], "values": [0.5, 1.5]},
                       {"axis": "x", "stops": ["4", "S", "edge"], "values": [1.5, 0.5]}]
    apply_dedicated_pads(data, _reading(chains))
    # No false contradiction from the merged stop; the tabs have no y chain,
    # so the reading confirms 4 pads of 5 and, since 2026-10-30, blocks.
    assert "stesso numero (S)" in data["_pin_map_conflict"]
    assert "non tornano" not in data["_pin_map_conflict"]
    assert "solo 4 pad su 5" in data["_pin_map_conflict"]


def test_a_malformed_redundant_chain_is_a_remark_not_a_block():
    chains = CHAINS + [{"axis": "x", "stops": ["1", "4"], "values": []}]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(chains))
    assert "_pin_map_conflict" not in data
    assert "confermate" in data["_numbering_note"] and "catena x n. 3" in data["_numbering_note"]


def test_a_closed_reading_with_another_view_still_blocks():
    data = _main(dict(TRUE, **{"4": (9.5, 6.5)}), view="top")
    apply_dedicated_pads(data, _reading(CHAINS, view="bottom"))
    assert "viste diverse" in data["_pin_map_conflict"]


def test_agreeing_readings_closed_on_the_body_are_confirmed():
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "_pin_map_conflict" not in data and "confermate" in data["_numbering_note"]


def test_a_body_given_in_inches_is_converted_before_the_closure():
    data = _main(TRUE, length=15.0 / 25.4, width=9.0 / 25.4)
    data["drawing_unit"] = "inch"
    for pin in data["package_geometry"]["pins"]:
        pin["x_mm"] /= 25.4
        pin["y_mm"] /= 25.4
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "_pin_map_conflict" not in data


# ── The main answer transcribes the chains too ───────────────────────────

def _single(pads, chains, **extra):
    """A report_single_component answer with the model's own coordinates and its chains."""
    answer = {"drawing_unit": "mm", "contacts_view": "bottom", "confidence": "medium",
              "body_length_mm": 15.0, "body_width_mm": 9.0, "pad_chains": chains,
              "pads": [{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                        "pad_length_mm": 1.8, "pad_width_mm": 1.8} for n, (x, y) in pads.items()]}
    answer.update(extra)
    return answer


def _pins(data):
    return {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}


#: The model's own sums with one quote of the x chain dropped (pad 4 short by 3.5).
SUMMED_WRONG = dict(TRUE, **{"4": (9.5, 6.5)})


#: The x chain of CHAINS with pad labels 1 and 4 swapped (same sums, wrong pinout).
SWAPPED_X = {"axis": "x", "stops": ["edge", "4", "2,3", "1", "edge"], "values": [2.0, 3.5, 7.5, 2.0]}
#: The x chain of CHAINS written right to left (same distances, mirrored layout).
MIRRORED_X = {"axis": "x", "stops": ["edge", "4", "2,3", "1", "edge"], "values": [2.0, 7.5, 3.5, 2.0]}


def test_a_wrong_model_sum_is_a_conflict_until_the_dedicated_reading_sides_with_the_chains():
    """Positive: the model summed wrong; its chains and the dedicated chains describe one layout."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    data = single_answer_to_data(_single(SUMMED_WRONG, CHAINS))
    assert "non coincidono con le sue catene" in data["_pin_map_conflict"]
    assert _pins(data) == SUMMED_WRONG, "nothing chosen before an independent reading"
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "_pin_map_conflict" not in data and "prese dalle catene" in data["_numbering_note"]
    assert data["package_geometry"]["origin"] == "body_center"
    assert _pins(data)["4"] == pytest.approx((5.5, 2.0))


def test_model_coordinates_agreeing_with_their_chains_take_the_program_sums():
    """Same layout both ways: the program's sums and the body centre, no conflict."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    data = single_answer_to_data(_single(TRUE, CHAINS))
    assert "_pin_map_conflict" not in data and data["package_geometry"]["origin"] == "body_center"
    assert _pins(data)["1"] == pytest.approx((-5.5, -2.0))


@pytest.mark.parametrize("x_chain", [SWAPPED_X, MIRRORED_X], ids=["swapped-labels", "mirrored"])
def test_a_slip_in_the_main_chains_is_never_adopted_over_agreeing_coordinates(x_chain):
    """Negative (adversarial review): same sums, other signs; the dedicated chains side with
    the model's coordinates, which stay; the main chains are discarded."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    data = single_answer_to_data(_single(TRUE, [x_chain, CHAINS[1]]))
    assert "_pin_map_conflict" in data and _pins(data) == TRUE
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "_pin_map_conflict" not in data and "scartate" in data["_numbering_note"]
    assert _pins(data) == TRUE


def test_without_a_dedicated_reading_the_main_answers_own_conflict_stands():
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    data = single_answer_to_data(_single(SUMMED_WRONG, CHAINS))
    apply_dedicated_pads(data, None)
    assert "non coincidono con le sue catene" in data["_pin_map_conflict"]
    assert _pins(data) == SUMMED_WRONG


def test_a_dedicated_reading_siding_with_neither_keeps_the_block():
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    data = single_answer_to_data(_single(SUMMED_WRONG, CHAINS))
    apply_dedicated_pads(data, _reading([MIRRORED_X, CHAINS[1]]))
    assert "ne' con le coordinate" in data["_pin_map_conflict"] and _pins(data) == SUMMED_WRONG


def test_a_pin_without_coordinates_keeps_the_model_answer_and_never_crashes():
    """Adversarial review: a null coordinate used to store a candidate and raise later."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    answer = _single(TRUE, CHAINS)
    answer["pads"][3]["x_mm"] = None
    data = single_answer_to_data(answer)
    assert "_pin_map_conflict" not in data and "senza coordinate: 4" in data["notes"]
    apply_dedicated_pads(data, _reading([MIRRORED_X, CHAINS[1]]))
    assert "_pin_map_conflict" in data, "the mirrored dedicated reading still disagrees"


def test_an_unclosed_dedicated_reading_cannot_outvote_closed_chains():
    """Adversarial review: the model and the dedicated call misread the same quote; the
    model's chains, closed on the body, are the only reading checked against the drawing."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    data = single_answer_to_data(_single(SUMMED_WRONG, CHAINS))
    open_wrong = [{"axis": "x", "stops": ["1", "2,3", "4"], "values": [3.5, 4.0]},
                  {"axis": "y", "stops": ["1,2", "3,4"], "values": [4.0]}]
    apply_dedicated_pads(data, _reading(open_wrong))
    assert "non e' chiusa" in data["_pin_map_conflict"] and _pins(data) == SUMMED_WRONG
    # Positive twin: the same agreeing reading, closed on the body, decides.
    data = single_answer_to_data(_single(SUMMED_WRONG, CHAINS))
    closed_wrong = [{"axis": "x", "stops": ["edge", "1", "2,3", "4", "edge"],
                     "values": [2.0, 3.5, 4.0, 5.5]}, CHAINS[1]]
    apply_dedicated_pads(data, _reading(closed_wrong))
    assert "_pin_map_conflict" not in data and "scartate" in data["_numbering_note"]


@pytest.mark.parametrize("dropped", [("body_length_mm",), ("body_length_mm", "body_width_mm")],
                         ids=["one-size", "no-size"])
def test_a_closed_span_without_a_body_size_to_check_it_is_not_a_closure(dropped):
    """Review 5, finding A: outline stops alone prove nothing when the body has no size."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    answer = _single(SUMMED_WRONG, CHAINS)
    for key in dropped:
        del answer[key]
    data = single_answer_to_data(answer)
    closed_wrong = [{"axis": "x", "stops": ["edge", "1", "2,3", "4", "edge"],
                     "values": [2.0, 3.5, 4.0, 5.5]}, CHAINS[1]]
    apply_dedicated_pads(data, _reading(closed_wrong))
    assert "non e' chiusa" in data["_pin_map_conflict"] and _pins(data) == SUMMED_WRONG


def test_an_incomplete_dedicated_reading_is_said_to_be_incomplete():
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    data = single_answer_to_data(_single(SUMMED_WRONG, CHAINS))
    partial = [{"axis": "x", "stops": ["edge", "1", "2,3", "edge"], "values": [2.0, 3.5, 9.5]},
               CHAINS[1]]
    apply_dedicated_pads(data, _reading(partial))
    assert "Incompleta" in data["_pin_map_conflict"] and "ne' con" not in data["_pin_map_conflict"]


def test_a_mirrored_dedicated_reading_no_longer_confirms():
    """Negative for the old distance comparison: same distances, opposite signs."""
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading([MIRRORED_X, CHAINS[1]]))
    assert "non coincidono" in data["_pin_map_conflict"]


def test_two_labels_swapped_on_a_symmetric_layout_are_told_apart():
    """A 5 x 5 mm square of four holes: every distance survives the swap, the signs do not."""
    square = {"1": (1.0, 1.0), "2": (6.0, 1.0), "3": (6.0, 6.0), "4": (1.0, 6.0)}
    swapped = [{"axis": "x", "stops": ["edge", "2,3", "1,4", "edge"], "values": [1.0, 5.0, 1.0]},
               {"axis": "y", "stops": ["edge", "1,2", "3,4", "edge"], "values": [1.0, 5.0, 1.0]}]
    data = _main(square, length=7.0, width=7.0)
    apply_dedicated_pads(data, _reading(swapped))
    assert "non coincidono" in data["_pin_map_conflict"]


@pytest.mark.parametrize("chains, reason", [
    ([CHAINS[0], {"axis": "y", "stops": ["edge", "1,2", "3", "edge"], "values": [2.5, 4.0, 2.5]}],
     "non raggiunti dalle catene: 4"),
    ([dict(CHAINS[0], values=[2.0, 3.5, 4.0, 2.0]), CHAINS[1]], "sommano"),
], ids=["pad-missing", "no-closure"])
def test_incomplete_or_unclosed_main_chains_keep_the_model_coordinates(chains, reason):
    """Negative: the program's sums are used only when they place every pad and close."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    data = single_answer_to_data(_single(TRUE, chains))
    assert _pins(data) == TRUE and data["package_geometry"]["origin"] == "custom"
    assert "somme del modello" in data["notes"] and reason in data["notes"]


# ── Pin numbers sent as integers (review S1, 2026-11-03) ─────────────────

def test_integer_pin_numbers_in_an_agreeing_reading_are_written_not_lost():
    """Positive: the model sent 1 instead of "1"; the pads are keyed by text, the
    agreeing reading is written and the numbers come out as text."""
    data = _main({int(n): xy for n, xy in TRUE.items()})
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "_pin_map_conflict" not in data and "confermate" in data["_numbering_note"]
    assert {p["number"] for p in data["package_geometry"]["pins"]} == set(TRUE)
    assert data["package_geometry"]["origin"] == "body_center"


def test_integer_pin_numbers_in_a_disagreeing_reading_still_block():
    """Negative: text keys do not turn a different layout into an agreement."""
    data = _main({int(n): xy for n, xy in SUMMED_WRONG.items()})
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "non coincidono" in data["_pin_map_conflict"]


# ── Review G10 (2026-11-03): the reading tolerance follows the drawing's unit ──

#: 0.004 inch = 0.1016 mm: below the rounding of one 0.01-inch quote (0.127 mm),
#: just above the 0.1 mm of a millimetre drawing.
_INCH_SLIP_MM = 0.004 * 25.4


def _inch_main(pads):
    data = _main({n: (x / 25.4, y / 25.4) for n, (x, y) in pads.items()},
                 length=15.0 / 25.4, width=9.0 / 25.4)
    data["drawing_unit"] = "inch"
    return data


def test_an_inch_drawing_tolerates_the_rounding_of_one_inch_quote():
    """Positive: two readings of an inch drawing 0.004 in apart agree."""
    slipped = dict(TRUE, **{"4": (TRUE["4"][0] + _INCH_SLIP_MM, TRUE["4"][1])})
    inches = [dict(chain, values=[v / 25.4 for v in chain["values"]]) for chain in CHAINS]
    data = _inch_main(slipped)
    apply_dedicated_pads(data, _reading(inches, factor=25.4))
    assert "_pin_map_conflict" not in data and "confermate" in data["_numbering_note"]


def test_a_millimetre_drawing_keeps_its_tighter_tolerance():
    """Negative: the same 0.1016 mm slip in a millimetre drawing is a disagreement."""
    slipped = dict(TRUE, **{"4": (TRUE["4"][0] + _INCH_SLIP_MM, TRUE["4"][1])})
    data = _main(slipped)
    apply_dedicated_pads(data, _reading(CHAINS))
    assert "non coincidono" in data["_pin_map_conflict"]


# ── Pads on a drawn centreline (user, 2026-11-04) ────────────────────────
#
# A relay drawing quoted its fifth pin in X only: in Y it sits on the
# dash-dot centreline half way between the two rows, which no chain could
# express, so the dedicated reading was incomplete and the main model
# invented the coordinate. The model now declares the centreline it sees
# and the program takes the mean; nothing is guessed. The drawing here is
# invented: two rows of two holes and a fifth hole on their centreline.

#: x: outline, 5, 1, 2, outline; y: outline, {1, 2}, {3, 4}, outline. Pad 5
#: has no y quote: it is on the centreline between the rows.
ROWS = [
    {"axis": "x", "stops": ["edge", "5", "1", "2", "edge"], "values": [1.0, 2.0, 12.0, 1.0]},
    {"axis": "x", "stops": ["edge", "3,1", "4,2", "edge"], "values": [3.0, 12.0, 1.0]},
    {"axis": "y", "stops": ["edge", "1,2", "3,4", "edge"], "values": [2.0, 10.0, 2.0]},
]
ON_ROWS_CENTRELINE = [{"axis": "y", "pad": "5", "between": ["1", "3"]}]


def test_a_pad_on_the_centreline_between_two_stops_is_placed_at_their_mean():
    """Positive: the y of pad 5 is the mean of the two rows; the body spans are untouched."""
    result = pads_from_chains(ROWS, 1.0, ON_ROWS_CENTRELINE)
    assert result["unplaced"] == [] and result["problems"] == [] and result["malformed"] == []
    assert result["pads"]["5"] == pytest.approx((1.0, 7.0))
    assert result["pads"]["1"] == pytest.approx((3.0, 2.0))
    assert result["spans"]["y"] == pytest.approx(14.0)


def test_a_pad_on_the_body_centreline_is_placed_between_the_two_outline_stops():
    """Positive: «edge, edge» means the middle of the body on that axis."""
    chains = [ROWS[0], ROWS[1], {"axis": "y", "stops": ["edge", "1,2", "3,4", "edge"],
                                 "values": [2.0, 10.0, 2.5]}]
    result = pads_from_chains(chains, 1.0, [{"axis": "y", "pad": "5", "between": ["edge", "edge"]}])
    assert result["pads"]["5"][1] == pytest.approx(14.5 / 2)


def test_a_centreline_through_another_centreline_pad_is_resolved_in_order():
    """Positive: pad 6 half way between pad 5 (itself a midpoint) and row 3-4."""
    chains = ROWS + [{"axis": "x", "stops": ["5", "6"], "values": [0.5]}]
    midpoints = [{"axis": "y", "pad": "6", "between": ["5", "3"]}] + ON_ROWS_CENTRELINE
    result = pads_from_chains(chains, 1.0, midpoints)
    assert result["pads"]["6"] == pytest.approx((1.5, 9.5)) and result["unplaced"] == []


def test_a_centreline_between_unsolved_stops_leaves_the_pad_unplaced_never_guessed():
    """Negative: a stop no chain places on that axis cannot anchor a mean."""
    result = pads_from_chains(ROWS, 1.0, [{"axis": "y", "pad": "5", "between": ["1", "9"]}])
    assert "5" not in result["pads"] and result["unplaced"] == ["5"]
    assert result["problems"] == [] and result["malformed"] == []


def test_a_centreline_that_contradicts_a_chain_is_a_problem():
    """Negative: pad 5 quoted 3 mm below row 1 cannot also be on the centreline (7 mm)."""
    chains = ROWS + [{"axis": "y", "stops": ["1", "5"], "values": [3.0]}]
    result = pads_from_chains(chains, 1.0, ON_ROWS_CENTRELINE)
    assert len(result["problems"]) == 1 and "linea d'asse" in result["problems"][0]
    assert "5" in result["pads"]


def test_a_centreline_agreeing_with_a_chain_within_the_rounding_is_no_problem():
    """Positive: the same pad quoted 5.05 mm below row 1 (one rounding) agrees with the mean."""
    chains = ROWS + [{"axis": "y", "stops": ["1", "5"], "values": [5.05]}]
    result = pads_from_chains(chains, 1.0, ON_ROWS_CENTRELINE)
    assert result["problems"] == []


@pytest.mark.parametrize("entry", [
    {"axis": "z", "pad": "5", "between": ["1", "3"]},
    {"axis": "y", "pad": "", "between": ["1", "3"]},
    {"axis": "y", "pad": "edge", "between": ["1", "3"]},
    {"axis": "y", "pad": "5", "between": ["1"]},
    {"axis": "y", "pad": "5", "between": ["1", "3", "4"]},
    {"axis": "y", "pad": "5", "between": ["1", 3]},
    {"axis": "y", "pad": "5", "between": ["5", "3"]},
    "5 between 1 and 3",
])
def test_a_malformed_centreline_entry_is_reported_not_read(entry):
    """Negative: every slip is named in `malformed`; the pad stays unplaced."""
    result = pads_from_chains(ROWS, 1.0, [entry])
    assert result["malformed"] == ["linea d'asse n. 1: voce non valida"]
    assert "5" not in result["pads"] and result["unplaced"] == ["5"]


def test_the_reader_passes_the_declared_centrelines_to_the_sums(tmp_path):
    """read_pad_positions hands `midpoints` to pads_from_chains, in the declared unit."""
    from unittest.mock import Mock
    from kicadgen.services.datasheet_extractor import read_pad_positions
    from tests.test_extraction_prompts import _TINY_PNG
    drawing = tmp_path / "pinout.png"
    drawing.write_bytes(_TINY_PNG)
    inches = [dict(chain, values=[v / 25.4 for v in chain["values"]]) for chain in ROWS]
    answer = {"drawing_unit": "inch", "contacts_view": "bottom", "chains": inches,
              "midpoints": ON_ROWS_CENTRELINE}
    reading = read_pad_positions([drawing], model="m", call=Mock(return_value=answer))
    assert reading["unplaced"] == [] and reading["pads"]["5"] == pytest.approx((1.0, 7.0))


def test_the_single_answer_carries_its_centrelines_into_the_chain_sums():
    """A main answer with pads on the centreline and matching pad_midpoints takes the sums."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    pads = {"1": (3.0, 2.0), "2": (15.0, 2.0), "3": (3.0, 12.0), "4": (15.0, 12.0),
            "5": (1.0, 7.0)}
    answer = {"drawing_unit": "mm", "contacts_view": "bottom", "confidence": "high",
              "pads_origin": "custom", "pad_chains": ROWS, "pad_midpoints": ON_ROWS_CENTRELINE,
              "pads": [{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                        "drill_mm": 1.0, "pad_length_mm": 2.0, "pad_width_mm": 2.0}
                       for n, (x, y) in pads.items()]}
    data = single_answer_to_data(answer)
    assert "_pin_map_conflict" not in data
    assert "catene di quote" not in str(data.get("notes") or "")


# ── Generality audit of the centreline rule (2026-11-04) ─────────────────

def test_a_pad_chained_only_to_the_centreline_pad_is_placed_through_it():
    """Positive (audit 1): pad 6 is quoted from pad 5 only; its group is moved onto the mean."""
    chains = ROWS + [{"axis": "x", "stops": ["5", "6"], "values": [0.5]},
                     {"axis": "y", "stops": ["5", "6"], "values": [2.0]}]
    result = pads_from_chains(chains, 1.0, ON_ROWS_CENTRELINE)
    assert result["problems"] == [] and result["unplaced"] == []
    assert result["pads"]["5"] == pytest.approx((1.0, 7.0))
    assert result["pads"]["6"] == pytest.approx((1.5, 9.0))


def test_a_chain_in_the_same_group_that_disagrees_with_the_centreline_still_contradicts():
    """Negative (audit 1): a quote from row 1 to pad 5 of 3 mm against a mean of 5 mm."""
    chains = ROWS + [{"axis": "y", "stops": ["1", "5"], "values": [3.0]}]
    result = pads_from_chains(chains, 1.0, ON_ROWS_CENTRELINE)
    assert len(result["problems"]) == 1


def test_two_pad_stops_written_against_the_page_order_are_a_reported_slip():
    """Negative (audit 2): the mean is symmetric, so the slip is reported, the pad still placed."""
    result = pads_from_chains(ROWS, 1.0, [{"axis": "y", "pad": "5", "between": ["3", "1"]}])
    assert result["pads"]["5"] == pytest.approx((1.0, 7.0))
    assert any("non in ordine" in note for note in result["malformed"])
    assert pads_from_chains(ROWS, 1.0, ON_ROWS_CENTRELINE)["malformed"] == []


def test_identical_stops_are_malformed_and_coincident_stops_leave_the_pad_unplaced():
    """Negative (audit 3): «1, 1» is no entry; «1, 2» on one row is no centreline."""
    same = pads_from_chains(ROWS, 1.0, [{"axis": "y", "pad": "5", "between": ["1", "1"]}])
    assert same["malformed"] == ["linea d'asse n. 1: voce non valida"] and "5" not in same["pads"]
    row = pads_from_chains(ROWS, 1.0, [{"axis": "y", "pad": "5", "between": ["1", "2"]}])
    assert "5" not in row["pads"] and row["unplaced"] == ["5"] and row["problems"] == []
    assert any("coincidono" in note for note in row["malformed"])
    # «edge, edge» stays the body centreline (positive).
    assert pads_from_chains(ROWS, 1.0, [{"axis": "y", "pad": "5", "between": ["edge", "edge"]}])[
        "pads"]["5"][1] == pytest.approx(7.0)


def test_the_centreline_tolerance_is_half_the_stops_roundings_plus_the_pads_own():
    """Audit 4, inch drawing: 1 quote to each stop, 1 to the pad -> 2 roundings (0.254 mm), not 3."""
    inches = [dict(chain, values=[v / 25.4 for v in chain["values"]]) for chain in ROWS]
    for offset, flagged in ((0.24, False), (0.30, True)):
        chains = inches + [{"axis": "y", "stops": ["1", "5"], "values": [(5.0 + offset) / 25.4]}]
        result = pads_from_chains(chains, 25.4, ON_ROWS_CENTRELINE)
        assert bool(result["problems"]) is flagged, offset


def test_the_main_group_is_chosen_on_chain_placed_pads_only():
    """Audit 5: two disconnected groups of two pads; a centreline pad does not tip the choice."""
    chains = [{"axis": "x", "stops": ["1", "2"], "values": [4.0]},
              {"axis": "x", "stops": ["3", "4", "7"], "values": [4.0, 1.0]},
              {"axis": "y", "stops": ["1", "2", "3", "4", "7"], "values": [1.0, 1.0, 1.0, 1.0]}]
    result = pads_from_chains(chains, 1.0, [{"axis": "x", "pad": "5", "between": ["1", "2"]}])
    assert set(result["pads"]) == {"3", "4", "7"}


def test_a_pad_declared_twice_with_different_stops_is_a_contradiction():
    doubled = ON_ROWS_CENTRELINE + [{"axis": "y", "pad": "5", "between": ["1", "edge"]}]
    result = pads_from_chains(ROWS, 1.0, doubled)
    assert len(result["problems"]) == 1


def test_a_pad_on_both_centrelines_needs_no_chain_and_empty_chains_never_crash():
    both = [{"axis": "x", "pad": "9", "between": ["1", "2"]},
            {"axis": "y", "pad": "9", "between": ["1", "3"]}]
    assert pads_from_chains(ROWS, 1.0, both)["pads"]["9"] == pytest.approx((9.0, 7.0))
    empty = pads_from_chains([], 1.0, ON_ROWS_CENTRELINE)
    assert empty["pads"] == {} and empty["unplaced"] == ["5"]


def test_a_single_answer_whose_coordinate_differs_from_its_centreline_waits_for_the_dedicated_reading():
    """Negative of the single-answer path: the model's y for pad 5 is not the mean -> conflict."""
    from kicadgen.services.datasheet_extractor import single_answer_to_data
    pads = {"1": (3.0, 2.0), "2": (15.0, 2.0), "3": (3.0, 12.0), "4": (15.0, 12.0),
            "5": (1.0, 2.0)}
    answer = {"drawing_unit": "mm", "contacts_view": "bottom", "confidence": "high",
              "pads_origin": "custom", "pad_chains": ROWS, "pad_midpoints": ON_ROWS_CENTRELINE,
              "pads": [{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                        "drill_mm": 1.0, "pad_length_mm": 2.0, "pad_width_mm": 2.0}
                       for n, (x, y) in pads.items()]}
    data = single_answer_to_data(answer)
    assert "_pin_map_conflict" in data and data["_chain_candidate"]["pads"]["5"][1] == pytest.approx(7.0)
