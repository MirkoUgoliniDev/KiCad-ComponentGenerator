"""@brief Round 6 of the extraction review (2026-11-11): what a real analysis of the user showed.

Two models read one drawing and both put a pad in the column of its
neighbours, 2 mm off the hole drawn for it; the footprint was generated on
their agreement, with a warning. And the main answer alone declared the
terminals pad by pad, wrong and «read», and the holes followed.

(a) A geometry witness that contradicts outranks two readings that agree:
    the agreed layout is not written, a layout the holes confirm is offered
    in the "pad_layout" question beside it, never adopted. A «distorted»
    verdict contradicts only on a page the other pads prove to scale: a
    page not drawn to scale still leaves two agreeing readings confirmed.
    Since round 6b the proof is the witness's own (`misplaced_pad`,
    drawing_holes._single_misplaced; the double below models it): the
    real witness on stretched pages is tested in test_review_round6b.py.
(b) A terminal read once is not a measure: «read» needs the dedicated
    reading to have transcribed the same terminal; otherwise the terminal,
    its hole and its land are «estimated».
(c) The instructions of the single through-hole type no longer tell the
    model to estimate the hole.

Every rule has a positive and a negative case that differ for a structural
reason (a page that confirms the other pads or bends for all of them, a
second transcription present or absent, the same or another model), never
for a number tuned to a part. Invented parts; no network, no AI call.
"""

from __future__ import annotations

import copy
import itertools
import sqlite3

import pytest

from kicadgen.core import tht_estimates
from kicadgen.services import component_types
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.component_types import SUPERSEDED_INSTRUCTION_DIGESTS, text_digest
from kicadgen.services.datasheet_extractor import (
    HOLES_SOURCE, MEASURED_PIXELS_SOURCE, apply_answers, apply_dedicated_pads, pads_from_chains,
    printed_terminals, read_pad_positions)
from kicadgen.services.openrouter import strict_tool

# ---------------------------------------------------------------------------
# (a) The invented drawing: an 18 x 12 mm body, five holes, pad 5 off the column of 1 and 2
# ---------------------------------------------------------------------------

#: The holes as drawn, from the outline's top-left corner (page: X right, Y down).
DRAWN = {"1": (2.0, 2.0), "2": (2.0, 10.0), "3": (14.0, 2.0), "4": (14.0, 10.0), "5": (5.0, 6.0)}
#: What BOTH readings say: pad 5 in the column of pads 1 and 2.
AGREED = dict(DRAWN, **{"5": (2.0, 6.0)})
#: A circle that is no hole (a glyph), close to where the readings put pad 5.
STRAY = (2.3, 6.0)

#: The dedicated reading's chains for AGREED, closed on the body on both axes.
CHAINS = [
    {"axis": "x", "stops": ["edge", "1,2,5", "3,4", "edge"], "values": [2.0, 12.0, 4.0]},
    {"axis": "y", "stops": ["edge", "1,3", "5", "2,4", "edge"], "values": [2.0, 4.0, 4.0, 2.0]},
]
#: The same chains with one more printed quote, attributed to NO pad: a datum line at x = 5.
CHAINS_WITH_DATUM = [
    {"axis": "x", "stops": ["edge", "1,2,5", "ref:datum", "3,4", "edge"], "values": [2.0, 3.0, 9.0, 4.0]},
    CHAINS[1],
]


class _Verdict:
    """The fields of drawing_holes.LayoutCheck the extractor reads."""

    def __init__(self, status, reason, observable, misplaced_pad=None):
        self.status, self.reason, self.image = status, reason, "drawing.png"
        self.max_error_mm, self.tolerance_mm, self.circles = 0.05, 0.15, 6
        self.scale_observable = observable
        self.misplaced_pad = misplaced_pad


