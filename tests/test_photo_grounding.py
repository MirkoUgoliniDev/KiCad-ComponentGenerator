"""Offline contracts for native grounding, fixed classification and inventory.

No real photo, credentials or model responses are required. All external calls
are injected; an autouse guard also fails accidental OpenRouter/HTTP access.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from kicadgen.services import openrouter
from kicadgen.services import photo_grounding as grounding
from kicadgen.services import photo_inventory as inventory

KINDS = ("generic", "resistor", "capacitor", "diode", "diode_melf", "ic_soic")
FULL = (0., 0., 1., 1.)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Grounding tests must not call an API")
    monkeypatch.setattr(openrouter, "call_tool", forbidden)
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", forbidden)


@pytest.fixture
def photo():
    # Different pixels in each third prevent accidental cache-key collisions
    # between equally sized crops of a uniform synthetic image.
    image = np.empty((90, 301, 3), np.uint8)
    image[:, :, 0] = np.arange(301, dtype=np.uint16) % 251
    image[:, :, 1] = np.arange(90, dtype=np.uint8)[:, None]
    image[:, :, 2] = 80
    return image


def native(box=None, **updates):
    result = {"box_2d": [200, 100, 400, 300] if box is None else box,
              "label": "Visible resistor", "kind": "resistor"}
    result.update(updates)
    return result


def decode(entries, region=FULL, log=None):
    return grounding.decode_boxes({"components": entries}, region, 40, 20,
                                  KINDS, "test view", log or (lambda message: None))


def component(identifier=1, box=None):
    result = decode([native(box)])[0]
    result["_id"] = identifier
    result["_classification_validated"] = False
    return result


def vote(identifier=1, **updates):
    result = dict(id=identifier, is_component=True, is_complete_package=True,
                  name="Resistor", kind="resistor", height_mm=.7,
                  rotation_deg=0, color_hex="#A0b1C2", confidence="high",
                  mating="none", package="R_0402",
                  observation="Rectangular package with two visible terminals")
    result.update(updates)
    return result


def classify(components, entries, log=None):
    return grounding.apply_classifications(components, {"components": entries},
                                          KINDS, 40, 20, log or (lambda message: None))


def api_script(*responses):
    """Strict finite script: unexpected extra calls fail instead of using a model."""
    pending = iter(responses)
    calls = []

    def call(**kwargs):
        calls.append(deepcopy(kwargs))
        try:
            response = next(pending)
        except StopIteration:
            pytest.fail("Unexpected extra API call")
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)
    return call, calls


def run_grounding(photo, call, **kwargs):
    # Detailed recognition is now an explicit legacy opt-in. Mechanical
    # integration tests below call the production function without this helper.
    kwargs.setdefault("mechanical", False)
    return grounding.grounded_components(photo, "test-model", 40, 20, KINDS,
                                         call, kwargs.pop("log", lambda message: None), **kwargs)


def test_native_schema_declares_yx_integer_coordinates_and_no_semantic_markings():
    properties = grounding.grounding_tool(KINDS)["function"]["parameters"]["properties"]
    item = properties["components"]["items"]
    box = item["properties"]["box_2d"]
    assert box["items"] == {"type": "integer", "minimum": 0, "maximum": 1000}
    assert box["minItems"] == box["maxItems"] == 4
    assert set(item["required"]) == {"box_2d", "label", "kind"}
    assert "[ymin,xmin,ymax,xmax]" in grounding.GROUNDING_PROMPT


def test_native_coordinate_order_crop_transform_and_metric_axes():
    c = decode([native()], region=(.25, .1, .75, .9))[0]
    assert c["_box"] == pytest.approx((.30, .26, .40, .42))
    assert (c["x_mm"], c["y_mm"]) == pytest.approx((-6, -3.2))
    assert (c["length_mm"], c["width_mm"]) == pytest.approx((4, 3.2))
    assert c["rotation_deg"] == 0
    assert c["marking"] == ""
    assert c["confidence"] == "low" and c["_uncertain"]


def test_native_vertical_package_and_unknown_type_are_retained():
    c = decode([native([100, 200, 500, 250], kind="invented", label="")])[0]
    assert c["kind"] == "generic" and c["name"]
    assert (c["length_mm"], c["width_mm"], c["rotation_deg"]) == pytest.approx((8, 2, 90))


def test_mathematically_integral_native_coordinates_are_valid():
    assert decode([native([200.0, 100.0, 400.0, 300.0])]) == decode([native()])


@pytest.mark.parametrize("bad", [
    None, [], "object", 12, {}, native("200,100,400,300"), native([1, 2, 3]),
    native([1, 2, 3, 4, 5]), native([True, 100, 400, 300]),
    native(["200", 100, 400, 300]), native([float("nan"), 100, 400, 300]),
    native([200, 100, float("inf"), 300]), native([200, -1, 400, 300]),
    native([200, 100, 1001, 300]), native([400, 100, 200, 300]),
    native([200, 300, 400, 100]), native([200, 100, 200, 300]),
    native([200, 100, 400, 100]),
])
def test_invalid_native_candidate_is_logged_without_losing_valid_sibling(bad):
    messages = []
    result = decode([bad, native(label="keep")], log=messages.append)
    assert len(result) == 1 and result[0]["name"] == "keep"
    assert len(messages) == 1 and "scartato" in messages[0]


def test_fractional_native_coordinates_violate_integer_contract():
    assert decode([native([200.5, 100, 400, 300])]) == []


@pytest.mark.parametrize("data", [None, [], {}, {"components": None}, {"components": {}}])
def test_missing_native_component_list_is_a_stage_error(data):
    with pytest.raises(ValueError):
        grounding.decode_boxes(data, FULL, 40, 20, KINDS, "test", lambda message: None)


@pytest.mark.parametrize("box,region", [
    ([200, 0, 400, 300], (.25, 0, .75, 1)),
    ([200, 2, 400, 300], (.25, 0, .75, 1)),
    ([200, 100, 400, 998], (.25, 0, .75, 1)),
    ([200, 100, 400, 1000], (.25, 0, .75, 1)),
])
def test_partial_boxes_on_interior_tile_edges_are_rejected(box, region):
    assert decode([native(box)], region=region) == []


def test_full_view_board_edges_and_just_inside_tile_margin_are_allowed():
    assert len(decode([native([0, 0, 1000, 1000])])) == 1
    assert len(decode([native([200, 3, 400, 997])], region=(.25, 0, .75, 1))) == 1


def test_regions_match_actual_pixel_slices_and_partition_ownership(photo):
    regions = list(grounding.grounding_regions(photo))
    assert len(regions) == 4
    assert regions[0][1:3] == (FULL, (0., 1.))
    assert regions[0][3] is photo
    for i, (_, region, ownership, crop) in enumerate(regions[1:]):
        assert ownership == (i/3, (i+1)/3)
        left, right = round(region[0]*301), round(region[2]*301)
        assert np.array_equal(crop, photo[:, left:right])
        assert region[0] <= ownership[0] < ownership[1] <= region[2]
    assert regions[1][1][2] > regions[2][1][0]
    assert regions[2][1][2] > regions[3][1][0]


def test_tile_ownership_is_half_open_without_merging_to_hide_duplicates(monkeypatch, photo):
    # Every view reports the same packages. Boundaries are exactly representable
    # native centers here; the ownership stage alone must assign one copy each.
    monkeypatch.setattr(grounding, "grounding_regions", lambda image: iter([
        ("left", FULL, (0, .5), image), ("right", FULL, (.5, 1), image)]))
    entries = [native([100, 190, 200, 210], label="left"),
               native([100, 490, 200, 510], label="boundary"),
               native([100, 790, 200, 810], label="right")]
    observed_groups = []
    real_merge = inventory.merge_components

    def capture(groups, log, **kwargs):
        observed_groups.append(deepcopy(groups))
        return real_merge(groups, log, **kwargs)
    monkeypatch.setattr(inventory, "merge_components", capture)
    call, _ = api_script({"components": entries}, {"components": entries}, {"components": []})
    result = run_grounding(photo, call)
    assert [[c["name"] for c in group] for group in observed_groups[0]] == [["left"], ["boundary", "right"]]
    assert len(result) == 3 and all(c["_uncertain"] for c in result)


def test_classifier_schema_has_no_geometry_or_marking_fields():
    schema = grounding.classification_tool(KINDS)["function"]["parameters"]["properties"]["components"]["items"]
    assert not ({"marking", "bbox", "_box", "box_2d", "x_mm", "y_mm", "length_mm", "width_mm"} & schema["properties"].keys())
    assert set(schema["required"]) == set(schema["properties"])


def test_classification_updates_semantics_but_cannot_move_boxes_or_restore_marking():
    original = component()
    snapshot = deepcopy(original)
    entry = vote(marking="DO NOT COPY", _box=(0, 0, 1, 1), box_2d=[0, 0, 1000, 1000],
                 x_mm=999, y_mm=888, length_mm=777, width_mm=666)
    result = classify([original], [entry])[0]
    for key in ("_box", "x_mm", "y_mm", "length_mm", "width_mm", "_id", "_source"):
        assert result[key] == original[key]
    assert result["height_mm"] == .7 and result["color_hex"] == "#A0b1C2"
    assert result["marking"] == "" and not result["_uncertain"]
    assert result["_height_source"] == "visual_package_estimate"
    assert original == snapshot and result is not original


@pytest.mark.parametrize("updates", [
    {"is_component": False, "observation": "empty copper pads"},
    {"is_complete_package": False, "observation": "isolated pins from adjacent IC"},
    {"is_component": False, "is_complete_package": False, "observation": "silkscreen"},
])
def test_classifier_rejects_pads_and_partial_pins_without_rejecting_neighbor(updates):
    result = classify([component(1), component(2)], [vote(1, **updates), vote(2)])
    assert [c["_id"] for c in result] == [2]


def test_unknown_electrical_type_is_kept_as_generic():
    original = dict(component(), kind="generic")
    result = classify([original], [vote(kind="generic", confidence="low")])[0]
    assert result["kind"] == "generic" and result["_uncertain"]
    assert result["marking"] == ""


def test_unknown_duplicate_and_missing_ids_never_add_or_drop_grounded_boxes():
    originals = [component(i) for i in range(1, 5)]
    for c in originals:
        c["_uncertain"] = False
    snapshot = deepcopy(originals)
    messages = []
    result = classify(originals, [vote(999), vote(True), vote("3"), None,
                                 vote(1, is_component=False), vote(1), vote(3)], messages.append)
    assert [c["_id"] for c in result] == [1, 2, 3, 4]
    assert [c["_uncertain"] for c in result] == [True, True, False, True]
    assert [c["_box"] for c in result] == [c["_box"] for c in originals]
    assert all(c["marking"] == "" for c in result)
    assert originals == snapshot
    assert any("sconosciuto" in m for m in messages)
    assert any("duplicata" in m for m in messages)


@pytest.mark.parametrize("updates", [
    {"height_mm": 0}, {"height_mm": -1}, {"height_mm": 101},
    {"height_mm": float("inf")}, {"height_mm": float("nan")}, {"height_mm": "bad"},
    {"rotation_deg": True}, {"rotation_deg": 90.5}, {"rotation_deg": 45},
    {"kind": "invented"}, {"confidence": "certain"}, {"color_hex": "#fff"},
    {"color_hex": "#12345g"}, {"color_hex": None},
    {"is_component": 1}, {"is_complete_package": 0},
])
def test_invalid_classification_keeps_original_geometry_uncertain(updates):
    original = component()
    original["_uncertain"] = False
    original["_classification_validated"] = True
    expected = dict(original, _uncertain=True, _classification_validated=False)
    assert classify([original], [vote(**updates)]) == [expected]


def test_boolean_height_is_not_a_numeric_package_estimate():
    original = component()
    assert classify([original], [vote(height_mm=True)])[0] == original


def test_missing_required_semantics_is_uncertain_not_an_implicit_rejection():
    entry = vote()
    del entry["height_mm"]
    assert classify([component()], [entry]) == [component()]


@pytest.mark.parametrize("count", [0, 2, 12, 15])
def test_empty_classifier_objects_retain_every_fixed_box_uncertain(count):
    originals = [component(i+1) for i in range(12)]
    snapshot = deepcopy(originals)
    result = classify(originals, [{} for _ in range(count)])
    assert result == snapshot
    assert all(c["_uncertain"] and c["marking"] == "" for c in result)
    assert originals == snapshot


def test_empty_classifier_objects_do_not_undo_valid_sibling_classification():
    result = classify([component(1), component(2)], [{}, vote(2), {}])
    assert result[0] == component(1)
    assert result[1]["name"] == "Resistor" and not result[1]["_uncertain"]


@pytest.mark.parametrize("identifier", [True, False, 1.25, float("nan"), float("inf"), "1", None, -1, 999])
def test_invalid_or_unknown_ids_are_ignored_without_losing_original(identifier):
    assert classify([component()], [vote(identifier)]) == [component()]


def test_integral_float_id_is_accepted_as_json_schema_integer():
    result = classify([component()], [vote(1.0)])[0]
    assert result["_id"] == 1 and result["name"] == "Resistor"
    assert not result["_uncertain"] and result["marking"] == ""


def test_integer_and_integral_float_ids_count_as_duplicate_votes():
    # The float vote must not evade duplicate protection and leave a single
    # negative integer vote able to remove a package.
    assert classify([component()], [vote(1, is_component=False), vote(1.0)]) == [component()]


@pytest.mark.parametrize("angle,expected,uncertain", [(0, 0, False), (180, 180, False),
                                                       (90, 180, True), (270, 0, True)])
def test_rotation_recomputes_dimensions_from_fixed_box(angle, expected, uncertain):
    result = classify([component()], [vote(rotation_deg=angle)])[0]
    assert result["rotation_deg"] == expected
    assert (result["length_mm"], result["width_mm"]) == pytest.approx((8, 4))
    assert result["_uncertain"] is uncertain


def test_observation_is_bounded_and_nearly_square_orientation_is_preserved():
    c = component(box=[100, 100, 210, 150])  # 2 x 2.2 mm
    result = classify([c], [vote(rotation_deg=180, observation="x"*600)])[0]
    assert result["rotation_deg"] == 180 and not result["_uncertain"]
    assert len(result["_review_reason"]) == 500


@pytest.mark.parametrize("response", [None, {}, {"components": None}, {"components": {}}])
def test_unstructured_classification_is_a_recoverable_stage_error(response):
    with pytest.raises(ValueError):
        grounding.apply_classifications([component()], response, KINDS, 40, 20, lambda message: None)


def test_contact_sheet_has_numbered_fixed_boxes_and_does_not_mutate_inputs(photo):
    items = [component(i+11, box=[100, 50+i*120, 250, 130+i*120]) for i in range(5)]
    snapshot, pixels = deepcopy(items), photo.copy()
    sheet = grounding.contact_sheet(photo, items)
    assert sheet.shape == (360, 960, 3) and sheet.dtype == np.uint8
    for i in range(5):
        cell = sheet[(i//4)*180:(i//4+1)*180, (i%4)*240:(i%4+1)*240]
        assert np.any(np.all(cell[30:] == (255, 190, 0), axis=2))
        assert np.any(np.all(cell[:30] == (0, 0, 0), axis=2))
    assert items == snapshot and np.array_equal(photo, pixels)


@pytest.fixture
def cache_kwargs(photo):
    return dict(model="test-model", system_prompt="locate packages", max_tokens=8192,
                has_pdf=False, tool=grounding.grounding_tool(KINDS),
                content_blocks=[inventory.image_block(photo)])


def test_cache_hit_is_content_addressed_and_does_not_store_request_images(tmp_path, cache_kwargs):
    call = Mock(return_value={"components": [native()]})
    messages = []
    first = grounding.cached_call(call, cache_kwargs, tmp_path, "first stage", messages.append)
    reordered = dict(reversed(list(cache_kwargs.items())))
    second = grounding.cached_call(call, reordered, tmp_path, "renamed stage", messages.append)
    assert second == first and call.call_count == 1
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(record) == {"key", "model", "stage", "response"}
    assert "data:image" not in files[0].read_text(encoding="utf-8")
    assert any("cache locale" in m for m in messages)


@pytest.mark.parametrize("field", ["model", "system_prompt", "max_tokens", "has_pdf", "tool", "content_blocks"])
def test_cache_invalidates_each_model_prompt_schema_image_parameter(tmp_path, cache_kwargs, photo, field):
    call = Mock(return_value={"components": []})
    grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda message: None)
    changed = deepcopy(cache_kwargs)
    if field == "tool":
        changed[field] = grounding.grounding_tool((*KINDS, "led"))
    elif field == "content_blocks":
        photo[0, 0, 0] ^= 1
        changed[field] = [inventory.image_block(photo)]
    else:
        changed[field] = {"model": "other", "system_prompt": "changed", "max_tokens": 4096, "has_pdf": True}[field]
    grounding.cached_call(call, changed, tmp_path, "stage", lambda message: None)
    assert call.call_count == 2 and len(list(tmp_path.glob("*.json"))) == 2


@pytest.mark.parametrize("corruption", ["{broken", "[]", "null", "wrong-key", "wrong-response"])
def test_corrupt_cache_is_refetched_and_repaired(tmp_path, cache_kwargs, corruption):
    call = Mock(return_value={"components": []})
    grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda message: None)
    path = next(tmp_path.glob("*.json"))
    if corruption.startswith("wrong"):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["key" if corruption == "wrong-key" else "response"] = "invalid"
        corruption = json.dumps(record)
    path.write_text(corruption, encoding="utf-8")
    grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda message: None)
    assert call.call_count == 2
    assert isinstance(json.loads(path.read_text(encoding="utf-8"))["response"], dict)


@pytest.mark.parametrize("failure", [openrouter.OpenRouterError("offline"), ValueError("invalid response"), None, [], "bad"])
def test_api_failures_are_not_cached_and_next_call_retries(tmp_path, cache_kwargs, failure):
    call, calls = api_script(failure, {"components": []})
    with pytest.raises((openrouter.OpenRouterError, ValueError)):
        grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda message: None)
    assert not list(tmp_path.iterdir())
    assert grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda message: None) == {"components": []}
    grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda message: None)
    assert len(calls) == 2


def test_cache_write_failure_keeps_successful_response(monkeypatch, tmp_path, cache_kwargs):
    messages = []
    def unwritable(*args, **kwargs):
        raise PermissionError("cache is read-only")
    monkeypatch.setattr(Path, "write_text", unwritable)
    result = grounding.cached_call(lambda **kwargs: {"components": []}, cache_kwargs,
                                    tmp_path, "stage", messages.append)
    assert result == {"components": []}
    assert any("non scrivibile" in m for m in messages)


def test_cache_disabled_never_reads_or_writes_paths(monkeypatch, cache_kwargs):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled cache touched disk")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    call = Mock(return_value={"components": []})
    for _ in range(2):
        grounding.cached_call(call, cache_kwargs, None, "stage", lambda message: None)
    assert call.call_count == 2


@pytest.mark.parametrize("stage", ["presence", "classification"])
@pytest.mark.parametrize("defect", ["partial", "duplicate", "unknown", "bool-id", "verdict", "confidence", "empty"])
def test_cache_rejects_old_invalid_id_responses_without_retry(tmp_path, cache_kwargs, stage, defect):
    if stage == "classification":
        cache_kwargs["tool"] = grounding.classification_tool(KINDS)
        entries = [vote(1), vote(2)]
        verdict_key = "is_component"
    else:
        cache_kwargs["tool"] = {"function": {"name": "verify_mounted_bodies", "parameters": {
            "type": "object", "required": ["components"], "properties": {"components": {
                "type": "array", "items": {"type": "object", "required": ["id", "present", "confidence"],
                    "properties": {"id": {"type": "integer"}, "present": {"type": "boolean"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}}}}}}
        entries = [dict(id=i, present=True, confidence="high") for i in (1, 2)]
        verdict_key = "present"
    valid_response = {"components": deepcopy(entries)}
    if defect == "partial":
        entries.pop()
    elif defect == "duplicate":
        entries[1]["id"] = 1
    elif defect == "unknown":
        entries[1]["id"] = 99
    elif defect == "bool-id":
        entries[0]["id"] = True
    elif defect == "verdict":
        entries[0][verdict_key] = "false"
    elif defect == "confidence":
        entries[0]["confidence"] = "certain"
    else:
        entries.clear()
    invalid_response = {"components": entries}
    key = hashlib.sha256(json.dumps(cache_kwargs, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    path = tmp_path / f"{key}.json"
    path.write_text(json.dumps(dict(key=key, response=invalid_response)), encoding="utf-8")
    call = Mock(return_value=invalid_response)
    assert grounding.cached_call(call, cache_kwargs, tmp_path, "test", lambda m: None,
                                 expected_ids=[1, 2]) == invalid_response
    call.assert_called_once_with(**cache_kwargs)
    # A fresh partial response is neither retried nor made reusable.
    call.return_value = valid_response
    assert grounding.cached_call(call, cache_kwargs, tmp_path, "test", lambda m: None,
                                 expected_ids=[1, 2]) == valid_response
    grounding.cached_call(call, cache_kwargs, tmp_path, "test", lambda m: None, expected_ids=[1, 2])
    assert call.call_count == 2
    assert list(tmp_path.glob("*.json")) == [path]  # Original key is retained.


@pytest.mark.parametrize("entry", [
    native([400, 100, 200, 300]), native([True, 100, 400, 300]),
    native([200, 100, float("inf"), 300]), native(kind="invented"), native(label=[]),
    vote(height_mm=True), vote(height_mm=float("nan")), vote(rotation_deg=45),
    vote(color_hex="#bad"), vote(name=[]),
])
def test_semantically_invalid_entries_are_not_cached(tmp_path, cache_kwargs, entry):
    if "id" in entry:
        cache_kwargs["tool"] = grounding.classification_tool(KINDS)
    call = Mock(return_value={"components": [entry]})
    grounding.cached_call(call, cache_kwargs, tmp_path, "test", lambda m: None)
    assert call.call_count == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("present", [True, False])
def test_mechanical_integral_float_id_is_applied_on_fresh_and_cached_response(photo, tmp_path, present):
    call = Mock(return_value={"components": [dict(id=1.0, present=present, confidence="high")]})
    first = grounding.mechanical_envelopes(photo, [component()], "test-model", call, lambda m: None, tmp_path)
    second = grounding.mechanical_envelopes(photo, [component()], "test-model", call, lambda m: None, tmp_path)
    assert first == second and call.call_count == 1
    if present:
        assert len(first) == 1 and not first[0]["_uncertain"]
        assert first[0]["confidence"] == "high"
    else:
        assert first == []


def test_mechanical_integer_and_integral_float_duplicate_votes_preserve_body(photo):
    call = Mock(return_value={"components": [dict(id=1, present=False, confidence="high"),
                                            dict(id=1.0, present=True, confidence="high")]})
    result = grounding.mechanical_envelopes(photo, [component()], "test-model", call, lambda m: None, None)
    assert len(result) == 1 and result[0]["_uncertain"]


def test_cached_partial_grounding_with_bad_box_is_refetched_and_valid_sibling_survives(tmp_path, cache_kwargs):
    response = {"components": [native(), native([400, 100, 200, 300])]}
    key = hashlib.sha256(json.dumps(cache_kwargs, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    path = tmp_path / f"{key}.json"
    path.write_text(json.dumps(dict(key=key, response=response)), encoding="utf-8")
    call = Mock(return_value=response)
    result = grounding.cached_call(call, cache_kwargs, tmp_path, "test", lambda m: None)
    assert decode(result["components"]) == decode([native()])
    call.assert_called_once_with(**cache_kwargs)
    # Neither the old mixed response nor the fresh one qualifies for reuse.
    grounding.cached_call(call, cache_kwargs, tmp_path, "test", lambda m: None)
    assert call.call_count == 2


def test_grounded_path_keeps_successes_when_detail_api_calls_fail(photo):
    messages = []
    call, calls = api_script({"components": [native()]},
                            openrouter.OpenRouterError("tile down"), {"components": []},
                            openrouter.OpenRouterError("another tile down"), {"components": [vote()]})
    result = run_grounding(photo, call, log=messages.append, extra_instructions="Do not guess values")
    assert len(result) == 1 and not result[0]["_uncertain"]
    assert result[0]["_box"] == pytest.approx((.1, .2, .3, .4))
    assert result[0]["marking"] == ""
    assert [c["tool"]["function"]["name"] for c in calls] == ["locate_components"]*4+["classify_fixed_packages"]
    assert "Do not guess values" in calls[-1]["system_prompt"]
    assert "Do NOT transcribe markings" in calls[-1]["system_prompt"]
    assert sum("localizzazione fallita" in m for m in messages) == 2


def test_full_view_large_package_survives_partial_tile_detections(photo):
    entries = {"components": [native([100, 0, 900, 1000], label="partial")]}
    call, _ = api_script({"components": [native([100, 200, 900, 800], label="full package")]},
                         entries, entries, entries, {"components": []})
    result = run_grounding(photo, call)
    assert len(result) == 1 and result[0]["name"] == "full package"
    assert result[0]["_uncertain"] and result[0]["marking"] == ""


def test_failed_full_view_can_recover_candidates_from_owned_tile(photo):
    call, _ = api_script(openrouter.OpenRouterError("full failed"),
                         {"components": [native()]}, {"components": []}, {"components": []},
                         {"components": [vote()]})
    result = run_grounding(photo, call)
    assert len(result) == 1 and result[0]["_source"] == "Dettaglio 1/3"


@pytest.mark.parametrize("failure", [openrouter.OpenRouterError("offline"), {}, None])
def test_all_grounding_failures_skip_classification(photo, failure):
    call, calls = api_script(*([failure]*4))
    assert run_grounding(photo, call) == []
    assert len(calls) == 4


@pytest.mark.parametrize("failure", [openrouter.OpenRouterError("classifier failed"), {}, None])
def test_classification_failure_retains_geometry_uncertain(photo, failure):
    call, _ = api_script({"components": [native()]}, *([{"components": []}]*3), failure)
    result = run_grounding(photo, call)
    assert len(result) == 1 and result[0]["_uncertain"] and result[0]["marking"] == ""
    assert result[0]["_box"] == pytest.approx((.1, .2, .3, .4))


def test_classification_batches_keep_failed_batch_and_process_later_ids(monkeypatch, photo):
    observed_ids = []
    real_sheet = grounding.contact_sheet
    def capture(image, components):
        observed_ids.append([c["_id"] for c in components])
        return real_sheet(image, components)
    monkeypatch.setattr(grounding, "contact_sheet", capture)
    entries = [native([100, 20+i*70, 200, 60+i*70], label=f"target {i}") for i in range(13)]
    call, calls = api_script({"components": entries}, *([{"components": []}]*3),
                            openrouter.OpenRouterError("first batch failed"), {"components": [vote(13, rotation_deg=90)]})
    result = run_grounding(photo, call)
    assert observed_ids == [list(range(1, 13)), [13]]
    assert len(calls) == 6 and len(result) == 13
    assert all(c["_uncertain"] for c in result[:12])
    assert not result[12]["_uncertain"]
    assert all(c["marking"] == "" for c in result)


def test_grounded_response_ids_stay_bound_to_boxes_when_rejections_renumber_output(photo):
    call, _ = api_script({"components": [native(), native([600, 600, 800, 800])]},
                         *([{"components": []}]*3),
                         {"components": [vote(2, name="Survivor"), vote(1, is_component=False), vote(999)]})
    result = run_grounding(photo, call)
    assert len(result) == 1 and result[0]["name"] == "Survivor" and result[0]["_id"] == 1
    assert result[0]["_box"] == pytest.approx((.6, .6, .8, .8))


def test_full_grounded_run_is_reused_from_cache_and_notes_invalidate_classifier_only(photo, tmp_path):
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": [vote()]}, {"components": [vote(confidence="low")]})
    first = run_grounding(photo, call, cache_dir=tmp_path)
    second = run_grounding(photo, call, cache_dir=tmp_path)
    assert first == second and len(calls) == 5
    third = run_grounding(photo, call, cache_dir=tmp_path, extra_instructions="uncertain type")
    assert len(calls) == 6 and third[0]["_uncertain"]
    assert calls[-1]["tool"]["function"]["name"] == "classify_fixed_packages"


def test_api_failure_run_leaves_no_cache_and_can_retry(photo, tmp_path):
    call, calls = api_script(*([openrouter.OpenRouterError("offline")]*4), *([{"components": []}]*4))
    assert run_grounding(photo, call, cache_dir=tmp_path) == []
    assert not list(tmp_path.iterdir())
    assert run_grounding(photo, call, cache_dir=tmp_path) == []
    assert len(calls) == 8


def test_partial_api_failure_retries_only_failed_stages_on_next_run(photo, tmp_path):
    call, calls = api_script({"components": [native()]},
                            openrouter.OpenRouterError("detail failed"),
                            {"components": []}, {"components": []},
                            openrouter.OpenRouterError("review failed"),
                            {"components": []}, {"components": [vote()]})
    first = run_grounding(photo, call, cache_dir=tmp_path)
    assert first[0]["_uncertain"] and len(calls) == 5
    second = run_grounding(photo, call, cache_dir=tmp_path)
    assert len(calls) == 7 and not second[0]["_uncertain"]
    assert [c["tool"]["function"]["name"] for c in calls[5:]] == ["locate_components", "classify_fixed_packages"]


def test_semantically_invalid_stage_responses_are_not_cached(photo, tmp_path):
    # A dict is not necessarily a successful grounding response: a cached
    # malformed payload must not suppress retries after decode_boxes rejects it.
    call = Mock(return_value={"components": None})
    assert run_grounding(photo, call, cache_dir=tmp_path) == []
    assert not list(tmp_path.iterdir())


def test_grounded_empty_classifier_batch_is_retained_uncertain(photo):
    entries = [native(), native([600, 600, 800, 800])]
    call, calls = api_script({"components": entries}, *([{"components": []}]*3),
                            {"components": [{}, {}]})
    result = run_grounding(photo, call)
    assert len(calls) == 5 and len(result) == 2
    assert [c["_id"] for c in result] == [1, 2]
    assert all(c["_uncertain"] and c["marking"] == "" for c in result)
    assert result[0]["_box"] == pytest.approx((.1, .2, .3, .4))
    assert result[1]["_box"] == pytest.approx((.6, .6, .8, .8))


def test_empty_classifier_objects_are_retried_instead_of_cached_as_success(photo, tmp_path):
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": [{}]}, {"components": [vote()]})
    first = run_grounding(photo, call, cache_dir=tmp_path)
    assert first[0]["_uncertain"]
    second = run_grounding(photo, call, cache_dir=tmp_path)
    assert len(calls) == 6 and not second[0]["_uncertain"]


def test_classifier_model_is_independent_from_grounding_and_invalidates_only_review(photo, tmp_path):
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": [vote()]}, {"components": [vote()]})
    run_grounding(photo, call, cache_dir=tmp_path, classifier_model="classifier-A")
    assert [c["model"] for c in calls] == ["test-model"]*4+["classifier-A"]
    run_grounding(photo, call, cache_dir=tmp_path, classifier_model="classifier-B")
    assert len(calls) == 6 and calls[-1]["model"] == "classifier-B"


def test_extract_inventory_defaults_to_grounded_path_and_preserves_source_png(monkeypatch, photo):
    prepared = []
    def prepare(path, *args):
        prepared.append(path.name)
        return photo
    monkeypatch.setattr(inventory, "prepare_photo", prepare)
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3), {"components": [vote()]})
    result = inventory.extract_inventory(["bottom.png", "top.png"], "test-model", 40, 20,
                                         KINDS, call, top_photo_index=1, mechanical=False)
    assert prepared == ["top.png"] and result.photo == "top.png"
    assert len(calls) == 5 and len(result.components) == 1
    assert result.photo_png.startswith(b"\x89PNG") and result.overlay_png.startswith(b"\x89PNG")
    assert np.array_equal(cv2.imdecode(np.frombuffer(result.photo_png, np.uint8), cv2.IMREAD_COLOR), photo)
    assert result.components[0]["marking"] == ""


def test_extract_inventory_forwards_cache_and_instructions_without_extra_calls(monkeypatch, photo, tmp_path):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    grounded = Mock(return_value=[component()])
    monkeypatch.setattr(grounding, "grounded_components", grounded)
    call = Mock(side_effect=AssertionError("unexpected direct API call"))
    result = inventory.extract_inventory(["top.png"], "vision-model", 40, 20, KINDS, call,
                                         top_photo_index=0, cache_dir=tmp_path, extra_instructions="user notes",
                                         classifier_model="classification-model", mechanical=False)
    args = grounded.call_args.args
    assert args[0] is photo and args[1:6] == ("vision-model", 40, 20, KINDS, call)
    assert callable(args[6]) and args[7:] == (tmp_path, "user notes", "classification-model", False)
    assert result.components == [component()] and call.call_count == 0


def test_extract_inventory_surfaces_partial_api_failure_warning(monkeypatch, photo):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    call, _ = api_script({"components": [native()]}, openrouter.OpenRouterError("detail unavailable"),
                         {"components": []}, {"components": []}, openrouter.OpenRouterError("review unavailable"))
    result = inventory.extract_inventory(["top.png"], "test-model", 40, 20, KINDS, call, top_photo_index=0, mechanical=False)
    assert len(result.components) == 1 and result.components[0]["_uncertain"]
    assert any("detail unavailable" in m for m in result.warnings)
    assert any("review unavailable" in m for m in result.warnings)
    assert result.overlay_png.startswith(b"\x89PNG")


def test_extract_inventory_empty_classifier_objects_remain_reviewable(monkeypatch, photo):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    call, _ = api_script({"components": [native()]}, *([{"components": []}]*3),
                         {"components": [{}]})
    result = inventory.extract_inventory(["top.png"], "test-model", 40, 20, KINDS, call, top_photo_index=0, mechanical=False)
    assert len(result.components) == 1
    assert result.components[0]["_uncertain"] and result.components[0]["marking"] == ""
    assert result.overlay_png.startswith(b"\x89PNG")


def test_extract_inventory_all_api_failures_are_reported_without_legacy_fallback(monkeypatch, photo):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    call, calls = api_script(*([openrouter.OpenRouterError("offline")]*4))
    result = inventory.extract_inventory(["top.png"], "test-model", 40, 20, KINDS, call, top_photo_index=0, mechanical=False)
    assert not result.components and len(calls) == 4
    assert sum("offline" in m for m in result.warnings) == 4
    assert result.photo_png.startswith(b"\x89PNG")


def test_explicit_legacy_opt_out_does_not_invoke_grounding(monkeypatch, photo):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    monkeypatch.setattr(grounding, "grounded_components", Mock(side_effect=AssertionError("grounding unexpectedly enabled")))
    call, calls = api_script(*([{"components": []}]*7))
    inventory.extract_inventory(["top.png"], "test-model", 40, 20, KINDS, call,
                                top_photo_index=0, grounding=False, mechanical=False)
    assert len(calls) == 7
    assert all(c["tool"]["function"]["name"] == "report_photo_components" for c in calls)


@pytest.mark.parametrize("source", [-1, 99])
def test_absent_or_invalid_top_photo_never_calls_grounding(monkeypatch, source):
    monkeypatch.setattr(inventory, "prepare_photo", Mock(side_effect=AssertionError("unexpected photo read")))
    call = Mock(side_effect=AssertionError("unexpected API call"))
    result = inventory.extract_inventory(["top.png"], "test-model", 40, 20, KINDS, call, top_photo_index=source, mechanical=False)
    assert not result.components and result.warnings and call.call_count == 0


def audit_vote(identifier=1, duplicate_of=0, **updates):
    return vote(identifier, duplicate_of=duplicate_of, **updates)


def run_reconciliation(photo, components, entries, *, log=None, cache_dir=None):
    call = Mock(return_value={"components": entries})
    result = grounding.reconcile_packages(photo, components, "context-model", 40, 20,
                                          KINDS, call, log or (lambda message: None), cache_dir)
    return result, call


@pytest.mark.parametrize("initial,reviewed,conflict", [
    ("resistor", "capacitor", True), ("resistor", "generic", True),
    ("generic", "capacitor", False), ("resistor", "resistor", False),
])
def test_kind_disagreement_is_flagged_without_changing_grounded_geometry(initial, reviewed, conflict):
    original = component()
    original["kind"] = initial
    result = classify([original], [vote(kind=reviewed)])[0]
    assert result["_box"] == original["_box"]
    assert result["kind"] == (initial if conflict else reviewed)
    assert bool(result.get("_kind_conflict")) is conflict
    assert result["_uncertain"] is conflict
    assert result["marking"] == ""
    if conflict:
        assert result["_grounding_kind"] == initial
        assert result["_alternative_kind"] == reviewed
        assert result["name"] == original["name"] and result["confidence"] == "low"


def test_reconciliation_skips_confident_nonoverlapping_components(photo):
    items = [dict(component(1), _uncertain=False),
             dict(component(2, [600, 600, 800, 800]), _uncertain=False)]
    result, call = run_reconciliation(photo, items, [])
    assert result is items and call.call_count == 0
    result, call = run_reconciliation(photo, [], [])
    assert result == [] and call.call_count == 0


def test_reconciliation_valid_overlap_duplicate_keeps_target_extent_and_metadata(photo):
    items = [component(11), component(42, [210, 110, 390, 290])]
    before = deepcopy(items)
    result, call = run_reconciliation(photo, items, [
        audit_vote(11, duplicate_of=42, marking="ignored", _box=(0, 0, 1, 1)),
        audit_vote(42, name="Complete target", marking="ignored")])
    assert call.call_count == 1 and [c["_id"] for c in result] == [42]
    for key in ("_box", "x_mm", "y_mm", "length_mm", "width_mm"):
        assert result[0][key] == before[1][key]
    assert result[0]["name"] == "Complete target" and result[0]["marking"] == ""
    assert items == before


@pytest.mark.parametrize("left,right,removed", [
    (500, 600, False),  # separate body
    (200, 300, False),  # touching is zero intersection
    (175, 275, False),  # 25% of smaller box is below threshold
    (125, 225, True),   # 75% overlap permits a duplicate vote
])
def test_reconciliation_duplicate_requires_physical_overlap(photo, left, right, removed):
    items = [component(1, [200, 100, 400, 200]), component(2, [200, left, 400, right])]
    result, _ = run_reconciliation(photo, items, [audit_vote(1, 2, rotation_deg=90),
                                                audit_vote(2, rotation_deg=90)])
    assert [c["_id"] for c in result] == ([2] if removed else [1, 2])


@pytest.mark.parametrize("target", [999, -1, 0, 1, True, False, "2", 2.25, None, [], {}])
def test_reconciliation_invalid_unknown_or_self_target_cannot_delete(photo, target):
    result, _ = run_reconciliation(photo, [component(1), component(2)],
                                   [audit_vote(1, target), audit_vote(2)])
    assert [c["_id"] for c in result] == [1, 2]


@pytest.mark.parametrize("source", [999, -1, True, "1", 1.25, None])
def test_reconciliation_unknown_source_cannot_create_or_delete_components(photo, source):
    result, _ = run_reconciliation(photo, [component(1), component(2)],
                                   [audit_vote(source, 2), audit_vote(2)])
    assert [c["_id"] for c in result] == [1, 2]
    assert result[0]["_uncertain"]


@pytest.mark.parametrize("entries", [
    [audit_vote(1, 2), audit_vote(2, 1)],
    [audit_vote(1, 2), audit_vote(2, 3), audit_vote(3, 1)],
])
def test_reconciliation_cycles_never_delete_packages(photo, entries):
    items = [component(i+1) for i in range(len(entries))]
    result, _ = run_reconciliation(photo, items, entries)
    assert [c["_id"] for c in result] == [c["_id"] for c in items]


def test_reconciliation_never_follows_a_chain_through_a_removed_target(photo):
    # A->B->C: A must remain because its target B is not retained. Only the
    # direct B->C link to a uniquely declared retained target can be honored.
    messages = []
    result, _ = run_reconciliation(photo, [component(i) for i in (1, 2, 3)],
                                   [audit_vote(1, 2), audit_vote(2, 3), audit_vote(3)],
                                   log=messages.append)
    assert [c["_id"] for c in result] == [1, 3]
    assert any("catena/ciclo" in message for message in messages)


@pytest.mark.parametrize("entries", [
    [audit_vote(1, 2), audit_vote(1), audit_vote(2)],  # ambiguous source
    [audit_vote(1, 2), audit_vote(2), audit_vote(2)],  # ambiguous target
    [audit_vote(1, 2)],  # no explicit retained-target vote
])
def test_reconciliation_duplicate_or_missing_votes_prevent_merging(photo, entries):
    result, _ = run_reconciliation(photo, [component(1), component(2)], entries)
    assert [c["_id"] for c in result] == [1, 2]


def test_reconciliation_target_rejected_as_pad_cannot_absorb_source(photo):
    result, _ = run_reconciliation(photo, [component(1), component(2)],
                                   [audit_vote(1, 2), audit_vote(2, is_component=False)])
    assert [c["_id"] for c in result] == [1]


def test_reconciliation_many_duplicates_may_point_to_one_explicit_retained_target(photo):
    result, _ = run_reconciliation(photo, [component(i) for i in (1, 2, 3)],
                                   [audit_vote(3, 2), audit_vote(2), audit_vote(1, 2)])
    assert [c["_id"] for c in result] == [2]


def test_reconciliation_is_bounded_to_24_known_candidates_and_one_call(photo):
    items = [component(i) for i in range(1, 27)]
    snapshot = deepcopy(items)
    entries = [audit_vote(i, 25 if i == 1 else 0) for i in range(1, 25)]
    entries.append(audit_vote(25, is_component=False))  # real ID outside supplied batch
    result, call = run_reconciliation(photo, items, entries)
    assert call.call_count == 1 and len(result) == 26
    hints = json.loads(call.call_args.kwargs["content_blocks"][0]["text"])
    assert [entry["id"] for entry in hints] == list(range(1, 25))
    assert result[-2:] == snapshot[-2:] and items == snapshot


def test_reconciliation_requests_context_and_only_immutable_ids_without_markings(monkeypatch, photo):
    items = [dict(component(11), _kind_conflict=True, _grounding_kind="capacitor")]
    draw = Mock(wraps=inventory.draw_inventory)
    monkeypatch.setattr(inventory, "draw_inventory", draw)
    _, call = run_reconciliation(photo, items, [audit_vote(11)])
    kwargs = call.call_args.kwargs
    assert kwargs["model"] == "context-model"
    assert kwargs["tool"]["function"]["name"] == "reconcile_fixed_packages"
    schema = kwargs["tool"]["function"]["parameters"]["properties"]["components"]["items"]
    assert "duplicate_of" in schema["required"]
    assert "marking" not in schema["properties"]
    assert not ({"_box", "box_2d", "x_mm", "y_mm"} & schema["properties"].keys())
    assert "Do not transcribe markings" in kwargs["system_prompt"]
    blocks = kwargs["content_blocks"]
    assert [b["type"] for b in blocks] == ["text", "image_url", "image_url"]
    assert json.loads(blocks[0]["text"]) == [{"id": 11, "initial_type": "capacitor", "reviewed_type": "resistor"}]
    assert draw.call_args.kwargs == {"use_ids": True}


@pytest.mark.parametrize("failure", [openrouter.OpenRouterError("context offline"), ValueError("bad JSON"),
                                     None, {}, {"components": None}])
def test_reconciliation_api_or_response_failure_keeps_every_candidate(photo, failure):
    items = [dict(component(1), _kind_conflict=True), component(2)]
    snapshot = deepcopy(items)
    call, calls = api_script(failure)
    messages = []
    result = grounding.reconcile_packages(photo, items, "context-model", 40, 20, KINDS,
                                          call, messages.append, None)
    assert result == snapshot and items == snapshot and len(calls) == 1
    assert all(c["_uncertain"] and c["marking"] == "" for c in result)
    assert any("fallita" in message for message in messages)


def test_reconciliation_api_error_is_not_cached_and_retries(photo, tmp_path):
    items = [component()]
    call, calls = api_script(openrouter.OpenRouterError("offline"), {"components": [audit_vote()]})
    args = (photo, items, "context-model", 40, 20, KINDS, call, lambda message: None, tmp_path)
    assert grounding.reconcile_packages(*args) == items
    assert not list(tmp_path.iterdir())
    second = grounding.reconcile_packages(*args)
    assert not second[0]["_uncertain"] and len(calls) == 2
    assert grounding.reconcile_packages(*args) == second and len(calls) == 2


def test_reconciliation_empty_objects_preserve_boxes_uncertain(photo):
    items = [component(1), component(2)]
    result, call = run_reconciliation(photo, items, [{}, {}])
    assert call.call_count == 1
    assert result == items and all(c["_uncertain"] for c in result)


def test_reconciliation_incomplete_vote_cannot_delete_a_package(photo):
    items = [component(1), component(2)]
    result, _ = run_reconciliation(photo, items, [{"id": 1, "duplicate_of": 2}, audit_vote(2)])
    assert [c["_id"] for c in result] == [1, 2]
    assert result[0]["_uncertain"]


def test_grounded_default_classifier_uses_claude_without_contextual_self_review(monkeypatch, photo):
    monkeypatch.setattr(grounding, "reconcile_packages", Mock(side_effect=AssertionError("Experimental context must not run in production")))
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": [vote(kind="capacitor")]})
    result = run_grounding(photo, call)
    assert len(calls) == 5
    assert [c["model"] for c in calls] == ["test-model"]*4+[grounding.DEFAULT_MODEL]
    assert grounding.DEFAULT_MODEL.startswith("anthropic/")
    assert calls[-1]["tool"]["function"]["name"] == "classify_fixed_packages"
    assert result[0]["kind"] == "resistor" and result[0]["_kind_conflict"]
    assert result[0]["_alternative_kind"] == "capacitor"
    assert result[0]["marking"] == "" and result[0]["_candidate_id"] == 1


def test_grounded_custom_classifier_preserves_kind_conflict_and_geometry(photo):
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": [vote(kind="capacitor")]})
    result = run_grounding(photo, call, classifier_model="custom-classifier")
    assert len(calls) == 5 and calls[-1]["model"] == "custom-classifier"
    assert result[0]["_kind_conflict"] and result[0]["_uncertain"]
    assert result[0]["_box"] == pytest.approx((.1, .2, .3, .4))
    assert result[0]["_grounding_kind"] == "resistor" and result[0]["marking"] == ""


def test_grounded_same_source_overlap_is_not_merged_by_contextual_self_review(photo):
    call, calls = api_script({"components": [native(), native([210, 110, 390, 290])]},
                            *([{"components": []}]*3), {"components": [vote(1), vote(2)]})
    result = run_grounding(photo, call)
    assert len(calls) == 5 and len(result) == 2
    assert [c["_candidate_id"] for c in result] == [1, 2]
    assert result[1]["_box"] == pytest.approx((.11, .21, .29, .39))
    assert all(c["_uncertain"] and c["marking"] == "" for c in result)


def test_extract_inventory_kind_conflict_is_reported_without_context_call(monkeypatch, photo):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    monkeypatch.setattr(grounding, "reconcile_packages", Mock(side_effect=AssertionError("Experimental context must not run")))
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": [vote(kind="capacitor")]})
    result = inventory.extract_inventory(["top.png"], "photo-model", 40, 20, KINDS, call,
                                         top_photo_index=0, classifier_model="context-model", mechanical=False)
    assert len(calls) == 5 and result.components[0]["kind"] == "resistor"
    assert result.components[0]["_kind_conflict"] and result.components[0]["_uncertain"]
    assert result.components[0]["_alternative_kind"] == "capacitor"
    assert any("discordante" in message for message in result.warnings)
    assert result.photo_png.startswith(b"\x89PNG") and result.overlay_png.startswith(b"\x89PNG")


@pytest.mark.parametrize("angle", [0., 90., 180., 270.])
def test_integral_float_rotation_matches_integer_rotation(angle):
    assert classify([component()], [vote(rotation_deg=angle)]) == classify([component()], [vote(rotation_deg=int(angle))])


def test_empty_classification_list_is_retried_while_valid_grounding_is_cached(photo, tmp_path):
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": []}, {"components": [vote()]})
    assert run_grounding(photo, call, cache_dir=tmp_path)[0]["_uncertain"]
    assert not run_grounding(photo, call, cache_dir=tmp_path)[0]["_uncertain"]
    assert len(calls) == 6


def test_containment_merge_matches_only_cross_view_duplicates_one_to_one():
    outer = dict(component(), _box=(.2, .2, .6, .6), _source="full")
    inner = dict(component(), _box=(.25, .25, .55, .55), _source="tile")
    assert .4 < inventory.overlap(outer, inner)[0] < .6
    assert len(inventory.merge_components([[outer], [inner]])) == 2
    assert len(inventory.merge_components([[outer], [inner]], containment=True)) == 1
    assert len(inventory.merge_components([[outer, inner]], containment=True)) == 2
    assert len(inventory.merge_components([[outer], [inner, deepcopy(inner)]], containment=True)) == 2


@pytest.mark.parametrize("other", [(.30, .30, .40, .40), (.45, .2, .75, .6), (.65, .2, .95, .6)])
def test_containment_merge_does_not_absorb_small_or_adjacent_parts(other):
    outer = dict(component(), _box=(.2, .2, .6, .6))
    neighbor = dict(component(), _box=other)
    assert len(inventory.merge_components([[outer], [neighbor]], containment=True)) == 2


def test_grounded_path_enables_containment_only_for_cross_view_merge(monkeypatch, photo):
    real_merge = inventory.merge_components
    flags = []
    def capture(groups, log, containment=False):
        flags.append(containment)
        return real_merge(groups, log, containment=containment)
    monkeypatch.setattr(inventory, "merge_components", capture)
    call, _ = api_script({"components": [native()]}, *([{"components": []}]*3), {"components": [vote()]})
    assert len(run_grounding(photo, call)) == 1
    assert flags == [True, False]


def occupancy(identifier=1, present=True, confidence="high", **updates):
    return dict(id=identifier, present=present, confidence=confidence, **updates)


def run_mechanical(photo, components, responses, *, cache_dir=None, log=None):
    call, calls = api_script(*responses)
    result = grounding.mechanical_envelopes(photo, components, "occupancy-model", call,
                                           log or (lambda message: None), cache_dir)
    return result, calls


def test_mechanical_envelopes_are_generic_unmarked_fixed_geometry_and_nonmutating():
    photo = np.full((100, 200, 3), (9, 19, 29), np.uint8)
    original = dict(component(), name="SP3485E", marking="DO NOT RETAIN", height_mm=99,
                    color_hex="#ffffff")
    before, pixels = deepcopy(original), photo.copy()
    result, calls = run_mechanical(photo, [original], [{"components": [occupancy(
        kind="ic_soic", marking="MODEL TEXT", color_hex="#000000", height_mm=88,
        _box=[0, 0, 1, 1], x_mm=999)]}])
    c = result[0]
    assert c["kind"] == "generic" and c["name"] == "Ingombro 1" and c["marking"] == ""
    assert c["color_hex"] == "#1d1309" and c["height_mm"] == .6
    assert c["_height_source"] == "coarse_visual_prior_not_measured"
    for key in ("_box", "x_mm", "y_mm", "length_mm", "width_mm", "rotation_deg", "_id"):
        assert c[key] == before[key]
    assert original == before and np.array_equal(photo, pixels)
    assert len(calls) == 1 and calls[0]["model"] == "occupancy-model"


@pytest.mark.parametrize("kind,height", [("resistor", .6), ("capacitor", .8),
                                         ("ic_soic", 1.75), ("module", 4.), ("unknown", 1.5)])
def test_mechanical_height_is_explicit_prior_not_model_measurement(photo, kind, height):
    original = dict(component(), kind=kind)
    result, _ = run_mechanical(photo, [original], [{"components": [occupancy()]}])
    assert result[0]["height_mm"] == height
    assert result[0]["_height_source"] == "coarse_visual_prior_not_measured"
    assert result[0]["_detected_kind"] == kind
    assert result[0]["kind"] == "generic" and result[0]["marking"] == ""


def test_mechanical_color_uses_center_pixel_median_not_silkscreen_or_outliers():
    image = np.full((100, 200, 3), 245, np.uint8)
    # Target is pixels [20:60,20:40]; neutral border represents silk/terminals.
    image[24:36, 28:52] = (20, 40, 80)
    image[25, 30] = (255, 255, 255)
    result, _ = run_mechanical(image, [component()], [{"components": [occupancy()]}])
    assert result[0]["color_hex"] == "#502814"


def test_mechanical_schema_requests_occupancy_only(photo):
    _, calls = run_mechanical(photo, [component()], [{"components": [occupancy()]}])
    tool = calls[0]["tool"]["function"]
    assert tool["name"] == "verify_mounted_bodies"
    item = tool["parameters"]["properties"]["components"]["items"]
    assert set(item["properties"]) == set(item["required"]) == {"id", "present", "confidence"}
    assert "Do NOT identify electrical type" in calls[0]["system_prompt"]
    assert calls[0]["max_tokens"] == 4096


def test_mechanical_occupancy_drops_only_explicit_empty_body_and_keeps_unknowns(photo):
    items = [component(i) for i in (1, 2, 3, 4)]
    result, _ = run_mechanical(photo, items, [{"components": [
        occupancy(1, False), occupancy(2, True, "low"), occupancy(3, True, "medium")]}])
    assert [c["_id"] for c in result] == [2, 3, 4]
    assert [c["_uncertain"] for c in result] == [True, False, True]
    assert all(c["kind"] == "generic" and c["marking"] == "" for c in result)


@pytest.mark.parametrize("entry", [{}, occupancy(999, False), occupancy(True, False),
                                   occupancy("1", False), occupancy(1, "false"),
                                   occupancy(1, 0), occupancy(1, None), None])
def test_mechanical_missing_or_invalid_verdict_cannot_delete_body(photo, entry):
    result, _ = run_mechanical(photo, [component()], [{"components": [entry]}])
    assert len(result) == 1 and result[0]["_uncertain"]
    assert result[0]["_box"] == component()["_box"]


def test_mechanical_duplicate_occupancy_votes_cannot_delete_body(photo):
    result, _ = run_mechanical(photo, [component()], [{"components": [occupancy(1, False), occupancy(1)]}])
    assert len(result) == 1 and result[0]["_uncertain"]


@pytest.mark.parametrize("response", [{"components": []}, {"components": [{}]},
                                      openrouter.OpenRouterError("occupancy unavailable")])
def test_mechanical_unconfirmed_occupancy_marks_previously_certain_input_uncertain(photo, response):
    original = dict(component(), _uncertain=False, confidence="high")
    result, _ = run_mechanical(photo, [original], [response])
    assert len(result) == 1 and result[0]["_uncertain"]
    assert not original["_uncertain"]


@pytest.mark.parametrize("failure", [openrouter.OpenRouterError("offline"), None, {}, {"components": None}])
def test_mechanical_failure_preserves_generic_envelopes_with_prior_height(photo, failure):
    messages = []
    result, _ = run_mechanical(photo, [component(1), component(2)], [failure], log=messages.append)
    assert len(result) == 2
    assert all(c["_uncertain"] and c["kind"] == "generic" and c["marking"] == "" for c in result)
    assert all(c["_height_source"] == "coarse_visual_prior_not_measured" for c in result)
    assert any("fallita" in m for m in messages)


def test_mechanical_43_targets_need_only_two_occupancy_calls(monkeypatch, photo):
    observed = []
    sheet = grounding.contact_sheet
    def capture(image, items):
        observed.append([c["_id"] for c in items])
        return sheet(image, items)
    monkeypatch.setattr(grounding, "contact_sheet", capture)
    items = [component(i) for i in range(1, 44)]
    result, calls = run_mechanical(photo, items, [
        {"components": [occupancy(i) for i in range(1, 25)]},
        {"components": [occupancy(i) for i in range(25, 44)]}])
    assert len(result) == 43 and len(calls) == 2
    assert observed == [list(range(1, 25)), list(range(25, 44))]
    assert all(c["kind"] == "generic" and c["marking"] == "" for c in result)


def test_mechanical_first_batch_failure_keeps_targets_and_still_checks_second_batch(photo):
    result, calls = run_mechanical(photo, [component(i) for i in range(1, 26)], [
        openrouter.OpenRouterError("batch failed"), {"components": [occupancy(25, False)]}])
    assert len(calls) == 2 and [c["_id"] for c in result] == list(range(1, 25))
    assert all(c["_uncertain"] for c in result)


def test_mechanical_empty_inventory_needs_no_api(photo):
    result, calls = run_mechanical(photo, [], [])
    assert result == [] and calls == []


def test_mechanical_cache_reuses_success_and_retries_incomplete_verdicts(photo, tmp_path):
    items = [component()]
    call, calls = api_script({"components": [{}]}, {"components": [occupancy()]})
    args = (photo, items, "occupancy-model", call, lambda message: None, tmp_path)
    assert grounding.mechanical_envelopes(*args)[0]["_uncertain"]
    assert not list(tmp_path.iterdir())
    second = grounding.mechanical_envelopes(*args)
    assert not second[0]["_uncertain"] and len(calls) == 2
    assert grounding.mechanical_envelopes(*args) == second and len(calls) == 2


def test_grounded_default_mechanical_path_never_requests_exact_recognition(monkeypatch, photo):
    monkeypatch.setattr(grounding, "classification_tool", Mock(side_effect=AssertionError("Exact recognition is disabled")))
    monkeypatch.setattr(grounding, "reconcile_packages", Mock(side_effect=AssertionError("Context classification is disabled")))
    call, calls = api_script({"components": [native(), native([600, 600, 800, 800])]},
                            *([{"components": []}]*3),
                            {"components": [occupancy(1, False), occupancy(2)]})
    result = grounding.grounded_components(photo, "photo-model", 40, 20, KINDS, call,
                                           lambda message: None, classifier_model="occupancy-model")
    assert len(calls) == 5 and calls[-1]["tool"]["function"]["name"] == "verify_mounted_bodies"
    assert calls[-1]["model"] == "occupancy-model" and len(result) == 1
    assert result[0]["_id"] == 1 and result[0]["_candidate_id"] == 2
    assert result[0]["kind"] == "generic" and result[0]["marking"] == ""
    assert result[0]["_box"] == pytest.approx((.6, .6, .8, .8))


def test_inventory_mechanical_default_uses_only_occupancy_and_keeps_pngs(monkeypatch, photo):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    monkeypatch.setattr(grounding, "classification_tool", Mock(side_effect=AssertionError("No electrical recognition")))
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": [occupancy()]})
    result = inventory.extract_inventory(["top.png"], "photo-model", 40, 20, KINDS, call,
                                         top_photo_index=0, classifier_model="occupancy-model")
    assert len(calls) == 5 and calls[-1]["tool"]["function"]["name"] == "verify_mounted_bodies"
    assert len(result.components) == 1 and result.components[0]["kind"] == "generic"
    assert result.components[0]["marking"] == ""
    assert result.components[0]["_height_source"] == "coarse_visual_prior_not_measured"
    assert result.photo_png.startswith(b"\x89PNG") and result.overlay_png.startswith(b"\x89PNG")


def test_custom_stage_prompts_reach_only_their_api_calls(monkeypatch, photo, tmp_path):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    call, calls = api_script({"components": [native()]}, *([{"components": []}]*3),
                            {"components": [occupancy()]})
    result = inventory.extract_inventory(["top.png"], "photo-model", 40, 20, KINDS, call,
        top_photo_index=0, classifier_model="verify-model", cache_dir=tmp_path,
        grounding_prompt="Locate only", verification_prompt="Verify only")
    assert result.components
    assert [c["system_prompt"] for c in calls] == ["Locate only"] * 4 + ["Verify only"]
    # A prompt change invalidates only the matching stage's cache.
    changed_call, changed_calls = api_script({"components": [occupancy()]})
    inventory.extract_inventory(["top.png"], "photo-model", 40, 20, KINDS, changed_call,
        top_photo_index=0, classifier_model="verify-model", cache_dir=tmp_path,
        grounding_prompt="Locate only", verification_prompt="Verify differently")
    assert len(changed_calls) == 1
    assert changed_calls[0]["system_prompt"] == "Verify differently"


def test_inventory_mechanical_occupancy_failure_retains_envelope_and_warning(monkeypatch, photo):
    monkeypatch.setattr(inventory, "prepare_photo", lambda *args: photo)
    call, _ = api_script({"components": [native()]}, *([{"components": []}]*3),
                         openrouter.OpenRouterError("occupancy offline"))
    result = inventory.extract_inventory(["top.png"], "photo-model", 40, 20, KINDS, call, top_photo_index=0)
    assert len(result.components) == 1 and result.components[0]["_uncertain"]
    assert result.components[0]["kind"] == "generic" and result.components[0]["marking"] == ""
    assert any("occupancy offline" in message for message in result.warnings)


def test_the_classification_schema_asks_where_the_connector_opens():
    """Il verso di innesto si vede nella foto; dedurlo dall'ingombro no."""
    schema = grounding.classification_tool(KINDS)["function"]["parameters"]
    item = schema["properties"]["components"]["items"]["properties"]
    assert item["mating"]["enum"] == ["edge", "top", "none"]
    # Obbligatorio come ogni altro campo: il contratto del classificatore e'
    # che il modello risponda a tutto, senza omissioni silenziose. Per i non
    # connettori l'enum prevede "none".
    assert "mating" in schema["properties"]["components"]["items"]["required"]
