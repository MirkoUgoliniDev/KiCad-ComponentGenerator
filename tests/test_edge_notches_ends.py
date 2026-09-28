"""@brief The ends of a half-hole row: a count is valid only if the photo proves where the row ends.

HANDOFF point 139, final review (R-G1, R-M3, R-M2, R-G2 bound). Before the fix
a run touching an end of the edge (a corner arc, one or two background columns
of a tilted crop) was joined to the first notch BEFORE being set aside, so the
notch vanished with it and N-1 was reported as a valid count; a notch hidden
at an end of the row gave N-1 as well; a single bump on a declared side was a
row; a side without a declared contact style was sent to a paid recount.

Every case has a twin that differs by structure (how far the corner feature
is from the first notch, whether the notches seen sit on the declared row,
whether the side declares one contact or a style), never by a number read on
a sample board.
"""
import pytest

from edge_notch_photos import calibrated_profiles, row, tilted_board
from kicadgen.services.edge_notches import (
    _scales, check_declared_contacts, count_disagreements, count_notches, row_shift_bound_mm)


def _count(profiles, shape, length, width, edge, pitch, **extra):
    along, across = _scales(shape, length, width)[edge]
    return count_notches(profiles[edge], along, pitch, across_px_per_mm=across, **extra)


def _flat(runs, length=400, depth=8.0, unknown=()):
    """Profile at 20 px/mm: plain outline, `runs` of background `depth` px deep."""
    values = [0.0] * length
    for start, stop in runs:
        values[start:stop] = [depth] * (stop - start)
    for start, stop in unknown:
        values[start:stop] = [None] * (stop - start)
    return values


# ── Profiles: what touches an end is set aside before joining ────────────
#
# 20 px/mm, pitch 2.5 mm (50 px): the narrowest notch counted is 4 px
# (MIN_NOTCH_WIDTH_MM), the gap closed inside a notch 12.5 px.


def test_a_corner_artefact_does_not_swallow_the_first_notch():
    """Two background columns at the corner, 6 px of outline, then the first notch.

    Joined first (gap below 12.5 px) and discarded with the corner, the
    notch was lost: 2 reported as valid instead of 3.
    """
    result = count_notches(_flat([(0, 2), (8, 28), (58, 78), (108, 128)]), 20.0, 2.5)
    assert result.found == 3 and result.at_least is None


def test_a_notch_not_resolved_from_the_corner_leaves_only_a_lower_bound():
    """Negative twin: 2 px of outline (less than the narrowest notch) between the
    corner and the notch: they are one corner feature, and a notch may hide in it."""
    result = count_notches(_flat([(0, 6), (8, 28), (58, 78), (108, 128)]), 20.0, 2.5)
    assert result.found is None and result.at_least == 2
    assert "almeno 2" in result.reason


def test_an_outline_not_seen_next_to_the_row_leaves_only_a_lower_bound():
    """The next notch of the row would fall where the outline was not seen."""
    result = count_notches(_flat([(40, 60), (90, 110), (140, 160)], unknown=[(0, 30)]),
                           20.0, 2.5)
    assert result.found is None and result.at_least == 3


def test_an_outline_not_seen_away_from_the_row_does_not_matter():
    """Positive twin: the slot of the next notch lies on plain outline, seen."""
    result = count_notches(_flat([(70, 90), (120, 140), (170, 190)], unknown=[(0, 30)]),
                           20.0, 2.5)
    assert result.found == 3


def test_a_row_ending_near_the_end_of_the_edge_is_complete():
    """The next notch would be centred past the end of the edge: nothing can be there."""
    result = count_notches(_flat([(2, 12), (52, 62), (102, 112)], length=400), 20.0, 2.5)
    assert result.found == 3


# ── Calibrated photos: corners and tilt ──────────────────────────────────


def _right_row(first, count=6, edge=16.0):
    return [round(first + i * 2.54, 3) for i in range(count) if first + i * 2.54 < edge - 0.5]


