"""@brief Unconfirmed pad positions block; the drawn holes may pick the one right reading (user, 2026-10-30).

A relay footprint was generated from a reading nobody had confirmed and
came out wrong. Since then a single part whose explicit pads no second
reading confirms is BLOCKED, with one way out: the holes drawn in the
documents (drawing_holes, a local check independent of both models)
confirm exactly one of the readings at hand and none of the others.

The hole check is faked here by its contract: it sees GEOMETRY only, the
set of positions up to a translation, never the pad numbers. The drawing is
invented (a 20 x 12 mm body, five holes), not the relay.
"""
import pytest

from kicadgen.services.datasheet_extractor import (
    MEASURED_PIXELS_SOURCE, _pad_block_remedy, apply_dedicated_pads, pads_from_chains)
from test_pad_dimension_chains import unresolved  # noqa: E402

#: x: outline, 1, {2, 5}, {3, 4}, outline; y: outline, {1, 3}, 2, {4, 5}, outline.
CHAINS = [
    {"axis": "x", "stops": ["edge", "1", "2,5", "3,4", "edge"], "values": [2.0, 5.0, 9.0, 4.0]},
    {"axis": "y", "stops": ["edge", "1,3", "2", "4,5", "edge"], "values": [2.0, 3.0, 4.0, 3.0]},
]
#: The same holes from the outline's top-left corner (page: X right, Y down).
TRUE = {"1": (2.0, 2.0), "2": (7.0, 5.0), "3": (16.0, 2.0), "4": (16.0, 9.0), "5": (7.0, 9.0)}
#: A wrong reading: hole 2 one row too high (a quote attributed to the wrong stop).
SHIFTED = dict(TRUE, **{"2": (7.0, 2.0)})
#: The right positions with two pad numbers exchanged: the holes cannot tell.
SWAPPED = dict(TRUE, **{"1": TRUE["3"], "3": TRUE["1"]})


class FakeHoles:
    """Confirms a layout whose positions, up to a translation AND a uniform scale, are the drawn ones.

    Scale-free like the real check (drawing_holes fits the px/mm scale), so
    the tests cannot pass on a strictness the real check does not have.
    """

    def __init__(self, drawn, status_when_not="not_confirmed", reason="no_match", also=()):
        self.drawn, self.status_when_not, self.reason, self.calls = drawn, status_when_not, reason, 0
        self.seen = []
        #: Further layouts drawn on the same page (another view).
        self.also = list(also)

    def __call__(self, pads, body):
        self.calls += 1
        #: Every point set checked, to prove the memo asks each one once.
        # The body is part of the question (review 2026-11-10: the same pads
        # are asked with the body in both orientations to measure which way
        # it lies), so a «repeated» check is the same pads AND the same body.
        self.seen.append((tuple(sorted((k, round(x, 4), round(y, 4)) for k, (x, y) in pads.items())),
                          None if body is None else tuple(round(float(size), 4) for size in body)))

        def shape(points):
            points = list(points)
            x0 = min(x for x, _y in points)
            y0 = min(y for _x, y in points)
            size = max(max(x - x0, y - y0) for x, y in points) or 1.0
            return sorted((round((x - x0) / size, 3), round((y - y0) / size, 3)) for x, y in points)

        for drawn in [self.drawn] + self.also:
            if len(pads) == len(drawn) and shape(pads.values()) == shape(drawn.values()):
                return _Verdict("confirmed", "confirmed")
        return _Verdict(self.status_when_not, self.reason)


class _Verdict:
    """The fields of drawing_holes.LayoutCheck the extractor reads."""

    def __init__(self, status, reason):
        self.status, self.reason, self.image = status, reason, "drawing.png"
        self.max_error_mm, self.tolerance_mm, self.circles = 0.05, 0.2, 5


def _main(pads, view="bottom", length=20.0, width=12.0):
    return {"drawing_unit": "mm", "contacts_view": view, "package_geometry": {
        "origin": "custom", "body_length_mm": length, "body_width_mm": width,
        "pins": [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in pads.items()]}}


def _reading(chains=CHAINS, view="bottom"):
    return dict(pads_from_chains(chains), view=view, notes="")


