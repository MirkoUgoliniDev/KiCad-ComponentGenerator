"""@brief Round three of the extractor review (2026-11-11): findings E1-E9 and G1-G6 as classes.

Each rule has a positive and a negative case that differ for a structural
reason (a terminal declared or not, a slot on an elongated or a square pad,
pads that touch or not, quotes printed as bounds or as nominal, layouts that
are mirror images or not), never for a number tuned to a part. Invented
parts; no network, no AI call.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from kicadgen.core import tht_estimates
from kicadgen.core.package_geometry import package_from_mapping
from kicadgen.generators.pin_header import MIN_PAD_GAP_MM
from kicadgen.services import datasheet_extractor as ext


def _pad(number, x, y=0.0, **extra):
    """One through-hole pad of the single-part schema, 1.8 mm round unless `extra` says otherwise."""
    pad = {"number": number, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
           "pad_length_mm": 1.8, "pad_width_mm": 1.8, "shape": "circle"}
    pad.update(extra)
    return pad


def _answer(*pads, unit="mm", value_sources=None, **extra):
    """A single-part answer with the given pads, origin on pad 1."""
    answer = {"drawing_unit": unit, "contacts_view": "top", "confidence": "high",
              "printed_vertical_quotes": [], "pads_origin": "pin1", "pads": list(pads)}
    if value_sources is not None:
        answer["value_sources"] = value_sources
    answer.update(extra)
    return answer


def _params(answer, dedicated_holes=None):
    """The validated result of a single-part answer; `dedicated_holes` as apply_dedicated_pads leaves them."""
    data = ext.single_answer_to_data(answer)
    if dedicated_holes is not None:
        data["_dedicated_holes"] = dedicated_holes
    return ext._params_from_tool_arguments(data)


def _pins(params):
    """{number: pin dict} of the result's explicit geometry."""
    return {pin["number"]: pin for pin in params.package_geometry["pins"]}


# ---------------------------------------------------------------------------
# E1: no printed hole, no per-pad terminal
# ---------------------------------------------------------------------------

def test_the_part_lead_derives_the_hole_of_pads_without_hole_or_terminal():
    params = _params(_answer(_pad("1", 0.0), _pad("2", 2.54), lead_diameter_mm=0.6,
                             value_sources={"lead_diameter_mm": "read"}))
    drill, land = tht_estimates.estimated_round_pad({"shape": "round", "diameter_mm": 0.6})
    for pin in _pins(params).values():
        assert (pin["drill_mm"], pin["pad_length_mm"]) == (drill, land)
        assert pin["lead"] == {"shape": "round", "diameter_mm": 0.6}
        assert pin["sources"] == {"drill_mm": "derived", "pad_size": "derived", "shape": "derived",
                                  "lead": "read"}
    assert "terminale dichiarato per il componente" in params.notes
    assert "ne' foro stampato ne' terminale" not in params.notes
    assert params.unread_pads is None


def test_without_hole_or_terminal_the_pads_are_unread_geometry_not_an_error():
    """Negative twin: no part lead either -- nothing to derive, nothing invented, no exception."""
    params = _params(_answer(_pad("1", 0.0), _pad("2", 2.54)))
    assert params.package_geometry is None
    assert params.unread_pads == ["1", "2"] and params.footprint_pads_dropped()
    assert "foro non e' letto" in params.pin_map_conflict
    assert [pin.get("drill_mm") for pin in params.unread_pad_geometry["pins"]] == [None, None]
    # The note says the hole was not read; it does not claim positions were missing.
    assert "il disegno non stampa il foro" in params.notes
    assert "posizioni non lette" not in params.notes


def test_a_grid_without_drill_follows_the_same_rule():
    grid = {"rows": 1, "cols": 3, "pitch_mm": 2.54, "pad_diameter_mm": 1.8}
    with_lead = _params({"drawing_unit": "mm", "contacts_view": "top", "grid": grid,
                         "lead_diameter_mm": 0.6, "value_sources": {"lead_diameter_mm": "read"}})
    assert {pin["drill_mm"] for pin in with_lead.package_geometry["pins"]} == {0.85}
    without = _params({"drawing_unit": "mm", "contacts_view": "top", "grid": grid})
    assert without.package_geometry is None and without.unread_pads == ["1", "2", "3"]


