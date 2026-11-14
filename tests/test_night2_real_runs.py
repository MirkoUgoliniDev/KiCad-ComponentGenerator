"""@brief Defects of the night real runs of 2026-11-14, round two (F1, F2).

- F1, a castellated module with contact rows on the LEFT and RIGHT edges:
  the row-spacing question offered the board WIDTH (printed along the rows)
  as a bare «quota stampata»; chosen, it gave 16 mm pads with overlapping
  copper. After the generality audit (D2) such a quote is still offered --
  a genuine row spacing can equal the other board size -- but labelled with
  the size it equals and the inset it implies, never recommended; a read
  value of that size is a doubt asked, not used until chosen (D1: judged on
  the photo's styles).
- F2, a relay whose second dedicated reading misread one edge quote: the
  rows moved 0.5 mm on the body, the pads' offsets did not, so the saved
  pad-layout choice of the first reading (keyed by the offsets only) was
  applied without a question. The option value now carries where the pads
  sit on the body, and a saved choice applies only where that agrees.
Invented boards, not the real ones. No network.
"""
from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import pads_from_chains


def _questions(data, field):
    return [q for q in data.get("_questions") or () if q.get("field") == field]


# ── F1: an outline size is never a row spacing ─────────────────────────

def _left_right(quotes, spacing=None):
    """Rows on the left and right edges of a 40 x 16 mm board: the extent across them is the LENGTH."""
    data = {"layout": "castellated", "body_length_mm": 40.0, "body_width_mm": 16.0,
            "castellated_sides": [{"edge": edge, "count": 5, "pitch_mm": 2.54,
                                   "contact_style": "half_and_through"}
                                  for edge in ("left", "right")],
            "_row_quotes_mm": quotes}
    if spacing is not None:
        data["row_spacing_mm"] = spacing
        data["value_sources"] = {"row_spacing_mm": "read"}
    return data


def test_a_quote_equal_to_the_board_width_is_offered_with_its_absurd_inset():
    """The RS485 case: the width along the rows, holes it would put 12 mm from the edge."""
    data = _left_right([(16.0, "board")])
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    assert [o["value"] for o in question["options"]] == ["16.00" + ext.ROW_OUTLINE_MARK,
                                                         ext.ROW_SPACING_ESTIMATE]
    label = question["options"][0]["label"]
    assert "uguale alla larghezza della scheda" in label and "12.00 mm dal bordo" in label
    assert not any(o.get("recommended") for o in question["options"])


def test_a_genuine_spacing_equal_to_the_other_size_is_offered_too():
    """D2: a 25.4 x 20.32 mm board, rows on the short edges, spacing 20.32 = the width."""
    data = _left_right([(20.32, "contact_rows")])
    data.update(body_length_mm=25.4, body_width_mm=20.32)
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    label = question["options"][0]["label"]
    assert question["options"][0]["value"] == "20.32" + ext.ROW_OUTLINE_MARK
    assert "2.54 mm dal bordo" in label
    assert not any(o.get("recommended") for o in question["options"])


def test_a_quote_unlike_any_outline_size_keeps_the_plain_label():
    """Negative: 37.0 is neither 40 nor 16: a plain printed quote."""
    data = _left_right([(16.0, "board"), (37.0, "contact_rows")])
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    labels = {o["value"]: o["label"] for o in question["options"]}
    assert labels["37.00"] == "37.00 mm (quota stampata)"
    assert "larghezza" in labels["16.00" + ext.ROW_OUTLINE_MARK]


def test_a_read_value_equal_to_the_width_is_asked_even_alone():
    """D2: a read 16.0 is the only possible quote but equals the width: a doubt, asked."""
    data = _left_right([], spacing=16.0)
    ext._ask_row_spacing(data)
    (question,) = _questions(data, "row_spacing")
    assert question["options"][0]["value"] == "16.00" + ext.ROW_OUTLINE_MARK
    assert "larghezza" in question["options"][0]["label"]


def test_an_answer_given_to_the_bare_outline_quote_is_asked_again():
    """The RS485 saved «15.20» was chosen when the option did not say it was the width."""
    data = _left_right([(16.0, "board")])
    data["_answers"] = {"row_spacing:16.00|estimate": "16.00"}
    ext._ask_row_spacing(data)
    assert len(_questions(data, "row_spacing")) == 1 and "row_spacing_mm" not in data


def test_choosing_the_outline_option_writes_its_millimetres():
    """The marked value is still a number of millimetres for the writer."""
    data = _left_right([(16.0, "board")])
    data["_answers"] = {"row_spacing:16.00~outline|estimate": "16.00~outline"}
    ext._ask_row_spacing(data)
    assert not _questions(data, "row_spacing") and data["row_spacing_mm"] == 16.0