def _offsets(data):
    pins = {p["number"]: (p["x_mm"], p["y_mm"]) for p in data["package_geometry"]["pins"]}
    ox, oy = pins["1"]
    return {n: (round(x - ox, 3), round(y - oy, 3)) for n, (x, y) in pins.items()}


def _true_offsets():
    ox, oy = TRUE["1"]
    return {n: (round(x - ox, 3), round(y - oy, 3)) for n, (x, y) in TRUE.items()}


# ── No second reading ────────────────────────────────────────────────────

@pytest.mark.parametrize("reading", [
    None, {"view": "unknown", "pads": {}, "notes": "", "failed": "HTTP 500"}])
def test_an_unconfirmed_main_answer_blocks(reading):
    """Negative: no reading and no hole check -> the hard block (one layout: no question can close it).

    CONTRACT CHANGED (review 2026-11-04, B3): the block carries the fact and
    one sentence saying no question applies; the remedy is the GUI's.
    """
    data = _main(TRUE)
    apply_dedicated_pads(data, reading)
    assert "NON confermate" in data["_pin_map_conflict"]
    assert "Nessuna domanda puo' chiudere il caso" in data["_pin_map_conflict"]
    assert "Rigenera footprint" not in data["_pin_map_conflict"] and not data.get("_questions")