def test_the_part_lead_is_not_spread_over_pads_with_different_model_holes():
    """With one drill per pad kind the one part-level lead may belong to some pads only."""
    uniform = _params(_answer(_pad("1", 0.0, drill_mm=1.0), _pad("2", 5.0, drill_mm=1.0),
                              lead_diameter_mm=0.6, value_sources={"lead_diameter_mm": "read"}))
    assert {pin["drill_mm"] for pin in _pins(uniform).values()} == {0.85}
    mixed = _params(_answer(_pad("1", 0.0, drill_mm=1.0), _pad("2", 5.0, drill_mm=1.4),
                            lead_diameter_mm=0.6, value_sources={"lead_diameter_mm": "read"}))
    assert [pin["drill_mm"] for pin in _pins(mixed).values()] == [1.0, 1.4]
    assert all(pin["sources"]["drill_mm"] == "estimated" for pin in _pins(mixed).values())
    assert "non e' applicato perche' i pad hanno fori diversi" in mixed.notes
    # The note never claims that no terminal was declared when the part declares one.
    assert "ne' foro stampato ne' terminale" not in mixed.notes


def test_a_chosen_layout_does_not_restore_pads_without_a_hole():
    params = _params(_answer(_pad("1", 0.0), _pad("2", 2.54)))
    params.questions = [{"key": "pad_layout:a|b", "field": "pad_layout", "text": "?",
                         "options": [{"label": "a", "value": "a"}]}]
    params.candidates = {"pad_layout:a|b": {"a": {"pins": [{"number": "1", "x_mm": 0, "y_mm": 0},
                                                           {"number": "2", "x_mm": 2.54, "y_mm": 0}]}}}
    chosen = ext.apply_answers(params, {"pad_layout:a|b": "a"})
    assert chosen.package_geometry is None and len(chosen.questions) == 1


# ---------------------------------------------------------------------------
# E2: a slot read only by the dedicated reading
# ---------------------------------------------------------------------------

SLOT = {"drill_mm": 0.8, "drill_length_mm": 2.0, "text": "2-0.8x2.0", "rounding_mm": 0.05}


@pytest.mark.parametrize("length, width, expected", [(3.0, 1.5, (2.0, 0.8)), (1.5, 3.0, (0.8, 2.0))])
def test_a_dedicated_slot_is_laid_along_the_longer_side_of_its_pad(length, width, expected):
    params = _params(_answer(_pad("1", 0.0, pad_length_mm=length, pad_width_mm=width, shape="oval"),
                             _pad("2", 6.0, pad_length_mm=length, pad_width_mm=width, shape="oval")),
                     {"1": SLOT, "2": SLOT})
    for pin in _pins(params).values():
        assert (pin["drill_mm"], pin["drill_length_mm"]) == expected
        assert pin["sources"]["drill_mm"] == "read"


def test_a_dedicated_slot_on_a_square_pad_is_not_written():
    """Negative twin: equal pad sides give no orientation; the slot is flagged, never guessed."""
    params = _params(_answer(_pad("1", 0.0, pad_length_mm=2.5, pad_width_mm=2.5, shape="rect"),
                             _pad("2", 6.0, pad_length_mm=2.5, pad_width_mm=2.5, shape="rect"),
                             lead_diameter_mm=0.6, value_sources={"lead_diameter_mm": "read"}),
                     {"1": SLOT, "2": SLOT})
    assert all("drill_length_mm" not in pin for pin in _pins(params).values())
    assert "orientamento" in params.notes and "Asola non usata" in params.notes


# ---------------------------------------------------------------------------
# E3: a printed round hole keeps a shape the pad says was read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape_word, expected", [("read", "rect"), ("estimated", "circle")])
def test_a_derived_land_keeps_a_read_shape_and_derives_only_an_unread_one(shape_word, expected):
    params = _params(_answer(_pad("1", 0.0, shape="rect", drill_mm=1.0,
                                  value_sources={"drill_mm": "read", "shape": shape_word}),
                             _pad("2", 5.0, drill_mm=1.0, value_sources={"drill_mm": "read"})))
    pin = _pins(params)["1"]
    assert pin["shape"] == expected
    assert pin["pad_length_mm"] == pin["pad_width_mm"] == tht_estimates.estimated_pad_diameter(1.0)
    assert pin["sources"]["shape"] == ("read" if shape_word == "read" else "derived")


