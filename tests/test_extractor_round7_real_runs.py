"""@brief Fixes from the real end-to-end runs and the generality audit of 2026-11-13 (round 7).

- TEL: a pad layout that puts two pads on one point is never offered; the
  one layout left is a recommended confirmation, never adopted by itself;
  a saved choice applies whenever its layout is offered again.
- ESP32: the schema says what row_spacing_mm and the pinout name are; a row
  spacing equal to the board outline across the rows is not used.
- Audit H3: an impossible double contact is settled only on declared or
  measured sizes with a photo verdict; an assumed drill is reduced first.
- Audit M2: remark-like names are flagged, never a paid retry; their known
  false positives are pinned here as known limits.
Offline: no network, the AI calls are faked.
"""
import copy
from types import SimpleNamespace

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import pinout_coherence as pc


# --- 1a: impossible layouts are not offered ----------------------------------------------------

VALID = {"1": (0.0, 0.0), "2": (2.54, 0.0), "3": (0.0, 10.16)}
COINCIDENT = {"1": (0.0, 0.0), "2": (1.27, 0.0), "3": (1.27, 0.0)}


def _pins():
    return [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in VALID.items()]


def _questions(data):
    return [q for q in data.get("_questions") or () if q["field"] == "pad_layout"]


def test_two_pads_on_one_point_are_found():
    assert ext._coincident_pads(COINCIDENT, 0.1) == [("2", "3")]
    assert ext._coincident_pads(VALID, 0.1) == []


def test_a_layout_with_two_pads_on_one_point_is_not_offered():
    """Positive (TEL): the main reading puts 2 and 3 on one point -> only the dedicated layout,
    as a confirmation flagged recommended; nothing written by itself."""
    data = {"package_geometry": {"origin": "custom", "pins": _pins()}}
    holes = ext._HoleVerdicts(None, None)
    ext._block_or_ask(data, data["package_geometry"]["pins"],
                      [("main", dict(COINCIDENT), {}, False), ("dedicated", dict(VALID), {}, False)],
                      holes, 1.0, "msg", None)
    (question,) = _questions(data)
    assert len(question["options"]) == 1
    assert question["options"][0]["value"].startswith("dedicated:")
    assert question["options"][0].get("recommended") is True
    assert "due pad nello stesso punto" in question["text"]


def test_two_valid_layouts_are_both_offered_without_a_flag():
    """Negative: nothing impossible -> the usual choice, no recommendation from this rule."""
    other = {"1": (0.0, 0.0), "2": (5.08, 0.0), "3": (0.0, 10.16)}
    data = {"package_geometry": {"origin": "custom", "pins": _pins()}}
    ext._block_or_ask(data, data["package_geometry"]["pins"],
                      [("main", dict(other), {}, False), ("dedicated", dict(VALID), {}, False)],
                      ext._HoleVerdicts(None, None), 1.0, "msg", None)
    (question,) = _questions(data)
    assert len(question["options"]) == 2
    assert not any(option.get("recommended") for option in question["options"])


def test_only_impossible_layouts_keep_the_block():
    data = {"package_geometry": {"origin": "custom", "pins": _pins()}}
    ext._block_or_ask(data, data["package_geometry"]["pins"],
                      [("main", dict(COINCIDENT), {}, False)],
                      ext._HoleVerdicts(None, None), 1.0, "msg", None)
    assert data.get("_pin_map_conflict") and not _questions(data)


# --- 1b: a saved choice follows its layout ------------------------------------------------------

def _ask(answers, values):
    data = {"_answers": dict(answers)}
    options = [{"label": value, "value": value} for value in values]
    return data, ext._ask(data, "pad_layout", "?", options)


# CONTRACT CHANGED (night real run 2026-11-14, F2): a layout option value
# carries where its pads sit on the body («~at<x>,<y>», `_placement_mark`);
# the reuse below holds for values that carry it. A value stored before the
# mark applies only under its own question key (tests/test_night2_real_runs.py).
DEDICATED = "dedicated:68a2~at-8.00,-3.00,0.100/top/asis"