def test_the_drawn_holes_alone_confirm_the_main_answer():
    """Positive: same failed reading, the holes match the main layout -> it stands."""
    data = _main(TRUE)
    apply_dedicated_pads(data, None, check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data
    assert "non i numeri" in data["_numbering_note"]
    assert _offsets(data) == _true_offsets()


def test_holes_that_do_not_match_keep_the_block():
    data = _main(SHIFTED)
    apply_dedicated_pads(data, None, check_layout=FakeHoles(TRUE))
    assert "non coincidono con queste posizioni" in unresolved(data)


def test_a_photo_cannot_confirm_anything():
    """An unverifiable check (no line drawing) is never a confirmation."""
    data = _main(TRUE)
    apply_dedicated_pads(data, None, check_layout=FakeHoles({}, "unverifiable", "not_a_drawing"))
    assert "disegno a linee" in unresolved(data)


def test_a_check_that_cannot_run_blocks_and_does_not_raise():
    def broken(pads, body):
        raise OSError("disk")

    data = _main(TRUE)
    apply_dedicated_pads(data, None, check_layout=broken)
    assert "non riuscito (OSError)" in unresolved(data)


# ── Two readings that disagree ───────────────────────────────────────────

def test_the_reading_the_holes_confirm_is_written():
    """Positive: main wrong, dedicated right, only the dedicated matches the holes."""
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data
    assert "lettura dedicata" in data["_numbering_note"]
    assert _offsets(data) == _true_offsets()
    # Closed on the body on both axes: written with the body centre as origin.
    geometry = data["package_geometry"]
    assert geometry["origin"] == "body_center"
    assert geometry["pins"][0]["x_mm"] == pytest.approx(TRUE["1"][0] - 10.0)


def test_readings_differing_only_by_numbers_are_never_picked_by_the_holes():
    """Negative: both layouts match the holes (two numbers exchanged) -> blocked."""
    data = _main(SWAPPED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert "non coincidono" in unresolved(data)
    assert _offsets(data) != _true_offsets()


def test_neither_reading_matching_the_holes_keeps_the_block():
    wrong_drawing = dict(TRUE, **{"4": (18.0, 9.0)})
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(wrong_drawing))
    assert "Nessuna delle due" in unresolved(data)


def test_the_adopted_reading_brings_its_view_to_the_pads():
    """The view the footprint mirrors by is package_geometry.source_view, not contacts_view."""
    data = _main(SHIFTED, view="unknown")
    data["package_geometry"]["source_view"] = "top"     # what single_answer_to_data writes
    apply_dedicated_pads(data, _reading(view="bottom"), check_layout=FakeHoles(TRUE))
    assert data["package_geometry"]["source_view"] == "bottom"
    assert "(bottom)" in data["_numbering_note"]


def test_two_agreeing_readings_take_the_only_declared_view():
    data = _main(TRUE, view="unknown")
    data["package_geometry"]["source_view"] = "top"
    apply_dedicated_pads(data, _reading(view="bottom"))
    assert "_pin_map_conflict" not in data
    assert data["package_geometry"]["source_view"] == "bottom"


@pytest.mark.parametrize("main", [TRUE, SHIFTED])
def test_different_declared_views_are_never_settled_by_the_holes(main):
    """The holes say where, not from which side: even when they match the main layout."""
    data = _main(main, view="top")
    apply_dedicated_pads(data, _reading(view="bottom"), check_layout=FakeHoles(TRUE))
    assert "viste diverse" in unresolved(data)


def test_a_reading_of_the_same_shape_at_another_scale_blocks():
    """Wrong unit in the main answer (pads AND body scaled): the holes cannot see the scale."""
    scaled = {n: (x * 0.9, y * 0.9) for n, (x, y) in TRUE.items()}
    data = _main(scaled, length=18.0, width=10.8)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert "in scala 1.111" in unresolved(data)


def test_contradicting_chains_of_another_shape_do_not_stop_the_holes():
    """Positive twin: the discarded reading differs in shape, not in scale -> the main stands."""
    bad = [dict(CHAINS[0], values=[2.0, 5.0, 9.0, 6.0]), CHAINS[1]]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(bad), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data


def test_pads_sharing_a_number_are_never_settled_by_the_holes():
    pads = dict(SHIFTED)
    data = _main(pads)
    data["package_geometry"]["pins"].append({"number": "1", "x_mm": 1.0, "y_mm": 1.0})
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert unresolved(data)


def test_a_crashing_hole_check_never_stops_the_analysis():
    def broken(pads, body):
        raise RuntimeError("cv2.error")

    data = _main(TRUE)
    apply_dedicated_pads(data, None, check_layout=broken)
    assert "RuntimeError" in unresolved(data)


def test_the_remedy_follows_the_pad_type():
    """SMD pads have no manual editor: the remedy points to a better input instead.

    Since the review of 2026-11-04 (B3) the remedy sentence is the GUI's
    helper, no longer appended to the block text.
    """
    smd = [dict(pin, pad_type="smd") for pin in _main(TRUE)["package_geometry"]["pins"]]
    assert "Piazzole del componente THT" not in _pad_block_remedy(smd)
    assert "come immagine" in _pad_block_remedy(smd)
    assert "Piazzole del componente THT" in _pad_block_remedy(_main(TRUE)["package_geometry"]["pins"])
    data = _main(TRUE)
    apply_dedicated_pads(data, None)
    assert "Piazzole del componente THT" not in data["_pin_map_conflict"]


# ── Agreement, partial readings, contradictions ──────────────────────────

def test_two_agreeing_readings_refuted_by_the_holes_are_not_written():
    """CONTRACT CHANGED (review 2026-11-04, H6): two readings wrong the same way used to pass
    with a warning; an observable «no_match» of the drawn holes now refuses them. Without a
    measure and with one layout only, the hard block stayed.

    CONTRACT CHANGED AGAIN (round 7, coordinator's decision of 2026-11-11):
    the witness is imperfect, so the refusal is never a hard block: the
    readings' layout is the one option of a "pad_layout" question, its text
    saying the drawn holes do not confirm it."""
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(SHIFTED))
    assert "_pin_map_conflict" not in data
    question, = data["_questions"]
    assert question["field"] == "pad_layout" and len(question["options"]) == 1
    assert "non confermano questa disposizione" in question["text"]
    assert _offsets(data) == _true_offsets(), "the main pads stay as read, nothing adopted"


def test_two_agreeing_readings_stand_with_a_warning_when_the_page_may_not_be_to_scale():
    """Positive twin of the above: «distorted» is no refutation (a page not to scale)."""
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(SHIFTED, reason="distorted"))
    assert "_pin_map_conflict" not in data and "Attenzione" in data["_numbering_note"]


def test_a_collinear_three_pad_no_match_is_not_a_refutation():
    """M18: with three pads on one line the check cannot tell «wrong» from «not to scale»."""
    line = {"1": (0.0, 0.0), "2": (5.0, 0.0), "3": (12.0, 0.0)}
    chains = [{"axis": "x", "stops": ["1", "2", "3"], "values": [5.0, 7.0]},
              {"axis": "y", "stops": ["1,2,3", "edge"], "values": [2.0]}]
    data = _main(line)
    apply_dedicated_pads(data, _reading(chains), check_layout=FakeHoles(
        {"1": (0.0, 0.0), "2": (6.0, 0.0), "3": (12.0, 0.0)}))
    assert "_pin_map_conflict" not in data and "Attenzione" in data["_numbering_note"]