class DrawnPage:
    """A double of the drawn-hole witness that fits ANY number of pads to the circles of a page.

    Like the real check it sees positions only, up to a translation, and
    says whether the pads could show the page's scale (four or more, not
    on one line). A pad within `TOLERANCE` of a circle sits on it; one
    within `BEND` is reached only by bending the fit: «distorted», as the
    real check reports a fit that an affine map explains and a similarity
    does not. `stretched` models a page NOT drawn to scale: every fit, of
    the whole layout or of a part of it, shows the stretch.
    """

    TOLERANCE, BEND = 0.15, 0.5

    def __init__(self, circles, *, stretched=False, drawing=True):
        self.circles, self.stretched, self.drawing = list(circles), stretched, drawing
        self.asked = []

    @staticmethod
    def _observable(points) -> bool:
        if len(points) < 4:
            return False
        (x0, y0), rest = points[0], points[1:]
        return any(abs((a[0] - x0) * (b[1] - y0) - (a[1] - y0) * (b[0] - x0)) > 1e-6
                   for a, b in itertools.combinations(rest, 2))

    def _fits(self, points, reach) -> bool:
        """Whether one translation puts every pad within `reach` of a circle of its own."""
        for anchor, circle in itertools.product(points, self.circles):
            dx, dy = circle[0] - anchor[0], circle[1] - anchor[1]
            free, ok = list(self.circles), True
            for x, y in points:
                near = [c for c in free if abs(c[0] - x - dx) <= reach and abs(c[1] - y - dy) <= reach]
                if not near:
                    ok = False
                    break
                free.remove(near[0])
            if ok:
                return True
        return False

    def _misplaced(self, pads):
        """The one pad without which the others fit undistorted, as drawing_holes._single_misplaced says.

        Since round 6b the real check names that pad itself (`misplaced_pad`):
        the remaining pads must fit with no stretch (`stretched` pages never
        do) and be enough to show one (four, not on one line). Two such pads,
        or none, name nothing.
        """
        if self.stretched:
            return None
        singled = []
        for number in pads:
            rest = [xy for other, xy in pads.items() if other != number]
            if self._observable(rest) and self._fits(rest, self.TOLERANCE):
                singled.append(number)
        return singled[0] if len(singled) == 1 else None

    def __call__(self, pads, body):
        points = list(pads.values())
        self.asked.append(sorted(pads))
        if not self.drawing:
            return _Verdict("unverifiable", "not_a_drawing", False)
        observable = self._observable(points)
        if self._fits(points, self.TOLERANCE):
            if self.stretched and observable:
                return _Verdict("not_confirmed", "distorted", True)
            return _Verdict("confirmed", "confirmed", observable)
        if self._fits(points, self.BEND) and observable:
            return _Verdict("not_confirmed", "distorted", True, self._misplaced(pads))
        if observable:
            return _Verdict("not_confirmed", "no_match", True)
        return _Verdict("unverifiable", "no_match_unobservable", False)


def _page(**options):
    """The invented drawing: its five holes and the stray circle."""
    return DrawnPage(list(DRAWN.values()) + [STRAY], **options)


def _main(pads=AGREED, view="bottom", origin="custom"):
    """A main answer whose explicit pads are `pads`, origin on pad 1 (the model's own frame)."""
    ox, oy = pads["1"]
    return {"drawing_unit": "mm", "contacts_view": view, "package_geometry": {
        "origin": origin, "body_length_mm": 18.0, "body_width_mm": 12.0,
        "pins": [{"number": n, "x_mm": x - ox, "y_mm": y - oy} for n, (x, y) in pads.items()]}}


def _reading(chains=CHAINS, view="bottom"):
    return dict(pads_from_chains(chains), view=view, notes="")


def _offsets(pins):
    """{number: offset from pad 1} of a pin list, rounded: comparable across origins."""
    placed = {p["number"]: (p["x_mm"], p["y_mm"]) for p in pins}
    ox, oy = placed["1"]
    return {n: (round(x - ox, 3), round(y - oy, 3)) for n, (x, y) in placed.items()}


def _layout_offsets(pads):
    ox, oy = pads["1"]
    return {n: (round(x - ox, 3), round(y - oy, 3)) for n, (x, y) in pads.items()}


def test_the_page_double_tells_the_cases_apart():
    """The double behaves as the real check did on the user's drawing (reproduced 2026-11-11)."""
    page = _page()
    assert page(AGREED, None).reason == "distorted"
    assert page(AGREED, None).misplaced_pad == "5", "round 6b: the check itself names the pad"
    assert page(DRAWN, None).status == "confirmed"
    without_5 = {n: xy for n, xy in AGREED.items() if n != "5"}
    assert page(without_5, None).status == "confirmed" and page(without_5, None).scale_observable
    without_1 = {n: xy for n, xy in AGREED.items() if n != "1"}
    assert page(without_1, None).status == "not_confirmed"


# -- the contradiction ------------------------------------------------------

