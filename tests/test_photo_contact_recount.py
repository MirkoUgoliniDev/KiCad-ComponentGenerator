"""@brief Sides the photo counts differently are recounted once by the model (HANDOFF point 137).

User decision of 2026-09-28: when the half-holes counted on the calibrated
top photo disagree with a declared side, one paid recount of THAT side, told
what the photo measured. The recounted side is adopted only if it now agrees
with the photo and the pad numbering stays coherent; otherwise the answer
stays and the disagreement remains a warning.
"""
import copy

import cv2
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services.edge_notches import EdgeCount
from kicadgen.services.openrouter import OpenRouterError
from test_edge_notches import LENGTH, ROW_4, WIDTH, board_photo


def _answer(right_count, right_first=4, **extra):
    names = ["G", "R", "T", "V"][:right_count]
    return dict(layout="castellated", body_length_mm=42.8, body_width_mm=15.2,
                drawing_unit="mm", contacts_view="top", _canonical=True, notes="",
                castellated_sides=[
                    dict(edge="left", count=3, pitch_mm=5.08, first_pin=1,
                         contact_style="half_hole", pin_names=["PE", "B-", "A+"]),
                    dict(edge="right", count=right_count, pitch_mm=2.54, first_pin=right_first,
                         contact_style="half_hole", pin_names=names)], **extra)


DISAGREE = [EdgeCount("right", 4, 3, [3.52, 6.0, 8.55, 11.1])]


def _recount(data, reply, logs=None):
    calls = []

    def call(**request):
        calls.append(request)
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)

    logs = [] if logs is None else logs
    changed = extractor._recount_sides_from_photo(data, DISAGREE, [{"type": "text", "text": "doc"}],
                                                   call, "m", "sys", False, logs.append)
    return changed, calls


def test_a_recount_that_agrees_with_the_photo_replaces_the_side():
    data, logs = _answer(3), []
    changed, calls = _recount(data, _answer(4), logs)
    assert changed and len(calls) == 1
    right = [side for side in data["castellated_sides"] if side["edge"] == "right"][0]
    assert right["count"] == 4 and right["pin_names"] == ["G", "R", "T", "V"]
    prompt = calls[0]["content_blocks"][-1]["text"]
    assert "4 semi-fori" in prompt and "3.52" in prompt     # the photo's measure is told
    assert any("ricontati dopo il confronto con la foto" in line for line in logs)


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


def test_a_failed_recount_keeps_the_answer():
    data, logs = _answer(3), []
    changed, _ = _recount(data, OpenRouterError("HTTP 500"), logs)
    assert not changed and data["castellated_sides"][1]["count"] == 3
    assert any("non riuscito" in line for line in logs)


def test_a_recount_seen_from_below_is_converted_before_the_comparison():
    """The model answers the mirrored view: its «left» is the right edge seen from above."""
    reply = _answer(4)
    del reply["_canonical"]
    reply["contacts_view"] = "bottom"
    for side in reply["castellated_sides"]:
        side["edge"] = {"left": "right", "right": "left"}[side["edge"]]
    data = _answer(3)
    changed, _ = _recount(data, reply)
    assert changed
    assert {s["edge"]: s["count"] for s in data["castellated_sides"]} == {"left": 3, "right": 4}


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
