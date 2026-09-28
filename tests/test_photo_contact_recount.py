"""@brief Sides the photo counts differently are recounted once by the model (HANDOFF point 137).

User decision of 2026-09-28: when the half-holes counted on the calibrated
top photo disagree with a declared side, one paid recount of THAT side, told
what the photo measured. The recounted side is adopted only if it now agrees
with the photo and the pad numbering stays coherent; otherwise the answer
stays and the disagreement remains a warning.

The measured centres used here are never numbers read on a sample photo
(HANDOFF point 139, R-G2: a test that declared the RS485 offset «as the photo
measures it» passed only on that board). They are built from the declared
row itself, moved by a shift expressed as a fraction of the calibration
bound, so each case differs from its twin by structure: a shift the
calibration can explain, one it cannot, another spacing, a row one notch off.
"""
import copy
import http.client

import cv2
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services.ai_diagnostics import CallCancelled
from kicadgen.services.edge_notches import EdgeCount, row_shift_bound_mm
from kicadgen.services.openrouter import OpenRouterError
from test_edge_notches import LENGTH, ROW_4, WIDTH, board_photo

BOARD_L, BOARD_W = 40.0, 16.0
PITCH = 2.54
#: Shift the calibration alone allows on a 40 x 16 mm crop at 20 px/mm, from
#: the rule (edge_notches.row_shift_bound_mm), not from a photo.
BOUND = row_shift_bound_mm((int(BOARD_W * 20), int(BOARD_L * 20)), 20.0)
#: Uncertainty of each measured centre.
TOL = 0.1


def _answer(right_count, right_first=4, **extra):
    """A generic answer: 3 contacts on the left, a CENTRED row on the right (no offset)."""
    names = ["G", "R", "T", "V"][:right_count]
    return dict(layout="castellated", body_length_mm=BOARD_L, body_width_mm=BOARD_W,
                drawing_unit="mm", contacts_view="top", notes="",
                castellated_sides=[
                    dict(edge="left", count=3, pitch_mm=5.08, first_pin=1,
                         contact_style="half_hole", pin_names=["PE", "B-", "A+"]),
                    dict(edge="right", count=right_count, pitch_mm=PITCH,
                         first_pin=right_first, contact_style="half_hole",
                         pad_length_mm=1.2, pin_names=names)], **extra)


def _photo(found=4, shift=0.0, pitch=PITCH, bound=BOUND, edge="right", declared=3,
           edge_length=BOARD_W):
    """What the photo measured: `found` notches at `pitch`, centred, then moved by `shift`."""
    start = edge_length / 2 - (found - 1) * pitch / 2 + shift
    centres = [round(start + index * pitch, 3) for index in range(found)]
    return [EdgeCount(edge, found, declared, centres, tolerance_mm=TOL, shift_bound_mm=bound)]


#: The default disagreement: four notches where three are declared, the row
#: shifted by an arbitrary share of the calibration bound that no answer
#: declares.
DISAGREE = _photo(shift=0.43 * BOUND)


def _recount(data, reply, logs=None, disagreements=DISAGREE):
    calls = []

    def call(**request):
        calls.append(request)
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)

    logs = [] if logs is None else logs
    changed = extractor._recount_sides_from_photo(data, disagreements,
                                                   [{"type": "text", "text": "doc"}],
                                                   call, "m", "sys", False, logs.append)
    return changed, calls


def _side(data, edge):
    [side] = [side for side in data["castellated_sides"] if side["edge"] == edge]
    return side


def test_the_calibration_bound_stays_below_half_a_pitch():
    """Precondition of the cases below: a row one notch off is beyond the bound."""
    assert 0 < BOUND < PITCH / 2


@pytest.mark.parametrize("fraction", [0.0, 0.43, -0.9])
def test_a_recount_that_agrees_with_the_photo_replaces_the_side(fraction):
    """Positive: the photo's row is the declared row, shifted within the calibration bound."""
    data, logs = _answer(3), []
    changed, calls = _recount(data, _answer(4), logs,
                              disagreements=_photo(shift=fraction * BOUND))
    assert changed and len(calls) == 1
    right = _side(data, "right")
    assert right["count"] == 4 and right["pin_names"] == ["G", "R", "T", "V"]
    prompt = calls[0]["content_blocks"][-1]["text"]
    assert "4 semi-fori" in prompt       # the photo's measure is told
    assert any("ricontati dopo il confronto con la foto" in line for line in logs)