def test_two_agreeing_readings_the_holes_contradict_are_not_confirmed():
    """Positive: «distorted» on a page that the other four pads prove to scale -- nothing is written."""
    data = _main()
    before = copy.deepcopy(data["package_geometry"]["pins"])
    apply_dedicated_pads(data, _reading(), check_layout=_page())
    assert "confermate dalla lettura dedicata" not in data["_numbering_note"]
    assert data["package_geometry"]["pins"] == before, "the pads stay as the main answer read them"
    # CONTRACT CHANGED (round 7, coordinator's decision of 2026-11-11): the
    # witness is imperfect, so its contradiction is never a hard block: one
    # layout and nothing the holes confirm is a ONE-option question, and the
    # text says only what was measured (no «la pagina e' in scala»).
    assert "_pin_map_conflict" not in data
    question, options = _question(data)
    assert set(options) == {"main+dedicated"}
    assert "non confermano questa disposizione" in question["text"]
    assert "il pad 5 e' il piu' lontano dai fori" in question["text"]
    assert "in scala" not in question["text"] and "le smentiscono" not in question["text"]


def test_a_page_not_drawn_to_scale_leaves_two_agreeing_readings_confirmed():
    """Negative twin, the legitimate case kept: the page bends for EVERY subset of the pads.

    Same verdict on the whole layout («distorted»); the difference is
    structural -- no four pads fit undistorted either, so nothing proves the
    page to scale and nothing singles a pad out. The readings stand with
    the warning they have had since 2026-10-31.
    """
    page = DrawnPage(list(AGREED.values()), stretched=True)
    assert page(AGREED, None).reason == "distorted"
    data = _main()
    apply_dedicated_pads(data, _reading(), check_layout=page)
    assert "_pin_map_conflict" not in data and not data.get("_questions")
    assert "confermate dalla lettura dedicata" in data["_numbering_note"]
    assert "Attenzione" in data["_numbering_note"] and "deformando" in data["_numbering_note"]


def test_agreeing_readings_the_holes_confirm_are_confirmed_as_before():
    """Negative twin: the drawing shows what the readings say."""
    data = _main(DRAWN)
    chains = [{"axis": "x", "stops": ["edge", "1,2", "5", "3,4", "edge"], "values": [2.0, 3.0, 9.0, 4.0]},
              CHAINS[1]]
    apply_dedicated_pads(data, _reading(chains), check_layout=DrawnPage(list(DRAWN.values())))
    assert "_pin_map_conflict" not in data and not data.get("_questions")
    assert "confermate dalla lettura dedicata" in data["_numbering_note"]
    assert "Attenzione" not in data["_numbering_note"]
    assert _offsets(data["package_geometry"]["pins"]) == _layout_offsets(DRAWN)


@pytest.mark.parametrize("witness", [None, _page(drawing=False)], ids=["no drawing image", "a photo"])
def test_an_unverifiable_witness_contradicts_nothing(witness):
    """Negative twin: without a drawing the holes say nothing and the agreement confirms, as before."""
    data = _main()
    apply_dedicated_pads(data, _reading(), check_layout=witness)
    assert "_pin_map_conflict" not in data and not data.get("_questions")
    assert "confermate dalla lettura dedicata" in data["_numbering_note"]


def test_three_remaining_pads_prove_no_scale():
    """Negative: with four pads, the three left without one cannot show the page's scale.

    The same misplaced pad, but the other pads are too few to be a proof
    (drawing_holes.AFFINE_MIN_PADS): «distorted» stays a warning.
    """
    drawn = {n: xy for n, xy in DRAWN.items() if n != "4"}
    agreed = dict(drawn, **{"5": AGREED["5"]})
    chains = [{"axis": "x", "stops": ["edge", "1,2,5", "3", "edge"], "values": [2.0, 12.0, 4.0]},
              {"axis": "y", "stops": ["edge", "1,3", "5", "2", "edge"], "values": [2.0, 4.0, 4.0, 2.0]}]
    page = DrawnPage(list(drawn.values()) + [STRAY])
    assert page(agreed, None).reason == "distorted"
    data = _main(agreed)
    apply_dedicated_pads(data, _reading(chains), check_layout=page)
    assert "_pin_map_conflict" not in data and "Attenzione" in data["_numbering_note"]


def test_the_rule_is_the_same_when_the_dedicated_reading_sides_with_the_answers_own_chains():
    """The other place where an agreement confirmed: the main answer's chains against its coordinates."""
    def answer():
        data = _main(dict(AGREED, **{"3": (15.0, 2.0)}))   # the model's own sums are off on pad 3
        data["_chain_candidate"] = {"pads": dict(AGREED), "centred": True,
                                    "spans": _reading()["spans"]}
        data["_pin_map_conflict"] = "Le coordinate non tornano con le catene."
        return data

    data = answer()
    apply_dedicated_pads(data, _reading(), check_layout=_page())
    # CONTRACT CHANGED (round 7): the measured wording, never «in scala».
    assert "non confermano questa disposizione" in data["_numbering_note"]
    assert "il pad 5 e' il piu' lontano dai fori" in data["_numbering_note"]
    assert "in scala" not in data["_numbering_note"]
    assert "lettura dedicata concorda con esse" not in data["_numbering_note"]
    # Negative twin: the same three readings on a page that confirms the chains.
    data = answer()
    apply_dedicated_pads(data, _reading(), check_layout=DrawnPage(list(AGREED.values())))
    assert "_pin_map_conflict" not in data
    assert "lettura dedicata concorda con esse" in data["_numbering_note"]


