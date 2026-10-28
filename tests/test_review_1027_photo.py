"""@brief Adversarial review of 2026-10-27, photo pipeline: findings F1-F10 fixed by their class.

Every finding gets a positive and a negative case that differ for a
structural reason (the user's rule: no threshold tuned on the case at hand).

F1  a user cancellation during the marking reading propagates; a provider
    error is downgraded to a note; a good answer is applied.
F2  a measured contour is accepted only when its centre lies on the proposed
    body (the hypothesis bounds the shift, not an absolute 2 mm).
F3  the box's aspect overrides the network's axis vote only beyond the
    elongation the edge uncertainty can explain; `rotation_basis` says which.
F4  one source of the outline uncertainty: edge lands and edge notches report
    the same tolerance at the same scale.
F5  a body measured on its edges reaches the KiCad matcher with its measured
    sides and angle, not with the axis-aligned box.
F6  the resolution warning judges the coarser axis.
F7  elongation hinges and the detail border band derive from the uncertainty.
F8  a checkpoint declaring a different preprocessing recipe is refused.
F9  the crop box is named after the canvas it refers to (original or deskewed).
F10 a malformed rectangle in a cached answer makes it «not reusable», not a crash.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

pytest.importorskip("cv2")

from kicadgen.cad3d import measurement_uncertainty as mu
from kicadgen.services import edge_lands, edge_notches
from kicadgen.services import photo_grounding as grounding
from kicadgen.services import photo_inventory as inv
from kicadgen.services import photo_markings as markings
from kicadgen.services import photo_refine as refine
from kicadgen.services import smd_component_classifier as smd
from kicadgen.services.ai_diagnostics import CallCancelled

from test_photo_grounding import KINDS, cache_kwargs, component, native, photo, vote  # noqa: F401
from test_photo_markings import LENGTH, MODEL, WIDTH, _components, _photo
from test_photo_refine import BODY_LENGTH_MM, BODY_WIDTH_MM, SCALE, _synthetic_photo


# ── F1: cancellation during the marking reading ──────────────────────────

def test_a_cancellation_during_the_marking_reading_propagates():
    """@brief Positive: the user's decision reaches the caller, it is not a failed reading."""
    def cancelled(**kwargs):
        raise CallCancelled("user pressed cancel")
    logs = []
    with pytest.raises(CallCancelled):
        markings.read_markings(_photo(4000), _components(), LENGTH, WIDTH, MODEL,
                               cancelled, logs.append)
    assert not any("non riuscita" in line for line in logs)


def test_a_provider_error_during_the_marking_reading_is_a_note():
    """@brief Negative: a provider failure keeps the inventory markings and returns 0."""
    components = _components()

    def broken(**kwargs):
        raise ValueError("provider down")
    logs = []
    assert markings.read_markings(_photo(4000), components, LENGTH, WIDTH, MODEL,
                                  broken, logs.append) == 0
    assert components[0]["marking"] == "SP3?52"
    assert any("non riuscita" in line for line in logs)


def test_a_good_marking_answer_is_still_applied():
    """@brief The narrowed except does not swallow the happy path."""
    components = _components()

    def reading(**kwargs):
        return {"components": [{"id": 1, "marking": "SP3152", "marking_confidence": "high"},
                               {"id": 3, "marking": "472", "marking_confidence": "high"}]}
    assert markings.read_markings(_photo(4000), components, LENGTH, WIDTH, MODEL,
                                  reading, lambda _line: None) == 1
    assert components[0]["marking"] == "SP3152"
    assert components[0]["_marking_inventory"] == "SP3?52"


def test_the_markings_step_does_not_hide_a_defect():
    """@brief A programming error is neither a cancellation nor a provider failure: it propagates."""
    def defect(**kwargs):
        raise KeyError("bug")
    with pytest.raises(KeyError):
        markings.read_markings(_photo(4000), _components(), LENGTH, WIDTH, MODEL,
                               defect, lambda _line: None)


# ── F2: the hypothesis bounds the accepted shift ─────────────────────────

PROPOSAL = {"x_mm": 0.0, "y_mm": 0.0, "length_mm": 1.0, "width_mm": 0.5, "rotation_deg": 0.0}


def test_a_contour_centred_on_the_proposed_body_is_accepted():
    """@brief Positive: the measured centre lies inside the proposal's own rectangle."""
    assert refine.centre_on_hypothesis(0.4, 0.2, PROPOSAL, edge_mm=0.1)