@pytest.mark.parametrize("fraction", [1.5, -1.5])
def test_a_row_shifted_beyond_the_calibration_bound_is_not_adopted(fraction):
    """Negative twin: same count, same spacing, but a shift the calibration cannot explain."""
    data, logs = _answer(3), []
    changed, _ = _recount(data, _answer(4), logs, disagreements=_photo(shift=fraction * BOUND))
    assert not changed and _side(data, "right")["count"] == 3
    assert any("spostata" in line and "calibrazione" in line for line in logs)


def test_a_row_of_another_spacing_is_not_adopted():
    """Negative: four notches, centred, but 2.3 mm apart: not the declared 2.54 mm row."""
    data, logs = _answer(3), []
    changed, _ = _recount(data, _answer(4), logs, disagreements=_photo(pitch=2.3))
    assert not changed and _side(data, "right")["count"] == 3
    assert any("non cadono sui semi-fori" in line for line in logs)


def test_a_count_equal_to_the_photo_but_a_notch_off_on_the_edge_is_not_adopted():
    """Breaking the circle: the photo row is four of a CENTRED row of five.

    That is what an end contact hidden on the photo looks like: a recount
    to four would put the four pads half a pitch away from the notches.
    """
    data, logs = _answer(3), []
    changed, _ = _recount(data, _answer(4), logs, disagreements=_photo(shift=-PITCH / 2))
    assert not changed and _side(data, "right")["count"] == 3


def test_without_a_calibration_bound_no_shift_is_allowed():
    """An EdgeCount without shift_bound_mm admits only the centre tolerance."""
    data = _answer(3)
    changed, _ = _recount(data, _answer(4), disagreements=_photo(shift=0.43 * BOUND, bound=None))
    assert not changed


def test_a_recount_that_confirms_the_answer_keeps_it():
    """Negative: the model keeps 3; the photo does not overrule the documents."""
    data = _answer(3)
    changed, _ = _recount(data, _answer(3))
    assert not changed
    assert [s["count"] for s in data["castellated_sides"]] == [3, 3]


def test_a_recount_with_an_overlapping_numbering_is_not_adopted():
    data = _answer(3)
    changed, _ = _recount(data, _answer(4, right_first=2))   # 2..5 overlaps 1..3
    assert not changed and data["castellated_sides"][1]["count"] == 3


def test_a_recount_that_leaves_a_gap_in_the_numbering_is_not_adopted():
    """Pads 1-3 on the left and 5-8 on the right: pad 4 would not exist (audit A2/E1)."""
    data, logs = _answer(3), []
    changed, _ = _recount(data, _answer(4, right_first=5), logs)
    assert not changed and _side(data, "right")["count"] == 3
    assert any("buco" in line for line in logs)


def test_a_failed_recount_keeps_the_answer():
    data, logs = _answer(3), []
    changed, _ = _recount(data, OpenRouterError("HTTP 500"), logs)
    assert not changed and data["castellated_sides"][1]["count"] == 3
    assert any("non riuscito" in line for line in logs)


@pytest.mark.parametrize("error", [KeyError("castellated_sides"), ValueError("bad number"),
                                   http.client.IncompleteRead(b"{\"cast"), TypeError("x")])
def test_any_failure_of_the_recount_is_contained(error):
    """Not only OpenRouterError: the photo inventory must not be lost to the recount."""
    data, logs = _answer(3), []
    before = copy.deepcopy(data)
    changed, _ = _recount(data, error, logs)
    assert not changed and data == before
    assert any("non riuscito" in line and type(error).__name__ in line for line in logs)


def test_a_failure_while_reading_the_reply_is_contained(monkeypatch):
    def broken(_data):
        raise ValueError("unreadable")

    monkeypatch.setattr(extractor, "_canonicalize_answer", broken)
    data, logs = _answer(3), []
    before = copy.deepcopy(data)
    changed, _ = _recount(data, _answer(4), logs)
    assert not changed and data == before
    assert any("non riuscito" in line for line in logs)


@pytest.mark.parametrize("cancel", [CallCancelled("stop"), extractor.ExtractionCancelled("stop")])
def test_a_cancellation_is_never_swallowed(cancel):
    with pytest.raises(CallCancelled):
        _recount(_answer(3), cancel)


