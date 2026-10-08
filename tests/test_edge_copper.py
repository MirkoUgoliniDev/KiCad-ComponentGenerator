"""@brief A row of lands on a straight edge is the presence of contacts the outline cannot see.

ESP32 devkit, 2026-10-08: shallow half-holes whose plated barrel hides the
background showed no notch, the edge was «clean» and 22 contacts per edge
were dropped all day. The copper along the edge is a second measurement:
identical lands, periodic at the declared pitch, in a consecutive chain.
Positive and negative cases differ in STRUCTURE (a row against scattered
features of unequal width; a chain long enough against one too short).
"""
import cv2
import numpy as np
import pytest

from kicadgen.services.edge_lands import MIN_ROW_LANDS, count_edge_copper

SCALE = 30                      # px per mm, isotropic
LENGTH, WIDTH = 40.0, 16.0      # board mm
BOARD, GOLD, HOLE, LABEL = (28, 28, 28), (60, 170, 215), (90, 90, 90), (235, 235, 235)


def _px(mm):
    return int(round(mm * SCALE))


def board(lands=(), features=(), pitch=2.54, land_w=1.8, land_in=2.6, hole_r=0.5):
    """A straight-edged black board; `lands` = indices of top-edge contacts at `pitch`,
    `features` = (x_mm, w_mm, h_mm, colour) things touching the top edge."""
    image = np.full((_px(WIDTH), _px(LENGTH), 3), BOARD, np.uint8)
    for k in lands:
        cx = _px(5.0 + k * pitch)
        cv2.rectangle(image, (cx - _px(land_w / 2), 0), (cx + _px(land_w / 2), _px(land_in)), GOLD, -1)
        cv2.circle(image, (cx, _px(1.6)), _px(hole_r), HOLE, -1)
    for x, w, h, colour in features:
        cv2.rectangle(image, (_px(x), 0), (_px(x + w), _px(h)), colour, -1)
    return image


def test_a_row_of_identical_lands_on_the_edge_is_counted_at_the_pitch():
    """@brief Positivo: 22 piazzole uguali al passo, bordo dritto -> fila trovata."""
    row = count_edge_copper(board(lands=range(12)), "top", 2.54, LENGTH, WIDTH)
    assert row.found == 12 and len(row.centres_mm) == 12
    spacing = np.diff(row.centres_mm)
    assert spacing == pytest.approx([2.54] * 11, abs=0.15)


def test_scattered_features_of_unequal_width_on_the_edge_are_not_a_row():
    """@brief Negativo: etichette e corpi di larghezze diverse che toccano il bordo (RS485)."""
    features = [(3.0, 1.2, 0.6, LABEL), (7.1, 0.5, 1.5, LABEL), (12.0, 2.3, 1.0, LABEL),
                (16.3, 0.9, 0.4, LABEL), (22.0, 1.6, 2.0, HOLE), (27.7, 0.7, 1.1, LABEL),
                (31.0, 2.9, 0.8, LABEL)]
    row = count_edge_copper(board(features=features), "top", 2.54, LENGTH, WIDTH)
    assert row.found == 0


def test_a_chain_shorter_than_the_declared_minimum_is_not_a_row():
    """@brief Negativo (limite dichiarato): meno di MIN_ROW_LANDS piazzole non confermano nulla."""
    row = count_edge_copper(board(lands=range(MIN_ROW_LANDS - 1)), "top", 2.54, LENGTH, WIDTH)
    assert row.found == 0


def test_a_row_is_found_whichever_colour_the_mask_is():
    """@brief Stessa fila su maschera verde: nessun colore e' assunto."""
    image = board(lands=range(10))
    image[np.all(image == BOARD, axis=2)] = (60, 110, 40)
    assert count_edge_copper(image, "top", 2.54, LENGTH, WIDTH).found == 10


def test_an_edge_without_a_pitch_or_without_a_photo_says_so():
    assert count_edge_copper(board(), "top", None, LENGTH, WIDTH).found is None
    assert count_edge_copper(None, "top", 2.54, LENGTH, WIDTH).found is None


def test_junk_of_one_other_width_outnumbering_the_lands_does_not_hide_the_row():
    """@brief Positivo (revisione G): 13 segni di 0.6 mm accanto a 8 piazzole -> la fila resta."""
    marks = [(1.0 + k * 1.05, 0.6, 0.8, LABEL) for k in range(13)]
    # The marks start past the last land (5 + 7 x 2.54 + 0.9 = 23.7 mm): two families, apart.
    image = board(lands=range(8), features=[(x + 24.5, w, h, c) for x, w, h, c in marks])
    row = count_edge_copper(image, "top", 2.54, LENGTH, WIDTH)
    assert row.found == 8


def test_identical_marks_at_another_pitch_are_not_a_row_at_the_declared_one():
    """@brief Negativo (revisione G): 7 segni uguali a 3.0 mm, passo dichiarato 2.8 -> nessuna fila."""
    marks = [(3.0 + k * 3.0, 0.8, 1.0, LABEL) for k in range(7)]
    assert count_edge_copper(board(features=marks), "top", 2.8, LENGTH, WIDTH).found == 0
    assert count_edge_copper(board(features=marks), "top", 3.0, LENGTH, WIDTH).found == 7


def test_the_row_survives_a_scale_error_of_the_calibration():
    """@brief Positivo: passo reale 2.49 su dichiarato 2.54 (2 % come sulla foto campione) -> fila."""
    assert count_edge_copper(board(lands=range(12), pitch=2.49), "top", 2.54, LENGTH, WIDTH).found == 12
