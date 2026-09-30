"""@brief Declared castellated sides the photo contradicts or cannot verify (HANDOFF point 164).

User's report of 2026-09-30 («questi non ci devono essere»): the model
declared two contacts on the top and bottom edges of a board whose photo
shows a straight outline there, and the sides reached the footprint and the
3D because only a DIFFERENT count was ever questioned. Rules, from the data
the photo already measures (edge_notches.EdgeCount):

* a clean edge (`no_notch`) excludes the declared side, with no paid call;
* an edge the photo cannot count is asked again to the model, which may
  answer zero (excluded) or confirm (kept, with a warning that the photo
  could not verify it);
* a different count keeps the recount of point 137.

Each rule has its positive and its negative case, told apart by the state
of the outline, never by a tuned number.
"""
import copy

import cv2
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services.edge_notches import EdgeCount
from kicadgen.services.messages import WARNING, kind_of
from test_edge_notches import LENGTH, ROW_4, WIDTH, board_photo
from test_photo_contact_recount import BOARD_L, BOARD_W, PITCH, _answer, _photo, _side


def _with_sides(data, *sides):
    """`data` with extra declared sides appended (dicts as the model writes them)."""
    data = copy.deepcopy(data)
    data["castellated_sides"] = list(data["castellated_sides"]) + [dict(side) for side in sides]
    return data


def _top(count=2, first_pin=8, style="half_and_through"):
    return dict(edge="top", count=count, pitch_mm=PITCH, first_pin=first_pin,
                contact_style=style, pin_names=["NC", "120R"][:count])


def _run(data, disagreements, reply=None):
    """Run the recount with `reply` (None: the call must not happen); return (changed, calls, logs)."""
    calls, logs = [], []

    def call(**request):
        calls.append(request)
        assert reply is not None, "a paid call was made where none was expected"
        return copy.deepcopy(reply)

    changed = extractor._recount_sides_from_photo(
        data, disagreements, [{"type": "text", "text": "doc"}], call, "m", "sys", False,
        logs.append)
    return changed, calls, logs


CLEAN_TOP = EdgeCount("top", 0, 2, [], reason="nessuna tacca visibile sul bordo", no_notch=True)
UNCERTAIN_TOP = EdgeCount("top", None, 2, [], reason="tacche poco distinte dal rumore del contorno")


# ── The photo: a clean edge is a measurement, an unreadable one is a doubt ─

def test_the_photo_reports_a_declared_side_on_a_clean_edge_and_a_half_and_through_count(tmp_path):
    """@brief Positive: top declared, top straight -> found 0, no_notch. Negative: the right side,
    which the photo confirms, is not listed; a half_and_through side is compared like a half_hole one."""
    from kicadgen.services.photo_inventory import extract_inventory
    path = tmp_path / "top.png"
    cv2.imwrite(str(path), board_photo(right=ROW_4))
    declared = dict(layout="castellated", castellated_sides=[
        dict(edge="right", count=4, pitch_mm=2.54, contact_style="half_and_through"),
        dict(edge="top", count=2, pitch_mm=2.54, contact_style="half_and_through")])
    seen = []

    class _Stop(Exception):
        pass

    def recount(disagreements):
        seen.extend(disagreements)
        raise _Stop

    with pytest.raises(_Stop):
        extract_inventory([path], "m", LENGTH, WIDTH, ["generic"], lambda **_: {},
                          top_photo_index=0, detector="vlm", declared=declared,
                          recount_contacts=recount)
    assert [(item.edge, item.found, item.declared, item.no_notch) for item in seen] == [
        ("top", 0, 2, True)]
    # A different count on a half_and_through side is a disagreement too.
    seen.clear()
    declared["castellated_sides"][0]["count"] = 3
    with pytest.raises(_Stop):
        extract_inventory([path], "m", LENGTH, WIDTH, ["generic"], lambda **_: {},
                          top_photo_index=0, detector="vlm", declared=declared,
                          recount_contacts=recount)
    assert sorted((item.edge, item.found, item.declared) for item in seen) == [
        ("right", 4, 3), ("top", 0, 2)]


