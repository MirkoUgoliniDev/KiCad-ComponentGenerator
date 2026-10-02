"""Offline regressions reduced from contradictory crop answers, without project data.

These tests exercise reconciliation of recorded observations, not vision accuracy.
No provider call, project file, or accepted-model database is used.
"""

import pytest
import numpy as np

from kicadgen.services import photo_crop_reading as crop


def observation(visual_class="ic", confidence="high", sides=(4, 4, 0, 0),
                total=8, two_ends=False, evidence="Multiple separate side terminals."):
    return {
        "visual_class": visual_class,
        "visual_class_confidence": confidence,
        "visual_class_evidence": evidence,
        "bodies_in_frame": 1,
        "metal_contacts_total": total,
        "contacts_complete": True,
        "two_end_terminals_only": two_ends,
        "top_translucent": False,
        "something_visible_inside": False,
        "coloured_patch_at_end": False,
        "band_at_end": False,
        "tab": False,
        "body_shape": "flat_chip" if two_ends else "thick_moulded_block",
        "printed_text": "none",
        "marking": "",
        "size_family": "",
        "leads_by_side": dict(zip(crop.SIDES, sides)),
    }


def derive(answer, inventory_class="ic", envelope=(6.0, 4.7, .2)):
    parsed = crop.parse_observation(answer)
    assert parsed is not None
    return crop.derive_reading(parsed, envelope, inventory_class=inventory_class)


def test_complete_side_counts_cannot_disagree_with_total_contacts():
    # An actual answer claimed eight contacts while enumerating only six.
    reading = derive(observation(sides=(3, 0, 0, 3), total=8))
    assert reading["inconclusive"], "An internally contradictory count must not be adopted"


def test_two_complete_readings_eight_and_six_are_not_consensus():
    first = derive(observation())
    second = derive(observation(sides=(3, 3, 0, 0), total=6))
    assert "leads" in crop.disagreements(first, second)


@pytest.mark.parametrize(
    "inventory,visual,confidences",
    [
        ("resistor", "capacitor", ("high", "medium")),
        ("capacitor", "resistor", ("medium", "low")),
        ("capacitor", "resistor", ("medium", "medium")),
    ],
)
def test_visual_category_conflict_is_not_silent_inventory_confirmation(
        inventory, visual, confidences):
    readings = [derive(observation(
        visual_class=visual, confidence=confidence, sides=(None,) * 4,
        total=2, two_ends=True,
        evidence="Two metal end caps and an opaque rectangular body; category remains uncertain.",
    ), inventory_class=inventory, envelope=(2.0, 1.0, .15))
        for confidence in confidences]
    # A weak crop must not overwrite a category, but conflicting observations
    # must remain visible rather than appear as two confirmations of inventory.
    assert not any(r["class_decided"] and r["class"] == visual for r in readings)
    assert any(r["inconclusive"] or r["suspicions"] for r in readings) or (
        crop.disagreements(*readings)
    ), "A visual/inventory conflict was silently erased"


def test_visible_contact_total_cannot_overrule_incomplete_sides():
    answer = observation(sides=(3, None, 0, 0), total=8)
    answer["contacts_complete"] = False
    reading = derive(answer)
    assert reading["inconclusive"], "Partly hidden terminals cannot yield a complete package"


@pytest.mark.parametrize(
    "inventory,visual,confidences,expected_status",
    [
        ("resistor", "capacitor", ("high", "medium"), "conflict"),
        ("capacitor", "resistor", ("medium", "low"), "inventory_retained"),
        ("capacitor", "resistor", ("medium", "medium"), "conflict"),
    ],
)
def test_pipeline_reports_category_conflict_or_retention_without_claiming_support(
        inventory, visual, confidences, expected_status):
    pytest.importorskip("cv2")
    row = {
        "_id": 1, "component_id": "X1", "name": "X1", "class": inventory,
        "kind": inventory, "class_confidence": "medium", "package": "",
        "x_mm": 0.0, "y_mm": 0.0, "length_mm": 2.0, "width_mm": 1.0,
        "rotation_deg": 0.0,
    }
    answers = iter(observation(
        visual_class=visual, confidence=confidence, sides=(None,) * 4,
        total=2, two_ends=True,
        evidence="Opaque rectangular body and two metal end caps; alternative category remains possible.",
    ) for confidence in confidences)
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return next(answers)

    crop.read_bodies(np.zeros((100, 200, 3), dtype=np.uint8), [row], 10.0, 5.0,
                     "deepseek-api/deepseek-flash", fake_call, lambda _: None)
    assert len(calls) == 2
    assert row["class"] == inventory
    assert row["_crop_class_status"] == expected_status
    if expected_status == "conflict":
        assert row["_crop_reading"] == "inconclusive"
        assert row["class_confidence"] == "low"
