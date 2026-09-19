"""@brief Offline regression tests for native tile geometry and objectness contracts."""

from copy import deepcopy
from itertools import permutations

import pytest

from kicadgen.services.photo_detection_contract import (
    build_candidates,
    make_core_tiles,
    make_tiles,
    owns_box,
    tile_to_image_box,
    validate_objectness_reply,
)


@pytest.mark.parametrize("width,height,size,overlap", [
    (1, 1, 1, 0), (37, 29, (13, 11), (4, 3)),
    (17, 8, 6, 5), (9, 7, 30, 4), (20, 20, 10, 0),
])
def test_cores_partition_every_pixel_with_native_bounds(width, height, size, overlap):
    tiles = make_tiles(width, height, size, overlap)
    assert tiles == make_tiles(width, height, size, overlap)
    assert len({t["id"] for t in tiles}) == len(tiles)
    limit_x, limit_y = (size, size) if isinstance(size, int) else size
    for tile in tiles:
        x0, y0, x1, y1 = tile["bounds"]
        cx0, cy0, cx1, cy1 = tile["core"]
        assert 0 <= x0 <= cx0 < cx1 <= x1 <= width
        assert 0 <= y0 <= cy0 < cy1 <= y1 <= height
        assert x1 - x0 <= limit_x and y1 - y0 <= limit_y
    for y in range(height):
        for x in range(width):
            assert sum(owns_box([x, y, x + 1, y + 1], t) for t in tiles) == 1


def test_core_size_and_halo_are_supplied_and_last_core_is_clipped():
    tiles = make_core_tiles(1730, 1510, (800, 700), 100)
    assert len(tiles) == 9
    assert tiles[0]["core"] == [0, 0, 800, 700]
    assert tiles[0]["bounds"] == [0, 0, 900, 800]
    assert tiles[4]["core"] == [800, 700, 1600, 1400]
    assert tiles[4]["bounds"] == [700, 600, 1700, 1500]
    assert tiles[-1]["core"] == [1600, 1400, 1730, 1510]
    assert tiles[-1]["bounds"] == [1500, 1300, 1730, 1510]
    assert all(t["bounds"][2] - t["bounds"][0] <= 1000 for t in tiles)
    assert all(t["bounds"][3] - t["bounds"][1] <= 900 for t in tiles)


def test_large_halos_keep_ids_unique_even_when_crop_bounds_are_identical():
    tiles = make_core_tiles(8, 6, (4, 3), 100)
    assert all(t["bounds"] == [0, 0, 8, 6] for t in tiles)
    assert len({t["id"] for t in tiles}) == 4
    for y in range(6):
        for x in range(8):
            assert sum(owns_box([x, y, x + 1, y + 1], t) for t in tiles) == 1


def test_seam_center_belongs_to_right_and_bottom_core():
    tiles = make_core_tiles(40, 40, 20, 5)
    box = [19, 19, 21, 21]
    assert [owns_box(box, t) for t in tiles] == [False, False, False, True]
    # Odd overlap uses an integer midpoint with the same half-open rule.
    a, b = make_tiles(17, 8, (10, 8), (3, 0))
    assert a["core"][2] == b["core"][0] == 8
    assert not owns_box([7, 2, 9, 4], a)
    assert owns_box([7, 2, 9, 4], b)


@pytest.mark.parametrize("args", [
    (0, 10, 5, 1), (10, -1, 5, 1), (True, 10, 5, 1),
    (10, 10, 0, 0), (10, 10, 5.0, 0), (10, 10, [5], 0),
    (10, 10, (5, False), 0), (10, 10, 5, -1),
    (10, 10, 5, 5), (10, 10, 5, (0, 6)),
    (10, 10, 5, True), (float("nan"), 10, 5, 1),
])
def test_invalid_tiling_parameters_raise(args):
    with pytest.raises(ValueError):
        make_tiles(*args)


def test_budgets_reject_before_constructing_huge_grids():
    with pytest.raises(ValueError, match="budget"):
        make_tiles(10**12, 10**12, 10, 9, max_tiles=4)
    with pytest.raises(ValueError, match="budget"):
        make_core_tiles(10**12, 10**12, 10, 2, max_tiles=4)
    for budget in [0, True, 2.5]:
        with pytest.raises(ValueError):
            make_tiles(10, 10, 10, 0, max_tiles=budget)
    for halo in [-1, True, (2, -1), 1.5]:
        with pytest.raises(ValueError):
            make_core_tiles(10, 10, 5, halo)