@pytest.mark.parametrize("angle", [0.2, 0.4])
def test_a_first_notch_near_a_sharp_corner_of_a_tilted_board_is_counted(tmp_path, angle):
    """The review case: 15 half-holes, the first 0.8 mm from the corner; 14 before the fix."""
    positions = [0.8 + i * 2.54 for i in range(15)]
    image = tilted_board(40, 16, 20, angle_deg=angle, notches={"top": positions},
                         radius_mm=0.4)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    sides = [dict(edge="top", count=15, pitch_mm=2.54, offset_mm=0.8 + 7 * 2.54 - 20,
                  contact_style="half_hole")]
    assert _count(profiles, shape, 40, 16, "top", 2.54).found == 15
    assert count_disagreements(profiles, shape, 40, 16, sides) == []
    assert check_declared_contacts(profiles, shape, 40, 16, sides) == [
        ("log", "Lato superiore: sulla foto si contano 15 semi-fori, come i contatti "
                "dichiarati.")]


def test_a_notch_inside_the_arc_of_a_rounded_corner_is_never_a_smaller_count(tmp_path):
    """Corner radius 1 mm, first half-hole (r 0.45 mm) at 0.8 mm: it merges with the arc.

    The photo cannot count it, so the row is «at least 5», not «5»: no
    disagreement, no recount, only a log line.
    """
    positions = _right_row(0.8)
    image = tilted_board(40, 16, 20, angle_deg=0.3, corner_mm=1.0,
                         notches={"right": positions}, radius_mm=0.45)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    result = _count(profiles, shape, 40, 16, "right", 2.54)
    assert result.found is None and result.at_least == len(positions) - 1
    sides = [dict(edge="right", count=len(positions), pitch_mm=2.54, contact_style="half_hole")]
    assert count_disagreements(profiles, shape, 40, 16, sides) == []
    assert [kind for kind, _ in check_declared_contacts(profiles, shape, 40, 16, sides)] == ["log"]


def test_a_first_notch_clear_of_the_rounded_corner_is_counted(tmp_path):
    """Positive twin: the same corner, the first half-hole at 1.6 mm, off the arc."""
    positions = _right_row(1.6)
    image = tilted_board(40, 16, 20, angle_deg=0.3, corner_mm=1.0,
                         notches={"right": positions}, radius_mm=0.45)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    assert _count(profiles, shape, 40, 16, "right", 2.54).found == len(positions)


# ── A notch hidden at an end of the row (R-M3) ───────────────────────────


@pytest.mark.parametrize("which", [0, -1])
def test_a_hidden_end_notch_of_the_declared_row_is_a_lower_bound(tmp_path, which):
    """Dust over the first (last) of 15 declared half-holes: the outline is plain there.

    The 14 notches seen sit on 14 consecutive declared contacts: the photo
    cannot tell «14» from «15, one hidden». Before the fix: 14 against 15 and
    a paid recount.
    """
    positions = row(15, 2.54, 40)
    image = tilted_board(40, 16, 20, angle_deg=0.3, notches={"top": positions},
                         radius_mm=0.35, hidden=[("top", positions[which])])
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    sides = [dict(edge="top", count=15, pitch_mm=2.54, contact_style="half_hole")]
    assert count_disagreements(profiles, shape, 40, 16, sides) == []
    [(kind, message)] = check_declared_contacts(profiles, shape, 40, 16, sides)
    assert kind == "log" and "almeno 14" in message


def test_a_shorter_centred_row_is_a_real_disagreement(tmp_path):
    """Negative twin: 14 half-holes CENTRED on the edge, 15 declared (a label counted
    as a contact). They sit half a pitch off any 14 of the declared 15."""
    image = tilted_board(40, 16, 20, angle_deg=0.3, notches={"top": row(14, 2.54, 40)},
                         radius_mm=0.35)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    sides = [dict(edge="top", count=15, pitch_mm=2.54, contact_style="half_hole")]
    [item] = count_disagreements(profiles, shape, 40, 16, sides)
    assert (item.found, item.declared) == (14, 15)
    [(kind, _message)] = check_declared_contacts(profiles, shape, 40, 16, sides)
    assert kind == "warning"


# ── One notch on a declared side; sides without a style (R-M2) ───────────


def test_one_notch_is_not_a_row_on_a_side_declaring_several_contacts():
    result = count_notches(_flat([(190, 210)]), 20.0, 2.5, declared_count=5)
    assert result.found is None and not result.no_notch and result.at_least is None


