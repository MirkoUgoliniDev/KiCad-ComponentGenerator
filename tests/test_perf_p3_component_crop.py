"""@brief Bit-identity of the faster component crop and descriptor.

extract_component_crop used to pad the WHOLE board photo once per body and
describe_crop built one histogram per cell. Both were rewritten for speed;
these tests compare them, over many random cases, with frozen copies of the
previous algorithms kept here, and prove the comparison is able to fail.
"""
import cv2
import numpy as np
import pytest

from kicadgen.services import component_image_match as match

_SIZE = 96


def _old_extract_component_crop(image, component, board_length_mm, board_width_mm,
                                margin=0.12):
    """Frozen copy of the previous implementation (whole-image border)."""
    h, w = image.shape[:2]
    values = [board_length_mm, board_width_mm, component.get("x_mm"),
              component.get("y_mm"), component.get("length_mm"),
              component.get("width_mm")]
    board_l, board_w, x_mm, y_mm, length_mm, width_mm = map(float, values)
    sx, sy = w / board_l, h / board_w
    cx, cy = (x_mm / board_l + .5) * w, (y_mm / board_w + .5) * h
    angle = float(component.get("rotation_deg") or 0.0)
    radians = np.deg2rad(angle)
    cosine, sine = abs(float(np.cos(radians))), abs(float(np.sin(radians)))
    crop_w_mm = (cosine * length_mm + sine * width_mm) * (1.0 + 2.0 * margin)
    crop_h_mm = (sine * length_mm + cosine * width_mm) * (1.0 + 2.0 * margin)
    crop_w, crop_h = max(8, int(round(crop_w_mm * sx))), max(8, int(round(crop_h_mm * sy)))
    left, top = int(round(cx - crop_w / 2)), int(round(cy - crop_h / 2))
    right, bottom = left + crop_w, top + crop_h
    pad_left, pad_top = max(0, -left), max(0, -top)
    padded = cv2.copyMakeBorder(image, pad_top, max(0, bottom - h),
                                pad_left, max(0, right - w),
                                cv2.BORDER_CONSTANT, value=(127, 127, 127))
    left += pad_left
    top += pad_top
    crop = padded[top:top + crop_h, left:left + crop_w]
    px_per_mm = (sx + sy) / 2.0
    rect_w = max(8, int(round(crop_w_mm * px_per_mm)))
    rect_h = max(8, int(round(crop_h_mm * px_per_mm)))
    crop = cv2.resize(crop, (rect_w, rect_h), interpolation=cv2.INTER_AREA)
    if angle:
        rotated_w = max(8, int(round((cosine * crop_w_mm + sine * crop_h_mm) * px_per_mm)))
        rotated_h = max(8, int(round((sine * crop_w_mm + cosine * crop_h_mm) * px_per_mm)))
        center = (rect_w / 2.0, rect_h / 2.0)
        matrix = cv2.getRotationMatrix2D(center, -angle, 1.0)
        matrix[0, 2] += (rotated_w - rect_w) / 2.0
        matrix[1, 2] += (rotated_h - rect_h) / 2.0
        crop = cv2.warpAffine(crop, matrix, (rotated_w, rotated_h),
                              flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                              borderValue=(127, 127, 127))
    else:
        rotated_w, rotated_h = rect_w, rect_h
    target_w = max(8, int(round(length_mm * (1.0 + 2.0 * margin) * px_per_mm)))
    target_h = max(8, int(round(width_mm * (1.0 + 2.0 * margin) * px_per_mm)))
    x0 = (rotated_w - target_w) // 2
    y0 = (rotated_h - target_h) // 2
    pad = max(target_w, target_h, 8) // 2 + 4
    crop = cv2.copyMakeBorder(crop, pad, pad, pad, pad,
                              cv2.BORDER_CONSTANT, value=(127, 127, 127))
    x0 += pad
    y0 += pad
    target = crop[y0:y0 + target_h, x0:x0 + target_w]
    square = np.full((max(target_w, target_h), max(target_w, target_h), *image.shape[2:]),
                     127, dtype=image.dtype)
    dst_x = (square.shape[1] - target_w) // 2
    dst_y = (square.shape[0] - target_h) // 2
    square[dst_y:dst_y + target_h, dst_x:dst_x + target_w] = target
    return cv2.resize(square, (_SIZE, _SIZE), interpolation=cv2.INTER_AREA)


