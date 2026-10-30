"""@brief Full holes declared on an edge the photo shows cut (user, 2026-10-30).

The model answered «through_hole» for both sides of the RS485 sample while
describing, in its own note, a half-moon open on the edge; the STEP model
then had full rings set back from a straight edge. Full holes never touch
the outline, so the photo can contradict that style. Rules:

* notches counted = declared contacts AND on the declared centres -> the
  side becomes half_hole, with a note (corrected before the other checks);
* notches of another number -> a warning only, nothing changed;
* a straight outline, or full holes whose pads reach the edge -> nothing.

Each case is told apart by the state of the outline (cut or not, where the
contacts are), never by a number tuned on the sample.
"""
from pathlib import Path

import cv2
import pytest

from edge_notch_photos import calibrated_profiles, row, tilted_board
from kicadgen.services.edge_notches import check_declared_contacts, notched_full_hole_sides
from test_edge_notches import LENGTH, ROW_4, WIDTH, board_photo

ROOT = Path(__file__).resolve().parents[1]


def _through(edge, count, pitch=2.54, **extra):
    return dict(edge=edge, count=count, pitch_mm=pitch, contact_style="through_hole", **extra)


def _warnings(lines):
    return [message for kind, message in lines if kind == "warning"]


def _profiles(image, tmp_path, length=40, width=16):
    return calibrated_profiles(image, length, width, tmp_path)


# ── Pure rule ────────────────────────────────────────────────────────────

def test_notches_on_the_declared_full_holes_are_reported_as_half_holes(tmp_path):
    """@brief Positive: four notches where four full holes are declared."""
    profiles, shape = _profiles(
        tilted_board(40, 16, 20, notches={"right": row(4, 2.54, 16)}, radius_mm=0.5), tmp_path)
    [item] = notched_full_hole_sides(profiles, shape, 40, 16, [_through("right", 4)])
    assert (item.edge, item.found, item.declared) == ("right", 4, 4)
    assert len(item.centres_mm) == 4
    # The same side declared as half-holes is not this rule's business.
    half = [dict(_through("right", 4), contact_style="half_hole")]
    assert notched_full_hole_sides(profiles, shape, 40, 16, half) == []


def test_another_number_of_notches_only_warns(tmp_path):
    """@brief Negative for the correction, positive for the warning: 4 notches, 7 declared."""
    profiles, shape = _profiles(
        tilted_board(40, 16, 20, notches={"right": row(4, 2.54, 16)}, radius_mm=0.5), tmp_path)
    sides = [_through("right", 7)]
    assert notched_full_hole_sides(profiles, shape, 40, 16, sides) == []
    [warning] = _warnings(check_declared_contacts(profiles, shape, 40, 16, sides))
    assert "4" in warning


def test_notches_away_from_the_declared_contacts_are_not_corrected(tmp_path):
    """@brief Negative: the right count, but the row sits a whole pitch off the declared one."""
    profiles, shape = _profiles(
        tilted_board(40, 16, 20, notches={"right": row(4, 2.54, 16, offset_mm=2.54)},
                     radius_mm=0.5), tmp_path)
    assert notched_full_hole_sides(profiles, shape, 40, 16, [_through("right", 4)]) == []


def test_full_holes_with_pads_to_the_edge_stay_full_holes(tmp_path):
    """@brief Negative: real full holes, straight outline -> no correction, no warning."""
    profiles, shape = _profiles(
        tilted_board(40, 16, 20, full_holes={"right": row(4, 2.54, 16)}), tmp_path)
    sides = [_through("right", 4)]
    assert notched_full_hole_sides(profiles, shape, 40, 16, sides) == []
    assert _warnings(check_declared_contacts(profiles, shape, 40, 16, sides)) == []


