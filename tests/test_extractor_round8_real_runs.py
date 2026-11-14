"""@brief Fixes from the real runs with gpt-5.6-sol as main reader (2026-11-13, round 8).

- TEL: two readings that agree but come from one model, no witness -> ONE
  recommended confirmation question, never a hard block, never adopted.
- T7N: a saved «contacts_view» answer applies to a re-reading off by a few
  tenths of a millimetre (same pads, same counter-clockwise order), never
  to the mirror image.
- ESP32: no row spacing given -> one «row_spacing» question among the
  printed quotes across the rows, plus «non so: stima».
- Photo: one «contact_style» question per edge the photo measured but did
  not settle; a declared half_hole never stands against a seen through hole.
Offline: no network, no paid call.
"""
from types import SimpleNamespace

import pytest

from kicadgen.services import datasheet_extractor as ext
from test_review_round7 import _Verdict, _no_match

LAYOUT = {"1": (0.0, 0.0), "2": (2.54, 0.0), "3": (0.0, 10.16)}


def _pins(layout=LAYOUT):
    return [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in layout.items()]


def _questions(data, field):
    return [q for q in data.get("_questions") or () if q["field"] == field]


# --- (1) agreed readings of one model ------------------------------------------------------------

def test_agreeing_readings_of_one_model_are_asked_as_one_recommended_layout():
    """Positive (TEL): no second complete layout, no witness -> one confirmation, flagged."""
    data = {"package_geometry": {"origin": "custom", "pins": _pins()}}
    ext._block_or_ask(data, data["package_geometry"]["pins"], [("dedicated", dict(LAYOUT), {}, False)],
                      ext._HoleVerdicts(None, None), 1.0, "msg", None, agreed=True)
    (question,) = _questions(data, "pad_layout")
    assert len(question["options"]) == 1 and question["options"][0].get("recommended") is True
    assert not data.get("_pin_map_conflict"), "a question, never a hard block"
    assert data["package_geometry"]["pins"] == _pins(), "never adopted by itself"


def test_without_agreement_one_layout_still_blocks():
    """Negative: the same single layout of DISAGREEING readings keeps the block (no choice)."""
    data = {"package_geometry": {"origin": "custom", "pins": _pins()}}
    ext._block_or_ask(data, data["package_geometry"]["pins"], [("dedicated", dict(LAYOUT), {}, False)],
                      ext._HoleVerdicts(None, None), 1.0, "msg", None)
    assert data.get("_pin_map_conflict") and not _questions(data, "pad_layout")


def test_an_agreed_layout_the_holes_refute_is_not_offered():
    """Negative: no valid option -> the block stays."""
    data = {"package_geometry": {"origin": "custom", "pins": _pins()}}
    ext._block_or_ask(data, data["package_geometry"]["pins"], [("dedicated", dict(LAYOUT), {}, False)],
                      ext._HoleVerdicts(_no_match(0.9), None), 1.0, "msg", None, agreed=True)
    assert data.get("_pin_map_conflict") and not _questions(data, "pad_layout")


# --- (2) contacts_view by layout shape -----------------------------------------------------------

RELAY = {"A1": (0.0, 0.0), "A2": (0.0, 7.6), "11": (2.0, 3.8), "12": (14.2, 0.0), "14": (14.2, 7.6)}


def _view_data(layout, answers=None):
    data = {"drawing_unit": "mm", "contacts_view": "unknown",
            "package_geometry": {"pins": [{"number": n, "x_mm": x, "y_mm": y}
                                          for n, (x, y) in layout.items()]}}
    if answers is not None:
        data["_answers"] = dict(answers)
    return data


def _first_key():
    data = _view_data(RELAY)
    ext._require_declared_view(data)
    (question,) = _questions(data, "contacts_view")
    return question["key"]


def test_the_shape_is_stable_and_reverses_for_the_mirror():
    shape = ext._view_shape(list(RELAY.items()), 0.1)
    moved = ext._view_shape([(n, (x + 0.2, y - 0.2 * (n == "11"))) for n, (x, y) in RELAY.items()], 0.1)
    mirror = ext._view_shape([(n, (-x, y)) for n, (x, y) in RELAY.items()], 0.1)
    assert shape and shape == moved
    assert mirror == ext._mirrored_shape(shape) != shape