# ---------------------------------------------------------------------------
# G1: a derived land never makes copper overlap
# ---------------------------------------------------------------------------

def _row(pitch, hole, land, count=3):
    """Pads in one row with a PRINTED round hole and the model's (unprinted) land."""
    return _answer(*[_pad(str(i + 1), i * pitch, drill_mm=hole, pad_length_mm=land, pad_width_mm=land,
                          value_sources={"drill_mm": "read"}) for i in range(count)])


def _gaps(params):
    pins = sorted(_pins(params).values(), key=lambda pin: pin["x_mm"])
    return [b["x_mm"] - a["x_mm"] - (a["pad_length_mm"] + b["pad_length_mm"]) / 2
            for a, b in zip(pins, pins[1:])]


def test_a_derived_land_too_wide_for_the_pitch_keeps_the_models_land_when_it_fits():
    params = _params(_row(1.27, 0.65, 1.0))
    assert {pin["pad_length_mm"] for pin in _pins(params).values()} == {1.0}
    assert all(pin["sources"]["pad_size"] == "estimated" for pin in _pins(params).values())
    assert min(_gaps(params)) >= MIN_PAD_GAP_MM - 1e-9
    assert "tenuta quella della risposta" in params.notes


@pytest.mark.parametrize("pitch, hole, model", [(1.27, 0.65, 1.25), (2.0, 1.1, 1.95)])
def test_a_derived_land_is_capped_to_the_room_when_the_models_land_does_not_fit(pitch, hole, model):
    params = _params(_row(pitch, hole, model))
    pins = _pins(params)
    assert all(pin["pad_length_mm"] < tht_estimates.estimated_pad_diameter(hole) for pin in pins.values())
    assert all(pin["pad_length_mm"] > hole for pin in pins.values())
    assert min(_gaps(params)) == pytest.approx(MIN_PAD_GAP_MM, abs=1e-5)
    assert all(pin["sources"]["pad_size"] == "estimated" for pin in pins.values())
    assert "ridotta per lasciare" in params.notes
    package_from_mapping(dict(params.package_geometry))


@pytest.mark.parametrize("pitch, hole", [(2.54, 0.65), (2.0, 1.0)])
def test_a_derived_land_that_clears_its_neighbours_is_left_as_derived(pitch, hole):
    """Negative twin: wide pitch (and the 2.0 / Ø1.0 case at exactly the clearance)."""
    params = _params(_row(pitch, hole, 1.0))
    expected = tht_estimates.estimated_pad_diameter(hole)
    assert {pin["pad_length_mm"] for pin in _pins(params).values()} == {expected}
    assert all(pin["sources"]["pad_size"] == "derived" for pin in _pins(params).values())
    assert "ridotta per lasciare" not in params.notes and "tenuta quella" not in params.notes


# ---------------------------------------------------------------------------
# G2: body quotes printed as bounds only bound the drawn outline
# ---------------------------------------------------------------------------

class _ScaleWitness:
    """Refuses any layout checked WITH a body as «body_scale»; confirms it on the pads alone."""

    def __call__(self, pads, body):
        if body is None:
            return SimpleNamespace(status="confirmed", reason="confirmed", body_check="none",
                                   tolerance_mm=0.1, circles=len(pads), image="x", max_error_mm=0.0)
        return SimpleNamespace(status="not_confirmed", reason="body_scale", body_check="contradicted",
                               tolerance_mm=0.1, circles=len(pads), image="x", max_error_mm=0.0)


PADS = {"1": (0.0, 0.0), "2": (5.0, 0.0), "3": (5.0, 3.0)}


def test_a_body_printed_as_maximum_is_never_contradicted_by_the_outline():
    holes = ext._HoleVerdicts(_ScaleWitness(), (10.0, 10.0), body_bound=True)
    assert ext._holes_confirm(holes(PADS))
    # Negative twin: a nominal body is still contradicted by an outline of another size.
    nominal = ext._HoleVerdicts(_ScaleWitness(), (10.0, 10.0))
    assert nominal(PADS).reason == "body_scale"


@pytest.mark.parametrize("kind, bound", [("max", True), ("min", True), ("nominal", False), (None, False)])
def test_the_bound_follows_the_printed_qualifier(kind, bound):
    geometry = {} if kind is None else {"body_quote_kind": kind}
    assert ext._body_quotes_bound(geometry) is bound