def test_a_single_notch_is_not_an_unreadable_edge(tmp_path):
    """@brief Negative: one measured notch against 19 declared stays a log line (no paid recheck)."""
    from kicadgen.services.edge_notches import count_disagreements
    from test_edge_notches_ends import calibrated_profiles, tilted_board
    image = tilted_board(40, 16, 20, notches={"top": [20.0]}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    sides = [dict(edge="top", count=19, pitch_mm=2.54, contact_style="half_and_through")]
    assert count_disagreements(profiles, shape, 40, 16, sides) == []


# ── A clean edge: the side goes, without paying ───────────────────────────

def test_a_side_declared_on_a_clean_edge_is_excluded_without_a_call():
    data = _with_sides(_answer(4), _top())
    changed, calls, logs = _run(data, [CLEAN_TOP])
    assert changed and calls == []
    assert [side["edge"] for side in data["castellated_sides"]] == ["left", "right"]
    [warning] = [line for line in logs if kind_of(line) == WARNING]
    assert "superiore" in warning and "lato escluso" in warning and "2 contatti" in warning
    assert "Lati esclusi dopo il confronto con la foto" in data["notes"]
    assert "rinumerati" not in data["notes"], "1-3 and 4-7 stay contiguous: nothing renumbered"
    assert _side(data, "right")["first_pin"] == 4


def test_excluding_a_side_in_the_middle_of_the_numbering_renumbers_the_rest():
    """@brief Positive: left 1-3, top 4-5, right 6-9 -> right becomes 4-7, and the note says so."""
    data = _with_sides(_answer(4, right_first=6), _top(first_pin=4))
    changed, calls, _logs = _run(data, [CLEAN_TOP])
    assert changed and calls == []
    assert _side(data, "left")["first_pin"] == 1 and _side(data, "right")["first_pin"] == 4
    assert "rinumerati" in data["notes"]
    assert extractor._numbering_is_contiguous(copy.deepcopy(data["castellated_sides"]))


def test_a_clean_edge_nobody_declared_changes_nothing():
    """@brief Negative: an EdgeCount for an edge with no declared side is not a side to drop."""
    data = _answer(4)
    before = copy.deepcopy(data)
    changed, calls, logs = _run(data, [CLEAN_TOP])
    assert not changed and calls == [] and data == before and logs == []


# ── An edge the photo cannot read: the model looks again at that edge ────

def _reply(data, top_count):
    reply = copy.deepcopy(data)
    if top_count is None:
        reply["castellated_sides"] = [s for s in reply["castellated_sides"] if s["edge"] != "top"]
    else:
        _side(reply, "top")["count"] = top_count
        _side(reply, "top")["pin_names"] = ["NC", "120R"][:top_count]
    return reply


def test_an_unreadable_edge_is_asked_again_and_zero_excludes_the_side():
    data = _with_sides(_answer(4), _top())
    changed, calls, logs = _run(data, [UNCERTAIN_TOP], reply=_reply(data, 0))
    assert changed and len(calls) == 1
    prompt = calls[0]["content_blocks"][-1]["text"]
    assert "ingrandimento del bordo top" in prompt and "count 0" in prompt
    assert [side["edge"] for side in data["castellated_sides"]] == ["left", "right"]
    [warning] = [line for line in logs if kind_of(line) == WARNING]
    assert "lato escluso" in warning and "ricontrollo AI" in warning


def test_an_unreadable_edge_the_model_leaves_out_is_excluded_too():
    data = _with_sides(_answer(4), _top())
    changed, calls, _logs = _run(data, [UNCERTAIN_TOP], reply=_reply(data, None))
    assert changed and len(calls) == 1
    assert [side["edge"] for side in data["castellated_sides"]] == ["left", "right"]


def test_an_unreadable_edge_the_model_confirms_is_kept_with_a_warning():
    """@brief Negative of the two above: a confirmed count keeps the declared side, unchanged."""
    data = _with_sides(_answer(4), _top())
    before = copy.deepcopy(data["castellated_sides"])
    changed, calls, logs = _run(data, [UNCERTAIN_TOP], reply=_reply(data, 2))
    assert not changed and len(calls) == 1
    assert data["castellated_sides"] == before
    [warning] = [line for line in logs if kind_of(line) == WARNING]
    assert "da verificare a mano" in warning and "superiore" in warning


def test_a_new_number_for_an_unreadable_edge_is_never_adopted():
    """@brief The photo cannot check it: 3 instead of 2 keeps the declared 2 (with the warning)."""
    data = _with_sides(_answer(4), _top())
    changed, _calls, logs = _run(data, [UNCERTAIN_TOP], reply=_reply(data, 3))
    assert not changed and _side(data, "top")["count"] == 2
    assert any(kind_of(line) == WARNING for line in logs)


# ── The three kinds in one run ────────────────────────────────────────────

def test_a_clean_edge_and_a_miscounted_side_are_handled_in_the_same_run():
    """@brief The right side is recounted (point 137) and the top side excluded (point 164)."""
    data = _with_sides(_answer(3, right_first=6), _top(first_pin=4))
    # The prompt lists the sides AFTER the exclusion (top gone, right
    # renumbered 4-6): the reply follows that numbering.
    reply = _answer(4, right_first=4)
    changed, calls, _logs = _run(data, [CLEAN_TOP] + _photo(shift=0.0), reply=reply)
    assert changed and len(calls) == 1
    assert [side["edge"] for side in data["castellated_sides"]] == ["left", "right"]
    assert _side(data, "right")["count"] == 4
    assert extractor._numbering_is_contiguous(copy.deepcopy(data["castellated_sides"]))