def test_a_saved_layout_applies_when_the_losing_option_changed():
    """Positive (TEL): the user chose the dedicated layout against main digest A; the new
    analysis offers the same dedicated layout against main digest B -> applied, not asked."""
    data, chosen = _ask({f"pad_layout:{DEDICATED}|main:aaaa~at0.00,0.00,0.100/top/asis": DEDICATED},
                        [DEDICATED, "main:bbbb~at0.00,0.00,0.100/top/asis"])
    assert chosen == DEDICATED and not data.get("_questions")


def test_the_same_layout_under_another_source_name_applies():
    """The layout is the digest and its placement, whichever reading brings it."""
    _data, chosen = _ask({f"pad_layout:{DEDICATED}|main:aaaa~at0.00,0.00,0.100/top/asis": DEDICATED},
                         ["dedicated+holes:68a2~at-8.00,-3.00,0.100/top/asis",
                          "main:bbbb~at0.00,0.00,0.100/top/asis"])
    assert chosen == "dedicated+holes:68a2~at-8.00,-3.00,0.100/top/asis"


def test_a_saved_layout_not_offered_again_asks():
    """Negative: the chosen layout is not among the new options -> the question is asked."""
    data, chosen = _ask({"pad_layout:dedicated:68a2|main:aaaa": "dedicated:68a2"},
                        ["dedicated:9999", "main:bbbb"])
    assert chosen is None and data.get("_questions")


def test_two_saved_choices_of_two_offered_layouts_ask():
    """Negative: the saved answers point at two layouts both on offer -> nothing settled."""
    data, chosen = _ask({"pad_layout:a:1111|b:2222": "a:1111", "pad_layout:c:3333|d:4444": "d:4444"},
                        ["x:1111", "y:4444"])
    assert chosen is None and data.get("_questions")


# --- 2: what the schema asks, and a row spacing that is the board outline ------------------------

def test_the_schema_separates_signal_names_from_board_labels_and_rows_from_outline():
    properties = ext._openai_tool()["function"]["parameters"]["properties"]
    row = properties["row_spacing_mm"]["description"]
    assert "CENTRES of the contacts of the two rows" in row and "mounting holes" in row
    assert "outline of the board" in row
    name = properties["pinout"]["items"]["properties"]["name"]["description"]
    assert "SIGNAL NAME of the pinout diagram" in name and "pin_names" in name
    labels = properties["castellated_sides"]["items"]["properties"]["pin_names"]["description"]
    assert "abbreviations printed on the board" in labels


def _rows(spacing, style="through_hole", width=25.4):
    sides = [{"edge": e, "count": 22, "pitch_mm": 2.54, "contact_style": style}
             for e in ("top", "bottom")]
    return SimpleNamespace(row_spacing_mm=spacing, body_length_mm=63.3, body_width_mm=width,
                           castellated_sides=sides, value_sources={"row_spacing_mm": "read"})


def test_a_row_spacing_equal_to_the_board_outline_is_not_used():
    """Positive: 25.4 between top and bottom rows of a 25.4 mm board is the outline."""
    params, notes = _rows(25.4), []
    ext._drop_board_outline_row_spacing(params, notes)
    assert params.row_spacing_mm is None and "row_spacing_mm" not in params.value_sources
    assert notes and "contorno della scheda" in notes[0]


@pytest.mark.parametrize("spacing,style", [(22.86, "through_hole"), (25.4, "half_hole")])
def test_a_real_row_spacing_or_a_half_hole_module_is_kept(spacing, style):
    """Negatives: a distance between centres inside the board; a module without through-holes
    (the outline IS the half-hole row, and the value is never used)."""
    params, notes = _rows(spacing, style), []
    ext._drop_board_outline_row_spacing(params, notes)
    assert params.row_spacing_mm == spacing and not notes


# --- 3: impossible double contacts ---------------------------------------------------------------

def _double(spacing, half=1.6, drill=None, drill_source="read", photo=None):
    sides = [{"edge": e, "count": 10, "pitch_mm": 2.54, "contact_style": "half_and_through",
              "half_hole_diameter_mm": half} for e in ("top", "bottom")]
    sources = {f"castellated_sides.{e}.half_hole_diameter_mm": "read" for e in ("top", "bottom")}
    if drill is not None:
        for side in sides:
            side["through_hole_drill_mm"] = drill
        sources.update({f"castellated_sides.{e}.through_hole_drill_mm": drill_source
                        for e in ("top", "bottom")})
    params = SimpleNamespace(body_length_mm=30.0, body_width_mm=25.4, row_spacing_mm=spacing,
                             castellated_sides=sides, drill_mm=None, pad_diameter_mm=None,
                             value_sources=sources)
    data = {"_measured_holes": {e: {"style": photo} for e in ("top", "bottom")}} if photo else {}
    notes = []
    ext._settle_impossible_double_contacts(data, params, notes)
    return params, notes