def _old_describe_crop(crop):
    """Frozen copy of the previous implementation (one bincount per cell)."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    normalized = cv2.resize(gray, (_SIZE, _SIZE), interpolation=cv2.INTER_AREA)
    normalized = cv2.equalizeHist(normalized.astype(np.uint8, copy=False))
    gx = cv2.Sobel(normalized, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(normalized, cv2.CV_32F, 0, 1, ksize=3)
    magnitude, angle = cv2.cartToPolar(gx, gy, angleInDegrees=False)
    bins = 9
    orientation = np.mod(angle, np.pi) * (bins / np.pi)
    quantized = np.floor(orientation).astype(np.int32) % bins
    cells = 4
    cell_size = _SIZE // cells
    pieces = []
    for row in range(cells):
        for col in range(cells):
            ys = slice(row * cell_size, (row + 1) * cell_size)
            xs = slice(col * cell_size, (col + 1) * cell_size)
            hist = np.bincount(quantized[ys, xs].ravel(),
                               weights=magnitude[ys, xs].ravel(), minlength=bins)
            pieces.append(hist.astype(np.float32))
    vector = np.concatenate(pieces)
    norm = float(np.linalg.norm(vector))
    if norm > 1e-12:
        vector /= norm
    return vector


def _board_image(rng, shape, dtype=np.uint8):
    """Random textured board: noise plus blocks, so crops carry real gradients."""
    image = rng.integers(0, 256, size=shape).astype(dtype)
    for _ in range(30):
        y, x = rng.integers(0, shape[0]), rng.integers(0, shape[1])
        image[y:y + rng.integers(3, 40), x:x + rng.integers(3, 40)] = rng.integers(0, 256)
    return cv2.GaussianBlur(image, (3, 3), 0)


def _random_component(rng, board_l, board_w):
    """Random body: anywhere on the board, partly or fully outside, any angle."""
    choice = rng.random()
    angle = (0.0 if choice < .3 else float(rng.choice([90, 180, 270, -90]))
             if choice < .5 else float(rng.uniform(-180, 180)))
    reach = 0.75 if rng.random() < .3 else 0.5
    return {"x_mm": float(rng.uniform(-reach, reach) * board_l),
            "y_mm": float(rng.uniform(-reach, reach) * board_w),
            "length_mm": float(rng.uniform(0.3, board_l * 0.6)),
            "width_mm": float(rng.uniform(0.2, board_w * 0.6)),
            "rotation_deg": angle}


@pytest.mark.parametrize("shape", [(300, 420, 3), (257, 181), (120, 333, 3), (90, 70, 4)])
def test_crop_is_bit_identical_to_the_whole_image_border(shape):
    rng = np.random.default_rng(sum(shape))
    image = _board_image(rng, shape)
    board_l, board_w = 42.0, 42.0 * shape[0] / shape[1] * rng.uniform(.9, 1.1)
    for _ in range(150):
        component = _random_component(rng, board_l, board_w)
        margin = float(rng.choice([0.0, 0.12, 0.3]))
        new = match.extract_component_crop(image, component, board_l, board_w, margin)
        old = _old_extract_component_crop(image, component, board_l, board_w, margin)
        assert new.dtype == old.dtype and new.shape == old.shape
        assert np.array_equal(new, old), component


def test_crop_fully_outside_and_float_images_match():
    rng = np.random.default_rng(7)
    for dtype in (np.uint8, np.float32, np.uint16):
        image = _board_image(rng, (150, 200, 3)).astype(dtype)
        for component in ({"x_mm": 400.0, "y_mm": 0.0, "length_mm": 5, "width_mm": 3},
                          {"x_mm": -400.0, "y_mm": -300.0, "length_mm": 5, "width_mm": 3,
                           "rotation_deg": 33},
                          {"x_mm": 0.0, "y_mm": 300.0, "length_mm": 5, "width_mm": 3},
                          {"x_mm": 0.0, "y_mm": 0.0, "length_mm": 80, "width_mm": 60}):
            new = match.extract_component_crop(image, component, 40.0, 30.0)
            old = _old_extract_component_crop(image, component, 40.0, 30.0)
            assert new.dtype == old.dtype and np.array_equal(new, old)


def test_descriptor_is_bit_identical_to_the_per_cell_histograms():
    rng = np.random.default_rng(11)
    image = _board_image(rng, (300, 420, 3))
    crops = [match.extract_component_crop(image, _random_component(rng, 42.0, 30.0), 42.0, 30.0)
             for _ in range(120)]
    crops += [_board_image(rng, (rng.integers(9, 200), rng.integers(9, 200))) for _ in range(40)]
    crops.append(np.full((96, 96, 3), 127, np.uint8))  # no gradient: unnormalized zeros
    for crop in crops:
        new, old = match.describe_crop(crop), _old_describe_crop(crop)
        assert new.dtype == old.dtype == np.float32
        assert new.tobytes() == old.tobytes()


def test_the_comparison_detects_a_real_change():
    """Negative case: a one-pixel shift or a changed photo must change the outputs."""
    rng = np.random.default_rng(3)
    image = _board_image(rng, (300, 420, 3))
    component = {"x_mm": 2.0, "y_mm": -1.0, "length_mm": 6.0, "width_mm": 4.0,
                 "rotation_deg": 17.0}
    base = match.extract_component_crop(image, component, 42.0, 30.0)
    moved = match.extract_component_crop(image, dict(component, x_mm=2.5), 42.0, 30.0)
    assert not np.array_equal(base, moved)
    edited = image.copy()
    edited[140:160, 200:230] = 255 - edited[140:160, 200:230]
    assert not np.array_equal(base, match.extract_component_crop(edited, component, 42.0, 30.0))
    assert match.describe_crop(base).tobytes() != match.describe_crop(moved).tobytes()


def test_crop_cost_does_not_follow_the_board_size(monkeypatch):
    """The border is applied to the body window, never to the whole photo."""
    sizes = []
    original = cv2.copyMakeBorder

    def spy(source, *args, **kwargs):
        sizes.append(source.shape[0] * source.shape[1])
        return original(source, *args, **kwargs)

    monkeypatch.setattr(match.cv2, "copyMakeBorder", spy)
    image = np.zeros((2000, 3000, 3), np.uint8)
    match.extract_component_crop(image, {"x_mm": 0.0, "y_mm": 0.0, "length_mm": 2.0,
                                         "width_mm": 1.0}, 60.0, 40.0)
    assert sizes and max(sizes) < image.shape[0] * image.shape[1] / 100
