"""@brief Half-hole count on imperfect photos: tilt, dense rows, anisotropy, hidden notches.

HANDOFF point 139, block G1. The audit (point 138) showed a count taken for
valid while wrong: the reference of the outline was the median of the whole
profile, so a residual tilt made shallow notches vanish and a row covering
more than half of the edge read as «no notch»; the depth threshold was a
constant checked on two boards; a gap in the row passed as regular; two
mounting holes made a contact row. Every rule below has a positive and a
negative case that differ by structure, and the threshold tests sit close
enough to the derived bounds that halving or doubling a factor breaks one.

Photos are synthetic (tests/edge_notch_photos.py) except the two sample
boards tracked in docs/.
"""
from pathlib import Path

import numpy as np
import pytest

from edge_notch_photos import calibrated_profiles, row, tilted_board
from kicadgen.services import edge_notches
from kicadgen.services.edge_notches import (
    _scales, check_declared_contacts, count_disagreements, count_notches)

ROOT = Path(__file__).resolve().parents[1]


def _count(profiles, shape, length, width, edge, pitch, **extra):
    along, across = _scales(shape, length, width)[edge]
    return count_notches(profiles[edge], along, pitch, across_px_per_mm=across, **extra)


# ── Tilted and dense rows on calibrated photos ──────────────────────────


@pytest.mark.parametrize("angle", [0.2, 0.43, -0.5])
def test_shallow_half_holes_on_a_tilted_long_edge_are_all_counted(tmp_path, angle):
    """The audit case: 14 half-holes 0.35 mm deep on 40 mm; the median baseline gave 11 or 7."""
    image = tilted_board(40, 16, 20, angle_deg=angle, notches={"top": row(14, 2.54, 40)},
                         radius_mm=0.35)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    assert _count(profiles, shape, 40, 16, "top", 2.54).found == 14


def test_the_straight_opposite_edge_of_a_tilted_board_has_no_notch(tmp_path):
    """Negative of the previous test on the same photo: the tilt alone is not a notch."""
    image = tilted_board(40, 16, 20, angle_deg=0.43, notches={"top": row(14, 2.54, 40)},
                         radius_mm=0.35)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    result = _count(profiles, shape, 40, 16, "bottom", 2.54)
    assert result.found is None and result.no_notch


@pytest.mark.parametrize("length, count, radius, angle", [(60, 40, 0.45, 0.3),
                                                           (50, 30, 0.4, 0.0)])
def test_a_dense_row_covering_most_of_the_edge_is_counted(tmp_path, length, count, radius,
                                                          angle):
    """1.27 mm castellations with holes of 0.8-0.9 mm cover 63-71% of the outline.

    The median of such a profile IS the notch depth: the audit got «no notch».
    """
    image = tilted_board(length, 20, 20, angle_deg=angle,
                         notches={"top": row(count, 1.27, length)}, radius_mm=radius)
    profiles, shape = calibrated_profiles(image, length, 20, tmp_path)
    result = _count(profiles, shape, length, 20, "top", 1.27)
    assert result.found == count and not result.no_notch