# -- the layout the holes confirm is offered, never adopted -------------------

def _question(data):
    question, = data["_questions"]
    assert question["field"] == "pad_layout"
    return question, {option["value"].split(":")[0]: option for option in question["options"]}


def test_an_unattributed_printed_quote_gives_the_layout_the_holes_confirm_as_a_candidate():
    """Positive: the reading transcribed a quote to a datum that is where the singled-out pad is drawn."""
    data = _main()
    before = copy.deepcopy(data["package_geometry"]["pins"])
    apply_dedicated_pads(data, _reading(CHAINS_WITH_DATUM), check_layout=_page())
    assert "_pin_map_conflict" not in data
    assert data["package_geometry"]["pins"] == before, "neither layout is adopted"
    question, options = _question(data)
    assert set(options) == {"main+dedicated", HOLES_SOURCE}
    assert "deformando" in options["main+dedicated"]["label"], "the readings' option says what the holes said"
    assert "«datum»" in options[HOLES_SOURCE]["label"] and "pad 5" in options[HOLES_SOURCE]["label"]
    payloads = data["_candidates"][question["key"]]
    offered = payloads[options[HOLES_SOURCE]["value"]]
    assert offered["source"] == HOLES_SOURCE and offered["view"] is None
    assert _offsets(offered["pins"]) == _layout_offsets(DRAWN)
    # Printed quotes, not pixels: no pad carries the pixel provenance.
    assert not any(pin.get("source") for pin in offered["pins"])
    # The readings' option is the layout the agreement used to write, body placed by the edge quotes.
    kept = payloads[options["main+dedicated"]["value"]]
    assert _offsets(kept["pins"]) == _layout_offsets(AGREED)
    assert kept["origin"] == offered["origin"] == "body_center"
    # Only the singled-out pad differs between the two options.
    differing = [a["number"] for a, b in zip(kept["pins"], offered["pins"])
                 if (a["x_mm"], a["y_mm"]) != (b["x_mm"], b["y_mm"])]
    assert differing == ["5"]


def test_a_datum_that_is_not_where_a_hole_is_drawn_gives_no_candidate():
    """Negative twin: the same unattributed quote, but no hole is drawn there -- no hole-backed option.

    CONTRACT CHANGED (round 7): what used to be the hard block is a
    question with the readings' layout alone.
    """
    chains = [{"axis": "x", "stops": ["edge", "1,2,5", "ref:datum", "3,4", "edge"],
               "values": [2.0, 7.0, 5.0, 4.0]}, CHAINS[1]]
    data = _main()
    apply_dedicated_pads(data, _reading(chains), check_layout=_page())
    assert "_pin_map_conflict" not in data
    _question_, options = _question(data)
    assert set(options) == {"main+dedicated"}, "no layout the holes confirm is offered"


def test_a_saved_answer_and_a_fresh_one_write_the_same_layout():
    """The chosen hole-backed layout is written by the one writer, during or after the extraction."""
    def extraction(answers=None):
        data = _main()
        for pin in data["package_geometry"]["pins"]:
            pin.update(pad_type="thru_hole", drill_mm=1.0, pad_length_mm=1.8, pad_width_mm=1.8,
                       shape="circle")
        data.update(layout="matrix", name="X", body_length_mm=18.0, body_width_mm=12.0,
                    body_height_mm=5.0)
        data["package_geometry"]["name"] = "X"
        if answers:
            data["_answers"] = answers
        apply_dedicated_pads(data, _reading(CHAINS_WITH_DATUM), check_layout=_page())
        ext._canonicalize_answer(data)
        return ext._params_from_tool_arguments(data)

    asked = extraction()
    question, = [q for q in asked.questions if q["field"] == "pad_layout"]
    value = next(o["value"] for o in question["options"] if o["value"].startswith(HOLES_SOURCE + ":"))
    fresh = apply_answers(asked, {question["key"]: value})
    saved = extraction({question["key"]: value})
    assert fresh.pin_map_conflict is None and saved.pin_map_conflict is None
    assert not [q for q in fresh.questions or () if q["field"] == "pad_layout"]
    assert not [q for q in saved.questions or () if q["field"] == "pad_layout"]
    for params in (fresh, saved):
        # A bottom view is mirrored by the generator, not here: the pins are as read.
        assert _offsets(params.package_geometry["pins"]) == _layout_offsets(DRAWN)
    assert ([(p["number"], p["x_mm"], p["y_mm"]) for p in fresh.package_geometry["pins"]]
            == [(p["number"], p["x_mm"], p["y_mm"]) for p in saved.package_geometry["pins"]])
    assert "disposizione confermata dai fori disegnati" in fresh.notes


