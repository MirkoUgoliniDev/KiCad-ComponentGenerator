"""@brief Synthetic board photos for the half-hole notch tests (HANDOFF point 139, G1).

Not a test module: helpers shared by tests/test_edge_notches_*.py and
tests/test_photo_recount_*.py. A dark board on a light background, drawn in
millimetres and rotated by a small residual tilt, with plated half-holes cut
by the outline (the background shows through: a notch) or full holes whose
pad reaches the edge (the outline stays straight). Optionally squeezed along
X (a photo calibrated axis by axis) and with seeded pixel noise.
"""
import math

import cv2
import numpy as np

BACKGROUND = (235, 235, 235)
BOARD = (30, 60, 20)
GOLD = (60, 170, 210)


def tilted_board(length_mm, width_mm, scale, *, angle_deg=0.0, notches=None,
                 radius_mm=0.35, inset_mm=0.0, ring_mm=0.25, x_squeeze=1.0,
                 noise=0.0, margin=80, hidden=(), seed=7, full_holes=None,
                 corner_mm=0.0):
    """@brief Draw a board photo.

    @param notches {edge: [position in mm from the start of the edge]}: left
        and right from the top, top and bottom from the left, as seen from
        above before the tilt.
    @param radius_mm Half-hole radius; the notch is radius - inset deep.
    @param inset_mm How far the hole centre sits inside the board.
    @param x_squeeze Resize factor along X after drawing (anisotropic photo).
    @param hidden [(edge, position)] notches painted over with board colour
        (a dimension arrow or dust covering one half-hole).
    @param full_holes {edge: [positions]} full holes 1.2 mm inside the edge
        whose pad runs up to the edge: the outline stays straight.
    @param corner_mm Radius of rounded board corners.
    @return BGR image.
    """
    notches = notches or {}
    full_holes = full_holes or {}
    h = int(round(width_mm * scale)) + 2 * margin
    w = int(round(length_mm * scale)) + 2 * margin
    image = np.full((h, w, 3), BACKGROUND, np.uint8)
    cx, cy = w / 2.0, h / 2.0
    theta = math.radians(angle_deg)
    cos, sin = math.cos(theta), math.sin(theta)

    def px(x_mm, y_mm):
        """Board coordinates (mm, origin at the centre) -> photo pixels."""
        x, y = x_mm * scale, y_mm * scale
        return (cx + x * cos - y * sin, cy + x * sin + y * cos)

    half_l, half_w = length_mm / 2, width_mm / 2
    outline = np.zeros((h, w), np.uint8)
    if corner_mm:
        r = corner_mm
        points = []
        for (ox, oy, start) in ((half_l - r, half_w - r, 0), (-half_l + r, half_w - r, 90),
                                (-half_l + r, -half_w + r, 180), (half_l - r, -half_w + r, 270)):
            for step in range(0, 91, 5):
                a = math.radians(start + step)
                points.append(px(ox + r * math.cos(a), oy + r * math.sin(a)))
    else:
        points = [px(-half_l, -half_w), px(half_l, -half_w), px(half_l, half_w),
                  px(-half_l, half_w)]
    polygon = np.array([[int(round(x * 16)), int(round(y * 16))] for x, y in points], np.int32)
    cv2.fillPoly(outline, [polygon], 255, lineType=cv2.LINE_8, shift=4)
    image[outline > 0] = BOARD

    def edge_point(edge, pos, inward):
        if edge == "left":
            return -half_l + inward, -half_w + pos
        if edge == "right":
            return half_l - inward, -half_w + pos
        if edge == "top":
            return -half_l + pos, -half_w + inward
        return -half_l + pos, half_w - inward

    def circle(centre, radius_px, colour):
        x, y = centre
        cv2.circle(image, (int(round(x * 16)), int(round(y * 16))), int(round(radius_px * 16)),
                   colour, -1, lineType=cv2.LINE_8, shift=4)

    for edge, positions in full_holes.items():
        for pos in positions:
            hole = px(*edge_point(edge, pos, 1.2))
            pad = [px(*edge_point(edge, pos + d, i)) for d, i in
                   ((-radius_mm - 0.3, 0), (radius_mm + 0.3, 0),
                    (radius_mm + 0.3, 2.0), (-radius_mm - 0.3, 2.0))]
            cv2.fillPoly(image, [np.array([[int(round(x * 16)), int(round(y * 16))]
                                           for x, y in pad], np.int32)], GOLD, shift=4)
            circle(hole, 0.5 * scale, BACKGROUND)
    for edge, positions in notches.items():
        for pos in positions:
            circle(px(*edge_point(edge, pos, inset_mm)), (radius_mm + ring_mm) * scale, GOLD)
    image[outline == 0] = BACKGROUND
    for edge, positions in notches.items():
        for pos in positions:
            if (edge, pos) in hidden:
                continue
            circle(px(*edge_point(edge, pos, inset_mm)), radius_mm * scale, BACKGROUND)
    if noise:
        rng = np.random.default_rng(seed)
        image = np.clip(image.astype(float) + rng.normal(0, noise, image.shape), 0,
                        255).astype(np.uint8)
    if x_squeeze != 1.0:
        image = cv2.resize(image, (int(round(w * x_squeeze)), h), interpolation=cv2.INTER_AREA)
    return image


def row(count, pitch_mm, edge_length_mm, offset_mm=0.0):
    """@brief Positions (mm from the start of the edge) of a centred contact row."""
    start = edge_length_mm / 2 - (count - 1) * pitch_mm / 2 + offset_mm
    return [round(start + i * pitch_mm, 4) for i in range(count)]


def calibrated_profiles(image, length_mm, width_mm, tmp_path, name="top.png"):
    """@brief Run the real calibration (prepare_photo) and return (profiles, crop shape)."""
    from kicadgen.services.photo_inventory import prepare_photo
    path = tmp_path / name
    cv2.imwrite(str(path), image)
    meta = {}
    crop = prepare_photo(path, length_mm, width_mm, meta)
    assert crop is not None, meta.get("board_failure")
    return meta["_edge_depth_px"], crop.shape