@pytest.mark.parametrize("scale", [1, 2, 7])
def test_pixel_and_normalized_conversion_translate_and_scale_identically(scale):
    tile = make_core_tiles(600 * scale, 400 * scale,
                           (200 * scale, 100 * scale), (20 * scale, 10 * scale))[4]
    assert tile["bounds"] == [180 * scale, 90 * scale, 420 * scale, 210 * scale]
    before = deepcopy(tile)
    pixels = [24 * scale, 24 * scale, 192 * scale, 84 * scale]
    expected = [204 * scale, 114 * scale, 372 * scale, 174 * scale]
    assert tile_to_image_box(pixels, tile) == expected
    assert tile_to_image_box([200, 100, 700, 800], tile, "yxyx_1000") == pytest.approx(expected)
    assert tile == before


def test_fractional_pixels_are_not_rounded_and_default_is_xyxy():
    tile = make_tiles(200, 100, 300, 0)[0]
    assert tile_to_image_box([1.25, 3.5, 8.75, 9.5], tile) == [1.25, 3.5, 8.75, 9.5]
    assert tile_to_image_box([100, 50, 300, 200], tile, "yxyx_1000") == [10, 10, 40, 30]


@pytest.mark.parametrize("bad", [None, {}, "1,2,3,4", [], [1, 2, 3], [1, 2, 3, 4, 5]])
def test_invalid_box_structure_is_rejected(bad):
    with pytest.raises(ValueError):
        tile_to_image_box(bad, make_tiles(100, 100, 100, 0)[0])


@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"),
                                  -float("inf"), "10", None, 10**1000])
@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("coordinate_format", ["xyxy_pixels", "yxyx_1000"])
def test_nonfinite_boolean_and_nonnumeric_coordinates_are_rejected(bad, index, coordinate_format):
    box = [10, 20, 30, 40]
    box[index] = bad
    with pytest.raises(ValueError):
        tile_to_image_box(box, make_tiles(100, 100, 100, 0)[0], coordinate_format)


@pytest.mark.parametrize("box", [
    [-1, 10, 30, 40], [10, -1, 30, 40], [10, 20, 1001, 40],
    [10, 20, 30, 1001], [30, 20, 10, 40], [10, 40, 30, 20],
    [10, 20, 10, 40], [10, 20, 30, 20],
])
@pytest.mark.parametrize("coordinate_format", ["xyxy_pixels", "yxyx_1000"])
def test_out_of_range_reversed_and_zero_area_boxes_are_rejected(box, coordinate_format):
    with pytest.raises(ValueError):
        tile_to_image_box(box, make_tiles(1000, 1000, 1000, 0)[0], coordinate_format)


@pytest.mark.parametrize("edge", range(4))
@pytest.mark.parametrize("coordinate_format", ["xyxy_pixels", "yxyx_1000"])
def test_each_internal_border_is_rejected_but_image_outer_border_is_allowed(edge, coordinate_format):
    tile = make_tiles(240, 240, 100, 20)[4]
    box = [20, 20, 80, 80]
    box[edge] = 0 if edge < 2 else 100
    if coordinate_format == "yxyx_1000":
        box = [box[1] * 10, box[0] * 10, box[3] * 10, box[2] * 10]
    with pytest.raises(ValueError, match="internal tile border"):
        tile_to_image_box(box, tile, coordinate_format)
    outer = make_tiles(100, 100, 100, 0)[0]
    assert len(tile_to_image_box(box, outer, coordinate_format)) == 4


def test_cropped_last_tile_uses_actual_dimensions_and_allows_outer_edges():
    tile = make_tiles(170, 130, (100, 80), (20, 20))[-1]
    assert tile["bounds"] == [80, 60, 170, 130]
    assert tile_to_image_box([500, 500, 1000, 1000], tile, "yxyx_1000") == [125, 95, 170, 130]
    assert tile_to_image_box([45, 35, 90, 70], tile) == [125, 95, 170, 130]
    with pytest.raises(ValueError):
        tile_to_image_box([45, 35, 100, 80], tile)


