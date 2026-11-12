"""@brief The body's two plan sizes are an unordered pair until the plan view fixes their axes.

User report of 2026-11-10: a main answer that exchanged the two plan sizes of
the body (and misread the pads) made the drawn-hole witness refuse the correct
dedicated reading as «body_scale», so nothing was chosen and the generation
was blocked. Which size runs along page X is printed only when a quote of the
plan view says so; otherwise the drawn outline measures it.

Synthetic pages drawn with OpenCV in tmp_path: an invented five-hole part at
10 px/mm inside a 16 x 12 mm rectangle. Every positive has a negative twin
that differs for a structural reason (a size pair that fits neither way, a
plan quote that fixes the axes, a body that already fits as given), never for
a number tuned to a part. No network, no AI call.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import apply_dedicated_pads, pads_from_chains

# Invented asymmetric part, page orientation (Y down), mm.
PART = {"1": (0.0, 0.0), "2": (10.0, 0.0), "3": (10.0, 6.0), "4": (2.5, 6.0), "5": (5.0, 3.0)}
# What a main answer that misread the drawing says: another row spacing, pad 5 elsewhere.
MISREAD = {"1": (0.0, 0.0), "2": (10.0, 0.0), "3": (10.0, 4.0), "4": (2.5, 4.0), "5": (7.0, 2.0)}
PPMM = 10.0
ORIGIN = (300.0, 220.0)
# Outline drawn 3 mm outside the hole pattern: 16 mm along page X, 12 mm along page Y.
BOX = (-3.0, -3.0, 13.0, 9.0)
DRAWN = (16.0, 12.0)

# The printed chains of PART, without any stop on the body outline.
CHAINS = [{"axis": "x", "kind": "consecutive", "stops": ["1", "4", "5", "2,3"], "values": [2.5, 2.5, 5.0]},
          {"axis": "y", "kind": "consecutive", "stops": ["1,2", "5", "3,4"], "values": [3.0, 3.0]}]
# The same chains with a quote from outline to outline along X that sums 12 mm.
CHAINS_CLOSED_ON_X = [{"axis": "x", "kind": "consecutive", "stops": ["edge", "1", "4", "5", "2,3", "edge"],
                       "values": [1.0, 2.5, 2.5, 5.0, 1.0]}, CHAINS[1]]


def _page(path: Path) -> Path:
    """Draw the pin view of PART with its outline rectangle and return the path."""
    img = np.full((520, 760, 3), 255, np.uint8)

    def px(x_mm, y_mm):
        return int(round(ORIGIN[0] + x_mm * PPMM)), int(round(ORIGIN[1] + y_mm * PPMM))

    for x, y in PART.values():
        centre = (int(round((ORIGIN[0] + x * PPMM) * 16)), int(round((ORIGIN[1] + y * PPMM) * 16)))
        cv2.circle(img, centre, int(0.6 * PPMM * 16), (0, 0, 0), 1, cv2.LINE_AA, shift=4)
    left, top, right, bottom = BOX
    corners = [px(left, top), px(right, top), px(right, bottom), px(left, bottom)]
    for a, b in zip(corners, corners[1:] + corners[:1]):
        cv2.line(img, a, b, (0, 0, 0), 2)
    assert cv2.imwrite(str(path), img)
    return path


def _answer(pads, body):
    """A main answer before canonicalisation: explicit pads and a body (along X, along Y), mm."""
    return {"drawing_unit": "mm", "contacts_view": "top", "body_length_mm": body[0],
            "body_width_mm": body[1],
            "value_sources": {"body_length_mm": "read", "body_width_mm": "derived"},
            "package_geometry": {"origin": "custom", "body_length_mm": body[0],
                                 "body_width_mm": body[1],
                                 "pins": [{"number": n, "x_mm": x, "y_mm": y}
                                          for n, (x, y) in pads.items()]}}


def _reading(chains=CHAINS):
    """The dedicated reading the program sums from `chains`."""
    return dict(pads_from_chains(chains), view="top", notes="")


def _confront(tmp_path, pads, body, chains=CHAINS):
    """Run the pad confrontation against the drawn page; returns the answer and the log lines."""
    documents = [_page(tmp_path / "plan.png")]
    data, log = _answer(pads, body), []
    data["_outline_measurer"] = ext._outline_measurer(documents)
    apply_dedicated_pads(data, _reading(chains), log.append,
                         check_layout=ext._layout_checker(documents),
                         measure_layout=ext._layout_measurer(documents))
    return data, log


def _offsets(data):
    """Signed offsets of every pad from pad 1, as written in the answer."""
    pins = {pin["number"]: (pin["x_mm"], pin["y_mm"]) for pin in data["package_geometry"]["pins"]}
    ox, oy = pins["1"]
    return {n: (round(x - ox, 3), round(y - oy, 3)) for n, (x, y) in pins.items()}


EXCHANGED_NOTE = "Lunghezza e larghezza del corpo scambiate"


# ---------------------------------------------------------------------------
# End to end on a drawn page
# ---------------------------------------------------------------------------

def test_a_body_exchanged_by_the_main_answer_does_not_refuse_the_correct_reading(tmp_path):
    """The reported case: wrong main pads AND the two body sizes the other way round."""
    data, log = _confront(tmp_path, MISREAD, (DRAWN[1], DRAWN[0]))
    assert "_pin_map_conflict" not in data
    assert _offsets(data) == {n: (x, y) for n, (x, y) in PART.items()}
    geometry = data["package_geometry"]
    # The sizes follow the drawn outline, in the geometry and in the answer.
    assert (geometry["body_length_mm"], geometry["body_width_mm"]) == DRAWN
    assert (data["body_length_mm"], data["body_width_mm"]) == DRAWN
    # Their provenance moves with them.
    assert data["value_sources"] == {"body_length_mm": "derived", "body_width_mm": "read"}
    assert EXCHANGED_NOTE in data["notes"] and any(EXCHANGED_NOTE in line for line in log)


SHAPE_ONLY = "conferma della sola forma"


def test_a_size_pair_that_fits_the_outline_neither_way_is_never_exchanged(tmp_path):
    """Negative twin: not the ORDER of the sizes is wrong but a size itself (9 for 12).

    Contract changed in round two (2026-11-10, drawing_holes A9): a body with
    one size misread used to refuse every reading as «body_scale»; the drawn
    rectangle, whose proportions are not the declared body's, is now not
    identified as the body («unverifiable»), so the body reading's mistake no
    longer judges the pads. The correct dedicated reading is chosen by the
    holes for its SHAPE only (the note says the scale is unverified), and the
    sizes are neither exchanged nor corrected: an outline that agrees with
    neither order measures nothing.
    """
    data, _log = _confront(tmp_path, MISREAD, (9.0, DRAWN[0]))
    assert "_pin_map_conflict" not in data
    assert _offsets(data) == {n: (x, y) for n, (x, y) in PART.items()}
    assert SHAPE_ONLY in data["_numbering_note"]
    geometry = data["package_geometry"]
    assert (geometry["body_length_mm"], geometry["body_width_mm"]) == (9.0, DRAWN[0])
    assert EXCHANGED_NOTE not in str(data.get("notes"))
    assert "_body_axes_measured" not in data


def test_a_plan_quote_that_fixes_the_axes_is_not_overruled_by_the_outline(tmp_path):
    """Negative twin: an edge-to-edge chain of the plan says 12 mm along X, as the answer does.

    Contract changed in round two (2026-11-10): the outline that disagrees
    with the printed order is «unverifiable», no longer a «body_scale»
    refusal, so the generation is not blocked any more; what this test
    guards stays -- the printed order is kept, nothing is exchanged, and the
    confirmation is said to cover the shape only.
    """
    data, _log = _confront(tmp_path, MISREAD, (DRAWN[1], DRAWN[0]), CHAINS_CLOSED_ON_X)
    assert SHAPE_ONLY in data["_numbering_note"]
    geometry = data["package_geometry"]
    assert (geometry["body_length_mm"], geometry["body_width_mm"]) == (DRAWN[1], DRAWN[0])
    assert EXCHANGED_NOTE not in str(data.get("notes"))


def test_a_body_that_fits_as_given_is_never_exchanged(tmp_path):
    """Control: the same wrong main pads with the body the right way round."""
    data, _log = _confront(tmp_path, MISREAD, DRAWN)
    assert "_pin_map_conflict" not in data
    assert _offsets(data) == {n: (x, y) for n, (x, y) in PART.items()}
    geometry = data["package_geometry"]
    assert (geometry["body_length_mm"], geometry["body_width_mm"]) == DRAWN
    assert EXCHANGED_NOTE not in str(data.get("notes"))


def test_two_agreeing_readings_take_the_body_axes_from_the_outline(tmp_path):
    """Both readings right, only the body exchanged: confirmed, and the sizes follow the drawing."""
    data, _log = _confront(tmp_path, PART, (DRAWN[1], DRAWN[0]))
    assert "_pin_map_conflict" not in data
    geometry = data["package_geometry"]
    assert (geometry["body_length_mm"], geometry["body_width_mm"]) == DRAWN
    assert EXCHANGED_NOTE in data["notes"]
    # No warning about a scale that differs: the witness confirmed.
    assert "scala diversa" not in data["_numbering_note"]


# ---------------------------------------------------------------------------
# The memoised witness alone
# ---------------------------------------------------------------------------

def _verdict(status, reason):
    return SimpleNamespace(status=status, reason=reason, image="x", max_error_mm=0.0,
                           tolerance_mm=0.1, circles=5)


class _Witness:
    """Check stub: confirms only with the bodies in `fitting`; records every body it is given."""

    def __init__(self, *fitting, otherwise="body_scale"):
        self.fitting, self.otherwise, self.calls = fitting, otherwise, []

    def __call__(self, pads, body):
        self.calls.append(body)
        if body in self.fitting:
            return _verdict("confirmed", "confirmed")
        return _verdict("not_confirmed", self.otherwise)


def test_the_exchanged_sizes_are_tried_only_after_a_body_scale_refusal():
    witness = _Witness((16.0, 12.0))
    holes = ext._HoleVerdicts(witness, (12.0, 16.0), body_axes_known=False)
    verdict = holes(PART)
    assert ext._holes_confirm(verdict) and verdict.body_exchanged is True
    assert witness.calls == [(12.0, 16.0), (16.0, 12.0)]
    # Memoised: asking again costs no check.
    assert holes(PART) is verdict and len(witness.calls) == 2


@pytest.mark.parametrize("reason", ["no_match", "distorted"])
def test_a_layout_refused_for_its_positions_is_not_retried(reason):
    """Negative twin: pads off their holes are wrong whatever the body's orientation."""
    witness = _Witness((16.0, 12.0), otherwise=reason)
    holes = ext._HoleVerdicts(witness, (12.0, 16.0), body_axes_known=False)
    assert holes(PART).reason == reason and witness.calls == [(12.0, 16.0)]