@pytest.mark.parametrize("pads", [
    [("1", (0.0, 0.0)), ("2", (1.0, 0.0))],                         # two pads: no chirality
    [("1", (0.0, 0.0)), ("2", (1.0, 0.0)), ("3", (0.5, 0.05))],      # a pad on the centroid line
])
def test_an_unstable_shape_is_none(pads):
    assert ext._view_shape(pads, 0.1) is None


def test_a_saved_side_applies_to_a_re_reading_off_by_a_few_tenths():
    """Positive (T7N): the layout digest changed, the pads and their order did not."""
    key = _first_key()
    assert "~shape:" in key
    moved = {n: (x + 0.2 if n == "11" else x, y + 0.15) for n, (x, y) in RELAY.items()}
    data = _view_data(moved, {key: "bottom"})
    ext._require_declared_view(data)
    assert not _questions(data, "contacts_view")
    assert data["contacts_view"] == "bottom" or data["package_geometry"].get("source_view") == "bottom"


def test_a_saved_side_never_applies_to_the_mirror_image():
    """Negative: the same pads read from the other face -> asked again."""
    key = _first_key()
    mirror = {n: (-x, y) for n, (x, y) in RELAY.items()}
    data = _view_data(mirror, {key: "bottom"})
    ext._require_declared_view(data)
    assert _questions(data, "contacts_view")


def test_an_old_key_without_shape_still_applies_to_the_same_layout():
    """Backward compatibility: the key of before (digests only) answers the same layout."""
    key = _first_key()
    legacy = key.partition("~shape:")[0]
    data = _view_data(RELAY, {legacy: "top"})
    ext._require_declared_view(data)
    assert not _questions(data, "contacts_view")


# --- (3) row spacing among printed quotes --------------------------------------------------------

def _rows_data(quotes, style="half_and_through", spacing=None, source="read", user=False):
    data = {"layout": "castellated", "body_length_mm": 63.3, "body_width_mm": 25.4,
            "castellated_sides": [{"edge": e, "count": 22, "pitch_mm": 2.54, "contact_style": style}
                                  for e in ("top", "bottom")],
            "_row_quotes_mm": quotes}
    if spacing is not None:
        data["row_spacing_mm"] = spacing
        data["value_sources"] = {"row_spacing_mm": source}
        if user:
            data["_program_sources"] = {"row_spacing_mm": ext.USER_SOURCE}
    return data


QUOTES = [(22.86, "contact_rows"), (21.8, "mounting_holes"), (25.4, "board")]


def test_missing_row_spacing_is_asked_among_the_printed_quotes():
    data = _rows_data(QUOTES)
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    values = [option["value"] for option in question["options"]]
    # The outline (25.40) cannot be the distance between contact centres.
    assert values == ["22.86", "21.80", ext.ROW_SPACING_ESTIMATE]
    assert not any(option.get("recommended") for option in question["options"])


def test_a_saved_row_spacing_is_written_as_the_users():
    first = _rows_data(QUOTES)
    ext._ask_row_spacing(first)
    key = _questions(first, "row_spacing")[0]["key"]
    data = _rows_data(QUOTES)
    data["_answers"] = {key: "22.86"}
    ext._ask_row_spacing(data)
    assert data["row_spacing_mm"] == 22.86
    assert data["_program_sources"]["row_spacing_mm"] == ext.USER_SOURCE


@pytest.mark.parametrize("data", [
    _rows_data(QUOTES, style="half_hole"),                          # no through-hole anywhere
    _rows_data(QUOTES, spacing=22.86, user=True),                   # chosen by the user
    _rows_data([(22.86, "contact_rows"), (25.4, "board")], spacing=22.86),  # the only possible
    _rows_data([(25.4, "board")]),                                  # no quote inside the board
])
def test_no_row_spacing_question_without_a_use_or_a_candidate(data):
    """CONTRACT CHANGED 2026-11-13 (round 11): a value «given by the answer» is no longer
    enough by itself -- it is kept without a question only when it is the only physically
    possible printed quote, or the user chose it."""
    ext._ask_row_spacing(data)
    assert not _questions(data, "row_spacing")