@pytest.mark.parametrize("field,value", [
    ("image_size", [True, 100]), ("image_size", [100]), ("id", 5),
    ("bounds", [0, 0, 101, 100]), ("bounds", [0.0, 0, 100, 100]),
    ("core", [0, 0, 0, 100]), ("core", [0, 0, 101, 100]),
])
def test_malformed_tile_descriptors_are_rejected(field, value):
    tile = make_tiles(100, 100, 100, 0)[0]
    tile[field] = value
    with pytest.raises(ValueError):
        tile_to_image_box([20, 20, 30, 30], tile)
    with pytest.raises(ValueError):
        owns_box([20, 20, 30, 30], tile)


def test_unknown_coordinate_mode_fails_even_for_empty_batches():
    tiles = make_tiles(100, 100, 100, 0)
    with pytest.raises(ValueError):
        tile_to_image_box([20, 20, 30, 30], tiles[0], "auto")
    with pytest.raises(ValueError):
        build_candidates(tiles, {}, "auto")


def test_complete_nonowner_halo_observations_survive_missing_neighbor():
    tiles = make_core_tiles(100, 50, (50, 50), 10)
    # Center is 54: the second core owns this fully visible observation.
    box = [51, 10, 57, 20]
    data = {tiles[0]["id"]: [box]}
    assert tile_to_image_box(box, tiles[0]) == box
    kept = build_candidates(tiles, data)
    assert len(kept["candidates"]) == 1
    assert kept["candidates"][0]["owned"] is False
    assert kept["rejected"] == []
    filtered = build_candidates(tiles, data, require_ownership=True)
    assert filtered["candidates"] == []
    assert "ownership" in filtered["rejected"][0]["reason"]


def test_duplicate_halo_geometry_has_one_stable_id_and_all_provenance():
    tiles = make_core_tiles(100, 50, (50, 50), 10)
    data = {tiles[0]["id"]: [[51, 10, 57, 20]],
            tiles[1]["id"]: [[11, 10, 17, 20]]}
    candidate = build_candidates(tiles, data)["candidates"][0]
    assert candidate["owned"] is True
    assert candidate["tile_id"] == tiles[1]["id"]
    assert len(candidate["observations"]) == 2
    assert {obs["owned"] for obs in candidate["observations"]} == {True, False}
    just_halo = build_candidates(tiles, {tiles[0]["id"]: data[tiles[0]["id"]]})
    assert just_halo["candidates"][0]["id"] == candidate["id"]
    assert len(build_candidates(tiles, data)["candidates"]) == 1


def test_stable_ids_ignore_order_and_never_merge_adjacent_or_contained_bodies():
    tiles = make_tiles(100, 100, 100, 0)
    boxes = [[10, 10, 90, 90], [20, 20, 22, 22], [22.1, 20, 24.1, 22]]
    baseline = None
    for order in permutations(boxes):
        data = {tiles[0]["id"]: list(order)}
        before = deepcopy((tiles, data))
        output = build_candidates(tiles, data)
        ids = [c["id"] for c in output["candidates"]]
        assert len(ids) == len(set(ids)) == 3
        assert ids == (baseline or ids)
        baseline = ids
        assert (tiles, data) == before
        assert all(set(c) == {"id", "box", "tile_id", "owned", "observations"}
                   for c in output["candidates"])


def test_partial_detection_reply_retains_good_geometry_with_rejection_details():
    tiles = make_tiles(100, 100, 100, 0)
    data = {tiles[0]["id"]: [[1, 2, 3, 4], [1.0, 2.0, 3.0, 4.0],
                              [True, 2, 3, 4], [20, 20, 19, 30]]}
    output = build_candidates(tiles, data)
    assert len(output["candidates"]) == 1
    assert len(output["candidates"][0]["observations"]) == 2
    assert [r["index"] for r in output["rejected"]] == [2, 3]
    assert all(r["tile_id"] == tiles[0]["id"] for r in output["rejected"])


def test_candidate_budgets_and_batch_structure_are_strict():
    tiles = make_tiles(100, 100, 100, 0)
    tid = tiles[0]["id"]
    cases = [([tiles[0], tiles[0]], {}), (tiles, {"foreign": []}),
             (tiles, {tid: None}), (tiles, []),
             (tiles + make_tiles(200, 200, 200, 0), {})]
    for descriptors, data in cases:
        with pytest.raises(ValueError):
            build_candidates(descriptors, data)
    with pytest.raises(ValueError, match="budget"):
        build_candidates(tiles, {tid: [None, None]}, max_candidates=1)
    with pytest.raises(ValueError):
        build_candidates(tiles, {}, require_ownership=1)
    assert build_candidates([], {}) == {"candidates": [], "rejected": []}