def test_a_measured_layout_is_offered_with_its_pixel_provenance():
    """An observable «no_match»: the measure gives the layout, and the question carries it."""
    from types import SimpleNamespace

    def measure(pads, body):
        positions = dict(pads, **{"5": (pads["1"][0] + 3.0, pads["5"][1])})
        return SimpleNamespace(positions_mm=positions, measured_mm=positions, moved="5",
                               moved_from_mm=pads["5"], max_error_mm=0.05, tolerance_mm=0.15,
                               image="drawing.png")

    page = DrawnPage(list(DRAWN.values()))          # no stray circle: pad 5 lands on nothing
    assert page(AGREED, None).reason == "no_match"
    data = _main()
    apply_dedicated_pads(data, _reading(), check_layout=page, measure_layout=measure)
    question, options = _question(data)
    assert set(options) == {"main+dedicated", HOLES_SOURCE}
    offered = data["_candidates"][question["key"]][options[HOLES_SOURCE]["value"]]
    assert [p["number"] for p in offered["pins"] if p.get("source") == MEASURED_PIXELS_SOURCE] == ["5"]
    assert _offsets(offered["pins"]) == _layout_offsets(DRAWN)
    # Negative twin: without a measure no hole-backed layout. CONTRACT CHANGED
    # (round 7): the readings' layout is then offered alone, never blocked.
    data = _main()
    apply_dedicated_pads(data, _reading(), check_layout=page)
    assert "_pin_map_conflict" not in data
    _question_, options = _question(data)
    assert set(options) == {"main+dedicated"}


# ---------------------------------------------------------------------------
# (b) A terminal read once is not a measure
# ---------------------------------------------------------------------------

ROUND_06 = {"shape": "round", "diameter_mm": 0.6}
BLADE = {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}


def _pad(number, x, **extra):
    pad = {"number": number, "x_mm": x, "y_mm": 0.0, "pad_type": "thru_hole", "shape": "circle",
           "pad_length_mm": 2.0, "pad_width_mm": 2.0}
    pad.update(extra)
    return pad


def _answer(*pads, unit="mm", **extra):
    answer = {"drawing_unit": unit, "contacts_view": "top", "confidence": "high",
              "printed_vertical_quotes": [], "pads_origin": "pin1", "pads": list(pads)}
    answer.update(extra)
    return answer


def _params(answer, dedicated_leads=None):
    """The result of a single-part answer; `dedicated_leads` as an independent dedicated reading leaves them."""
    data = ext.single_answer_to_data(answer)
    if dedicated_leads is not None:
        data["_dedicated_leads"] = dedicated_leads
    return ext._params_from_tool_arguments(data)


def _pins(params):
    return {pin["number"]: pin for pin in params.package_geometry["pins"]}


def _two_pads(lead=ROUND_06, word="read"):
    return _answer(_pad("1", 0.0, lead=dict(lead), value_sources={"lead": word}),
                   _pad("2", 5.0, lead=dict(lead), value_sources={"lead": word}))


def _transcribed(entries, factor=1.0):
    return printed_terminals(entries, factor)


def test_a_terminal_both_readings_give_is_read_and_its_hole_derived():
    second = _transcribed([{"pads": "1,2", "shape": "round", "diameter": 0.6, "text": "Ø0.6"}])
    params = _params(_two_pads(), second)
    drill = tht_estimates.estimated_round_pad(ROUND_06).drill_mm
    for pin in _pins(params).values():
        assert pin["drill_mm"] == drill
        assert pin["sources"]["lead"] == "read" and pin["sources"]["drill_mm"] == "derived"
    assert "una sola volta" not in params.notes and "letto diversamente" not in params.notes


@pytest.mark.parametrize("second", [None, {}], ids=["no dedicated reading", "it transcribed no terminal"])
def test_a_terminal_only_the_main_answer_read_is_estimated(second):
    """Negative twin: the same answer without a second transcription -- also every saved answer."""
    params = _params(_two_pads(), second)
    drill = tht_estimates.estimated_round_pad(ROUND_06).drill_mm
    for pin in _pins(params).values():
        assert pin["drill_mm"] == drill, "the value is the same: only its provenance changes"
        assert pin["sources"] == {"drill_mm": "estimated", "pad_size": "estimated",
                                  "shape": "estimated", "lead": "estimated"}
    assert "Pad 1, 2: terminale letto una sola volta" in params.notes
    assert "1 (foro, dimensione, forma)" in params.notes, "listed among the values to verify"