def test_known_axes_and_square_bodies_have_one_assignment():
    witness = _Witness((16.0, 12.0))
    known = ext._HoleVerdicts(witness, (12.0, 16.0))
    assert known(PART).reason == "body_scale" and witness.calls == [(12.0, 16.0)]
    assert known.bodies() == [(12.0, 16.0)]
    square = ext._HoleVerdicts(_Witness(), (12.0, 12.0), body_axes_known=False)
    assert square.bodies() == [(12.0, 12.0)]
    assert ext._HoleVerdicts(None, None, body_axes_known=False).bodies() == [None]


def test_a_body_that_fits_as_given_is_confirmed_without_the_mark():
    """Contract changed in round two: both orders are checked when the layout is (no lazy probe
    left in the answer); a witness that fits both measured nothing."""
    witness = _Witness((12.0, 16.0), (16.0, 12.0))
    holes = ext._HoleVerdicts(witness, (12.0, 16.0), body_axes_known=False)
    assert not getattr(holes(PART), "body_exchanged", False)
    assert witness.calls == [(12.0, 16.0), (16.0, 12.0)] and not holes.measures_as_given(PART)


def test_the_exchange_is_written_once_and_only_for_a_marked_verdict():
    holes = ext._HoleVerdicts(_Witness((16.0, 12.0)), (12.0, 16.0), body_axes_known=False)
    data = _answer(PART, (12.0, 16.0))
    ext._align_body_axes(data, holes, PART)
    ext._align_body_axes(data, holes, PART)  # a second write site must not exchange back
    assert (data["body_length_mm"], data["body_width_mm"]) == (16.0, 12.0)
    assert holes.body == (16.0, 12.0) and holes.body_axes_known
    assert data["notes"].count(EXCHANGED_NOTE) == 1
    # Negative twin: a plain confirmation leaves the answer alone.
    plain = ext._HoleVerdicts(_Witness((12.0, 16.0)), (12.0, 16.0), body_axes_known=False)
    untouched = _answer(PART, (12.0, 16.0))
    ext._align_body_axes(untouched, plain, PART)
    assert (untouched["body_length_mm"], untouched["body_width_mm"]) == (12.0, 16.0)
    assert "notes" not in untouched