def test_the_quotes_are_converted_from_the_drawing_unit():
    data = {"drawing_unit": "inch", "printed_row_quotes": [{"value": 0.9, "measures": "contact_rows"},
                                                          {"value": 1.0, "measures": "bogus"}]}
    assert ext._row_quotes_mm(data) == [(22.86, "contact_rows"), (25.4, "other")]


def test_a_user_row_spacing_survives_the_unread_rule():
    params = SimpleNamespace(row_spacing_mm=22.86, value_sources={"row_spacing_mm": ext.USER_SOURCE},
                             castellated_sides=[], body_length_mm=63.3, body_width_mm=25.4)
    notes = []
    ext._drop_unread_row_spacing(params, notes)
    assert params.row_spacing_mm == 22.86 and not notes


def test_apply_answers_writes_the_row_spacing():
    params = ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                 layout="castellated", castellated_sides=[],
                                 questions=[{"key": "row_spacing:21.80|22.86|estimate",
                                             "field": "row_spacing", "text": "?",
                                             "options": [{"label": "a", "value": "22.86"},
                                                         {"label": "b", "value": "21.80"},
                                                         {"label": "c", "value": "estimate"}]}])
    applied = ext.apply_answers(params, {"row_spacing:21.80|22.86|estimate": "22.86"})
    assert applied.row_spacing_mm == 22.86 and not applied.questions
    assert applied.value_sources["row_spacing_mm"] == ext.USER_SOURCE


# --- (4) the photo's contact-style question ------------------------------------------------------

def _photo_data(declared="half_hole", seen=True, like="bottom", recommended="through_hole",
                answers=None):
    data = {"castellated_sides": [{"edge": "top", "count": 22, "pitch_mm": 2.54,
                                   "contact_style": declared},
                                  {"edge": "bottom", "count": 22, "pitch_mm": 2.54,
                                   "contact_style": "through_hole"}],
            ext.PHOTO_CONTACT_STYLES_KEY: {"top": {
                "edge": "top", "measured_style": "half_and_through", "through_hole_seen": seen,
                "options": ["through_hole", "half_and_through"] if seen
                else ["through_hole", "half_and_through", "half_hole"],
                "like_edge": like, "recommended": recommended,
                "evidence": {"document": "photo.jpg", "crop": [0, 0, 10, 10]}}}}
    if answers is not None:
        data["_answers"] = answers
    return data


def test_an_unsettled_edge_is_asked_with_the_like_edge_recommended():
    data, notes = _photo_data(), []
    ext._ask_photo_contact_styles(data, notes)
    (question,) = _questions(data, "contact_style")
    assert "come il lato inferiore" in question["text"]
    assert [o["value"] for o in question["options"]] == ["through_hole", "half_and_through"]
    assert [o.get("recommended") for o in question["options"]] == [True, None]
    assert question["evidence"] == {"document": "photo.jpg", "crop": [0, 0, 10, 10]}
    # Rule (a): the declared half_hole does not stand against the seen through hole.
    assert data["castellated_sides"][0]["contact_style"] == "half_and_through"
    assert notes and "provvisorio" in notes[0]


def test_without_a_seen_through_hole_the_declared_style_stays_while_asked():
    """Negative of rule (a): no closed hole seen -> half_hole is an option and stays."""
    data, notes = _photo_data(seen=False, like=None, recommended=None), []
    ext._ask_photo_contact_styles(data, notes)
    (question,) = _questions(data, "contact_style")
    assert "come il lato" not in question["text"]
    assert data["castellated_sides"][0]["contact_style"] == "half_hole" and not notes


def test_a_withheld_short_row_says_why_when_the_photo_recommends():
    """Task E (2026-11-14): a short located row whose photo style contradicts the declaration.

    photo_inventory resolves the recommendation (twin edge > photo style >
    none) into `recommended` and `recommended_from`. When it comes from the
    photo the question says `why` (the outline confirmed the cut, so «the
    photo does not show whether the edge is cut» would be false).
    """
    data, notes = _photo_data(like=None, recommended="half_and_through"), []
    entry = data[ext.PHOTO_CONTACT_STYLES_KEY]["top"]
    entry.update(photo_style="half_and_through", recommended_from="photo",
                 why="Fila corta: la foto contraddice il tipo dichiarato.")
    ext._ask_photo_contact_styles(data, notes)
    (question,) = _questions(data, "contact_style")
    assert question["text"].startswith("Fila corta: la foto contraddice il tipo dichiarato.")
    assert "non mostra se il bordo" not in question["text"]
    assert [o.get("recommended") for o in question["options"]] == [None, True]


