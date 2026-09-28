"""@brief Half-hole notches counted on the calibrated top photo (HANDOFF point 133, B).

Synthetic photos: a dark board on a light background, with the contacts drawn
as the two structures that must be told apart --
- a half-hole: a circle cut by the edge, so the background shows through the
  outline (a notch);
- a full hole whose pad reaches the edge: the outline stays straight.
Positive and negative cases differ by that structure, not by a number tuned on
a sample board.
"""
import cv2
import numpy as np
import pytest

from kicadgen.services import edge_notches
from kicadgen.services.edge_notches import (
    check_declared_contacts, count_notches, depth_profiles)
from kicadgen.services.photo_inventory import prepare_photo

SCALE = 20.0          # px/mm of the synthetic photo
LENGTH, WIDTH = 40.0, 16.0
MARGIN = 60           # px of background around the board
BACKGROUND = (235, 235, 235)
BOARD = (30, 60, 20)
GOLD = (60, 170, 210)


def _px(mm):
    return int(round(mm * SCALE))


def board_photo(left=(), right=(), top=(), style="half", radius_mm=0.8, corner_mm=0.0):
    """A photo with contacts at the given positions (mm along each edge)."""
    h, w = _px(WIDTH) + 2 * MARGIN, _px(LENGTH) + 2 * MARGIN
    image = np.full((h, w, 3), BACKGROUND, np.uint8)
    x0, y0, x1, y1 = MARGIN, MARGIN, MARGIN + _px(LENGTH), MARGIN + _px(WIDTH)
    cv2.rectangle(image, (x0, y0), (x1 - 1, y1 - 1), BOARD, -1)
    if corner_mm:
        r = _px(corner_mm)
        for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1 - 1, y0, -1, 1), (x0, y1 - 1, 1, -1),
                               (x1 - 1, y1 - 1, -1, -1)):
            cv2.rectangle(image, (min(cx, cx + sx * r), min(cy, cy + sy * r)),
                          (max(cx, cx + sx * r), max(cy, cy + sy * r)), BACKGROUND, -1)
            cv2.circle(image, (cx + sx * r, cy + sy * r), r, BOARD, -1)
    rad = _px(radius_mm)
    centres = ([(x0, y0 + _px(p)) for p in left] + [(x1 - 1, y0 + _px(p)) for p in right]
               + [(x0 + _px(p), y0) for p in top])
    for cx, cy in centres:
        if style == "half":
            # Plated ring, then the hole cut open by the edge.
            cv2.circle(image, (cx, cy), rad + _px(0.3), GOLD, -1)
            cv2.circle(image, (cx, cy), rad, BACKGROUND, -1)
            cv2.rectangle(image, (0, 0), (w - 1, y0 - 1), BACKGROUND, -1)
            cv2.rectangle(image, (0, y1), (w - 1, h - 1), BACKGROUND, -1)
            cv2.rectangle(image, (0, 0), (x0 - 1, h - 1), BACKGROUND, -1)
            cv2.rectangle(image, (x1, 0), (w - 1, h - 1), BACKGROUND, -1)
        else:
            # Full hole set inside the board, its pad running up to the edge.
            inward = (1 if cx == x0 else -1 if cx == x1 - 1 else 0,
                      1 if cy == y0 else 0)
            hx, hy = cx + inward[0] * _px(1.2), cy + inward[1] * _px(1.2)
            cv2.rectangle(image, (min(cx, hx) - rad, min(cy, hy) - rad),
                          (max(cx, hx) + rad, max(cy, hy) + rad), GOLD, -1)
            cv2.circle(image, (hx, hy), _px(0.5), BACKGROUND, -1)
            cv2.rectangle(image, (0, 0), (w - 1, y0 - 1), BACKGROUND, -1)
            cv2.rectangle(image, (0, 0), (x0 - 1, h - 1), BACKGROUND, -1)
            cv2.rectangle(image, (x1, 0), (w - 1, h - 1), BACKGROUND, -1)
    return image


