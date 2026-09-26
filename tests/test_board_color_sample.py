"""@brief The board colour is sampled from the calibrated photo, whatever the mask colour.

HANDOFF point 85, medium 11: an undeclared board colour used to keep the
sample's default. photo_refine.sample_board_color reads the solder mask from
the calibrated crop -- the surface covering the largest area -- so a green,
blue, black, white or red board comes out as itself, with components, pads,
silkscreen and gentle shading on top.
"""

import re

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="opencv-python non installato")

from kicadgen.services.photo_refine import sample_board_color

MASKS = {
    "green": (40, 110, 30),
    "blue": (140, 60, 20),
    "black": (28, 28, 28),
    "white": (232, 232, 228),
    "red": (30, 30, 170),
}


def _board(mask, seed=0, module_fraction=0.19):
    """Calibrated crop: mask with a shading ramp, a large dark module, ICs, gold and silver pads,
    silkscreen lines and noise."""
    rng = np.random.default_rng(seed)
    height, width = 600, 1500
    image = np.zeros((height, width, 3), np.float32)
    image[:] = mask
    image += np.linspace(-8, 8, width)[None, :, None]            # illuminazione non uniforme
    side = int(np.sqrt(module_fraction * height * width))
    cv2.rectangle(image, (100, 100), (100 + side, 100 + side), (20, 20, 20), -1)
    for index in range(40):
        x, y = int(rng.integers(0, width - 50)), int(rng.integers(0, height - 30))
        body = (18, 18, 18) if index % 3 else (60, 150, 200)
        cv2.rectangle(image, (x, y), (x + 40, y + 22), body, -1)
        cv2.rectangle(image, (x - 6, y + 4), (x - 1, y + 18), (200, 200, 200), -1)
    for _ in range(30):
        y = int(rng.integers(0, height))
        cv2.line(image, (0, y), (width, y), (235, 235, 235), 2)
    image += rng.normal(0, 3, image.shape)
    return np.clip(image, 0, 255).astype(np.uint8)


def _rgb(hex_colour):
    assert re.fullmatch(r"#[0-9a-f]{6}", hex_colour)
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


@pytest.mark.parametrize("name", sorted(MASKS))
def test_the_mask_colour_is_found_under_the_components(name):
    """@brief Positive, per colour: within a few levels of the true mask (BGR -> #rrggbb)."""
    b, g, r = MASKS[name]
    for seed in range(2):
        found = _rgb(sample_board_color(_board(MASKS[name], seed)))
        assert all(abs(f - t) <= 6 for f, t in zip(found, (r, g, b))), (name, found)


def test_a_component_colour_does_not_win_while_the_mask_covers_more():
    """@brief Structural negative: a dark module over a green board is not the board.

    The module is the largest single component but covers less than the
    visible mask; the answer stays green, and a crop of the module alone is
    (by the same rule) the module's colour -- the rule is area, not a colour
    preference.
    """
    image = _board(MASKS["green"], module_fraction=0.35)
    found = _rgb(sample_board_color(image))
    assert found[1] > found[0] + 40 and found[1] > found[2] + 40
    module_only = image[110:400, 110:400]
    assert max(_rgb(sample_board_color(module_only))) < 40


@pytest.mark.parametrize("bad", [None, np.zeros((0, 0, 3), np.uint8), np.zeros((20, 20), np.uint8)])
def test_an_unusable_crop_gives_none(bad):
    """@brief No colour is invented from an empty or single-channel image."""
    assert sample_board_color(bad) is None