def test_rounded_corners_of_a_tilted_board_are_not_contacts(tmp_path):
    image = tilted_board(40, 16, 20, angle_deg=0.3, corner_mm=1.0,
                         notches={"right": row(4, 2.54, 16)}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    assert _count(profiles, shape, 40, 16, "right", 2.54).found == 4


def test_positions_use_the_scale_along_the_edge_on_an_anisotropic_photo(tmp_path):
    """X squeezed by 18%: a left edge runs along Y, so its positions use the Y scale.

    With the X scale the centres would shrink by 18% and miss the row.
    """
    positions = row(5, 2.54, 16)
    image = tilted_board(40, 16, 20, notches={"left": positions}, radius_mm=0.4,
                         x_squeeze=0.82, noise=3)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    assert shape[1] / 40 == pytest.approx(0.82 * shape[0] / 16, rel=0.03)
    result = _count(profiles, shape, 40, 16, "left", 2.54)
    assert result.found == 5
    assert result.centres_mm == pytest.approx(positions, abs=result.tolerance_mm)


# ── A hidden notch ───────────────────────────────────────────────────────


def test_a_hidden_notch_makes_the_count_a_lower_bound_not_a_disagreement(tmp_path):
    """A dimension arrow or dust over one half-hole: 13 seen of 14, never «13».

    The count is only a minimum, so it neither asks for a recount nor warns of
    another number (point 139, G1 d).
    """
    positions = row(14, 2.54, 40)
    image = tilted_board(40, 16, 20, angle_deg=0.3, notches={"top": positions},
                         radius_mm=0.35, hidden=[("top", positions[6])])
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    result = _count(profiles, shape, 40, 16, "top", 2.54)
    assert result.found is None and result.at_least == 13
    sides = [dict(edge="top", count=14, pitch_mm=2.54, contact_style="half_hole")]
    assert count_disagreements(profiles, shape, 40, 16, sides) == []
    lines = check_declared_contacts(profiles, shape, 40, 16, sides)
    assert [kind for kind, _ in lines] == ["log"]
    assert "almeno 13" in lines[0][1]


def test_the_same_row_without_the_hidden_notch_is_counted(tmp_path):
    """Positive twin of the previous test: nothing covered, 14 counted and confirmed."""
    image = tilted_board(40, 16, 20, angle_deg=0.3, notches={"top": row(14, 2.54, 40)},
                         radius_mm=0.35)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    sides = [dict(edge="top", count=14, pitch_mm=2.54, contact_style="half_hole")]
    lines = check_declared_contacts(profiles, shape, 40, 16, sides)
    assert lines == [("log", "Lato superiore: sulla foto si contano 14 semi-fori, come i "
                              "contatti dichiarati.")]


# ── Undeclared edges ─────────────────────────────────────────────────────


def test_two_mounting_notches_on_an_undeclared_edge_are_no_contact_row(tmp_path):
    image = tilted_board(40, 16, 20, notches={"top": [8.0, 32.0]}, radius_mm=1.0)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    lines = check_declared_contacts(profiles, shape, 40, 16,
                                    [dict(edge="left", count=1, pitch_mm=0)])
    assert not [message for _, message in lines if "superiore" in message]


def test_three_regular_notches_on_an_undeclared_edge_are_reported(tmp_path):
    image = tilted_board(40, 16, 20, notches={"top": row(3, 2.54, 40)}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    lines = check_declared_contacts(profiles, shape, 40, 16,
                                    [dict(edge="left", count=1, pitch_mm=0)])
    assert ("warning", "Lato superiore: sulla foto si contano 3 semi-fori, ma l'analisi non "
                       "dichiara contatti su questo lato: da verificare.") in lines


# ── Full holes on a tilted photo ─────────────────────────────────────────


def test_full_holes_with_pads_to_the_edge_never_give_a_number(tmp_path):
    image = tilted_board(40, 16, 20, angle_deg=0.3, full_holes={"top": row(14, 2.54, 40)},
                         radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    assert _count(profiles, shape, 40, 16, "top", 2.54).found is None


# ── Declarations as the model writes them ────────────────────────────────


def test_counts_and_pitches_written_as_strings_are_compared_as_numbers(tmp_path):
    """The audit: «14 contro 14» and a paid recount for a count written "14"."""
    image = tilted_board(40, 16, 20, notches={"right": row(4, 2.54, 16)}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    sides = [dict(edge=" Right ", count="4", pitch_mm="2.54", contact_style="half_hole")]
    assert count_disagreements(profiles, shape, 40, 16, sides) == []
    assert check_declared_contacts(profiles, shape, 40, 16, sides) == [
        ("log", "Lato destro: sulla foto si contano 4 semi-fori, come i contatti dichiarati.")]


def test_a_string_count_that_differs_is_still_a_disagreement(tmp_path):
    """Negative of the previous test: normalizing must not hide a real difference."""
    image = tilted_board(40, 16, 20, notches={"right": row(4, 2.54, 16)}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    sides = [dict(edge="RIGHT", count="5", pitch_mm="2.54", contact_style="Half_Hole")]
    [item] = count_disagreements(profiles, shape, 40, 16, sides)
    assert (item.edge, item.found, item.declared) == ("right", 4, 5)
    [(kind, _message)] = check_declared_contacts(profiles, shape, 40, 16, sides)
    assert kind == "warning"


# ── Thresholds derived from the data: boundary tests ─────────────────────
#
# Profiles in pixels, 800 positions at 20 px/mm, pitch 2.5 mm (50 px). The
# notches are flat and noise-free so that their depth is exact; the outline
# carries the noise that sets the band. With sigma = 1 px the band is 3 px
# (NOISE_BAND_SIGMAS) and a notch must reach 6 px (NOTCH_SEPARATION): 8 px
# is counted, 4.5 px is ambiguous. Halving or doubling either factor, or
# dropping the measured noise, flips one of the two.


def _noisy(depth, *, sigma=1.0, starts=(200, 250, 300, 350, 400), width=20, slope=0.0,
           length=800, seed=3):
    rng = np.random.default_rng(seed)
    values = rng.normal(0.0, sigma, length) + slope * np.arange(length)
    for start in starts:
        values[start:start + width] = depth + slope * np.arange(start, start + width)
    return values.tolist()


def test_a_notch_clearly_above_the_measured_noise_is_counted():
    assert count_notches(_noisy(8.0), 20.0, 2.5).found == 5


def test_a_notch_inside_twice_the_measured_noise_band_is_not_countable():
    result = count_notches(_noisy(4.5), 20.0, 2.5)
    assert result.found is None and not result.no_notch
    assert "rumore" in result.reason


def test_the_noise_band_follows_the_photo_not_a_constant():
    """The 4.5 px notch counted above as ambiguous is a notch on a clean outline."""
    assert count_notches(_noisy(4.5, sigma=0.0), 20.0, 2.5).found == 5


def test_a_declared_diameter_sets_a_structural_minimum_depth():
    """Diameter 2 mm at 20 px/mm: radius 20 px, a notch must reach 10 px."""
    deep = count_notches(_noisy(12.0, sigma=0.0), 20.0, 2.5, hole_diameter_mm=2.0)
    shallow = count_notches(_noisy(8.0, sigma=0.0), 20.0, 2.5, hole_diameter_mm=2.0)
    assert deep.found == 5
    assert shallow.found is None and not shallow.no_notch


def test_the_depth_bound_uses_the_scale_across_the_edge():
    """Across 10 px/mm: radius 10 px, bound 5 px; the along scale (20) would ask 10 px."""
    result = count_notches(_noisy(8.0, sigma=0.0), 20.0, 2.5, across_px_per_mm=10.0,
                           hole_diameter_mm=2.0)
    assert result.found == 5


def test_a_straight_edge_is_no_notch_only_when_the_photo_could_show_one():
    """Sigma 1 px: band 3, a notch must reach 6 px. The smallest half-hole assumed
    (MIN_HALF_HOLE_DIAMETER_MM) is 4 px deep at 20 px/mm and 8 px at 40 px/mm."""
    straight = _noisy(0.0, starts=())
    coarse = count_notches(straight, 20.0, 2.5, across_px_per_mm=20.0)
    fine = count_notches(straight, 20.0, 2.5, across_px_per_mm=40.0)
    assert coarse.found is None and not coarse.no_notch
    assert fine.no_notch
    # A declared diameter replaces the assumed one: 1 mm is 10 px deep at 20 px/mm.
    assert count_notches(straight, 20.0, 2.5, across_px_per_mm=20.0,
                         hole_diameter_mm=1.0).no_notch


def test_a_tilted_dense_profile_is_measured_from_its_lower_envelope():
    """A 16 px ramp and notches over 70% of the edge: the median would be the notch."""
    starts = tuple(range(20, 760, 50))
    values = _noisy(7.0, sigma=0.0, starts=starts, width=35, slope=0.02)
    result = count_notches(values, 20.0, 2.5)
    assert result.found == len(starts)


def _flat(runs, length=800):
    values = [0.0] * length
    for start, stop in runs:
        values[start:stop] = [8.0] * (stop - start)
    return values


def test_three_notches_of_similar_width_are_an_undeclared_row():
    """Widths 20, 20, 25 px: within WIDTH_TOLERANCE of the median."""
    assert count_notches(_flat([(40, 60), (90, 110), (138, 163)]), 20.0).found == 3


def test_three_notches_of_different_widths_are_not_an_undeclared_row():
    """Widths 20, 20, 27 px: one of them is another kind of cut."""
    result = count_notches(_flat([(40, 60), (90, 110), (137, 164)]), 20.0)
    assert result.found is None and "larghezze" in result.reason


def test_two_notches_are_not_an_undeclared_row():
    result = count_notches(_flat([(40, 60), (90, 110)]), 20.0)
    assert result.found is None and not result.no_notch


def test_a_row_with_one_pitch_between_neighbours_has_no_missing_notch():
    """Positive twin of the missing-notch rule: neighbours one pitch apart are complete."""
    result = count_notches(_flat([(40, 60), (90, 110), (140, 160)]), 20.0, 2.5)
    assert result.found == 3 and result.at_least is None


def test_a_spacing_a_fifth_off_the_pitch_is_still_regular():
    """PITCH_TOLERANCE: measured spacings 50 and 60 px on a 50 px pitch are one pitch each."""
    assert count_notches(_flat([(40, 60), (90, 110), (150, 170)]), 20.0, 2.5).found == 3


def test_a_spacing_more_than_a_quarter_off_the_pitch_is_not():
    """Negative: 50 and 65 px on a 50 px pitch is neither one pitch nor a missing notch."""
    result = count_notches(_flat([(40, 60), (90, 110), (155, 175)]), 20.0, 2.5)
    assert result.found is None and "passo" in result.reason


def test_narrow_notches_of_a_quarter_millimetre_are_counted():
    """MIN_NOTCH_WIDTH_MM: 5 px at 20 px/mm is 0.25 mm, above the 0.2 mm floor."""
    assert count_notches(_flat([(40, 45), (90, 95), (140, 145)]), 20.0, 2.5).found == 3


def test_specks_narrower_than_the_smallest_half_hole_are_not_notches():
    """Negative: 3 px (0.15 mm) runs are specks, not half-holes."""
    result = count_notches(_flat([(40, 43), (90, 93), (140, 143)]), 20.0, 2.5)
    assert result.found is None


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


def test_rs485_sample_half_holes_are_counted(tmp_path):
    """42.8 x 15.2 mm, calibrated axis by axis (-17.7%): left 3, right 4."""
    profiles, shape = _real("RS485/AltaRisoluzione.jpg", 42.8, 15.2, tmp_path)
    assert _count(profiles, shape, 42.8, 15.2, "left", 5.08).found == 3
    right = _count(profiles, shape, 42.8, 15.2, "right", 2.54)
    assert right.found == 4
    # Structure, not the positions read on this photo: one row at the pitch.
    assert np.diff(right.centres_mm) == pytest.approx([2.54] * 3, abs=right.tolerance_mm)


def test_rs485_sample_row_is_recounted_within_the_calibration_bound(tmp_path):
    """The review case R-G2: an answer declaring 5 centred contacts on the right.

    The photo's four notches sit where a CENTRED row of four would, within
    the shift the calibration allows (no offset declared anywhere), and a
    recount to four is adopted; four of the declared five would be a notch
    off, so the photo does not read «one hidden».
    """
    import copy
    from kicadgen.services import datasheet_extractor as extractor
    profiles, shape = _real("RS485/AltaRisoluzione.jpg", 42.8, 15.2, tmp_path)

    def answer(count):
        return dict(layout="castellated", body_length_mm=42.8, body_width_mm=15.2,
                    drawing_unit="mm", contacts_view="top", notes="", castellated_sides=[
                        dict(edge="left", count=3, pitch_mm=5.08, first_pin=1,
                             contact_style="half_hole", pin_names=["a", "b", "c"]),
                        dict(edge="right", count=count, pitch_mm=2.54, first_pin=4,
                             contact_style="half_hole", pin_names=list("defgh")[:count])])

    data = answer(5)
    [item] = count_disagreements(profiles, shape, 42.8, 15.2, data["castellated_sides"])
    assert (item.edge, item.found, item.declared) == ("right", 4, 5)
    logs = []
    assert extractor._recount_sides_from_photo(
        data, [item], [], lambda **_: copy.deepcopy(answer(4)), "m", "s", False, logs.append)
    assert [side["count"] for side in data["castellated_sides"]] == [3, 4]


def test_esp32_sample_full_holes_give_no_number(tmp_path):
    """63.3 x 25.4 mm dev kit: full holes, pads to the edge, straight outline."""
    profiles, shape = _real("ESP32/AltaRisoluzione2.jpg", 63.3, 25.4, tmp_path)
    for edge in edge_notches.EDGES:
        for pitch in (2.54, None):
            assert _count(profiles, shape, 63.3, 25.4, edge, pitch).found is None, (edge, pitch)
    # Two rows of 19 on the long edges, full holes or no style declared: no
    # number, no warning, no recount.
    for style in ("through_hole", None):
        sides = [dict(edge=edge, count=19, pitch_mm=2.54, contact_style=style)
                 for edge in ("top", "bottom")]
        assert count_disagreements(profiles, shape, 63.3, 25.4, sides) == []
        assert not [line for line in check_declared_contacts(profiles, shape, 63.3, 25.4, sides)
                    if line[0] == "warning"]


def test_three_irregularly_spaced_notches_are_not_an_undeclared_row():
    """Negative: without a pitch the notches must be evenly spaced among themselves."""
    result = count_notches(_flat([(40, 60), (90, 110), (165, 185)]), 20.0)
    assert result.found is None and "passo" in result.reason


def test_a_side_declared_with_full_holes_never_asks_for_a_recount(tmp_path):
    """Full holes do not cut the outline: the notches on that edge are something else."""
    image = tilted_board(40, 16, 20, notches={"right": row(4, 2.54, 16)}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    through = [dict(edge="right", count=7, pitch_mm=2.54, contact_style="through_hole")]
    half = [dict(edge="right", count=7, pitch_mm=2.54, contact_style="half_hole")]
    assert count_disagreements(profiles, shape, 40, 16, through) == []
    assert [item.found for item in count_disagreements(profiles, shape, 40, 16, half)] == [4]