# ---------------------------------------------------------------------------
# G3: an answer-level «read» drill is not spread over different holes
# ---------------------------------------------------------------------------

def test_an_answer_level_read_drill_covers_pads_with_one_drill():
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.0), _pad("2", 5.0, drill_mm=1.0),
                             value_sources={"drill_mm": "read"}))
    assert {pin["sources"]["drill_mm"] for pin in _pins(params).values()} == {"read"}


def test_an_answer_level_read_drill_does_not_cover_mixed_holes():
    """Negative twin: two drills -- one answer-level word cannot say which one was printed."""
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.0), _pad("2", 5.0, drill_mm=1.3),
                             value_sources={"drill_mm": "read"}))
    assert {pin["sources"]["drill_mm"] for pin in _pins(params).values()} == {"estimated"}
    assert "fori diversi" in params.notes
    # A hole the dedicated reading transcribed keeps its «read».
    printed = {"2": {"drill_mm": 1.3, "drill_length_mm": None, "text": "Ø1.3", "rounding_mm": 0.05}}
    confirmed = _params(_answer(_pad("1", 0.0, drill_mm=1.0), _pad("2", 5.0, drill_mm=1.3),
                                value_sources={"drill_mm": "read"}), printed)
    assert _pins(confirmed)["2"]["sources"]["drill_mm"] == "read"
    assert _pins(confirmed)["1"]["sources"]["drill_mm"] == "estimated"


# ---------------------------------------------------------------------------
# G4: one estimate rule for the part-level lead
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lead", [0.6, 0.62])
def test_lifting_a_drill_above_the_lead_uses_the_one_estimate(lead):
    params = ext._params_from_tool_arguments({
        "layout": "matrix", "rows": 1, "cols": 2, "pitch_mm": 2.54, "drill_mm": 0.5,
        "pad_diameter_mm": 1.0, "lead_diameter_mm": lead, "drawing_unit": "mm",
        "value_sources": {"drill_mm": "estimated", "lead_diameter_mm": "read"}})
    expected = tht_estimates.estimated_hole({"shape": "round", "diameter_mm": lead})
    assert params.drill_mm == expected
    assert params.pad_diameter_mm == tht_estimates.estimated_pad_diameter(expected)


# ---------------------------------------------------------------------------
# G5: a round estimated hole too loose on a blade
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lead, loose", [
    ({"shape": "flat", "width_mm": 6.3, "thickness_mm": 0.8}, True),
    ({"shape": "flat", "width_mm": 0.64, "thickness_mm": 0.64}, False),
    ({"shape": "round", "diameter_mm": 1.0}, False),
])
def test_an_estimated_round_hole_for_a_wide_blade_asks_for_a_slot(lead, loose):
    params = _params(_answer(_pad("1", 0.0, lead=lead, value_sources={"lead": "read"}),
                             _pad("2", 12.0, lead=lead, value_sources={"lead": "read"})))
    assert ("Serve un'asola stampata" in params.notes) is loose


# ---------------------------------------------------------------------------
# G6: two transcriptions of one printed hole
# ---------------------------------------------------------------------------

def test_two_transcriptions_agree_within_the_last_printed_digit():
    inch = ext.printed_holes([{"pads": "1", "diameter": 0.040, "text": ".040"}], 25.4)["1"]
    assert ext._same_printed_hole(1.02, None, inch, 0.005)
    millimetre = {"drill_mm": 1.3, "drill_length_mm": None, "rounding_mm": 0.05}
    assert not ext._same_printed_hole(1.2, None, millimetre, 0.05)


def test_a_printed_tolerance_is_the_nominal_hole():
    holes = ext.printed_holes([{"pads": "1", "diameter": 1.4, "text": "Ø1.3+0.1"}], 1.0)
    assert holes["1"]["drill_mm"] == pytest.approx(1.3)
    params = _params(_answer(_pad("1", 0.0, drill_mm=1.4, hole_callout="Ø1.3+0.1",
                                  value_sources={"drill_mm": "read"}),
                             _pad("2", 5.0, drill_mm=1.4, hole_callout="Ø1.3+0.1",
                                  value_sources={"drill_mm": "read"})))
    assert [pin["drill_mm"] for pin in _pins(params).values()] == pytest.approx([1.3, 1.3])
    assert "valore nominale" in params.notes
    # Negative twin: a callout that prints the value itself keeps it.
    plain = _params(_answer(_pad("1", 0.0, drill_mm=1.4, hole_callout="Ø1.4",
                                 value_sources={"drill_mm": "read"}),
                            _pad("2", 5.0, drill_mm=1.4, value_sources={"drill_mm": "read"})))
    assert {pin["drill_mm"] for pin in _pins(plain).values()} == {1.4}