def test_a_same_size_contour_one_body_width_away_is_refused():
    """@brief Negative: an 0402-sized silkscreen outline beside the 0402 (0.6 mm off, < 2 mm)."""
    assert not refine.centre_on_hypothesis(0.0, 0.6, PROPOSAL, edge_mm=0.1)
    # The old absolute cap would have accepted it: 0.6 mm is well within 2 mm.
    assert 0.6 < 2.0


def test_the_bound_follows_the_proposal_rotation():
    """@brief The rectangle is tested in the proposal's own frame, for any angle."""
    turned = dict(PROPOSAL, rotation_deg=90.0)
    # Along the turned length (Y) 0.4 mm is inside; across it (X) 0.4 mm is not.
    assert refine.centre_on_hypothesis(0.0, 0.4, turned, edge_mm=0.0)
    assert not refine.centre_on_hypothesis(0.4, 0.0, turned, edge_mm=0.0)


def test_on_a_photo_the_twin_outline_is_not_taken_for_the_body():
    """@brief Same body, two proposals: on it (corrected) and one body width beside it (refused)."""
    image = _synthetic_photo((5.0, -3.0), (3.0, 1.2), rotated=False)
    frame = refine.find_board(image, BODY_LENGTH_MM, BODY_WIDTH_MM)
    assert frame is not None
    on_body = {"x_mm": 5.5, "y_mm": -3.2, "length_mm": 3.0, "width_mm": 1.2, "rotation_deg": 0.0}
    beside = {"x_mm": 5.0, "y_mm": -1.5, "length_mm": 3.0, "width_mm": 1.2, "rotation_deg": 0.0}
    measured = refine.refine_component_edges(image, frame, on_body)
    assert measured is not None and measured["y_mm"] == pytest.approx(-3.0, abs=0.3)
    assert refine.refine_component_edges(image, frame, beside) is None


# ── F3: box aspect versus network vote ───────────────────────────────────

VERTICAL_VOTE = (0.1, 0.4, 0.1, 0.4)   # the network says 90


def test_a_box_elongated_beyond_the_uncertainty_decides_the_axis():
    angle, confidence, basis = smd._rotation("axis", VERTICAL_VOTE, (0, 0, 100, 50))
    assert (angle, basis) == (0, smd.ROTATION_BASIS_BOX)
    assert confidence == pytest.approx(0.2)


def test_a_square_box_leaves_the_axis_to_the_network():
    angle, confidence, basis = smd._rotation("axis", VERTICAL_VOTE, (0, 0, 50, 50))
    assert (angle, basis) == (90, smd.ROTATION_BASIS_NETWORK)
    assert confidence == pytest.approx(0.8)


@pytest.mark.parametrize("long_px", [51, 52, 53])
def test_an_aspect_inside_the_uncertainty_band_never_overrides(long_px):
    """@brief Negative: 50 px short side, hinge 1 + 4/50 = 1.08; up to 54 px the box says nothing."""
    assert long_px < mu.elongation_hinge(50) * 50
    angle, _, basis = smd._rotation("axis", VERTICAL_VOTE, (0, 0, long_px, 50))
    assert (angle, basis) == (90, smd.ROTATION_BASIS_NETWORK)


def test_the_hinge_is_scale_aware():
    """@brief The same 1.1 ratio is a shape on a large box and noise on a tiny one."""
    assert 1.1 > mu.elongation_hinge(200)      # 200 px short side: 1.02
    assert 1.1 < mu.elongation_hinge(12)       # 12 px short side (0402 at 24 px/mm): 1.33
    assert mu.elongation_hinge(0) == float("inf") and mu.elongation_hinge(None) == float("inf")


def test_full_pose_classes_report_the_network_basis():
    assert smd._rotation("full", (0.1, 0.7, 0.1, 0.1), (0, 0, 10, 10)) == (
        90, 0.7, smd.ROTATION_BASIS_NETWORK)
    assert smd._rotation("none", VERTICAL_VOTE, (0, 0, 10, 10)) == (None, None, None)


# ── F4: one source of the outline uncertainty ────────────────────────────

def test_lands_and_notches_share_the_outline_uncertainty():
    assert edge_lands.EDGE_UNCERTAINTY_PX == mu.EDGE_UNCERTAINTY_PX
    assert edge_notches.EDGE_UNCERTAINTY_PX == mu.EDGE_UNCERTAINTY_PX
    scale = 37.0
    land_tolerance = 2 * edge_lands.EDGE_UNCERTAINTY_PX / scale
    notch_tolerance = 2 * edge_notches.EDGE_UNCERTAINTY_PX / scale
    assert land_tolerance == pytest.approx(notch_tolerance)
    assert land_tolerance == pytest.approx(mu.side_uncertainty_mm(mu.edge_uncertainty_mm(
        scale_px_per_mm=scale)[0]))


