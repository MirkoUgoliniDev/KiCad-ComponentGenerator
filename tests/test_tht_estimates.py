"""@brief Lead sections, estimated hole and pad, and the «can it pass» predicate (`core.tht_estimates`).

Each rule has a positive and a negative case that differ for a structural
reason: a flat blade is judged by its diagonal in a round hole and by its
rectangle in a slot, a hole equal to the lead does not let it through, and
a value out of contract is refused instead of being dropped.
"""
import math

import pytest

from kicadgen.core import tht_estimates as te

ROUND = {"shape": "round", "diameter_mm": 0.6}
FLAT = {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}


# ── contract ───────────────────────────────────────────────────────────────

def test_a_lead_of_the_contract_is_copied_and_none_stays_none():
    clean = te.normalised_lead(ROUND)
    assert clean == ROUND and clean is not ROUND
    assert te.normalised_lead({"shape": "flat", "width_mm": 1, "thickness_mm": 0.4}) == FLAT
    assert te.normalised_lead(None) is None


@pytest.mark.parametrize("bad", [
    {"shape": "square", "diameter_mm": 0.6},                       # unknown shape
    {"shape": "round"},                                            # size missing
    {"shape": "round", "diameter_mm": 0.0},                        # not positive
    {"shape": "round", "diameter_mm": float("nan")},               # not finite
    {"shape": "round", "diameter_mm": "0.6"},                      # a string is not a number
    {"shape": "round", "diameter_mm": True},                       # nor is a boolean
    {"shape": "round", "diameter_mm": 0.6, "width_mm": 1.0},       # a flat lead's key on a round one
    {"shape": "flat", "width_mm": 1.0},                            # thickness missing
    {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4, "diametre_mm": 1},  # typo: never dropped
    ["round", 0.6],                                                # not a mapping
])
def test_a_lead_out_of_contract_is_refused(bad):
    with pytest.raises(ValueError):
        te.normalised_lead(bad, "7")


def test_sources_keep_known_keys_and_words_and_an_empty_mapping_states_nothing():
    given = {"shape": "estimated", "drill_mm": "derived", "lead": "read"}
    clean = te.normalised_sources(given)
    assert clean == given and clean is not given
    assert list(clean) == ["drill_mm", "shape", "lead"]           # contract order, whatever the input's
    assert te.normalised_sources({}) is None and te.normalised_sources(None) is None


@pytest.mark.parametrize("bad", [
    {"drill_mm": "guessed"},          # unknown word: would read as «not estimated»
    {"drill": "read"},                # unknown key
    {"pad_size": None},               # a key present must carry a word
    {"pad_size": 1},
    "estimated",
])
def test_sources_out_of_contract_are_refused(bad):
    with pytest.raises(ValueError):
        te.normalised_sources(bad, "7")


def test_unverified_values_lists_derived_and_estimated_pad_values_only():
    assert te.unverified_values({"drill_mm": "derived", "pad_size": "estimated", "shape": "read"}) == (
        "drill_mm", "pad_size")
    # An estimated LEAD is not a pad value; read values are verified; no mapping states nothing.
    assert te.unverified_values({"lead": "estimated", "drill_mm": "read"}) == ()
    assert te.unverified_values(None) == ()


# ── circumscribed diameter, hole and pad ───────────────────────────────────

def test_the_circumscribed_diameter_is_the_diameter_or_the_diagonal():
    assert te.lead_circumscribed_diameter(ROUND) == 0.6
    assert te.lead_circumscribed_diameter(FLAT) == pytest.approx(math.hypot(1.0, 0.4))
    # The diagonal, not the width: the same blade is wider than its own width.
    assert te.lead_circumscribed_diameter(FLAT) > FLAT["width_mm"]
    with pytest.raises(ValueError):
        te.lead_circumscribed_diameter(None)


def test_the_estimated_hole_is_lead_plus_clearance_rounded_up_to_the_drill_step():
    # 0.6 + 0.25 = 0.85: already on the 0.05 step, kept (not lifted by float noise).
    assert te.estimated_hole(ROUND) == 0.85
    assert te.estimated_hole({"shape": "round", "diameter_mm": 0.5}) == 0.75
    # 1.0770 + 0.25 = 1.3270: off the step, rounded UP, never down.
    hole = te.estimated_hole(FLAT)
    assert hole == 1.35
    assert hole >= te.lead_circumscribed_diameter(FLAT) + te.LEAD_TO_HOLE_CLEARANCE_MM