def test_a_read_row_spacing_equal_to_the_width_is_dropped_with_a_note():
    params = ext.ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                                 body_length_mm=40.0, body_width_mm=16.0, row_spacing_mm=16.0,
                                 value_sources={"row_spacing_mm": "read"},
                                 castellated_sides=[{"edge": edge, "count": 5, "pitch_mm": 2.54,
                                                     "contact_style": "half_and_through"}
                                                    for edge in ("left", "right")])
    notes = []
    ext._drop_board_outline_row_spacing(params, notes)
    assert params.row_spacing_mm is None and "row_spacing_mm" not in params.value_sources
    assert notes and "probabilmente" in notes[0] and "12.00 mm dal bordo" in notes[0]


def test_a_width_sized_spacing_the_user_chose_is_kept():
    """D2: the doubt is the user's to settle; once chosen it is not dropped."""
    params = ext.ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                                 body_length_mm=40.0, body_width_mm=16.0, row_spacing_mm=16.0,
                                 value_sources={"row_spacing_mm": ext.USER_SOURCE},
                                 castellated_sides=[{"edge": edge, "count": 5, "pitch_mm": 2.54,
                                                     "contact_style": "half_and_through"}
                                                    for edge in ("left", "right")])
    notes = []
    ext._drop_board_outline_row_spacing(params, notes)
    assert params.row_spacing_mm == 16.0 and not notes


def _photo_rows(style_seen):
    """Top/bottom half-hole rows on a 40 x 16 mm board, a read row spacing = the width (extent)."""
    sides = [dict(edge=edge, count=10, pitch_mm=2.54, first_pin=1 if edge == "top" else 11,
                  contact_style="half_hole", value_sources={"pitch_mm": "read"})
             for edge in ("top", "bottom")]
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=40.0, body_width_mm=16.0, row_spacing_mm=16.0,
                value_sources={"row_spacing_mm": "read"}, castellated_sides=sides)
    ext._canonicalize_answer(data)
    if style_seen:
        data["_measured_holes"] = {edge: dict(edge=edge, style=style_seen, contacts=10, agreeing=9,
                                              drill_mm=0.9, chord_mm=0.7, reason="")
                                   for edge in ("top", "bottom")}
    return data


def test_the_outline_spacing_is_judged_on_the_photo_styles():
    """D1: declared half_hole (no through-hole), the photo settles half_and_through -> dropped."""
    params = ext._params_from_tool_arguments(_photo_rows("half_and_through"))
    assert {side["contact_style"] for side in params.castellated_sides} == {"half_and_through"}
    assert params.row_spacing_mm is None


def test_without_a_through_hole_the_outline_spacing_is_left_alone():
    """Negative of D1: half_hole declared and confirmed -> the spacing places no hole, untouched."""
    params = ext._params_from_tool_arguments(_photo_rows("half_hole"))
    assert params.row_spacing_mm == 16.0


def test_a_read_row_spacing_unlike_the_outline_is_kept():
    """Negative of the drop: 37.0 mm is no outline size."""
    params = ext.ExtractedParams(name="X", description="", tags=[], confidence="low", notes="",
                                 body_length_mm=40.0, body_width_mm=16.0, row_spacing_mm=37.0,
                                 value_sources={"row_spacing_mm": "read"},
                                 castellated_sides=[{"edge": edge, "count": 5, "pitch_mm": 2.54,
                                                     "contact_style": "half_and_through"}
                                                    for edge in ("left", "right")])
    notes = []
    ext._drop_board_outline_row_spacing(params, notes)
    assert params.row_spacing_mm == 37.0 and not notes


# ── F2: a saved pad-layout choice is about where the pads sit ──────────

def _chain_reading(left):
    """Two pads 16 mm apart on a 20 x 12 mm body, `left` mm from the left edge."""
    chains = [{"axis": "x", "stops": ["edge", "1", "2", "edge"], "values": [left, 16.0, 4.0 - left]},
              {"axis": "y", "stops": ["edge", "1,2", "edge"], "values": [3.0, 9.0]}]
    return pads_from_chains(chains, 1.0)


def _agreed_question(left, answers=None):
    """The one-layout confirmation of two agreeing readings of one model (`agreed`)."""
    reading = _chain_reading(left)
    data = {"drawing_unit": "mm", "notes": "", "package_geometry": {
        "origin": "custom", "body_length_mm": 20.0, "body_width_mm": 12.0,
        "pins": [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in reading["pads"].items()]}}
    if answers is not None:
        data["_answers"] = answers
    ext._block_or_ask(data, data["package_geometry"]["pins"],
                      [("dedicated", reading["pads"], reading["spans"],
                        ext._centred(reading["spans"], False))],
                      ext._HoleVerdicts(None, None), 1.0, "msg", None, agreed=True)
    return data


def _saved(left):
    first = _agreed_question(left)
    (question,) = _questions(first, "pad_layout")
    return question["key"], question["options"][0]["value"]