def test_the_detector_box_uncertainties_keep_their_own_names():
    """@brief A drawn box has no edge-mask growth: its quantisation floor is a different quantity."""
    from kicadgen.services import photo_yolo
    assert inv.DETECTION_EDGE_UNCERTAINTY_PX == 1.0
    assert photo_yolo._BOX_UNCERTAINTY_INPUT_PX == 8
    assert inv.DETECTION_EDGE_UNCERTAINTY_PX != mu.EDGE_UNCERTAINTY_PX


# ── F5: measured geometry reaches the matcher ────────────────────────────

def _prediction(**updates):
    base = {"class": "QFN", "kind": "ic", "package": "QFN", "accepted": True, "body": None,
            "rotation_deg": 0, "rotation_confidence": 0.9, "rotation_kind": "axis",
            "rotation_views_agree": True, "terminal_count": None, "model_match": None,
            "footprint": None, "footprint_candidates": [], "size_mm": None,
            "class_confidence": 0.9}
    base.update(updates)
    return base


def _row(identifier, measured):
    row = dict(_id=identifier, _box=[.2, .2, .6, .6], x_mm=0., y_mm=0., length_mm=5., width_mm=5.,
               rotation_deg=45.0 if measured else 0, height_mm=1.)
    if measured:
        row['_edges_measured'] = True
    return row


def test_a_measured_row_sends_its_sides_and_angle_and_an_unmeasured_one_sends_none(monkeypatch):
    captured = {}

    def classify(photo, boxes, **kwargs):
        captured.update(kwargs)
        return [_prediction() for _ in boxes]
    monkeypatch.setattr(smd, "classify_photo_boxes", classify)
    rows = [_row(1, measured=True), _row(2, measured=False)]
    inv.classify_smd_rows(rows, np.zeros((40, 60, 3), np.uint8), (10, 10, 30, 30), (0.5, 0.5),
                          lambda _m: None, None, lambda *_a: None, model_path="x.pt")
    assert captured["measured_mm"] == [
        {"length_mm": 5.0, "width_mm": 5.0, "rotation_deg": 45.0}, None]


def test_the_matcher_is_asked_for_the_measured_body_not_the_enclosing_box(monkeypatch):
    """@brief A 5 x 5 QFN at 45 degrees: measured 5 x 5 / 45, enclosing box 7.1 x 7.1 / 0."""
    from kicadgen.cad3d import kicad_models
    asked = []

    def candidates(component, **kwargs):
        asked.append(dict(component))
        return {"status": "unresolved", "candidates": [], "selected_path": None}
    monkeypatch.setattr(kicad_models, "model_candidates", candidates)
    monkeypatch.setattr(kicad_models, "footprint_sizes", lambda *a, **k: {})
    measured = _prediction(size_mm=[5.0, 5.0], size_basis="measured_edges",
                           measured_mm={"length_mm": 5.0, "width_mm": 5.0, "rotation_deg": 45.0})
    boxed = _prediction(size_mm=[7.1, 7.1], size_basis="detector_box", measured_mm=None)
    smd._attach_kicad_results([measured, boxed], None, lambda _m: None, None)
    assert (asked[0]["length_mm"], asked[0]["width_mm"], asked[0]["rotation_deg"],
            asked[0]["bbox_basis"]) == (5.0, 5.0, 45.0, "measured_edges")
    assert (asked[1]["length_mm"], asked[1]["width_mm"], asked[1]["rotation_deg"],
            asked[1]["bbox_basis"]) == (7.1, 7.1, 0, "unknown")


@pytest.mark.parametrize("bad", [
    [None], ["x", None], [{"length_mm": 0, "width_mm": 1}], [{"length_mm": float("nan"),
                                                                "width_mm": 1}, None],
])
def test_malformed_measurements_are_refused_before_the_network_runs(bad):
    with pytest.raises(ValueError):
        smd._checked_measurements(bad, 2)
    assert smd._checked_measurements(None, 2) == [None, None]


# ── F6: the coarser axis decides the resolution warning ──────────────────

def test_a_photo_squeezed_along_y_warns_on_y():
    messages = []
    # X: 3200 px / 63.3 mm = 50 px/mm (fine); Y: 600 px / 63.3 mm = 9.5 px/mm (coarse).
    axis = inv._warn_if_too_coarse(3200, 63.3, messages.append, crop_py=600, body_width=63.3)
    assert axis == "Y" and len(messages) == 1
    assert "9.5 px/mm" in messages[0] and "600" in messages[0]