def test_two_sizes_closer_than_the_checks_allowance_are_never_exchanged():
    """Audit D1: a nearly square body refused by a hair is no measure of its orientation.

    The stub fit has a tolerance of 0.1 mm, so the body check allows each
    size `body_allowance_mm(size, 0.1)`; the outline tells two sizes apart
    only when they differ by more than the sum of their two allowances.
    Ported in round two (2026-11-10): the fixed BODY_ALLOWANCE_MM became the
    proportional `body_allowance_mm(size, tolerance)`, so the limit is
    solved from that function instead of doubled from a constant.
    """
    from kicadgen.services.drawing_holes import body_allowance_mm, body_axes_distinguishable
    # Smallest second size b with b - 12 = allowance(12) + allowance(b), by bisection
    # on the module's own function (no copy of its formula here).
    low, high = 12.0, 20.0
    for _ in range(60):
        middle = (low + high) / 2
        if middle - 12.0 > body_allowance_mm(12.0, 0.1) + body_allowance_mm(middle, 0.1):
            high = middle
        else:
            low = middle
    close = (12.0, low - 0.01)    # just inside the limit: not distinguishable
    apart = (12.0, high + 0.01)   # just beyond it
    assert not body_axes_distinguishable(close, 0.1) and body_axes_distinguishable(apart, 0.1)
    assert not body_axes_distinguishable(apart, None)  # no fit, no measure
    witness = _Witness((close[1], close[0]))
    holes = ext._HoleVerdicts(witness, close, body_axes_known=False)
    assert holes(PART).reason == "body_scale" and witness.calls == [close]
    # Positive twin: the same stub with the sizes far enough apart is retried.
    witness = _Witness((apart[1], apart[0]))
    holes = ext._HoleVerdicts(witness, apart, body_axes_known=False)
    assert holes(PART).body_exchanged and len(witness.calls) == 2


