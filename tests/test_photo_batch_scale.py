"""@brief Offline scale/count evidence contracts for mechanical contact sheets."""
from copy import deepcopy
import json
from unittest.mock import Mock

import numpy as np
import pytest

from kicadgen.services import photo_grounding as grounding, openrouter


@pytest.fixture(autouse=True)
def no_paid_api_calls(monkeypatch):
    """@brief Fail any call to the AI client, even one that would never reach the socket.

    @note Named apart from conftest's autouse `no_network` guard: a fixture
        with the same name used to override it in this module.
    """
    def forbidden(*args, **kwargs):
        pytest.fail("Contact-sheet tests must not call an API")
    monkeypatch.setattr(openrouter, "call_tool", forbidden)
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", forbidden)


def component(identity=1, **extra):
    return dict(_id=identity, _box=(.2, .2, .4, .4), kind="generic", name="body",
                length_mm=extra.pop("length_mm", 2.3), width_mm=extra.pop("width_mm", .9),
                **extra)


def measurements(block):
    assert block["type"] == "text"
    return json.loads(block["text"].split("\nMeasurements:\n", 1)[1])


def test_mechanical_batches_append_corresponding_scale_after_image(monkeypatch):
    image = np.zeros((100, 200, 3), np.uint8)
    bodies = [component(identity=100+i, length_mm=.8+i*.37, width_mm=.3+i*.02)
              for i in range(25)]
    before = deepcopy(bodies)
    sheets = []
    def contact_sheet(photo, batch):
        sheets.append([c["_id"] for c in batch])
        return image
    monkeypatch.setattr(grounding, "contact_sheet", contact_sheet)
    requests = []
    def call(**kwargs):
        requests.append(kwargs)
        blocks = kwargs["content_blocks"]
        assert blocks[0]["type"] == "image_url"
        assert len(blocks) == 2
        rows = measurements(blocks[1])
        assert [r["id"] for r in rows] == sheets[-1]
        return {"components": [dict(id=r["id"], present=True, confidence="high", package="")
                               for r in rows]}
    result = grounding.mechanical_envelopes(image, bodies, "offline", call, lambda _: None,
                                            None, verification_prompt="Stored occupancy prompt")
    assert len(requests) == 2 and len(result) == len(bodies)
    rows = [r for req in requests for r in measurements(req["content_blocks"][1])]
    assert rows == [dict(id=c["_id"], length_mm=c["length_mm"], width_mm=c["width_mm"])
                    for c in bodies]
    assert bodies == before
    assert requests[0]["tool"] == requests[1]["tool"]


def test_scale_is_estimated_envelope_and_never_package_identity():
    block = grounding._batch_geometry_block([component()])
    text = block["text"]
    for evidence in ("independently resized", "calibrated photographic", "PCB solder pads",
                     "never identify a package from size alone", "gull-wing", "leadless",
                     "leave package empty"):
        assert evidence in text
    assert set(measurements(block)[0]) == {"id", "length_mm", "width_mm"}


def test_appended_text_requires_exact_mechanical_tool_root_and_every_id():
    text = grounding._batch_geometry_block([component()])["text"]
    assert "Call verify_mounted_bodies with a JSON object containing a components array" in text
    assert "For every supplied ID include id, present, confidence, package" in text
    assert "Never return an empty object" in text
    assert "package must be an empty string when unknown" in text


def test_explicit_pin_and_observed_terminal_counts_remain_distinct():
    c = component(pin_count=8, pins_by_side={"left": 4}, terminal_geometry={
        "confidence": "high", "terminals": [{"bbox": [0, .2, .2, .3]},
                                               {"bbox": [.8, .4, 1, .5]}]})
    before = deepcopy(c)
    block = grounding._batch_geometry_block([c])
    row = measurements(block)[0]
    assert row["pin_count"] == 8 and row["observed_terminal_count"] == 2
    assert row["pins_by_side"] == {"left": 4}  # Do not fill sides or infer a total.
    assert "not hidden pins or the complete package pin count" in block["text"]
    assert c == before


@pytest.mark.parametrize("count", [None, True, False, 0, -1, "8", 2.5])
def test_missing_or_invalid_counts_are_not_inferred_from_package(count):
    row = measurements(grounding._batch_geometry_block([
        component(pin_count=count, package="SOIC-8", kind_hint="eight pins")]))[0]
    assert "pin_count" not in row and "observed_terminal_count" not in row


@pytest.mark.parametrize("geometry", [None, {}, {"confidence": "high", "terminals": []},
    {"confidence": "low", "terminals": [{"bbox": [0, 0, .2, .2]}]},
    {"confidence": "high", "terminals": [{}]},
    {"confidence": "high", "terminals": [{"bbox": [True, 0, .2, .2]}]}])
def test_unresolved_or_invalid_terminal_records_do_not_claim_a_count(geometry):
    row = measurements(grounding._batch_geometry_block([component(terminal_geometry=geometry)]))[0]
    assert "observed_terminal_count" not in row


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), "2.3"])
def test_invalid_dimensions_are_unknown_not_fabricated(value):
    row = measurements(grounding._batch_geometry_block([component(length_mm=value)]))[0]
    assert row["length_mm"] is None and row["width_mm"] == .9


def test_changed_calibration_invalidates_cache_even_when_contact_sheet_is_identical(tmp_path):
    image = np.zeros((100, 200, 3), np.uint8)
    call = Mock(return_value={"components": [dict(id=1, present=True, confidence="high", package="")]})
    original = component()
    grounding.mechanical_envelopes(image, [original], "offline", call, lambda _: None, tmp_path)
    grounding.mechanical_envelopes(image, [original], "offline", call, lambda _: None, tmp_path)
    assert call.call_count == 1
    grounding.mechanical_envelopes(image, [dict(original, length_mm=4.6)], "offline",
                                   call, lambda _: None, tmp_path)
    assert call.call_count == 2
    assert call.call_args_list[0].kwargs["content_blocks"][0] == call.call_args.kwargs["content_blocks"][0]
    assert measurements(call.call_args.kwargs["content_blocks"][1])[0]["length_mm"] == 4.6