def test_an_isotropic_fine_photo_does_not_warn():
    messages = []
    axis = inv._warn_if_too_coarse(3200, 63.3, messages.append, crop_py=1600, body_width=31.65)
    assert axis is None and not messages


def test_without_the_y_axis_x_alone_is_judged_as_before():
    messages = []
    assert inv._warn_if_too_coarse(770, 63.3, messages.append) == "X"
    assert "12.2 px/mm" in messages[0]


# ── F7: hinges and bands derived from the uncertainty ────────────────────

FULL = (0.0, 0.0, 1.0, 1.0)


def test_the_same_box_contradicts_the_axis_on_a_fine_photo_and_not_on_a_coarse_one():
    """@brief 2.0 x 2.2 mm: 20 px apart at 100 px/mm (shape), 2 px at 10 px/mm (noise)."""
    assert inv.axes_contradict(2.0, 2.2, FULL, (4000, 2000), 40.0, 20.0)
    assert not inv.axes_contradict(2.0, 2.2, FULL, (400, 200), 40.0, 20.0)
    assert not inv.axes_contradict(2.2, 2.0, FULL, (4000, 2000), 40.0, 20.0)


def test_without_pixels_the_declared_fallback_hinge_applies():
    assert not inv.axes_contradict(2.0, 2.2, FULL, None, 40.0, 20.0)
    assert inv.axes_contradict(2.0, 2.0 * inv.UNKNOWN_SCALE_ELONGATION_HINGE + 0.01, FULL,
                               None, 40.0, 20.0)


def test_decode_detections_flags_the_contradiction_only_beyond_the_uncertainty():
    item = dict(name="R", kind="resistor", bbox=[.1, .1, .15, .21], height_mm=.5, rotation_deg=180,
                color_hex="#303030", marking="", confidence="high", object_type="component",
                fully_visible=True)
    fine = inv.decode_detections([dict(item)], FULL, 40, 20, "v", lambda _m: None,
                                 photo_px=(4000, 2000))[0]
    coarse = inv.decode_detections([dict(item)], FULL, 40, 20, "v", lambda _m: None,
                                   photo_px=(400, 200))[0]
    assert fine["_uncertain"] and fine["rotation_deg"] == 270
    assert not coarse.get("_uncertain") and coarse["rotation_deg"] == 180


def test_the_verification_uses_the_same_hinge():
    """@brief 4.0 x 4.2 mm declared vertical: contradiction at 100 px/mm, none at 4.5 px/mm."""
    def verify(photo_px):
        candidate = component(box=[100, 100, 300, 205])
        [result] = grounding.apply_classifications(
            [candidate], {"components": [vote(rotation_deg=90)]}, KINDS, 40, 20,
            lambda _m: None, photo_px=photo_px)
        return result
    fine, coarse = verify((4000, 2000)), verify((301, 90))
    # The declared 90 is turned by a quarter so the long axis follows the box.
    assert fine["_uncertain"] and fine["rotation_deg"] == 180
    assert not coarse["_uncertain"] and coarse["rotation_deg"] == 90


def test_the_detail_border_band_is_the_quantum_plus_the_edge_uncertainty():
    assert grounding.detail_border_band((2000, 1000), FULL, 40, 20) == pytest.approx((0.002, 0.003))
    assert grounding.detail_border_band((200, 400), FULL, 40, 20) == pytest.approx((0.011, 0.006))
    assert grounding.detail_border_band(None, FULL, 40, 20) == pytest.approx((0.002, 0.002))


def test_a_box_near_the_tile_edge_is_cut_on_a_small_detail_and_whole_on_a_large_one():
    region = (.25, 0, .75, 1)
    entry = native([200, 5, 400, 300])      # left edge 5/1000 inside the detail
    small = grounding.decode_boxes({"components": [dict(entry)]}, region, 40, 20, KINDS, "d",
                                   lambda _m: None, photo_px=(200, 200))
    large = grounding.decode_boxes({"components": [dict(entry)]}, region, 40, 20, KINDS, "d",
                                   lambda _m: None, photo_px=(2000, 2000))
    assert small == [] and len(large) == 1


# ── F8: declared preprocessing recipe ────────────────────────────────────

def test_a_checkpoint_without_a_recipe_is_accepted_on_the_declared_contract():
    smd.validate_preprocessing({"classes": ["x"]}, "ckpt.pt")


def test_a_matching_recipe_is_accepted_with_float_rounding():
    recipe = dict(smd.PREPROCESSING, mean=[0.48500001, 0.456, 0.406], resize="Letterbox")
    smd.validate_preprocessing({"preprocessing": recipe}, "ckpt.pt")