def test_with_a_twin_edge_the_like_edge_text_and_recommendation_stay():
    """Negative: a `why` is present but the recommendation comes from the twin edge."""
    data, notes = _photo_data(like="bottom", recommended="through_hole"), []
    data[ext.PHOTO_CONTACT_STYLES_KEY]["top"].update(
        photo_style="half_and_through", recommended_from="like_edge",
        why="Fila corta: la foto contraddice il tipo dichiarato.")
    ext._ask_photo_contact_styles(data, notes)
    (question,) = _questions(data, "contact_style")
    assert "come il lato inferiore" in question["text"] and "Fila corta" not in question["text"]
    # The recommendation is photo_inventory's `recommended`, never photo_style.
    assert [o.get("recommended") for o in question["options"]] == [True, None]


def test_a_saved_contact_style_answer_sets_the_side():
    first = _photo_data()
    ext._ask_photo_contact_styles(first, [])
    key = _questions(first, "contact_style")[0]["key"]
    data, notes = _photo_data(answers={key: "through_hole"}), []
    ext._ask_photo_contact_styles(data, notes)
    assert not _questions(data, "contact_style")
    assert data["castellated_sides"][0]["contact_style"] == "through_hole"
    assert "scelto dall'utente" in notes[0]


def test_apply_answers_sets_the_contact_style_of_its_edge():
    first = _photo_data()
    ext._ask_photo_contact_styles(first, [])
    (question,) = _questions(first, "contact_style")
    params = ext.ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                 layout="castellated",
                                 castellated_sides=[dict(side) for side in first["castellated_sides"]],
                                 questions=[question])
    applied = ext.apply_answers(params, {question["key"]: "through_hole"})
    assert applied.castellated_sides[0]["contact_style"] == "through_hole"
    assert applied.castellated_sides[1]["contact_style"] == "through_hole"
    assert not applied.questions


def test_a_raw_answer_cannot_carry_the_photo_doubts():
    data = {"layout": "castellated", ext.PHOTO_CONTACT_STYLES_KEY: {"top": {}}}
    ext._canonicalize_answer(data)
    assert ext.PHOTO_CONTACT_STYLES_KEY not in data


# --- round 10: a weakening witness removes the recommendation (real SRD run A) -------------------

def _witness(status, reason, misplaced=None):
    """A drawn-hole check returning one verdict for any layout."""
    def check(pads, body):
        return _Verdict(status=status, reason=reason, image="pinout.jpeg", max_error_mm=0.21,
                        tolerance_mm=0.15, circles=6, scale_observable=True,
                        misplaced_pad=misplaced)
    return check


def _agreed(check):
    data = {"package_geometry": {"origin": "custom", "pins": _pins()}}
    ext._block_or_ask(data, data["package_geometry"]["pins"], [("dedicated", dict(LAYOUT), {}, False)],
                      ext._HoleVerdicts(check, None), 1.0, "msg", None, agreed=True)
    return data


def test_a_coincidence_of_the_drawn_holes_removes_the_recommendation():
    """Positive: the holes match circles «not all drawn as holes» -> asked, not recommended,
    and the text says what the holes said, the farthest pad and the error."""
    (question,) = _questions(_agreed(_witness("unverifiable", "unanchored", misplaced="3")),
                             "pad_layout")
    assert len(question["options"]) == 1 and not question["options"][0].get("recommended")
    assert "puo' essere una coincidenza" in question["text"]
    assert "il pad 3 e' il piu' lontano dai fori" in question["text"]
    assert "scarto massimo 0.21 mm" in question["text"]