@pytest.mark.parametrize("depth", [0.10, 0.13, 0.16])
def test_pad_dents_on_the_declared_contacts_only_warn(tmp_path, depth):
    """@brief Negative: dents of a tenth of a mm, one per declared contact, at the pitch.

    They model the pads of real full holes reaching the edge (0.11-0.16 mm on
    the ESP32 sample): same number and same positions as the declared row,
    so only their depth -- far less than any half-hole's radius -- keeps
    them from being corrected. The contradiction is still reported.
    """
    radius = 0.35
    image = tilted_board(63.3, 25.4, 41, notches={"top": row(19, 2.54, 63.3)},
                         radius_mm=radius, inset_mm=depth - radius, ring_mm=0.0)
    profiles, shape = calibrated_profiles(image, 63.3, 25.4, tmp_path)
    sides = [_through("top", 19)]
    assert notched_full_hole_sides(profiles, shape, 63.3, 25.4, sides) == []
    assert _warnings(check_declared_contacts(profiles, shape, 63.3, 25.4, sides))


def test_the_same_row_as_deep_as_half_holes_is_corrected(tmp_path):
    """@brief Positive twin of the dents: the same 19 contacts, cut at their centre (depth = radius)."""
    image = tilted_board(63.3, 25.4, 41, notches={"top": row(19, 2.54, 63.3)},
                         radius_mm=0.5, ring_mm=0.0)
    profiles, shape = calibrated_profiles(image, 63.3, 25.4, tmp_path)
    [item] = notched_full_hole_sides(profiles, shape, 63.3, 25.4, [_through("top", 19)])
    assert (item.edge, item.found) == ("top", 19)


# ── The two sample boards tracked in the repository ──────────────────────

def _real(name, length, width, tmp_path):
    from kicadgen.services.photo_inventory import prepare_photo
    path = ROOT / "docs" / name
    if not path.exists():
        pytest.skip(f"{name} non presente")
    meta = {}
    crop = prepare_photo(path, length, width, meta)
    assert crop is not None
    return meta["_edge_depth_px"], crop.shape


def test_rs485_sample_declared_with_full_holes_is_corrected(tmp_path):
    """@brief The reported case: 3 at 5.08 mm left, 4 at 2.54 mm right, declared through_hole."""
    profiles, shape = _real("RS485/AltaRisoluzione.jpg", 42.8, 15.2, tmp_path)
    sides = [_through("left", 3, 5.08), _through("right", 4, 2.54)]
    found = notched_full_hole_sides(profiles, shape, 42.8, 15.2, sides)
    assert sorted((item.edge, item.found) for item in found) == [("left", 3), ("right", 4)]


def test_esp32_sample_full_holes_are_not_corrected(tmp_path):
    """@brief Negative on a real board: full holes, pads to the edge, straight outline."""
    profiles, shape = _real("ESP32/AltaRisoluzione.jpg", 63.3, 25.4, tmp_path)
    sides = [_through("top", 19), _through("bottom", 19)]
    assert notched_full_hole_sides(profiles, shape, 63.3, 25.4, sides) == []
    assert _warnings(check_declared_contacts(profiles, shape, 63.3, 25.4, sides)) == []


# ── The inventory applies it before the half-hole checks ─────────────────

def test_the_inventory_corrects_the_style_before_the_other_checks(tmp_path):
    """@brief The declared side changes to half_hole, with a note; a straight side stays as declared."""
    from kicadgen.services.photo_inventory import extract_inventory
    path = tmp_path / "top.png"
    cv2.imwrite(str(path), board_photo(right=ROW_4))
    declared = dict(layout="castellated", notes="", castellated_sides=[
        dict(edge="right", count=4, pitch_mm=2.54, contact_style="through_hole"),
        dict(edge="left", count=4, pitch_mm=2.54, contact_style="through_hole")])
    seen = []
    # The empty fake answer of the paid inventory fails AFTER the photo checks.
    with pytest.raises(ValueError):
        extract_inventory([path], "m", LENGTH, WIDTH, ["generic"], lambda **_: {},
                          top_photo_index=0, detector="vlm", declared=declared,
                          recount_contacts=seen.extend)
    right, left = declared["castellated_sides"]
    assert right["contact_style"] == "half_hole"
    assert right["_contact_style_evidence"]
    assert left["contact_style"] == "through_hole"
    assert "_contact_style_evidence" not in left
    assert "destro" in declared["notes"]
    # Corrected first: the right side is then compared as half-holes, and agrees.
    assert [item.edge for item in seen] == []