def test_review_valid_statuses_optional_parent_and_no_mutation():
    ids = ["body", "lead", "pad", "doubt", "loose"]
    reply = [{"id": "body", "status": "mounted_body"},
             {"id": "lead", "status": "attached_part", "parent_id": "body"},
             {"id": "pad", "status": "pcb_feature"},
             {"id": "doubt", "status": "uncertain"},
             {"id": "loose", "status": "attached_part"}]
    before = deepcopy(reply)
    output = validate_objectness_reply(ids, reply[::-1])
    assert output["valid"] is True and output["errors"] == []
    assert [r["id"] for r in output["reviews"]] == ids
    assert [r["keep"] for r in output["reviews"]] == [True, False, False, True, True]
    assert [r["verified"] for r in output["reviews"]] == [True, True, True, False, True]
    assert reply == before


@pytest.mark.parametrize("second", ["mounted_body", "pcb_feature", "uncertain"])
def test_duplicate_agreeing_or_conflicting_votes_cannot_remove_body(second):
    votes = [{"id": "a", "status": "pcb_feature"}, {"id": "a", "status": second},
             {"id": "b", "status": "mounted_body"}]
    for order in permutations(votes):
        output = validate_objectness_reply(["a", "b"], list(order))
        a, b = output["reviews"]
        assert output["valid"] is False
        assert (a["status"], a["keep"], a["verified"]) == ("uncertain", True, False)
        assert b["status"] == "mounted_body"


def test_missing_votes_stay_uncertain_and_independent_verified_rows_survive():
    output = validate_objectness_reply(["a", "b"], [{"id": "b", "status": "mounted_body"}])
    assert output["valid"] is False
    assert [r["status"] for r in output["reviews"]] == ["uncertain", "mounted_body"]
    assert all(r["keep"] for r in output["reviews"])


@pytest.mark.parametrize("foreign", [
    {"id": "foreign", "status": "mounted_body"}, {"id": True, "status": "pcb_feature"},
    {"id": [], "status": "pcb_feature"}, {"status": "pcb_feature"}, None,
])
def test_foreign_or_unassignable_votes_fail_closed_for_entire_reply(foreign):
    output = validate_objectness_reply(["a"], [{"id": "a", "status": "pcb_feature"}, foreign])
    assert output["valid"] is False
    assert output["reviews"] == [{"id": "a", "status": "uncertain", "parent_id": None,
                                   "verified": False, "keep": True}]


@pytest.mark.parametrize("vote", [
    {"id": "a"}, {"id": "a", "status": True}, {"id": "a", "status": []},
    {"id": "a", "status": "resistor"}, {"id": "a", "status": "mounted_body "},
    {"id": "a", "status": "pcb_feature", "box": [0, 0, 1, 1]},
    {"id": "a", "status": "pcb_feature", "kind": "pad"},
    {"id": "a", "status": "mounted_body", "parent_id": "b"},
    {"id": "a", "status": "attached_part", "parent_id": "a"},
    {"id": "a", "status": "attached_part", "parent_id": "foreign"},
    {"id": "a", "status": "attached_part", "parent_id": True},
    {"id": "a", "status": "attached_part", "parent_id": []},
])
def test_bad_known_vote_never_drops_candidate_or_corrupts_other_rows(vote):
    output = validate_objectness_reply(["a", "b"], [vote, {"id": "b", "status": "mounted_body"}])
    assert output["valid"] is False
    a, b = output["reviews"]
    assert a["status"] == "uncertain" and a["keep"] and not a["verified"]
    assert b["status"] == "mounted_body" and b["verified"]


@pytest.mark.parametrize("parent_status", ["uncertain", "pcb_feature", "attached_part"])
def test_attached_part_requires_verified_body_parent(parent_status):
    output = validate_objectness_reply(["a", "b"], [
        {"id": "a", "status": "attached_part", "parent_id": "b"},
        {"id": "b", "status": parent_status}])
    assert output["valid"] is False
    assert output["reviews"][0]["status"] == "uncertain"
    assert output["reviews"][0]["keep"] is True