def test_declared_sizes_and_a_photo_through_hole_verdict_switch_in_the_real_order():
    """CONTRACT CHANGED 2026-11-13 (generality audit item 3): the style switch of the settle was
    unreachable -- `_apply_measured_holes` runs first and adopts the photo's through-hole. In
    the real call order the side is a through-hole after the photo adoption, and the settle
    changes nothing more."""
    sides = [{"edge": e, "count": 10, "pitch_mm": 2.54, "contact_style": "half_and_through",
              "half_hole_diameter_mm": 1.6, "through_hole_drill_mm": 1.0} for e in ("top", "bottom")]
    sources = {f"castellated_sides.{e}.{k}": "read" for e in ("top", "bottom")
               for k in ("half_hole_diameter_mm", "through_hole_drill_mm")}
    params = SimpleNamespace(body_length_mm=30.0, body_width_mm=25.4, row_spacing_mm=21.4,
                             castellated_sides=sides, drill_mm=None, pad_diameter_mm=None,
                             value_sources=sources)
    data = {"_measured_holes": {e: {"style": "through_hole", "agreeing": 10, "contacts": 10,
                                    "drill_mm": 1.0, "chord_mm": 0.0} for e in ("top", "bottom")}}
    notes = []
    ext._apply_measured_holes(data, params, notes)
    ext._settle_impossible_double_contacts(data, params, notes)
    assert {s["contact_style"] for s in params.castellated_sides} == {"through_hole"}
    assert len(notes) == 2 and all("letto sulla foto" in note for note in notes)


def test_no_photo_verdict_is_no_permission():
    """Negative (audit H3): the same declared sizes without a photo verdict stay as declared.

    Since item 3 the settle never switches a style: declared sizes are left to the generator."""
    params, _notes = _double(21.4, drill=1.0)
    assert {s["contact_style"] for s in params.castellated_sides} == {"half_and_through"}


def test_an_assumed_drill_is_reduced_first():
    """Negative of the settle: the drill was not declared -> reduced as an estimate until the
    wall holds, the double contact stays."""
    params, notes = _double(21.4, photo="through_hole")
    assert {s["contact_style"] for s in params.castellated_sides} == {"half_and_through"}
    drills = {s["through_hole_drill_mm"] for s in params.castellated_sides}
    assert drills == {0.78}
    assert params.value_sources["castellated_sides.top.through_hole_drill_mm"] == "estimated"
    assert any("ridotto" in note for note in notes)


def test_an_estimated_drill_is_no_declaration():
    """Negative: a drill the model only estimated is an assumed size too."""
    params, _notes = _double(21.4, drill=1.0, drill_source="estimated", photo="through_hole")
    assert {s["contact_style"] for s in params.castellated_sides} == {"half_and_through"}


# --- 4: remark-like names never cost a paid retry ------------------------------------------------

#: Names the remark heuristic flags although they are what some documents print: KNOWN LIMITS
#: of `pinout_coherence.name_remarks` (generality audit M2). They are flagged for review only,
#: never a paid retry, until the schema separates names from remarks.
KNOWN_FALSE_POSITIVES = ["NC (not connected)", "EN (chip enable)", "VBAT (battery)",
                         "3V3 (max 500mA)", "GPIO0 (strapping pin)", "sda/scl", "led_red",
                         "RST (reset)", "SCL (I2C clock)", "do not connect", "GND (thermal pad)",
                         "ANT (RF out)"]


@pytest.mark.parametrize("name", KNOWN_FALSE_POSITIVES)
def test_known_false_positives_are_still_flagged(name):
    """Pinned limit: these read as remarks to the heuristic. If one stops being flagged, the
    heuristic changed: update this list on purpose."""
    assert pc.name_remarks(name)


def test_the_name_retry_is_gone():
    assert not hasattr(ext, "PinoutNameProblems")
    assert not hasattr(ext, "_raise_on_pinout_name_problems")