def test_only_the_pad_the_second_reading_names_is_read():
    """One reading gives pad 1 only: pad 2 is the one read once."""
    second = _transcribed([{"pads": "1", "shape": "round", "diameter": 0.6}])
    pins = _pins(_params(_two_pads(), second))
    assert pins["1"]["sources"]["lead"] == "read" and pins["1"]["sources"]["drill_mm"] == "derived"
    assert pins["2"]["sources"]["lead"] == "estimated" and pins["2"]["sources"]["drill_mm"] == "estimated"


def test_two_readings_that_disagree_keep_the_main_value_as_an_estimate_and_name_both():
    second = _transcribed([{"pads": "1", "shape": "flat", "width": 1.0, "thickness": 0.4},
                           {"pads": "2", "shape": "round", "diameter": 0.8}])
    params = _params(_two_pads(), second)
    drill = tht_estimates.estimated_round_pad(ROUND_06).drill_mm
    for pin in _pins(params).values():
        assert pin["lead"] == ROUND_06 and pin["drill_mm"] == drill, "the main value, never an average"
        assert pin["sources"]["lead"] == "estimated" and pin["sources"]["drill_mm"] == "estimated"
    assert "terminale letto diversamente dalle due letture" in params.notes
    assert "pad 1: risposta principale Ø0.60 mm, lettura dedicata 1.00 x 0.40 mm" in params.notes
    assert "pad 2: risposta principale Ø0.60 mm, lettura dedicata Ø0.80 mm" in params.notes
    assert "una sola volta" not in params.notes
    assert "2 (foro, dimensione, forma)" in params.notes


def test_a_dedicated_reading_that_contradicts_itself_confirms_nothing():
    second = _transcribed([{"pads": "1", "shape": "round", "diameter": 0.6},
                           {"pads": "1", "shape": "round", "diameter": 0.8}])
    assert second == {"1": {"conflict": True}}
    params = _params(_two_pads(), second)
    assert _pins(params)["1"]["sources"]["lead"] == "estimated"
    assert "lettura dedicata due valori diversi" in params.notes


def test_a_terminal_the_model_declared_estimated_stays_so_without_being_named():
    params = _params(_two_pads(word="estimated"), None)
    assert all(pin["sources"]["lead"] == "estimated" for pin in _pins(params).values())
    assert "una sola volta" not in params.notes, "it was never passed off as read"


def test_a_blade_agrees_whichever_side_a_reading_calls_width():
    """Flat terminals compare as an unordered pair; a round one never equals a blade."""
    swapped = _transcribed([{"pads": "1,2", "shape": "flat", "width": 0.4, "thickness": 1.0}])
    assert all(pin["sources"]["lead"] == "read" for pin in _pins(_params(_two_pads(BLADE), swapped)).values())
    other = _transcribed([{"pads": "1,2", "shape": "flat", "width": 1.2, "thickness": 0.4}])
    assert all(pin["sources"]["lead"] == "estimated"
               for pin in _pins(_params(_two_pads(BLADE), other)).values())


def test_two_units_agree_within_the_coarser_rounding_only():
    """0.024 in and 0.6 mm are one terminal (the tenth of a millimetre of the coarser text); 0.5 is not."""
    inch = _answer(_pad("1", 0.0, pad_length_mm=0.08, pad_width_mm=0.08,
                        lead={"shape": "round", "diameter_mm": 0.024}, value_sources={"lead": "read"}),
                   _pad("2", 0.2, pad_length_mm=0.08, pad_width_mm=0.08,
                        lead={"shape": "round", "diameter_mm": 0.024}, value_sources={"lead": "read"}),
                   unit="inch")
    same = _transcribed([{"pads": "1,2", "shape": "round", "diameter": 0.6, "text": "Ø0.6"}])
    assert all(pin["sources"]["lead"] == "read" for pin in _pins(_params(copy.deepcopy(inch), same)).values())
    other = _transcribed([{"pads": "1,2", "shape": "round", "diameter": 0.5, "text": "Ø0.5"}])
    assert all(pin["sources"]["lead"] == "estimated"
               for pin in _pins(_params(copy.deepcopy(inch), other)).values())


