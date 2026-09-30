import numpy as np

from kicadgen.services.component_image_match import (
    describe_crop,
    extract_component_crop,
    rank_references,
    similarity,
)


def _synthetic_board():
    import cv2

    image = np.full((400, 600, 3), 35, dtype=np.uint8)
    # A simple two-terminal package with contrasting body and metal ends.
    cv2.rectangle(image, (270, 175), (330, 225), (180, 180, 180), -1)
    cv2.rectangle(image, (282, 180), (318, 220), (20, 20, 20), -1)
    cv2.line(image, (287, 185), (313, 185), (110, 110, 110), 2)
    return image


def test_extracts_fixed_size_crop_and_hog_descriptor():
    image = _synthetic_board()
    component = {"x_mm": 0, "y_mm": 0, "length_mm": 3, "width_mm": 2}
    crop = extract_component_crop(image, component, 60, 40)

    assert crop.shape == (96, 96, 3)
    descriptor = describe_crop(crop)
    assert descriptor.ndim == 1
    assert np.isclose(np.linalg.norm(descriptor), 1.0)
    assert similarity(descriptor, descriptor) == 1.0


def test_crop_excludes_nearby_components_from_the_visual_reference():
    import cv2

    image = np.full((1000, 1000, 3), 35, dtype=np.uint8)
    # Target body: 2 x 1 mm at the board centre (100 px/mm).
    cv2.rectangle(image, (400, 450), (600, 550), (180, 180, 180), -1)
    # A red neighbouring component falls inside the old square context crop,
    # but outside the measured target body and its margin.
    cv2.rectangle(image, (460, 590), (540, 630), (0, 0, 255), -1)
    component = {"x_mm": 0, "y_mm": 0, "length_mm": 2, "width_mm": 1}

    crop = extract_component_crop(image, component, 10, 10)

    red_pixels = ((crop[:, :, 2] > 200) & (crop[:, :, 0] < 80)
                  & (crop[:, :, 1] < 80))
    assert crop.shape == (96, 96, 3)
    assert not red_pixels.any()


def test_reference_ranking_prefilters_by_category():
    image = _synthetic_board()
    component = {"x_mm": 0, "y_mm": 0, "length_mm": 3, "width_mm": 2}
    descriptor = describe_crop(extract_component_crop(image, component, 60, 40))
    refs = [("r0603", "resistor", descriptor),
            ("c0603", "capacitor", descriptor)]

    matches = rank_references(descriptor, refs, "resistor")

    assert [match.reference for match in matches] == ["r0603"]
    assert matches[0].score == 1.0