def profiles_of(image, tmp_path):
    path = tmp_path / "top.png"
    cv2.imwrite(str(path), image)
    meta = {}
    crop = prepare_photo(path, LENGTH, WIDTH, meta)
    assert crop is not None, meta.get("board_failure")
    return meta["_edge_depth_px"], crop.shape


def side(edge, count, pitch, style="half_hole"):
    return dict(edge=edge, count=count, pitch_mm=pitch, contact_style=style)


ROW_4 = (4.19, 6.73, 9.27, 11.81)          # four contacts at 2.54 mm, centred on 16 mm


# ── Counting ─────────────────────────────────────────────────────────────


def test_half_holes_are_counted_and_match_the_declared_contacts(tmp_path):
    profiles, shape = profiles_of(board_photo(right=ROW_4), tmp_path)
    lines = check_declared_contacts(profiles, shape, LENGTH, WIDTH, [side("right", 4, 2.54)])
    assert lines == [("log", "Lato destro: sulla foto si contano 4 semi-fori, come i contatti "
                              "dichiarati.")]


def test_a_declared_count_the_photo_contradicts_is_a_warning(tmp_path):
    """The RS485 case: an LED label read as a fifth contact."""
    profiles, shape = profiles_of(board_photo(right=ROW_4), tmp_path)
    [(kind, message)] = check_declared_contacts(profiles, shape, LENGTH, WIDTH,
                                                [side("right", 5, 2.54)])
    assert kind == "warning"
    assert "4 semi-fori" in message and "5 contatti" in message


def test_full_holes_with_pads_to_the_edge_are_not_notches(tmp_path):
    """The ESP32 dev kit case -- a straight outline: no notch is counted.

    Declared as half-holes, the side is contradicted by the photo (warning,
    point 135); declared as full holes, it is not countable and only logged.
    """
    profiles, shape = profiles_of(board_photo(right=ROW_4, style="full"), tmp_path)
    assert count_notches(profiles["right"], shape[0] / WIDTH, 2.54).no_notch
    [(kind, message)] = check_declared_contacts(profiles, shape, LENGTH, WIDTH,
                                                [side("right", 4, 2.54)])
    assert kind == "warning" and "nessuna tacca" in message
    [(kind, message)] = check_declared_contacts(
        profiles, shape, LENGTH, WIDTH, [side("right", 4, 2.54, style="through_hole")])
    assert kind == "log" and "foro intero" in message


def test_rounded_board_corners_are_not_contacts(tmp_path):
    profiles, shape = profiles_of(board_photo(right=ROW_4, corner_mm=1.0), tmp_path)
    result = count_notches(profiles["right"], shape[0] / WIDTH, 2.54)
    assert result.found == 4


def test_a_side_the_analysis_left_out_but_the_photo_shows_is_a_warning(tmp_path):
    profiles, shape = profiles_of(board_photo(left=(4.0, 9.08), right=ROW_4), tmp_path)
    lines = check_declared_contacts(profiles, shape, LENGTH, WIDTH, [side("right", 4, 2.54)])
    assert ("warning", "Lato sinistro: sulla foto si contano 2 semi-fori, ma l'analisi non "
                       "dichiara contatti su questo lato: da verificare.") in lines


def test_full_hole_sides_are_not_counted(tmp_path):
    profiles, shape = profiles_of(board_photo(right=ROW_4), tmp_path)
    [(kind, message)] = check_declared_contacts(
        profiles, shape, LENGTH, WIDTH, [side("right", 7, 2.54, style="through_hole")])
    assert kind == "log" and "foro intero" in message


# ── The rules on a profile ───────────────────────────────────────────────


def _profile(notches, length_px=400, depth=12):
    values = [0] * length_px
    for start, stop in notches:
        for i in range(start, stop):
            values[i] = depth
    return values


def test_notches_at_the_declared_pitch_are_counted():
    values = _profile([(40, 60), (90, 110), (140, 160)])      # 50 px apart = 2.5 mm at 20 px/mm
    assert count_notches(values, 20.0, 2.5).found == 3