def test_agreeing_readings_refuted_by_the_holes_are_offered_the_measure_never_given_it():
    """The measure on the drawn holes is OFFERED beside the layout the two readings agree on.

    CONTRACT CHANGED (user, 2026-11-11, round 6 (a)): since H6 the measured
    layout was written at once («the measure decides»). The holes verify
    geometry, not numbering, and two agreeing readings are not nothing: the
    program now adopts neither -- it records the "pad_layout" question with
    both layouts, each with what the holes said. The answer writes the
    measured layout exactly as before, the moved pad with its provenance.
    """
    class Measure:
        def __call__(self, pads, body):
            from types import SimpleNamespace
            positions = dict(pads, **{"2": TRUE["2"]})
            return SimpleNamespace(positions_mm=positions, measured_mm=positions, moved="2",
                                   moved_from_mm=pads["2"], max_error_mm=0.05,
                                   tolerance_mm=0.2, image="drawing.png")
    shifted_chains = [CHAINS[0], {"axis": "y", "stops": ["edge", "1,2,3", "4,5", "edge"],
                                  "values": [2.0, 7.0, 3.0]}]
    data = _main(SHIFTED)
    before = _offsets(data)
    apply_dedicated_pads(data, _reading(shifted_chains), check_layout=FakeHoles(TRUE),
                         measure_layout=Measure())
    # Round 7: the measured wording («non confermano», never «smentiscono»).
    assert "_pin_map_conflict" not in data and "non confermano" in data["_numbering_note"]
    assert _offsets(data) == before, "nothing is adopted: neither the readings nor the measure"
    question, = data["_questions"]
    values = sorted(option["value"].split(":")[0] for option in question["options"])
    assert question["field"] == "pad_layout" and values == ["holes", "main+dedicated"]
    measured = next(option for option in question["options"] if option["value"].startswith("holes:"))
    assert "misurate sui fori" in measured["label"] and "STIMATA dai pixel" in measured["label"]
    # The user's (saved) answer: the measured layout is written, as the measure used to write it.
    data = _main(SHIFTED)
    data["_answers"] = {question["key"]: measured["value"]}
    apply_dedicated_pads(data, _reading(shifted_chains), check_layout=FakeHoles(TRUE),
                         measure_layout=Measure())
    assert "_pin_map_conflict" not in data and not data.get("_questions")
    moved = next(p for p in data["package_geometry"]["pins"] if p["number"] == "2")
    assert moved["source"] == MEASURED_PIXELS_SOURCE
    assert _offsets(data) == _true_offsets()


def test_a_partial_reading_blocks_unless_the_holes_confirm_the_main_answer():
    partial = [CHAINS[0], {"axis": "y", "stops": ["edge", "1,3", "2", "edge"],
                           "values": [2.0, 3.0, 7.0]}]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(partial))
    assert "solo 3 pad su 5" in unresolved(data)
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(partial), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data


def test_contradicting_chains_leave_the_main_answer_to_the_holes():
    """The chains do not close on the body: the dedicated reading is out either way."""
    bad = [dict(CHAINS[0], values=[2.0, 5.0, 9.0, 6.0]), CHAINS[1]]
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(bad))
    assert "non tornano" in unresolved(data)
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading(bad), check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data


def test_the_hole_check_runs_once_per_layout():
    """Main, dedicated and the chosen layout's TWO mirror images: four distinct layouts, four checks.

    CONTRACT CHANGED (review 2026-11-04, low finding «mirror tested along X
    only»): the side guard now shares drawing_holes.mirror_variants with the
    measure, so the chosen reading is tried against its X flip AND its Y
    flip (a page may be flipped either way). Each distinct layout is still
    checked once: the memo of _HoleVerdicts is what this test protects.
    """
    holes = FakeHoles(TRUE)
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=holes)
    assert holes.calls == 4
    # The same four layouts asked again cost nothing more.
    assert len(set(holes.seen)) == holes.calls