@pytest.mark.parametrize("change", [
    {"img_size": 256}, {"interpolation": "bilinear"}, {"pad_rgb": [0, 0, 0]},
    {"std": [0.5, 0.5, 0.5]}, {"center_crop": 200},
])
def test_a_mismatching_recipe_is_refused(change):
    recipe = dict(smd.PREPROCESSING, **change)
    with pytest.raises(ValueError, match="preprocessing"):
        smd.validate_preprocessing({"preprocessing": recipe}, "ckpt.pt")
    with pytest.raises(ValueError, match="preprocessing"):
        smd.validate_preprocessing({"preprocessing": "letterbox"}, "ckpt.pt")


# ── F9: crop provenance ───────────────────────────────────────────────────

def test_an_unrotated_crop_keeps_the_original_key_and_a_rotated_one_is_named_honestly():
    plain = inv.crop_provenance([1, 2, 30, 40], None, 100, 80)
    assert plain == {"crop_bbox_original_px": [1, 2, 30, 40]}
    turned = inv.crop_provenance([1, 2, 30, 40], 2.5, 120, 90)
    assert "crop_bbox_original_px" not in turned
    assert turned["crop_bbox_deskewed_px"] == [1, 2, 30, 40]
    assert turned["deskew"] == {"angle_deg": 2.5, "canvas_width": 120, "canvas_height": 90}


def test_readers_accept_both_layouts_and_the_legacy_record():
    assert inv.crop_box_from_metadata({"crop_bbox_original_px": [1, 2, 3, 4]}) == ([1, 2, 3, 4], "original")
    assert inv.crop_box_from_metadata({"crop_bbox_deskewed_px": [5, 6, 7, 8],
                                       "deskew": {}}) == ([5, 6, 7, 8], "deskewed")
    assert inv.crop_box_from_metadata({}) == (None, None)
    assert inv.crop_box_from_metadata(None) == (None, None)


def test_prepare_photo_records_the_deskew_beside_the_source_fingerprint(monkeypatch, tmp_path):
    """@brief Integration: find_board fails, the deskew succeeds, the metadata says so."""
    from kicadgen.services import photo_refine
    source = np.full((50, 80, 3), 90, np.uint8)
    straight = np.full((60, 90, 3), 90, np.uint8)
    path = tmp_path / "board.png"
    path.write_bytes(b"not decoded: read_photo is stubbed")
    monkeypatch.setattr(inv, "read_photo", lambda _p: source)
    frames = iter([None, SimpleNamespace(cx=45, cy=30, width_px=60, height_px=40)])
    monkeypatch.setattr(photo_refine, "find_board", lambda *a: next(frames))
    monkeypatch.setattr(photo_refine, "deskew_board_report", lambda *a: (straight, -3.0))
    boxes = []
    crop = inv.prepare_photo(path, 60, 40, frame_callback=lambda frame, box: boxes.append(box))
    assert crop.shape == (40, 60, 3) and boxes == [(15, 10, 75, 50)]
    assert inv.crop_provenance(list(boxes[0]), -3.0, 90, 60)["deskew"]["canvas_width"] == 90


# ── F10: malformed rectangles in cached answers ──────────────────────────

@pytest.mark.parametrize("corrupt", [
    {"refined_bbox": "0,0,1,1"}, {"refined_bbox": [1, 2, 3]}, {"refined_bbox": None},
    {"box_2d": [1, 2, 3]}, {"box_2d": "bad"}, {"box_2d": [True, 1, 2, 3]}, {"color_hex": 7},
])
def test_a_malformed_cached_rectangle_is_not_reusable_and_does_not_crash(tmp_path, cache_kwargs,
                                                                          corrupt):
    good = {"components": [native()]}
    call = Mock(return_value=good)
    grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda _m: None)
    [record] = list(tmp_path.glob("*.json"))
    data = json.loads(record.read_text(encoding="utf-8"))
    data["response"]["components"][0].update(corrupt)
    record.write_text(json.dumps(data), encoding="utf-8")
    assert grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda _m: None) == good
    assert call.call_count == 2


def test_a_well_formed_legacy_refined_bbox_is_still_reusable(tmp_path, cache_kwargs):
    good = {"components": [native(refined_bbox=[0.1, 0.1, 0.4, 0.3])]}
    call = Mock(return_value=good)
    grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda _m: None)
    assert grounding.cached_call(call, cache_kwargs, tmp_path, "stage", lambda _m: None) == good
    assert call.call_count == 1