def test_a_reply_that_omits_the_view_is_read_as_the_top_view_asked_for():
    """No «DUBBIO vista» note is made up: the request asked contacts_view="top"."""
    reply = _answer(4)
    del reply["contacts_view"]
    data, logs = _answer(3), []
    changed, _ = _recount(data, reply, logs)
    assert changed
    assert not [line for line in logs if "DUBBIO vista" in line]
    assert not [note for note in data.get("_canonical_notes") or () if "DUBBIO vista" in note]


def test_a_reply_with_an_unknown_view_still_gets_the_doubt():
    """Negative twin: a view the model states but that means nothing is a real doubt."""
    reply = _answer(4)
    reply["contacts_view"] = "sideways"
    data, logs = _answer(3), []
    _recount(data, reply, logs)
    assert any("DUBBIO vista" in line for line in logs)


def test_a_recount_seen_from_below_is_converted_before_the_comparison():
    """The model answers the mirrored view: its «left» is the right edge seen from above.

    The note of that conversion is logged and kept with the answer (point 139, G2 d).
    """
    reply = _answer(4)
    reply["contacts_view"] = "bottom"
    for side in reply["castellated_sides"]:
        side["edge"] = {"left": "right", "right": "left"}[side["edge"]]
    data, logs = _answer(3), []
    changed, _ = _recount(data, reply, logs)
    assert changed
    assert {s["edge"]: s["count"] for s in data["castellated_sides"]} == {"left": 3, "right": 4}
    assert any("vista dal basso" in line and "Ricontrollo" in line for line in logs)
    assert any("vista dal basso" in note for note in data["_canonical_notes"])


# ── The numbering direction (R-M1) ───────────────────────────────────────


def _reversed_answer(right_count, **reply_side):
    answer = _answer(right_count)
    answer["castellated_sides"][1]["reverse"] = True
    answer["castellated_sides"][1].update(reply_side)
    return answer


def test_a_reply_that_omits_reverse_keeps_the_declared_direction():
    """The mutation that survived the review: an omitted `reverse` flipped the side."""
    reply = _answer(4)
    assert "reverse" not in reply["castellated_sides"][1]
    data = _reversed_answer(3)
    changed, _ = _recount(data, reply)
    assert changed and _side(data, "right")["reverse"] is True


def test_a_reply_that_states_reverse_false_changes_the_direction():
    """Negative twin: the same reply, with reverse stated, is taken."""
    reply = _answer(4)
    reply["castellated_sides"][1]["reverse"] = False
    data = _reversed_answer(3)
    changed, _ = _recount(data, reply)
    assert changed and "reverse" not in _side(data, "right")


def test_a_reply_that_states_reverse_true_is_taken():
    data = _answer(3)
    changed, _ = _recount(data, _reversed_answer(4))
    assert changed and _side(data, "right")["reverse"] is True


def test_a_bottom_view_reply_that_omits_reverse_keeps_the_declared_direction():
    """The mirror writes `reverse` on horizontal sides: that is not the model stating it."""
    data = dict(layout="castellated", body_length_mm=BOARD_L, body_width_mm=BOARD_W,
                drawing_unit="mm", contacts_view="top", notes="",
                castellated_sides=[dict(edge="top", count=3, pitch_mm=PITCH, first_pin=1,
                                        contact_style="half_hole", pin_names=["A", "B", "C"])])
    reply = copy.deepcopy(data)
    reply["contacts_view"] = "bottom"
    reply["castellated_sides"][0].update(count=4, pin_names=["D", "C", "B", "A"])
    photo = _photo(edge="top", edge_length=BOARD_L)
    changed, _ = _recount(data, reply, disagreements=photo)
    assert changed
    [top] = data["castellated_sides"]
    assert top["count"] == 4 and top["pin_names"] == ["A", "B", "C", "D"]
    assert "reverse" not in top


# ── What is taken from the reply ─────────────────────────────────────────


def test_only_the_count_and_the_numbering_of_the_side_are_taken():
    """The reply also changes pitch, style and pads: none of that is adopted (G2 a)."""
    reply = _answer(4)
    right = reply["castellated_sides"][1]
    right.update(pitch_mm=1.27, offset_mm=0.4, contact_style="through_hole", pad_length_mm=9.0)
    data = _answer(3)
    before = copy.deepcopy(_side(data, "right"))
    changed, _ = _recount(data, reply)
    assert changed
    after = _side(data, "right")
    assert after["count"] == 4 and after["pin_names"] == ["G", "R", "T", "V"]
    assert {key: after.get(key) for key in ("pitch_mm", "offset_mm", "contact_style",
                                             "pad_length_mm")} == {
        key: before.get(key) for key in ("pitch_mm", "offset_mm", "contact_style",
                                         "pad_length_mm")}