def test_a_check_with_nothing_to_compare_keeps_the_recommendation():
    """Negative: no round circle at all (nothing near the pads) -> recommended as before."""
    (question,) = _questions(_agreed(_witness("unverifiable", "too_few_circles")), "pad_layout")
    assert question["options"][0].get("recommended") is True
    assert "Controllare il disegno prima di confermare" not in question["text"]


#: Every reason drawing_holes.LayoutCheck lists, with its status and whether it weakens an
#: agreed reading. CONTRACT CHANGED 2026-11-13 (generality audit item 1): only the «nothing to
#: compare» reasons leave a reading unweakened; every other non-confirmation weakens it,
#: low_resolution and search_limit included.
_ALL_REASONS = [
    ("confirmed", "confirmed", False),
    ("unverifiable", "too_few_pads", False), ("unverifiable", "too_many_pads", False),
    ("unverifiable", "no_image", False), ("unverifiable", "not_a_drawing", False),
    ("unverifiable", "no_round_holes", False), ("unverifiable", "too_few_circles", False),
    ("unverifiable", "low_resolution", True), ("unverifiable", "search_limit", True),
    ("unverifiable", "page_skewed", True), ("unverifiable", "unanchored", True),
    ("unverifiable", "no_match_unobservable", True),
    ("not_confirmed", "no_match", True), ("not_confirmed", "distorted", True),
    ("not_confirmed", "body_scale", True),
]


def test_the_reason_list_matches_the_witness():
    """Every reason the witness documents is classified here (a new one would fail)."""
    import re
    from kicadgen.services import drawing_holes
    documented = set(re.findall(r'"([a-z_]+)"', drawing_holes.LayoutCheck.__doc__.split(
        "``reason`` is a machine code", 1)[1].split(";", 1)[0]))
    assert documented == {reason for _status, reason, _weak in _ALL_REASONS}


@pytest.mark.parametrize("verdict,weak", [(None, False)] + [
    (_Verdict(status=status, reason=reason), weak) for status, reason, weak in _ALL_REASONS])
def test_which_verdicts_weaken_a_reading(verdict, weak):
    assert ext._witness_weakens(verdict) is weak


# --- round 11: a read row spacing with no witness (real run realrun7, ESP32) ----------------------

#: The labels gpt-5.6-sol gave in both main readings of realrun7: backwards.
SWAPPED = [(21.8, "contact_rows"), (22.86, "mounting_holes"), (25.4, "board")]


def test_a_read_row_spacing_without_a_witness_is_asked_among_the_quotes():
    """Positive: 21.8 «read» from one labelling, 22.86 also possible -> asked, the read value
    first and NOT recommended, the text saying it rests on the labels only."""
    data = _rows_data(SWAPPED, spacing=21.8)
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    assert [o["value"] for o in question["options"]] == ["21.80", "22.86", ext.ROW_SPACING_ESTIMATE]
    assert not any(o.get("recommended") for o in question["options"])
    assert "una sola lettura delle etichette" in question["options"][0]["label"]
    assert "solo da come ha etichettato le quote" in question["text"]


def test_a_derived_row_spacing_is_asked_like_a_missing_one():
    """A provenance the later rule drops is no row spacing: the missing-value question."""
    data = _rows_data(SWAPPED, spacing=21.8, source="derived")
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    assert "non data dall'analisi" in question["text"]


def test_the_photo_s_through_hole_makes_the_rows_count():
    """A side declared half_hole that the photo reads with a through hole needs the spacing."""
    data = _rows_data(SWAPPED, style="half_hole", spacing=21.8)
    data["_measured_holes"] = {"top": {"style": "through_hole"}}
    ext._ask_row_spacing(data)
    assert _questions(data, "row_spacing")


def test_the_saved_answer_is_reused_whatever_the_reading_said():
    """The key is the set of printed quotes: a later analysis that reads 21.8, or nothing,
    finds the user's 22.86."""
    first = _rows_data(SWAPPED, spacing=21.8)
    ext._ask_row_spacing(first)
    key = _questions(first, "row_spacing")[0]["key"]
    assert key == "row_spacing:21.80|22.86|estimate"
    for later in (_rows_data(SWAPPED, spacing=21.8), _rows_data(SWAPPED),
                  _rows_data(QUOTES, spacing=22.86, source="estimated")):
        later["_answers"] = {key: "22.86"}
        ext._ask_row_spacing(later)
        assert not _questions(later, "row_spacing")
        assert later["row_spacing_mm"] == 22.86
        assert later["_program_sources"]["row_spacing_mm"] == ext.USER_SOURCE