def test_both_mirror_images_are_tried_but_a_repeated_layout_is_not_rechecked():
    """Negative: whatever the layouts offered, no point set is ever sent to the witness twice with the same body."""
    square = {"1": (0.0, 0.0), "2": (4.0, 0.0), "3": (4.0, 4.0), "4": (0.0, 4.0)}
    drawn = {"1": (0.0, 0.0), "2": (4.0, 0.0), "3": (4.0, 4.0), "4": (0.0, 4.0)}
    holes = FakeHoles(drawn)
    data = _main({"1": (0.0, 0.0), "2": (4.0, 0.0), "3": (4.0, 4.0), "4": (0.0, 5.0)})
    reading = {"pads": square, "view": "bottom", "source": "dedicated"}
    apply_dedicated_pads(data, reading, check_layout=holes)
    assert holes.calls == len(set(holes.seen))


def test_two_layouts_the_holes_cannot_judge_are_offered_to_the_user():
    """Positive (generality audit 2026-11-05, A1): an unverifiable witness refutes nothing, so both
    complete layouts are candidates of the «pad_layout» question and the hard block is lifted."""
    elsewhere = {k: (x + 40.0, y * 3.0) for k, (x, y) in TRUE.items()}
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(elsewhere, status_when_not="unverifiable",
                                                                  reason="no_image"))
    assert "_pin_map_conflict" not in data
    question, = [q for q in data["_questions"] if q["field"] == "pad_layout"]
    assert len(question["options"]) == 2


def test_two_layouts_the_holes_refute_are_not_offered_as_a_choice():
    """Negative: five non-collinear pads make the page observable, so a «no_match» on BOTH layouts is a
    refutation of both; the block stays and no question offers a layout the witness says is wrong."""
    elsewhere = {k: (x + 40.0, y * 3.0) for k, (x, y) in TRUE.items()}
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(elsewhere))
    assert "_pin_map_conflict" in data
    assert not any(q["field"] == "pad_layout" for q in data.get("_questions", []))
    assert "smentite dai fori" in data["_pin_map_conflict"]


def test_only_the_layout_the_holes_do_not_refute_survives_the_question():
    """One refuted candidate leaves one survivor: fewer than two options, hence the block, never a
    question with a single answer."""
    data = _main(SHIFTED)
    # The holes refute the main layout and cannot judge the dedicated one (held unverifiable).
    class Mixed(FakeHoles):
        def __call__(self, pads, body):
            self.calls += 1
            if sorted(pads.items()) == sorted(SHIFTED.items()):
                return _Verdict("not_confirmed", "no_match")
            return _Verdict("unverifiable", "no_image")
    apply_dedicated_pads(data, _reading(), check_layout=Mixed(TRUE))
    assert "_pin_map_conflict" in data
    assert not any(q["field"] == "pad_layout" for q in data.get("_questions", []))


def test_a_chain_reading_confirmed_undistorted_beats_a_competitor_that_fits_only_distorted():
    """CONTRACT CHANGED (user, 2026-11-03, T7N relay): the chain sums fitting the drawn
    circles without distortion prove the page to scale with its printed quotes, so a
    competitor that fits only by distorting that page is wrong and the chains win."""
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE, reason="distorted"))
    assert "_pin_map_conflict" not in data
    assert _offsets(data) == _true_offsets()


def test_a_page_not_to_scale_leaves_the_distorted_chain_reading_unchosen():
    """Negative twin: the page is not to scale, so the CHAIN reading is the «distorted» one and
    the main answer (copying the drawn proportions) is confirmed: not a chain reading, no choice."""
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(SHIFTED, reason="distorted"))
    assert unresolved(data)


def test_a_distorted_competitor_still_blocks_a_confirmed_main_answer():
    """The exception is for chain readings only: a main answer confirmed while the chain reading
    is «distorted» chooses nothing (same page, the main answer proves no scale)."""
    data = _main(TRUE)
    apply_dedicated_pads(data, _reading([
        {"axis": "x", "stops": ["edge", "1", "2,5", "3,4", "edge"], "values": [2.0, 5.0, 9.0, 4.0]},
        {"axis": "y", "stops": ["edge", "1,3", "2", "4,5", "edge"], "values": [2.0, 0.5, 6.5, 3.0]},
    ]), check_layout=FakeHoles(TRUE, reason="distorted"))
    assert unresolved(data)


# ── An answer that left a terminal out ─────────────────────────────────