def test_the_part_level_terminal_follows_the_same_rule():
    answer = _answer(_pad("1", 0.0), _pad("2", 5.0), lead_diameter_mm=0.6,
                     value_sources={"lead_diameter_mm": "read"})
    second = _transcribed([{"pads": "1,2", "shape": "round", "diameter": 0.6}])
    assert all(pin["sources"] == {"drill_mm": "derived", "pad_size": "derived", "shape": "derived",
                                  "lead": "read"}
               for pin in _pins(_params(copy.deepcopy(answer), second)).values())
    assert all(pin["sources"]["lead"] == "estimated" and pin["sources"]["drill_mm"] == "estimated"
               for pin in _pins(_params(copy.deepcopy(answer), None)).values())


def test_a_printed_hole_stays_read_beside_a_terminal_read_once():
    """The rule is about what is DERIVED from the terminal: a printed hole keeps its own provenance."""
    answer = _answer(*(_pad(n, x, drill_mm=1.3, hole_callout="2-Ø1.3", lead=dict(ROUND_06),
                            value_sources={"drill_mm": "read", "lead": "read"})
                       for n, x in (("1", 0.0), ("2", 5.0))))
    for pin in _pins(_params(answer, None)).values():
        assert pin["drill_mm"] == 1.3 and pin["sources"]["drill_mm"] == "read"
        assert pin["sources"]["lead"] == "estimated"


# -- the second transcription: schema, parsing, who may give it ---------------

def test_the_pad_reading_schema_asks_for_the_terminals_and_stays_strict():
    tool = ext._pad_positions_tool()
    terminals = tool["function"]["parameters"]["properties"]["printed_terminals"]
    assert set(terminals["items"]["properties"]) == {"pads", "shape", "diameter", "width", "thickness", "text"}
    assert terminals["items"]["properties"]["shape"]["enum"] == ["round", "flat"]
    assert "printed_terminals" not in tool["function"]["parameters"]["required"]
    assert strict_tool(tool) is not None, "every field nullable: the strict conversion stays lossless"
    assert "printed_terminals" in ext.DEFAULT_PAD_POSITIONS_PROMPT


def test_transcribed_terminals_are_converted_and_malformed_ones_left_out():
    leads = printed_terminals([
        {"pads": "1, 5", "shape": "round", "diameter": 0.024, "text": "Ø.024"},
        {"pads": "2", "shape": "flat", "width": 0.04, "thickness": 0.016},
        {"pads": "3", "shape": "flat", "width": 0.04},                  # a blade without thickness
        {"pads": "4", "shape": "square", "diameter": 0.02},             # an unknown shape
        {"pads": "", "shape": "round", "diameter": 0.02},               # no pad
        {"pads": "6", "shape": "round", "diameter": -1},
        {"pads": "7", "shape": None, "diameter": None, "width": None, "thickness": None, "text": None},
        "not an entry"], 25.4)
    assert set(leads) == {"1", "5", "2"}
    assert leads["1"]["lead"] == {"shape": "round", "diameter_mm": pytest.approx(0.6096)}
    assert leads["2"]["lead"] == {"shape": "flat", "width_mm": pytest.approx(1.016),
                                  "thickness_mm": pytest.approx(0.4064)}
    assert printed_terminals(None, 1.0) == {} and printed_terminals("x", 1.0) == {}


def test_the_dedicated_reading_returns_the_terminals_it_transcribed(tmp_path):
    from PIL import Image
    drawing = tmp_path / "drawing.png"
    Image.new("RGB", (40, 30), "white").save(drawing)

    def call(**kwargs):
        assert kwargs["tool"]["function"]["name"] == "report_pad_positions"
        return {"drawing_unit": "mm", "contacts_view": "bottom", "chains": CHAINS,
                "printed_terminals": [{"pads": "1,2", "shape": "round", "diameter": 0.6}]}

    reading = read_pad_positions([drawing], model="reader", call=call)
    assert reading["leads"]["1"]["lead"] == ROUND_06 and set(reading["leads"]) == {"1", "2"}


def _confronted(same_model, chains=CHAINS):
    """A main answer confronted with a dedicated reading that transcribed the same terminal."""
    data = _main()
    reading = _reading(chains)
    reading["leads"] = _transcribed([{"pads": "1,2,3,4,5", "shape": "round", "diameter": 0.6}])
    apply_dedicated_pads(data, reading, same_model=same_model)
    return data


def test_the_same_model_reading_twice_is_one_reading_of_the_terminal():
    """The reading is the second one only when another model made it (`same_model`)."""
    assert set(_confronted(False)["_dedicated_leads"]) == {"1", "2", "3", "4", "5"}
    assert "_dedicated_leads" not in _confronted(True)