def test_notches_at_spacings_incompatible_with_the_pitch_are_not_counted():
    """Negative: 2.5 and 3.75 mm apart cannot both be multiples of a 2.5 mm pitch."""
    values = _profile([(40, 60), (90, 110), (165, 185)])
    result = count_notches(values, 20.0, 2.5)
    assert result.found is None and "passo" in result.reason


def test_gaps_at_a_multiple_of_the_pitch_are_regular():
    values = _profile([(40, 60), (140, 160)])                 # two pitches apart
    assert count_notches(values, 20.0, 2.5).found == 2


def test_a_shallow_dip_is_not_a_half_hole():
    """Negative: below MIN_NOTCH_DEPTH_MM the outline only wobbles."""
    shallow = int(edge_notches.MIN_NOTCH_DEPTH_MM * 20.0) - 1
    values = _profile([(40, 60), (90, 110)], depth=shallow)
    assert count_notches(values, 20.0, 2.5).found is None


def test_a_run_wider_than_the_pitch_is_not_one_notch():
    values = _profile([(40, 120)])                            # 4 mm wide, pitch 2.5 mm
    assert count_notches(values, 20.0, 2.5).found is None


def test_a_notch_broken_by_a_reflection_is_one_notch():
    values = _profile([(40, 48), (50, 60), (90, 110)])        # 2 px gap inside the first
    assert count_notches(values, 20.0, 2.5).found == 2


def test_nothing_to_compare_without_profiles_or_sides():
    assert check_declared_contacts(None, (100, 100), 10, 10, [side("left", 2, 2.54)]) == []
    assert check_declared_contacts({"left": [0] * 50}, (100, 100), 10, 10, None) == []


def test_depth_profiles_follow_the_background_into_the_board():
    mask = np.zeros((20, 30), bool)
    mask[:, :2] = True           # background beside the box
    mask[8:12, 5:8] = True       # a notch 3 px deep on the left edge of the box
    profiles = depth_profiles(mask, (5, 2, 25, 18), 10)
    assert profiles["left"][6:10] == [3, 3, 3, 3]
    assert profiles["left"][0] == 0 and len(profiles["top"]) == 20


def test_the_profiles_do_not_reach_the_saved_calibration():
    """The key is private: extract_inventory pops it before anything is saved."""
    import inspect
    from kicadgen.services import photo_inventory
    source = inspect.getsource(photo_inventory.extract_inventory)
    assert 'report.calibration.pop("_edge_depth_px", None)' in source


def test_declared_half_holes_on_a_straight_edge_are_a_warning(tmp_path):
    """The 07:29 run: contacts declared on the top and bottom edges, where there is none."""
    profiles, shape = profiles_of(board_photo(right=ROW_4), tmp_path)
    lines = check_declared_contacts(profiles, shape, LENGTH, WIDTH,
                                    [side("right", 4, 2.54), side("top", 3, 2.54)])
    assert ("warning", "Lato superiore: l'analisi dichiara 3 contatti a semi-foro, ma sulla "
                       "foto il bordo non ha nessuna tacca: lato da verificare.") in lines


def test_a_straight_edge_with_undeclared_style_is_only_logged(tmp_path):
    """Negative: without a declared half-hole style nothing contradicts the side."""
    profiles, shape = profiles_of(board_photo(right=ROW_4), tmp_path)
    lines = check_declared_contacts(profiles, shape, LENGTH, WIDTH,
                                    [side("right", 4, 2.54), side("top", 3, 2.54, style=None)])
    assert [kind for kind, message in lines if "superiore" in message] == ["log"]


def test_uncountable_notches_are_not_called_a_straight_edge():
    """Negative: notches at incompatible spacings exist; that is not «no notch»."""
    values = _profile([(40, 60), (90, 110), (165, 185)])
    result = count_notches(values, 20.0, 2.5)
    assert result.found is None and not result.no_notch