def test_estimate_removes_a_read_row_spacing():
    data = _rows_data(SWAPPED, spacing=21.8)
    data["_answers"] = {"row_spacing:21.80|22.86|estimate": ext.ROW_SPACING_ESTIMATE}
    ext._ask_row_spacing(data)
    assert "row_spacing_mm" not in data and "row_spacing_mm" not in data["value_sources"]


# --- round 11 audit (M1-M4, L2) ------------------------------------------------------------------

def _canonical(style="half_hole", spacing=None, quotes=SWAPPED, edges=("top", "bottom"),
               length=63.3, width=25.4):
    """A canonical castellated answer as the extraction holds it between its two result builds."""
    data = {"layout": "castellated", "drawing_unit": "mm", "body_length_mm": length,
            "body_width_mm": width, "confidence": "medium", "notes": "",
            "castellated_sides": [{"edge": e, "count": 8, "pitch_mm": 2.54, "contact_style": style}
                                  for e in edges],
            "printed_row_quotes": [{"value": v, "measures": r} for v, r in quotes]}
    if spacing is not None:
        data["row_spacing_mm"] = spacing
        data["value_sources"] = {"row_spacing_mm": "read"}
    data["_row_quotes_mm"] = ext._row_quotes_mm(data)
    ext._canonicalize_answer(data)
    return data


def _row_questions(params):
    return [q for q in params.questions or () if q["field"] == "row_spacing"]


def test_the_question_follows_the_photo_between_the_two_result_builds():
    """M1: built before the photo (half_hole, no question), then after (through hole seen)."""
    data = _canonical(spacing=21.8)
    assert not _row_questions(ext._params_from_tool_arguments(data))
    data["_measured_holes"] = {"top": {"style": "through_hole"}, "bottom": {"style": "through_hole"}}
    assert len(_row_questions(ext._params_from_tool_arguments(data))) == 1


def test_a_stale_question_of_the_first_build_is_dropped():
    """M1 negative: asked before the photo, the photo then settles half holes -> no question."""
    data = _canonical(style="half_and_through", spacing=21.8)
    assert _row_questions(ext._params_from_tool_arguments(data))
    data["_measured_holes"] = {"top": {"style": "half_hole"}, "bottom": {"style": "half_hole"}}
    assert not _row_questions(ext._params_from_tool_arguments(data))


def test_a_saved_answer_is_applied_and_noted_once_over_two_builds():
    data = _canonical(style="half_and_through", spacing=21.8)
    data["_answers"] = {"row_spacing:21.80|22.86|estimate": ext.ROW_SPACING_ESTIMATE}
    ext._params_from_tool_arguments(data)
    params = ext._params_from_tool_arguments(data)
    assert params.notes.count("nessuna quota scelta") == 1
    assert params.row_spacing_mm is None


def test_a_settled_photo_style_replaces_the_declared_one():
    """M2: declared through_hole, the photo settled half_hole on both rows -> no question."""
    data = _rows_data(SWAPPED, style="through_hole", spacing=21.8)
    data["_measured_holes"] = {"top": {"style": "half_hole"}, "bottom": {"style": "half_hole"}}
    ext._ask_row_spacing(data)
    assert not _questions(data, "row_spacing")


def test_possible_quotes_respect_the_ring_and_the_outline():
    """M3: a quote that leaves less than the smallest ring's radius, or is the outline within
    the reading tolerance, is not a row spacing (another extent, left/right rows)."""
    ring = ext._smallest_row_ring({}, [])
    assert ring == pytest.approx(0.7)
    assert ext._row_spacing_possible(18.0, 20.0, ring)
    assert not ext._row_spacing_possible(19.5, 20.0, ring)       # inset 0.25 < ring/2
    assert not ext._row_spacing_possible(19.95, 20.0, ring)      # the outline, within tolerance
    data = _rows_data([(18.0, "contact_rows"), (19.5, "board"), (17.0, "mounting_holes")],
                      spacing=18.0)
    data["castellated_sides"] = [{"edge": e, "count": 6, "pitch_mm": 2.54,
                                  "contact_style": "through_hole"} for e in ("left", "right")]
    data["body_length_mm"], data["body_width_mm"] = 20.0, 40.0   # left/right: extent = length
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    assert [o["value"] for o in question["options"]] == ["18.00", "17.00", ext.ROW_SPACING_ESTIMATE]


