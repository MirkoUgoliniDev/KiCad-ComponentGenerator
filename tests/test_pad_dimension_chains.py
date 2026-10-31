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
    assert result["spans"] == {"x": pytest.approx(15.0), "y": pytest.approx(9.0)}


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