@pytest.mark.parametrize("diameter", [0.3, 0.45, 0.51, 0.64, 0.8, 1.0, 1.27, 2.0, 3.3])
def test_every_estimated_hole_lets_its_lead_pass_with_the_clearance(diameter):
    # The rule holds for any lead, not for the sizes of one part.
    for lead in ({"shape": "round", "diameter_mm": diameter},
                 {"shape": "flat", "width_mm": diameter, "thickness_mm": diameter / 3}):
        estimate = te.estimated_round_pad(lead)
        play = estimate.drill_mm - te.lead_circumscribed_diameter(lead)
        # 1 nm: the resolution at which a hole already on the step is kept.
        assert te.LEAD_TO_HOLE_CLEARANCE_MM - 1e-6 <= play < (
            te.LEAD_TO_HOLE_CLEARANCE_MM + te.ESTIMATED_DRILL_STEP_MM)
        assert te.lead_passes(lead, estimate.drill_mm)
        ring = (estimate.pad_diameter_mm - estimate.drill_mm) / 2
        assert ring == pytest.approx(te.HOLE_TO_PAD_ANNULUS_MM / 2) and ring > te.MIN_ANNULAR_RING_MM


def test_the_estimated_pad_is_the_hole_plus_the_annulus_and_the_pair_is_one_rule():
    assert te.estimated_pad_diameter(0.85) == 1.65
    assert te.estimated_round_pad(ROUND) == (0.85, 1.65)
    assert te.estimated_round_pad(FLAT) == (1.35, 2.15)
    with pytest.raises(ValueError):
        te.estimated_pad_diameter(0.0)


def test_two_different_terminals_of_one_part_get_two_different_estimates():
    # A relay-like mix: the round pin and the flat blade never share one hole size.
    assert te.estimated_round_pad(ROUND) != te.estimated_round_pad(FLAT)


# ── lead_passes ────────────────────────────────────────────────────────────

def test_a_round_hole_must_exceed_the_lead_equality_does_not_pass():
    assert te.lead_passes(ROUND, 0.61)
    assert not te.lead_passes(ROUND, 0.6)            # a hole of the lead's own diameter
    assert not te.lead_passes(ROUND, 0.5)


def test_a_flat_blade_in_a_round_hole_is_judged_by_its_diagonal_not_its_width():
    # 1.05 mm clears the 1.0 mm width but not the 1.077 mm diagonal: same hole,
    # a round Ø1.0 lead passes and the 1 x 0.4 blade does not.
    assert te.lead_passes({"shape": "round", "diameter_mm": 1.0}, 1.05)
    assert not te.lead_passes(FLAT, 1.05)
    assert te.lead_passes(FLAT, 1.08)


def test_a_slot_is_judged_on_each_of_its_own_axes_whichever_pad_axis_it_lies_on():
    # drill_mm runs along the pad LENGTH, drill_length_mm along the pad WIDTH:
    # the blade fits a 0.6 x 1.5 slot and the same slot turned, 1.5 x 0.6.
    assert te.lead_passes(FLAT, 0.6, 1.5)
    assert te.lead_passes(FLAT, 1.5, 0.6)
    # Long enough but too narrow for the 0.4 mm thickness.
    assert not te.lead_passes(FLAT, 0.4, 1.5)
    # Wide enough but shorter than the 1.0 mm width.
    assert not te.lead_passes(FLAT, 0.6, 0.9)


def test_a_slot_has_round_ends_a_rectangle_reaching_into_them_does_not_fit():
    # 1.05 x 0.6 exceeds the blade on both axes, yet its ends are half circles
    # of radius 0.3: the blade's corners (0.5, 0.2) lie outside them. A slot
    # with the same width and longer straight sides takes it.
    assert not te.lead_passes(FLAT, 1.05, 0.6)
    assert te.lead_passes(FLAT, 1.3, 0.6)


def test_a_round_lead_in_a_slot_is_limited_by_the_shorter_side():
    assert te.lead_passes(ROUND, 0.7, 2.0)
    assert not te.lead_passes(ROUND, 0.6, 2.0)
    assert not te.lead_passes(ROUND, 2.0, 0.5)


def test_equal_slot_sides_are_a_round_hole():
    assert te.lead_passes(FLAT, 1.1, 1.1) == te.lead_passes(FLAT, 1.1) is True
    assert te.lead_passes(FLAT, 1.05, 1.05) == te.lead_passes(FLAT, 1.05) is False


def test_lead_passes_refuses_values_out_of_contract():
    with pytest.raises(ValueError):
        te.lead_passes(None, 1.0)
    with pytest.raises(ValueError):
        te.lead_passes(ROUND, 0.0)
    with pytest.raises(ValueError):
        te.lead_passes(ROUND, 1.0, float("inf"))


def test_the_ring_constant_is_the_one_the_checks_use():
    from kicadgen.services import footprint_checks as fc
    assert fc.ANNULAR_RING_MIN_MM is te.MIN_ANNULAR_RING_MM
    assert fc.check_annular_ring.__defaults__ == (te.MIN_ANNULAR_RING_MM,)
