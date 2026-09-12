"""Offline pixel-grounded tests: known geometry, distractors and abstention."""
import copy

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from kicadgen.services.component_geometry import (
    ComponentGeometry, GeometryConfig, analyze_geometry,
)


def scene(scale=1):
    image = np.full((180*scale, 280*scale, 3), (32, 42, 28), np.uint8)
    cv2.rectangle(image, (80*scale, 60*scale), (139*scale, 99*scale), (85, 95, 80), -1)
    return image


def box(coords, image):
    return tuple(v/s for v, s in zip(coords, (image.shape[1], image.shape[0])*2))


def iou(a, b):
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0]))*max(0, min(a[3], b[3])-max(a[1], b[1]))
    return intersection/((a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection)


@pytest.mark.parametrize("scale", [1, 2, 3])
def test_noisy_box_improves_against_known_pixels_at_multiple_scales(scale):
    image = scene(scale)
    truth = box(tuple(v*scale for v in (80, 60, 140, 100)), image)
    guess = box(tuple(v*scale for v in (85, 64, 145, 104)), image)
    decision = ComponentGeometry(image).refine_box(guess)
    assert decision.accepted, decision.reason
    assert iou(decision.bbox, truth) > .9
    assert iou(decision.bbox, truth) > iou(guess, truth)+.1


def test_blank_and_random_sensor_noise_abstain():
    rng = np.random.default_rng(92)
    for image in (np.full((180, 280, 3), 35, np.uint8),
                  np.clip(rng.normal(35, 2, (180, 280, 3)), 0, 255).astype(np.uint8)):
        guess = (.3, .3, .5, .5)
        geometry = ComponentGeometry(image)
        decision = geometry.refine_box(guess)
        assert not decision.accepted
        assert decision.bbox == guess
        assert geometry.propose_candidates() == ()


def test_silkscreen_frame_is_review_only_even_when_empty():
    image = np.full((180, 280, 3), (32, 42, 28), np.uint8)
    cv2.rectangle(image, (80, 60), (139, 99), (240, 240, 240), 2)
    geometry = ComponentGeometry(image)
    candidates = geometry.propose_candidates()
    assert any(c.source == "silkscreen_frame" for c in candidates)
    assert all(c.requires_review for c in candidates)
    decision = geometry.refine_box(box((85, 64, 145, 104), image))
    assert not decision.accepted


def test_circular_vias_and_text_are_not_rectangle_proposals():
    image = np.full((180, 280, 3), 30, np.uint8)
    cv2.circle(image, (110, 80), 19, (235, 235, 235), 2)
    cv2.putText(image, "R12", (170, 95), cv2.FONT_HERSHEY_SIMPLEX, 1, (230, 230, 230), 2)
    assert ComponentGeometry(image).propose_candidates() == ()


def test_neighbor_blocks_new_overlap_and_inputs_are_unchanged():
    image = scene()
    guess = box((85, 64, 145, 104), image)
    neighbor = box((60, 60, 82, 100), image)
    detections = [{"_box": guess, "name": "R1", "x_mm": 123}, {"_box": neighbor}]
    previous = copy.deepcopy(detections)
    original_image = image.copy()
    result = analyze_geometry(image, detections, refine=True)
    assert not result.decisions[0].accepted
    assert result.decisions[0].reason == "neighbor_overlap"
    assert detections == previous
    assert np.array_equal(image, original_image)


def test_close_parts_are_not_merged_without_neighbor_hints():
    image = scene()
    cv2.rectangle(image, (146, 60), (205, 99), (85, 95, 80), -1)
    guess = box((85, 64, 145, 104), image)
    decision = ComponentGeometry(image).refine_box(guess)
    # The adjacent left edge competes with the target's right edge: abstention
    # is the safe answer, even though a target rectangle really exists.
    assert not decision.accepted
    assert decision.reason == "ambiguous_edges"
    assert decision.bbox == guess


def test_grossly_misplaced_box_cannot_jump_to_component():
    image = scene()
    guess = box((175, 60, 235, 100), image)
    decision = ComponentGeometry(image).refine_box(guess)
    assert not decision.accepted
    assert decision.bbox == guess


def test_tightening_to_body_must_not_clip_bright_terminals():
    image = scene()
    # Narrow pins do not support an entire side, but belong to the package.
    for y in (64, 87):
        cv2.rectangle(image, (72, y), (82, y+7), (225, 225, 225), -1)
        cv2.rectangle(image, (137, y), (147, y+7), (225, 225, 225), -1)
    guess = box((72, 56, 148, 104), image)
    result = ComponentGeometry(image).refine_box(guess)
    assert not result.accepted
    assert result.bbox == guess


def test_geometry_is_independent_of_component_identity():
    image = scene()
    guess = box((85, 64, 145, 104), image)
    a = analyze_geometry(image, [{"_box": guess, "kind": "resistor", "name": "R1"}], refine=True)
    b = analyze_geometry(image, [{"_box": guess, "kind": "transformer", "name": "anything"}], refine=True)
    assert a == b


def test_crop_boundary_and_subresolution_boxes_are_preserved():
    geometry = ComponentGeometry(scene())
    for guess, reason in (((0, .2, .2, .5), "crop_boundary"),
                          ((.3, .3, .31, .31), "too_small")):
        decision = geometry.refine_box(guess)
        assert decision.reason == reason
        assert decision.bbox == guess


@pytest.mark.parametrize("value", [None, [0, 0, 0, 1], [-.1, 0, 1, 1],
                                  [0, 0, 2, 1], [0, 0, float("nan"), 1], [0, 1, 2]])
def test_invalid_box_fails_explicitly(value):
    with pytest.raises(ValueError):
        ComponentGeometry(scene()).refine_box(value)


@pytest.mark.parametrize("image", [None, np.zeros((20, 20), np.uint8),
                                  np.zeros((20, 20, 3), float), np.zeros((1, 20, 3), np.uint8)])
def test_invalid_image_fails_explicitly(image):
    with pytest.raises(ValueError):
        ComponentGeometry(image)


def test_invalid_batch_and_config_fail_explicitly():
    with pytest.raises(ValueError):
        analyze_geometry(scene(), [{"bbox": [0, 0, 1, 1]}])
    with pytest.raises(ValueError):
        GeometryConfig(search_fraction=float("nan"))
    with pytest.raises(ValueError):
        GeometryConfig(search_fraction=.9)


def test_deterministic_empty_inventory_still_proposes_regions():
    image = scene()
    first = analyze_geometry(image, [])
    assert first == analyze_geometry(image, [])
    assert not first.decisions
    assert first.candidates
    assert all(c.requires_review for c in first.candidates)


def test_default_analysis_cannot_change_even_a_refinable_box():
    image = scene()
    guess = box((85, 64, 145, 104), image)
    result = analyze_geometry(image, [{"_box": guess}])
    assert result.decisions[0].bbox == guess
    assert result.decisions[0].reason == "refinement_disabled"
    experimental = analyze_geometry(image, [{"_box": guess}], refine=True)
    assert experimental.decisions[0].accepted
    assert experimental.decisions[0].requires_review


def test_missing_side_cannot_be_replaced_by_three_strong_edges():
    image = np.full((180, 280, 3), 30, np.uint8)
    for p, q in (((80, 60), (140, 60)), ((140, 60), (140, 100)), ((140, 100), (80, 100))):
        cv2.line(image, p, q, (110, 110, 110), 1)
    guess = box((85, 64, 145, 104), image)
    assert not ComponentGeometry(image).refine_box(guess).accepted


def test_batch_result_is_order_independent_and_overlap_does_not_grow():
    image = scene()
    cv2.rectangle(image, (156, 60), (215, 99), (85, 95, 80), -1)
    detections = [{"_box": box((85, 64, 145, 104), image)},
                  {"_box": box((151, 56, 211, 96), image)}]
    result = analyze_geometry(image, detections, refine=True)
    reverse = analyze_geometry(image, list(reversed(detections)), refine=True)
    assert result.decisions == tuple(reversed(reverse.decisions))
    assert result.decisions[0].bbox[2] < result.decisions[1].bbox[0]


def test_rotated_rectangle_is_not_an_axis_aligned_candidate():
    image = np.full((180, 280, 3), 30, np.uint8)
    polygon = cv2.boxPoints(((120, 85), (70, 32), 30)).astype(np.int32)
    cv2.fillConvexPoly(image, polygon, (120, 120, 120))
    geometry = ComponentGeometry(image)
    assert not geometry.propose_candidates()
    assert not geometry.refine_box(box((80, 50, 160, 120), image)).accepted