def test_a_recount_with_names_of_another_length_is_not_adopted():
    reply = _answer(4)
    reply["castellated_sides"][1]["pin_names"] = ["G", "R", "T"]
    data, logs = _answer(3), []
    changed, _ = _recount(data, reply, logs)
    assert not changed and _side(data, "right")["pin_names"] == ["G", "R", "T"]
    assert any("3 nomi per 4 contatti" in line for line in logs)


def test_a_recount_that_drops_the_names_is_not_adopted():
    reply = _answer(4)
    del reply["castellated_sides"][1]["pin_names"]
    data = _answer(3)
    changed, _ = _recount(data, reply)
    assert not changed and _side(data, "right")["count"] == 3


def test_a_side_the_photo_did_not_dispute_is_never_changed():
    """The reply rewrites the left side too: only the disputed right side is adopted (D4)."""
    reply = _answer(4)
    reply["castellated_sides"][0].update(count=2, pin_names=["PE", "B-"])
    data = _answer(3)
    changed, _ = _recount(data, reply)
    assert changed
    assert _side(data, "left")["count"] == 3
    assert _side(data, "left")["pin_names"] == ["PE", "B-", "A+"]
    assert _side(data, "right")["count"] == 4


class _Stop(Exception):
    pass


def test_the_inventory_asks_for_the_recount_when_the_photo_disagrees(tmp_path):
    """The hook is called with the measured disagreement, before any detector runs."""
    from kicadgen.services.photo_inventory import extract_inventory
    path = tmp_path / "top.png"
    cv2.imwrite(str(path), board_photo(right=ROW_4))
    declared = dict(layout="castellated", castellated_sides=[
        dict(edge="right", count=3, pitch_mm=2.54, contact_style="half_hole")])
    seen = []

    def recount(disagreements):
        seen.extend(disagreements)
        raise _Stop

    with pytest.raises(_Stop):
        extract_inventory([path], "m", LENGTH, WIDTH, ["generic"], lambda **_: {},
                          top_photo_index=0, detector="vlm", declared=declared,
                          recount_contacts=recount)
    assert [(item.edge, item.found, item.declared) for item in seen] == [("right", 4, 3)]


def test_no_recount_when_the_photo_agrees(tmp_path):
    from kicadgen.services.photo_inventory import extract_inventory
    path = tmp_path / "top.png"
    cv2.imwrite(str(path), board_photo(right=ROW_4))
    declared = dict(layout="castellated", castellated_sides=[
        dict(edge="right", count=4, pitch_mm=2.54, contact_style="half_hole")])
    called = []
    logs = []

    def first_paid_call(**_request):
        raise _Stop    # the check is done by now; nothing else is under test

    with pytest.raises(_Stop):
        extract_inventory([path], "m", LENGTH, WIDTH, ["generic"], first_paid_call,
                          progress=logs.append, top_photo_index=0, detector="vlm",
                          declared=declared, recount_contacts=called.append)
    assert not called
    assert any("4 semi-fori, come i contatti dichiarati" in line for line in logs)


def test_the_inventory_warnings_reach_the_progress_as_warnings(tmp_path):
    """The progress card highlights warnings by their kind, never by their words (point 136)."""
    from kicadgen.services.messages import WARNING, kind_of
    from kicadgen.services.photo_inventory import extract_inventory
    path = tmp_path / "top.png"
    cv2.imwrite(str(path), board_photo(right=ROW_4))
    declared = dict(layout="castellated", castellated_sides=[
        dict(edge="right", count=3, pitch_mm=2.54, contact_style="half_hole")])
    logs = []

    def first_paid_call(**_request):
        raise _Stop

    with pytest.raises(_Stop):
        extract_inventory([path], "m", LENGTH, WIDTH, ["generic"], first_paid_call,
                          progress=logs.append, top_photo_index=0, detector="vlm",
                          declared=declared)
    [warning] = [line for line in logs if "semi-fori" in line]
    assert kind_of(warning) == WARNING
    assert all(kind_of(line) != WARNING for line in logs if "scheda trovata" in line)