def test_an_incomplete_main_answer_is_replaced_by_the_confirmed_chain_reading():
    """User, 2026-11-03 (T7N relay): numbers READ by the main answer, but one pad fewer than the
    chain reading the holes confirm -> its numbering cannot be the complete one: replaced."""
    for reason in ("no_match", "distorted"):
        data = _main({n: p for n, p in SHIFTED.items() if n != "5"})
        apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE, reason=reason))
        assert "_pin_map_conflict" not in data, reason
        assert sorted(p["number"] for p in data["package_geometry"]["pins"]) == ["1", "2", "3", "4", "5"]
        assert _offsets(data) == _true_offsets()
        assert data["package_geometry"]["needs_review"] is True
        assert "manca un terminale" in data["_numbering_note"]


def test_an_incomplete_main_answer_stays_blocked_without_a_confirmed_reading():
    """Negative twin: the same incomplete answer, but the holes confirm neither reading."""
    data = _main({n: p for n, p in SHIFTED.items() if n != "5"})
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(
        dict(TRUE, **{"4": (16.0, 11.0)})))
    assert unresolved(data)
    assert sorted(p["number"] for p in data["package_geometry"]["pins"]) == ["1", "2", "3", "4"]


def test_a_main_answer_with_more_pads_than_the_reading_is_never_replaced():
    """Negative twin (audit H1): read numbers and MORE pads than the chain reading -> block."""
    data = _main(dict(SHIFTED, **{"6": (10.0, 11.0)}))
    apply_dedicated_pads(data, _reading(), check_layout=FakeHoles(TRUE))
    assert unresolved(data)


def test_a_page_where_the_mirror_also_fits_follows_the_declared_side():
    """Top and bottom views on one page: the mirrored layout fits too.

    CONTRACT CHANGED (review 2026-11-04, M9): the witness cannot tell the
    side, but a side the chosen reading DECLARES, with no other reading
    declaring a different one, is a fact: the reading is chosen. With the
    chosen reading silent on its side, the geometry is chosen and the side
    is withdrawn from the refuted main reading, to be asked.
    """
    mirrored = {n: (-x, y) for n, (x, y) in TRUE.items()}
    data = _main(SHIFTED)
    apply_dedicated_pads(data, _reading(view="bottom"), check_layout=FakeHoles(TRUE, also=[mirrored]))
    assert "_pin_map_conflict" not in data and data["package_geometry"]["source_view"] == "bottom"
    assert _offsets(data) == _true_offsets()
    data = _main(SHIFTED, view="bottom")
    apply_dedicated_pads(data, _reading(view="unknown"), check_layout=FakeHoles(TRUE, also=[mirrored]))
    assert "_pin_map_conflict" not in data and _offsets(data) == _true_offsets()
    assert data["contacts_view"] == "unknown" and "ritirata" in data["notes"]
    # Negative: the two readings declare different sides -> never settled by the holes.
    data = _main(SHIFTED, view="top")
    apply_dedicated_pads(data, _reading(view="bottom"), check_layout=FakeHoles(TRUE, also=[mirrored]))
    assert "viste diverse" in unresolved(data)


# ── The main answer's own chains ─────────────────────────────────────────

def test_the_holes_settle_the_main_answer_against_its_own_chains():
    """Coordinates and own chains disagree, no dedicated reading: the holes pick the chains."""
    data = _main(SHIFTED)
    data["_chain_candidate"] = {"pads": pads_from_chains(CHAINS)["pads"],
                                "spans": {"x": 20.0, "y": 12.0}, "centred": True}
    data["_pin_map_conflict"] = "own conflict."
    apply_dedicated_pads(data, None, check_layout=FakeHoles(TRUE))
    assert "_pin_map_conflict" not in data and "_chain_candidate" not in data
    assert "catene di quote" in data["_numbering_note"]
    assert _offsets(data) == _true_offsets()
    # Negative: the holes match neither -> the own conflict stays, extended.
    data = _main(SHIFTED)
    data["_chain_candidate"] = {"pads": dict(SHIFTED), "spans": {"x": 20.0, "y": 12.0},
                                "centred": True}
    data["_pin_map_conflict"] = "own conflict."
    apply_dedicated_pads(data, None, check_layout=FakeHoles(TRUE))
    assert unresolved(data).startswith("own conflict.")