def test_duplicate_parent_and_parent_cycles_never_authorize_child_removal():
    duplicate = [{"id": "a", "status": "attached_part", "parent_id": "b"},
                 {"id": "b", "status": "mounted_body"},
                 {"id": "b", "status": "mounted_body"}]
    cycle = [{"id": "a", "status": "attached_part", "parent_id": "b"},
             {"id": "b", "status": "attached_part", "parent_id": "a"}]
    for reply in [duplicate, cycle]:
        output = validate_objectness_reply(["a", "b"], reply)
        assert not output["valid"]
        assert all(r["keep"] and r["status"] == "uncertain" for r in output["reviews"])


@pytest.mark.parametrize("reply", [None, {}, "[]", [{"id": "a", "status": "pcb_feature"}] * 3])
def test_malformed_or_overbudget_reply_retains_unverified_bodies(reply):
    output = validate_objectness_reply(["a"], reply, max_review_rows=2)
    assert output["valid"] is False
    assert output["reviews"][0]["keep"] is True
    assert output["reviews"][0]["verified"] is False


@pytest.mark.parametrize("ids", [None, "a", ["a", "a"], [True], [""], [[]]])
def test_expected_id_contract_is_strict(ids):
    with pytest.raises(ValueError):
        validate_objectness_reply(ids, [])


def test_review_expected_budget_and_empty_inventory():
    with pytest.raises(ValueError):
        validate_objectness_reply(["a", "b"], [], max_candidates=1)
    with pytest.raises(ValueError):
        validate_objectness_reply([], [], max_review_rows=True)
    assert validate_objectness_reply([], []) == {"valid": True, "reviews": [], "errors": []}


def test_integer_review_ids_and_evidence_match_numbered_overlay_schema():
    reply = [{"id": 1, "status": "mounted_body", "evidence": "separate dark body"},
             {"id": 2, "status": "attached_part", "parent_id": 1,
              "evidence": "metal tab continuous with body 1"}]
    output = validate_objectness_reply([1, 2], reply)
    assert output["valid"] is True
    assert [r["keep"] for r in output["reviews"]] == [True, False]
    assert [r["evidence"] for r in output["reviews"]] == [r["evidence"] for r in reply]
    assert output["reviews"][1]["parent_id"] == 1


def test_numeric_and_string_ids_are_not_coerced_and_boolean_cannot_alias_integer():
    output = validate_objectness_reply([1, "1"], [
        {"id": 1, "status": "pcb_feature"}, {"id": "1", "status": "mounted_body"}])
    assert output["valid"] is True
    assert [r["keep"] for r in output["reviews"]] == [False, True]
    for bad in [True, 1.0, "1"]:
        output = validate_objectness_reply([1], [{"id": bad, "status": "pcb_feature"}])
        assert output["valid"] is False
        assert output["reviews"][0]["keep"] is True


@pytest.mark.parametrize("evidence", [True, 7, None, [], {}])
def test_nonstring_review_evidence_is_not_accepted_as_a_verified_vote(evidence):
    output = validate_objectness_reply([1], [
        {"id": 1, "status": "pcb_feature", "evidence": evidence}])
    assert not output["valid"]
    assert output["reviews"][0]["keep"] is True


def test_parent_id_types_cannot_alias_and_missing_parent_remains_uncertain():
    for parent in [True, 1.0, "1"]:
        output = validate_objectness_reply([1, 2], [
            {"id": 1, "status": "mounted_body"},
            {"id": 2, "status": "attached_part", "parent_id": parent}])
        assert not output["valid"]
        assert output["reviews"][1]["status"] == "uncertain"
        assert output["reviews"][1]["keep"] is True
    output = validate_objectness_reply([1, 2], [
        {"id": 2, "status": "attached_part", "parent_id": 1}])
    assert all(r["keep"] for r in output["reviews"])


def test_equivalent_pixel_and_normalized_geometry_share_candidate_id():
    tiles = make_tiles(100, 100, 100, 0)
    tid = tiles[0]["id"]
    pixels = build_candidates(tiles, {tid: [[-0.0, 10, 20, 30]]})
    normalized = build_candidates(tiles, {tid: [[100, 0, 300, 200]]}, "yxyx_1000")
    assert pixels["candidates"][0]["id"] == normalized["candidates"][0]["id"]


def test_bounds_do_not_impose_a_fuzzy_interior_margin():
    tile = make_tiles(240, 240, 100, 20)[4]
    # An explicitly complete box very near an edge is not automatically cropped.
    assert tile_to_image_box([0.01, 0.01, 99.99, 99.99], tile) == [80.01, 80.01, 179.99, 179.99]