def test_a_declared_ring_narrows_the_possible_quotes():
    """M3: a declared 2.0 mm ring makes a 0.75 mm inset impossible; the smallest assumed ring
    (0.7 mm) does not."""
    data = {"pad_diameter_mm": 2.0, "value_sources": {"pad_diameter_mm": "read"}}
    ring = ext._smallest_row_ring(data, [])
    assert ring == 2.0
    assert not ext._row_spacing_possible(18.5, 20.0, ring)
    assert ext._row_spacing_possible(18.5, 20.0, ext._smallest_row_ring({}, []))


def test_a_saved_choice_wins_even_when_the_read_value_is_the_only_possible_one():
    """M4: the user chose «estimate» before; now the read value is the only possible quote
    -> the saved choice still applies, never overridden silently."""
    data = _rows_data([(22.86, "contact_rows"), (25.4, "board")], spacing=22.86)
    data["_answers"] = {"row_spacing:21.80|22.86|estimate": ext.ROW_SPACING_ESTIMATE}
    ext._ask_row_spacing(data)
    assert "row_spacing_mm" not in data


def test_a_saved_value_applies_whatever_the_other_options():
    """M4: the saved 22.86 is on offer again among different quotes."""
    data = _rows_data([(22.86, "mounting_holes"), (20.0, "other")], spacing=20.0)
    data["_answers"] = {"row_spacing:21.80|22.86|estimate": "22.86"}
    ext._ask_row_spacing(data)
    assert not _questions(data, "row_spacing") and data["row_spacing_mm"] == 22.86


def test_two_saved_choices_of_different_values_ask():
    data = _rows_data(SWAPPED, spacing=21.8)
    # Neither is the exact question of now (that one would be applied as the same question).
    data["_answers"] = {"row_spacing:22.86|23.00|estimate": "22.86",
                        "row_spacing:21.80|estimate": "21.80"}
    ext._ask_row_spacing(data)
    assert _questions(data, "row_spacing")


def test_a_non_castellated_answer_is_never_asked():
    data = _rows_data(SWAPPED, spacing=21.8)
    data["layout"] = "matrix"
    ext._ask_row_spacing(data)
    assert not _questions(data, "row_spacing")


def test_unusable_sides_do_not_make_a_pair():
    """L2: a side the normalisation drops (no count) does not form the opposite pair."""
    data = _rows_data(SWAPPED, spacing=21.8)
    data["castellated_sides"][1].pop("count")
    ext._ask_row_spacing(data)
    assert not _questions(data, "row_spacing")


# --- overnight: no unverified roles in the row-spacing options (realrun8 ESP32) -------------------

def test_the_options_show_printed_values_without_the_models_roles():
    """Positive: the quotes are shown as printed values; the model's «mounting_holes» /
    «contact_rows» labels -- what nothing confirms -- never reach the user."""
    data = _rows_data(SWAPPED, spacing=21.8)
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    labels = {o["value"]: o["label"] for o in question["options"]}
    assert labels["22.86"] == "22.86 mm (quota stampata)"
    assert "letta dall'analisi" in labels["21.80"]
    assert not any("secondo l'analisi" in label for label in labels.values())


def test_a_missing_spacing_also_hides_the_roles():
    """Negative of a role-based shortcut: with no read value the labels are not shown either,
    and the outline is still left out by geometry, not by its label."""
    data = _rows_data([(22.86, "board"), (21.8, "contact_rows"), (25.4, "contact_rows")])
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    assert [o["value"] for o in question["options"]] == ["22.86", "21.80", ext.ROW_SPACING_ESTIMATE]
    assert all("secondo l'analisi" not in o["label"] for o in question["options"])