def _body_reading(length, width):
    """A dedicated body reading (mm) with the two plan sizes only."""
    return {"values": {"body_length": length, "body_width": width}, "printed": [], "notes": ""}


@pytest.mark.parametrize("sources", [
    {"body_length_mm": "read", "body_width_mm": "read"},
    {"body_length_mm": "estimated", "body_width_mm": "read"},
])
def test_the_dedicated_body_reading_does_not_undo_the_measured_axes(tmp_path, sources):
    """Audit D2: the body reading that follows repeats the main answer's order (12 x 16)."""
    data, _log = _confront(tmp_path, MISREAD, (DRAWN[1], DRAWN[0]))
    data["value_sources"] = dict(sources)
    ext.apply_dedicated_body(data, _body_reading(DRAWN[1], DRAWN[0]))
    geometry = data["package_geometry"]
    assert (geometry["body_length_mm"], geometry["body_width_mm"]) == DRAWN
    assert (data["body_length_mm"], data["body_width_mm"]) == DRAWN
    assert "non coincidono" not in data["notes"] and "sostituisce la stima" not in data["notes"]
    assert "_body_axes_measured" not in data


def test_a_body_reading_that_really_differs_is_still_reported_after_the_measure(tmp_path):
    """Negative twin of the above: the pair is reordered, a size that differs stays a disagreement."""
    data, _log = _confront(tmp_path, MISREAD, (DRAWN[1], DRAWN[0]))
    ext.apply_dedicated_body(data, _body_reading(DRAWN[1], DRAWN[0] + 2.0))  # 12 x 18 against 16 x 12
    assert data["notes"].count("non coincidono") == 1 and "18.00 mm" in data["notes"]
    assert (data["body_length_mm"], data["body_width_mm"]) == DRAWN