def test_a_reading_whose_chains_contradict_the_drawing_confirms_no_terminal():
    """Negative twin: the body is 18 mm long and these chains add up to 19."""
    bad = [dict(CHAINS[0], values=[2.0, 12.0, 5.0]), CHAINS[1]]
    assert "_dedicated_leads" not in _confronted(False, bad)


# ---------------------------------------------------------------------------
# (c) The type instructions say what the system prompt says
# ---------------------------------------------------------------------------

#: The instructions of the single through-hole type until 2026-11-11, verbatim.
OLD_SINGLE_THT = (
    "TIPOLOGIA: componente SINGOLO a FORI PASSANTI (THT). E' un singolo "
    "componente discreto o connettore, con terminali che attraversano il PCB "
    "e vanno in fori metallizzati.\n"
    "- Terminali su una griglia regolare: layout \"matrix\" con rows, cols, "
    "pad_diameter_mm e drill_mm; pitch_mm solo con piu' di una colonna, "
    "row_spacing_mm solo con piu' di una riga. Se i terminali non stanno su "
    "una griglia regolare, descrivi ogni pad in package_geometry (pad_type "
    "\"thru_hole\" con il suo drill_mm).\n"
    "- Ogni pad ha un foro: drill_mm e' maggiore di zero e minore di "
    "pad_diameter_mm. Leggi le due quote dalla tabella/disegno \"Recommended "
    "PCB layout\", \"PCB hole dimensions\" o simili; se il datasheet da' solo "
    "il diametro del terminale, stima drill_mm = diametro terminale + "
    "0.2..0.3 mm e pad_diameter_mm = drill_mm + 0.7..0.9 mm: dichiara "
    "entrambi \"estimated\" in value_sources e spiega la stima nelle note.\n"
    "- pitch_mm e row_spacing_mm sono quote distinte e si leggono dalla vista "
    "dall'alto o dal basso del disegno meccanico quotato, non dalla vista "
    "laterale: dichiara in contacts_view quale vista hai letto (la "
    "specchiatura la fa il programma) e in drawing_unit l'unita' delle quote.\n"
    "- Le quote del corpo (body_*) sono l'ingombro del componente reale, "
    "non quello dei pad."
)


def test_no_type_instruction_tells_the_model_to_estimate_a_hole():
    """Every type, not only the one found: a hole that is not printed is the program's to derive."""
    for component_type in component_types.COMPONENT_TYPES:
        text = component_type.prompt
        assert "stima drill_mm" not in text and "0.2..0.3" not in text, component_type.key
        assert "diametro terminale +" not in text, component_type.key
    single = component_types.get("single_tht").prompt
    for field in ("hole_callout", "lead", "lead_diameter_mm", "value_sources", "drill_mm"):
        assert field in single
    assert "non stimarlo" in single and "li ricava il programma" in single


def test_the_old_instruction_is_what_the_test_says_it_is():
    """Negative: the checks above do reject the text that was sent with the user's analysis."""
    assert "stima drill_mm" in OLD_SINGLE_THT and "0.2..0.3" in OLD_SINGLE_THT
    assert text_digest(OLD_SINGLE_THT) in SUPERSEDED_INSTRUCTION_DIGESTS["single_tht"]
    assert (text_digest(component_types.get("single_tht").prompt)
            not in SUPERSEDED_INSTRUCTION_DIGESTS["single_tht"])


def _stored(path, instructions):
    ProfileStore(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE component_types SET instructions=? WHERE key='single_tht'", (instructions,))
    db.close()
    return ProfileStore(path).get_type("single_tht")["instructions"]


def test_a_stored_copy_of_the_old_instruction_is_replaced_and_an_edited_one_kept(tmp_path):
    current = component_types.get("single_tht").prompt
    assert _stored(tmp_path / "old.sqlite3", OLD_SINGLE_THT) == current
    edited = OLD_SINGLE_THT + "\n- I miei rele' hanno sempre fori da 1.3."
    assert _stored(tmp_path / "edited.sqlite3", edited) == edited


def test_a_project_block_generated_from_the_old_text_is_replaced_by_the_new_one():
    """What the window does when a project is opened: the generated block follows the stored type."""
    old_block = component_types.build_block(
        component_types.ComponentType("single_tht", "x", OLD_SINGLE_THT))
    notes = old_block + "\n\nNota dell'utente: rele' a 5 pin."
    refreshed = component_types.apply_block(notes, component_types.get("single_tht"))
    assert "stima drill_mm" not in refreshed and "non stimarlo" in refreshed
    assert refreshed.endswith("Nota dell'utente: rele' a 5 pin.")