def test_one_notch_is_the_row_of_a_side_declaring_one_contact():
    """Positive twin: the same profile, the side declares exactly one contact."""
    assert count_notches(_flat([(190, 210)]), 20.0, 2.5, declared_count=1).found == 1


def test_one_bump_never_asks_for_a_recount_of_a_long_side(tmp_path):
    """The review case: one notch on an edge declaring 19 contacts gave «1 contro 19»."""
    image = tilted_board(40, 16, 20, notches={"top": [20.0]}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    sides = [dict(edge="top", count=19, pitch_mm=2.54, contact_style="half_hole")]
    assert count_disagreements(profiles, shape, 40, 16, sides) == []
    assert [kind for kind, _ in check_declared_contacts(profiles, shape, 40, 16, sides)] == ["log"]


def test_a_side_without_a_declared_style_is_reported_but_never_recounted(tmp_path):
    """Full holes whose pads bump the outline look like notches: without a declared
    half-hole style the photo may warn, but never pays for a recount."""
    image = tilted_board(40, 16, 20, notches={"right": row(4, 2.54, 16)}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    no_style = [dict(edge="right", count=5, pitch_mm=2.54)]
    assert count_disagreements(profiles, shape, 40, 16, no_style) == []
    assert [kind for kind, _ in check_declared_contacts(profiles, shape, 40, 16, no_style)] \
        == ["warning"]
    half = [dict(edge="right", count=5, pitch_mm=2.54, contact_style="half_hole")]
    assert [(i.found, i.declared) for i in count_disagreements(profiles, shape, 40, 16, half)] \
        == [(4, 5)]


# ── Undeclared edges keep their warning as a lower bound ─────────────────


def test_an_undeclared_row_that_may_go_on_is_reported_as_at_least():
    profiles = {"left": _flat([(40, 60), (90, 110), (140, 160)], length=320,
                              unknown=[(0, 30)]),
                "right": [0.0] * 320, "top": [0.0] * 800, "bottom": [0.0] * 800}
    lines = check_declared_contacts(profiles, (320, 800), 40, 16,
                                    [dict(edge="right", count=1, pitch_mm=2.54)])
    assert ("warning", "Lato sinistro: sulla foto si contano almeno 3 semi-fori, ma l'analisi "
                       "non dichiara contatti su questo lato: da verificare.") in lines


# ── The calibration bound of a row shift (R-G2) ──────────────────────────


def test_the_shift_bound_follows_the_calibration_contract():
    """Pixel floor on a small crop, a share of the long side on a large one, in mm along."""
    from kicadgen.services.photo_refine import (MAX_BOARD_EDGE_DRIFT_PX,
                                                MAX_BOARD_EDGE_DRIFT_RATIO)
    from kicadgen.cad3d.measurement_uncertainty import EDGE_UNCERTAINTY_PX
    small = row_shift_bound_mm((100, 300), 10.0)
    large = row_shift_bound_mm((1000, 3000), 40.0)
    assert small == pytest.approx((MAX_BOARD_EDGE_DRIFT_PX + EDGE_UNCERTAINTY_PX) / 10.0,
                                  abs=1e-3)
    assert large == pytest.approx((MAX_BOARD_EDGE_DRIFT_RATIO * 3000 + EDGE_UNCERTAINTY_PX)
                                  / 40.0, abs=1e-3)
    assert row_shift_bound_mm((100, 300), 0) is None


def test_a_disagreement_carries_the_shift_bound_of_its_edge(tmp_path):
    image = tilted_board(40, 16, 20, notches={"right": row(4, 2.54, 16)}, radius_mm=0.5)
    profiles, shape = calibrated_profiles(image, 40, 16, tmp_path)
    [item] = count_disagreements(profiles, shape, 40, 16, [
        dict(edge="right", count=3, pitch_mm=2.54, contact_style="half_hole")])
    along = _scales(shape, 40, 16)["right"][0]
    assert item.shift_bound_mm == row_shift_bound_mm(shape, along)
    assert 0 < item.shift_bound_mm < 2.54 / 2