def test_the_exchanged_verdict_can_be_copied_and_hides_no_missing_attribute():
    """Review finding 4: delegation must not recurse on an instance that was not initialised."""
    import copy
    holes = ext._HoleVerdicts(_Witness((16.0, 12.0)), (12.0, 16.0), body_axes_known=False)
    verdict = copy.deepcopy(holes(PART))
    assert verdict.body_exchanged and verdict.status == "confirmed"
    assert not hasattr(verdict, "source")


UNORDERED_NOTE = "solo nell'ordine opposto"


def test_without_a_measure_two_read_pairs_in_opposite_order_are_one_unordered_pair():
    """Review A6: nothing fixes the order, so 12 x 16 against 16 x 12 is the same pair of quotes.

    Contract changed on 2026-11-10 (it used to be two «non coincidono»): the
    order each reader gives is its inference from the elevation views, and an
    ordered comparison of two inferences is what crossed one printed number
    onto both axes. The sizes stay in the main answer's order, said unverified.
    """
    data = _answer(PART, (DRAWN[1], DRAWN[0]))
    ext.apply_dedicated_body(data, _body_reading(*DRAWN))
    assert "non coincidono" not in data["notes"] and UNORDERED_NOTE in data["notes"]
    assert "da verificare" in data["notes"]
    assert (data["body_length_mm"], data["body_width_mm"]) == (DRAWN[1], DRAWN[0])
    # Negative twin: the same pair in the same order needs no remark.
    same = _answer(PART, (DRAWN[1], DRAWN[0]))
    ext.apply_dedicated_body(same, _body_reading(DRAWN[1], DRAWN[0]))
    assert UNORDERED_NOTE not in same["notes"] and "non coincidono" not in same["notes"]
    # Negative twin: a size that really differs is still ONE disagreement, on its own axis.
    differs = _answer(PART, (DRAWN[1], DRAWN[0]))
    ext.apply_dedicated_body(differs, _body_reading(DRAWN[0], DRAWN[1] + 1.0))  # 16 x 13 against 12 x 16
    assert differs["notes"].count("non coincidono") == 1 and "13.00 mm" in differs["notes"]
    assert (differs["body_length_mm"], differs["body_width_mm"]) == (DRAWN[1], DRAWN[0])


def _estimated_width(body):
    data = _answer(PART, body)
    data["value_sources"] = {"body_length_mm": "read", "body_width_mm": "estimated"}
    return data


def test_one_printed_size_never_lands_on_both_axes():
    """Review A6: 16 read x 12 estimated against a dedicated 12 x 16 gave a 16 x 16 body."""
    data = _estimated_width((16.0, 12.0))
    ext.apply_dedicated_body(data, _body_reading(12.0, 16.0))
    assert (data["body_length_mm"], data["body_width_mm"]) == (16.0, 12.0)
    assert data["package_geometry"]["body_width_mm"] == 12.0
    assert "sostituisce la stima" not in data["notes"] and "non coincidono" not in data["notes"]
    # The estimate the printed pair agrees with is now a reading.
    assert data["value_sources"]["body_width_mm"] == "read"
    # The reading claims its printed value before the estimate does: an
    # estimate that happens to equal the other printed size is replaced.
    data = _estimated_width((16.0, 16.0))
    ext.apply_dedicated_body(data, _body_reading(12.0, 16.0))
    assert (data["body_length_mm"], data["body_width_mm"]) == (16.0, 12.0)