def test_the_option_value_carries_the_placement_on_the_body():
    key, value = _saved(2.0)
    other_key, other_value = _saved(2.5)
    # Same offsets between the pads, so the same layout digest ...
    assert ext._layout_value_parts(value)[0] == ext._layout_value_parts(other_value)[0]
    # ... but not the same place on the body.
    assert value != other_value and key != other_key


def test_a_saved_choice_applies_to_the_same_placement():
    key, value = _saved(2.0)
    data = _agreed_question(2.0, answers={key: value})
    assert not _questions(data, "pad_layout")


def test_a_saved_choice_applies_within_the_reading_tolerance():
    """A re-reading 0.05 mm off: another key, the same placement within tolerance."""
    key, value = _saved(2.0)
    data = _agreed_question(2.05, answers={key: value})
    assert not _questions(data, "pad_layout")


def test_a_saved_choice_never_applies_to_rows_moved_on_the_body():
    """Negative (F2): the same offsets 0.5 mm further along the body -> asked again."""
    key, value = _saved(2.0)
    data = _agreed_question(2.5, answers={key: value})
    assert len(_questions(data, "pad_layout")) == 1


def test_a_legacy_saved_choice_without_placement_asks_again():
    """A value stored before the placement mark cannot be compared: the question is asked."""
    key, value = _saved(2.0)
    legacy_value = value.split(ext._PLACEMENT_MARK)[0]
    legacy_key = "pad_layout:" + legacy_value
    data = _agreed_question(2.0, answers={legacy_key: legacy_value})
    assert len(_questions(data, "pad_layout")) == 1


# ── Audit S1-S4: the identity of a saved layout choice ─────────────────

def _payload(pins, origin="body_center", view=None, exchange=None):
    payload = {"pins": [{"number": n, "x_mm": x, "y_mm": y} for n, (x, y) in pins.items()],
               "origin": origin, "view": view}
    if exchange:
        payload["exchange_body"] = exchange
    return payload


PADS = {"1": (-8.0, -3.0), "2": (8.0, -3.0)}


def test_a_choice_saved_for_a_top_view_is_not_one_for_a_bottom_view():
    top = "dedicated:abcd" + ext._placement_mark(_payload(PADS, view="top"), 0.1)
    bottom = "dedicated:abcd" + ext._placement_mark(_payload(PADS, view="bottom"), 0.1)
    assert not ext._same_layout_choice(top, bottom)
    assert ext._same_layout_choice(top, top)


def test_an_exchanged_body_is_another_choice():
    plain = "dedicated:abcd" + ext._placement_mark(_payload(PADS), 0.1)
    swapped = "dedicated:abcd" + ext._placement_mark(_payload(PADS, exchange={"measured_mm": [1, 2]}),
                                                     0.1)
    assert not ext._same_layout_choice(plain, swapped)


def test_a_custom_origin_marks_the_first_pad_from_the_pad_field_centre():
    """S1: in the main frame the body is centred on the pads, wherever the origin is."""
    shifted = {n: (x + 30.0, y - 7.0) for n, (x, y) in PADS.items()}
    one = ext._placement_mark(_payload(PADS, origin="custom"), 0.1)
    two = ext._placement_mark(_payload(shifted, origin="custom"), 0.1)
    assert one == two and one.startswith(ext._PLACEMENT_MARK + "-8.00,0.00")


def test_the_tolerance_follows_the_drawing_unit():
    """S3: 0.12 mm apart is the same choice in an inch drawing, not in a millimetre one."""
    moved = {n: (x + 0.12, y) for n, (x, y) in PADS.items()}
    inch = ext._reading_tolerance(25.4)
    mm = ext._reading_tolerance(1.0)
    assert inch > 0.12 > mm
    assert ext._same_layout_choice("d:abcd" + ext._placement_mark(_payload(PADS), inch),
                                   "d:abcd" + ext._placement_mark(_payload(moved), inch))
    assert not ext._same_layout_choice("d:abcd" + ext._placement_mark(_payload(PADS), mm),
                                       "d:abcd" + ext._placement_mark(_payload(moved), mm))


def test_a_newer_answer_supersedes_a_near_equal_one():
    """S4: the same layout 0.05 mm off, chosen once and refused later -> the older answer goes."""
    near = "dedicated:abcd~at-8.00,-3.00,0.100/top/asis"
    near_again = "dedicated:abcd~at-8.05,-3.00,0.100/top/asis"
    other = "main:ffff~at0.00,0.00,0.100/top/asis"
    old_key = f"pad_layout:{near}"
    new_key = f"pad_layout:{near_again}|{other}"
    kept = ext.remember_answer({old_key: near}, new_key, other)
    assert old_key not in kept and kept[new_key] == other
    # The same layout chosen again is no contradiction.
    assert not ext.answers_contradict(old_key, near, new_key, near_again)