def test_the_count_before_a_size_is_not_its_precision():
    """«2-0.8x2.0»: the slot length 2.0 is printed to a tenth, the leading «2» is a count."""
    hole = ext.printed_holes([{"pads": "1", "diameter": 0.8, "slot_length": 2.0,
                               "text": "2-0.8x2.0"}], 1.0)["1"]
    assert hole["rounding_mm"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# E4: a replaced pad list keeps terminal, slot and provenance
# ---------------------------------------------------------------------------

def test_uniform_pad_fields_carry_terminal_slot_and_provenance():
    lead = {"shape": "round", "diameter_mm": 0.6}
    pins = [{"pad_type": "thru_hole", "drill_mm": 1.0, "drill_length_mm": 2.0, "lead": dict(lead),
             "value_sources": {"drill_mm": "read", "lead": "read"}} for _ in range(2)]
    uniform, differing = ext._uniform_pad_fields(pins)
    assert not differing
    assert uniform["lead"] == lead and uniform["drill_length_mm"] == 2.0
    assert uniform["value_sources"] == {"drill_mm": "read", "lead": "read"}
    # Negative twin: different terminals block, a provenance word not shared becomes «estimated».
    pins[1]["lead"] = {"shape": "round", "diameter_mm": 0.8}
    pins[1]["value_sources"] = {"drill_mm": "estimated"}
    uniform, differing = ext._uniform_pad_fields(pins)
    assert differing == ["lead"]
    assert uniform["value_sources"] == {"drill_mm": "estimated", "lead": "estimated"}


# ---------------------------------------------------------------------------
# E5: a merged re-read imports only the notes of its scope
# ---------------------------------------------------------------------------

def test_a_merged_reread_imports_only_its_scope_notes():
    settled = _params(_answer(_pad("1", 0.0, drill_mm=1.0), _pad("2", 5.0, drill_mm=1.0)))
    marked = dataclasses.replace(settled, notes="SCOPE\nOUTSIDE", partial_reread={
        "field": "body_height_mm", "body_axes_measured": False, "notes": ["SCOPE"]})
    merged = ext.merge_reread(settled, marked)
    assert "SCOPE" in merged.notes and "OUTSIDE" not in merged.notes
    # Negative twin: a partial result that names no scope notes (older caller) gives all of them.
    legacy = dataclasses.replace(marked, partial_reread={"field": "body_height_mm",
                                                         "body_axes_measured": False})
    assert "OUTSIDE" in ext.merge_reread(settled, legacy).notes


# ---------------------------------------------------------------------------
# E6: saved view answers for mirror-image layouts
# ---------------------------------------------------------------------------

ASYM = {"1": (0.0, 0.0), "2": (5.0, 0.0), "3": (5.0, 3.0), "4": (1.0, 3.0)}


def _view_key(pads):
    data = {"drawing_unit": "mm", "contacts_view": "unknown",
            "package_geometry": {"pins": [{"number": n, "x_mm": x, "y_mm": y}
                                          for n, (x, y) in pads.items()]}}
    ext._require_declared_view(data)
    (question,) = data["_questions"]
    return question["key"]


def test_mirror_layouts_answered_with_opposite_sides_are_consistent():
    key = _view_key(ASYM)
    mirror = _view_key({n: (-x, y) for n, (x, y) in ASYM.items()})
    assert key != mirror
    assert not ext.answers_contradict(key, "top", mirror, "bottom")
    assert ext.answers_contradict(key, "top", mirror, "top")


def test_layouts_that_are_not_mirror_images_keep_the_one_side_rule():
    """Negative twin: another layout of the same pads -- two sides cannot both be true."""
    key = _view_key(ASYM)
    other = _view_key({**ASYM, "4": (2.0, 3.0)})
    assert ext.answers_contradict(key, "top", other, "bottom")
    assert not ext.answers_contradict(key, "top", other, "top")
    # A key saved before the mirror digests existed cannot say: the one-side rule.
    legacy = key.rpartition(ext._MIRROR_MARK)[0]
    mirror_legacy = _view_key({n: (-x, y) for n, (x, y) in ASYM.items()}).rpartition(ext._MIRROR_MARK)[0]
    assert ext.answers_contradict(legacy, "top", mirror_legacy, "bottom")


def test_an_answer_saved_under_the_key_without_mirror_digests_still_applies():
    key = _view_key(ASYM)
    legacy = key.rpartition(ext._MIRROR_MARK)[0]
    data = {"drawing_unit": "mm", "contacts_view": "unknown", "_answers": {legacy: "bottom"},
            "package_geometry": {"pins": [{"number": n, "x_mm": x, "y_mm": y}
                                          for n, (x, y) in ASYM.items()]}}
    ext._require_declared_view(data)
    assert not data.get("_questions") and data["contacts_view"] == "bottom"


# ---------------------------------------------------------------------------
# E7, E9: the exchanged-body probe and the verdict cache
# ---------------------------------------------------------------------------

class _ProbeWitness:
    """Confirms the body as given (outline agreeing); the exchanged probe raises or confirms."""

    def __init__(self, given, probe_raises):
        self.given, self.probe_raises = given, probe_raises

    def __call__(self, pads, body):
        if body != self.given and self.probe_raises:
            raise RuntimeError("probe failed")
        check = "agrees" if body == self.given else "unverifiable"
        return SimpleNamespace(status="confirmed", reason="confirmed", image="x", max_error_mm=0.0,
                               tolerance_mm=0.1, circles=len(pads), body_check=check,
                               scale_verified=check == "agrees")


@pytest.mark.parametrize("raises", [True, False])
def test_a_failed_exchange_probe_keeps_the_first_verdict(raises):
    holes = ext._HoleVerdicts(_ProbeWitness((12.0, 16.0), raises), (12.0, 16.0), body_axes_known=False)
    verdict = holes(PADS)
    assert ext._holes_confirm(verdict) and not getattr(verdict, "body_exchanged", False)
    # Only a probe that ran measures the order as given.
    assert holes.measures_as_given(PADS) is (not raises)


def test_the_verdict_cache_is_cleared_when_the_body_is_exchanged():
    class Witness:
        def __call__(self, pads, body):
            check = "agrees" if body == (16.0, 12.0) else "unverifiable"
            return SimpleNamespace(status="confirmed", reason="confirmed", image="x", max_error_mm=0.0,
                                   tolerance_mm=0.1, circles=3, body_check=check,
                                   scale_verified=check == "agrees")

    holes = ext._HoleVerdicts(Witness(), (12.0, 16.0), body_axes_known=False)
    assert holes(PADS).body_exchanged
    data = {"body_length_mm": 12.0, "body_width_mm": 16.0, "package_geometry": {}}
    ext._align_body_axes(data, holes, PADS)
    assert data["body_length_mm"] == 16.0 and not holes.cache
    # After the exchange the witness is asked again with the measured body: no stale wrapper.
    assert not getattr(holes(PADS), "body_exchanged", False) and holes(PADS).body_check == "agrees"


# ---------------------------------------------------------------------------
# E8: printed holes of a dedicated reading the drawing contradicts
# ---------------------------------------------------------------------------

def _pad_data():
    return {"drawing_unit": "mm", "contacts_view": "top",
            "package_geometry": {"origin": "custom", "pins": [
                {"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole", "pad_length_mm": 1.8,
                 "pad_width_mm": 1.8} for n, (x, y) in PADS.items()]}}


@pytest.mark.parametrize("problems", [["chains do not add up"], []])
def test_holes_of_a_contradicted_dedicated_reading_are_not_taken(problems):
    data = _pad_data()
    reading = {"pads": dict(PADS), "view": "top", "spans": {}, "problems": problems,
               "holes": {"1": {"drill_mm": 1.0, "drill_length_mm": None, "text": "Ø1", "rounding_mm": 0.5}}}
    ext.apply_dedicated_pads(data, reading)
    assert ("_dedicated_holes" in data) is (not problems)
    assert ("lettura dedicata non usati" in data.get("notes", "")) is bool(problems)