def test_a_printed_quote_still_replaces_an_estimate_on_its_own_axis():
    """Negative twin: same provenance, the dedicated pair in the SAME order: plain replacement."""
    data = _estimated_width((16.0, 12.0))
    ext.apply_dedicated_body(data, _body_reading(16.0, 13.0))
    assert (data["body_length_mm"], data["body_width_mm"]) == (16.0, 13.0)
    assert "sostituisce la stima" in data["notes"] and UNORDERED_NOTE not in data["notes"]


def test_a_confirmation_as_given_is_a_measure_only_when_the_other_way_is_refused():
    """Review A6 (A2): a confirmation as given orders the pair like an exchange does.

    Two READ sizes are used so the unordered rule alone would not tell the
    cases apart by the result: the note says which rule compared the pair.
    """
    MEASURED = "misurato sul contorno disegnato"

    def kept(check, known=False, reading=(12.0, 16.0)):
        holes = ext._HoleVerdicts(check, (16.0, 12.0), body_axes_known=known)
        data = _answer(PART, (16.0, 12.0))
        ext._align_body_axes(data, holes, PART)
        assert holes(PART).status == "confirmed" and "notes" not in data
        ext.apply_dedicated_body(data, _body_reading(*reading))
        assert (data["body_length_mm"], data["body_width_mm"]) == (16.0, 12.0)
        assert "_body_axes_probe" not in data and "non coincidono" not in data["notes"]
        return data["notes"]

    # Confirmed as given, «body_scale» the other way round: the outline measured the order.
    witness = _Witness((16.0, 12.0))
    assert MEASURED in kept(witness)
    # Contract changed in round two: the other assignment is tried once, when the
    # layout is checked (no lazy probe callable left in the answer), whatever
    # order the body reading then gives; a body reading in the same order needs
    # no reordering note.
    assert witness.calls == [(16.0, 12.0), (12.0, 16.0)]
    witness = _Witness((16.0, 12.0))
    assert MEASURED not in kept(witness, reading=(16.0, 12.0))
    assert witness.calls == [(16.0, 12.0), (12.0, 16.0)]
    # Negative twin: a witness that confirms either way (no outline found) measured nothing.
    notes = kept(_Witness((16.0, 12.0), (12.0, 16.0)))
    assert MEASURED not in notes and UNORDERED_NOTE in notes

    # Negative twin: a library footprint compares pads, never the body.
    def library(pads, body):
        verdict = _verdict("confirmed", "confirmed") if body == (16.0, 12.0) else _verdict(
            "not_confirmed", "body_scale")
        verdict.source = "library"
        return verdict

    notes = kept(library)
    assert MEASURED not in notes and UNORDERED_NOTE in notes
    # Negative twin: axes a plan quote already fixed are not probed.
    witness = _Witness((16.0, 12.0))
    assert MEASURED not in kept(witness, known=True) and witness.calls == [(16.0, 12.0)]


def test_the_drawn_outline_measures_a_body_given_the_right_way_round(tmp_path):
    """End to end on the drawn page: confirmed as given, then a body reading the other way round."""
    data, _log = _confront(tmp_path, MISREAD, DRAWN)
    data["value_sources"] = {"body_length_mm": "read", "body_width_mm": "estimated"}
    ext.apply_dedicated_body(data, _body_reading(DRAWN[1], DRAWN[0]))
    assert (data["body_length_mm"], data["body_width_mm"]) == DRAWN
    assert "misurato sul contorno disegnato" in data["notes"]
    assert EXCHANGED_NOTE not in data["notes"] and "sostituisce la stima" not in data["notes"]


def test_only_a_span_equal_to_its_own_size_prints_the_order_of_the_pair():
    geometry = {"body_length_mm": 12.0, "body_width_mm": 16.0}
    closed = _reading(CHAINS_CLOSED_ON_X)   # 12 mm from outline to outline along X
    assert ext._plan_prints_body_axes(geometry, 1.0, closed)
    assert ext._plan_prints_body_axes(geometry, 1.0, None, {"spans": closed["spans"]})
    # No span, a span that equals the OTHER size, a square body, a size quoted as a bound.
    assert not ext._plan_prints_body_axes(geometry, 1.0, _reading())
    assert not ext._plan_prints_body_axes({"body_length_mm": 16.0, "body_width_mm": 12.0}, 1.0, closed)
    assert not ext._plan_prints_body_axes({"body_length_mm": 12.0, "body_width_mm": 12.0}, 1.0, closed)
    assert not ext._plan_prints_body_axes(dict(geometry, body_quote_kind="max"), 1.0, closed)
    assert not ext._plan_prints_body_axes({"body_length_mm": 12.0}, 1.0, closed)


def test_a_printed_order_is_kept_against_a_body_reading_the_other_way_round(tmp_path):
    """The plan prints 12 mm along X: the dedicated 16 x 12 is compared in that order."""
    data, _log = _confront(tmp_path, MISREAD, (DRAWN[1], DRAWN[0]), CHAINS_CLOSED_ON_X)
    assert data.get("_body_axes_printed") is True
    data["value_sources"] = {"body_length_mm": "read", "body_width_mm": "estimated"}
    ext.apply_dedicated_body(data, _body_reading(*DRAWN))
    assert (data["body_length_mm"], data["body_width_mm"]) == (DRAWN[1], DRAWN[0])
    assert "fissato dalle quote da bordo a bordo" in data["notes"]
    assert "_body_axes_printed" not in data


def test_a_layout_chosen_by_the_user_takes_the_measured_axes_with_it():
    """Audit D3: two layouts the witness cannot choose between, both fitting only exchanged."""
    other = dict(PART, **{"5": (7.0, 2.0)})
    readings = [("main", other, {}, False), ("dedicated", PART, {}, False)]

    def ask(answers):
        data = _answer(other, (12.0, 16.0))
        data["_answers"] = answers
        holes = ext._HoleVerdicts(_Witness((16.0, 12.0)), (12.0, 16.0), body_axes_known=False)
        ext._block_or_ask(data, data["package_geometry"]["pins"], readings, holes, 1.0, "msg", None)
        return data

    asked = ask({})
    (key, payloads), = asked["_candidates"].items()
    assert all(payload["exchange_body"]["measured_mm"] == [16.0, 12.0] for payload in payloads.values())
    assert "scambiate" in ext._hole_text(
        ext._HoleVerdicts(_Witness((16.0, 12.0)), (12.0, 16.0), body_axes_known=False)(PART))
    # Until the answer the sizes stay as read.
    assert (asked["body_length_mm"], asked["body_width_mm"]) == (12.0, 16.0)
    chosen = next(value for value in payloads if value.startswith("dedicated"))
    answered = ask({key: chosen})
    assert (answered["body_length_mm"], answered["body_width_mm"]) == (16.0, 12.0)
    geometry = answered["package_geometry"]
    assert (geometry["body_length_mm"], geometry["body_width_mm"]) == (16.0, 12.0)
    assert answered["notes"].count(EXCHANGED_NOTE) == 1
    # Negative twin: layouts confirmed with the body as given carry no exchange.
    data = _answer(other, (12.0, 16.0))
    plain = ext._HoleVerdicts(_Witness((12.0, 16.0)), (12.0, 16.0), body_axes_known=False)
    ext._block_or_ask(data, data["package_geometry"]["pins"], readings, plain, 1.0, "msg", None)
    assert all("exchange_body" not in payload
               for payloads in data["_candidates"].values() for payload in payloads.values())


def test_an_edge_to_edge_span_is_what_fixes_the_axes():
    assert not ext._plan_fixes_body_axes(_reading(), None)
    assert ext._plan_fixes_body_axes(_reading(CHAINS_CLOSED_ON_X))
    assert ext._plan_fixes_body_axes(None, {"spans": {"x": None, "y": 4.0}})


# ---------------------------------------------------------------------------
# Round two (2026-11-10): the witness says body_check / scale_verified itself
# ---------------------------------------------------------------------------

class _BodyCheckWitness:
    """Check stub of the new contract: the pads always fit; the outline AGREES only with `agrees`."""

    def __init__(self, *agrees):
        self.agrees, self.calls = agrees, []

    def __call__(self, pads, body):
        self.calls.append(body)
        check = "agrees" if body in self.agrees else "unverifiable"
        return SimpleNamespace(status="confirmed", reason="confirmed", image="x", max_error_mm=0.0,
                               tolerance_mm=0.1, circles=5, body_check=check,
                               scale_verified=check == "agrees")


def test_the_exchange_is_measured_by_the_order_the_outline_agrees_with():
    holes = ext._HoleVerdicts(_BodyCheckWitness((16.0, 12.0)), (12.0, 16.0), body_axes_known=False)
    verdict = holes(PART)
    assert verdict.body_exchanged and verdict.scale_verified
    # Negative twin: the outline agrees with the order as given -- measured, nothing exchanged.
    given = ext._HoleVerdicts(_BodyCheckWitness((12.0, 16.0)), (12.0, 16.0), body_axes_known=False)
    assert not getattr(given(PART), "body_exchanged", False) and given.measures_as_given(PART)
    # Negative twin: an outline that agrees with neither order (none identified) measures nothing.
    neither = ext._HoleVerdicts(_BodyCheckWitness(), (12.0, 16.0), body_axes_known=False)
    assert not getattr(neither(PART), "body_exchanged", False)
    assert ext._holes_confirm(neither(PART)) and not neither.measures_as_given(PART)


def test_the_measure_as_given_leaves_a_json_able_mark_not_a_callable():
    import json
    holes = ext._HoleVerdicts(_BodyCheckWitness((16.0, 12.0)), (16.0, 12.0), body_axes_known=False)
    data = _answer(PART, (16.0, 12.0))
    ext._align_body_axes(data, holes, PART)
    assert data["_body_axes_measured"] is True and "_body_axes_probe" not in data
    json.dumps(data)
    # Negative twin: nothing measured, no mark.
    plain = ext._HoleVerdicts(_BodyCheckWitness(), (16.0, 12.0), body_axes_known=False)
    untouched = _answer(PART, (16.0, 12.0))
    ext._align_body_axes(untouched, plain, PART)
    assert "_body_axes_measured" not in untouched


def test_the_note_says_whether_the_scale_was_verified():
    agreed = _BodyCheckWitness((16.0, 12.0))(PART, (16.0, 12.0))
    shape = _BodyCheckWitness()(PART, (16.0, 12.0))
    assert "scala verificata" in ext._hole_text(agreed) and SHAPE_ONLY not in ext._hole_text(agreed)
    assert SHAPE_ONLY in ext._hole_text(shape)
    # A witness that does not say (older, library) adds nothing.
    assert "scala" not in ext._hole_text(_verdict("confirmed", "confirmed"))


@pytest.mark.parametrize("reason", ["page_skewed", "unanchored"])
def test_the_two_new_cannot_tell_reasons_have_their_own_text(reason):
    text = ext._hole_text(SimpleNamespace(status="unverifiable", reason=reason, image="p.png",
                                          max_error_mm=None, tolerance_mm=None, circles=5))
    assert "p.png" in text and "non riuscito" not in text


def test_the_holes_never_choose_between_two_readings_of_one_shape_at_two_scales():
    """A shape-only confirmation fits a uniformly scaled reading too: no choice by the holes."""
    scaled = {n: (x * 1.27, y * 1.27) for n, (x, y) in PART.items()}
    readings = [("main", scaled), ("dedicated", PART)]
    holes = ext._HoleVerdicts(_BodyCheckWitness(), (16.0, 12.0))
    assert ext._pick_by_holes(readings, holes) is None
    # Negative twin: a reading of another SHAPE is refuted by the holes, so the choice stands.

    def shape_witness(pads, body):
        fits = pads == PART
        return SimpleNamespace(status="confirmed" if fits else "not_confirmed",
                               reason="confirmed" if fits else "no_match", image="x",
                               max_error_mm=0.0, tolerance_mm=0.1, circles=5,
                               body_check="unverifiable", scale_verified=False,
                               scale_observable=True)
    other = ext._HoleVerdicts(shape_witness, (16.0, 12.0))
    assert ext._pick_by_holes([("main", MISREAD), ("dedicated", PART)], other)[0] == "dedicated"
