"""@brief Two views of one detected body, preserving image axes and native evidence.

Margins are framing heuristics, not segmentation or a guarantee that every
terminal is visible. The context remains available when the clean detail clips
a contact extending beyond the measured rectangle.
"""
import math


def prepare_observation_views(photo, component, board_length_mm, board_width_mm,
                              long_side_px=1024):
    """@brief Return context/detail BGR arrays and JSON-safe geometry, or None.

    ``context_bgr`` has a magenta target rectangle and 0.6 margins on each axis.
    ``detail_bgr`` has no overlay and 0.35 margins plus two source pixels.
    Both correct unequal X/Y pixel scales without rotating image axes. Native
    sizes describe source pixels, never the interpolated output resolution.
    ``clipped_edges`` records photo-boundary clipping for each view.

    @param photo BGR photo of the calibrated board crop, as a numpy array.
    @param component Component dict with x_mm, y_mm, length_mm, width_mm and
        optional rotation_deg, in the board frame (origin at the centre, Y down).
    @param board_length_mm Declared board extent along X, in mm.
    @param board_width_mm Declared board extent along Y, in mm.
    @param long_side_px Long side of each interpolated output view, in px.
    @return A dict with ``context_bgr``/``detail_bgr`` (views), ``context_box_px``/
        ``detail_box_px`` (source rectangles, in px), ``native_target_size_px``,
        ``target_corners_px`` and ``clipped_edges``; None when the component or
        board values are missing or not numeric, or a view would be smaller
        than two pixels.
    """
    import cv2
    import numpy as np
    try:
        cx, cy = float(component['x_mm']), float(component['y_mm'])
        length, width = float(component['length_mm']), float(component['width_mm'])
        angle = float(component.get('rotation_deg') or 0)
        board_l, board_w = float(board_length_mm), float(board_width_mm)
        target = int(long_side_px)
        height_px, width_px = photo.shape[:2]
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None
    if (not all(math.isfinite(v) for v in (cx, cy, length, width, angle, board_l, board_w))
            or min(length, width, board_l, board_w, target, height_px, width_px) <= 0):
        return None
    sx, sy = width_px / board_l, height_px / board_w
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    corners = [((cx + dx * cosine - dy * sine + board_l / 2) * sx,
                (cy + dx * sine + dy * cosine + board_w / 2) * sy)
               for dx, dy in ((-length/2, -width/2), (length/2, -width/2),
                              (length/2, width/2), (-length/2, width/2))]
    xs, ys = zip(*corners)
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    # Do not manufacture a target from background when only its margin overlaps.
    if x1 <= 0 or y1 <= 0 or x0 >= width_px or y0 >= height_px:
        return None
    bw, bh = x1-x0, y1-y0
    square = max(sx, sy)
    fx, fy = square/sx, square/sy
    output = {'native_target_size_px': [bw, bh],
              'target_corners_px': [list(p) for p in corners],
              'clipped_edges': {}}
    for name, margin, extra in (('context', .6, 0), ('detail', .35, 2)):
        desired = [math.floor(x0-margin*bw-extra), math.floor(y0-margin*bh-extra),
                   math.ceil(x1+margin*bw+extra), math.ceil(y1+margin*bh+extra)]
        left, top = max(0, desired[0]), max(0, desired[1])
        right, bottom = min(width_px, desired[2]), min(height_px, desired[3])
        if right-left < 2 or bottom-top < 2:
            return None
        factor = target / max((right-left)*fx, (bottom-top)*fy)
        size = (max(1, round((right-left)*fx*factor)),
                max(1, round((bottom-top)*fy*factor)))
        view = cv2.resize(photo[top:bottom, left:right], size,
                          interpolation=cv2.INTER_CUBIC if factor >= 1 else cv2.INTER_AREA)
        if name == 'context':
            points = np.array([[round((x-left)*fx*factor), round((y-top)*fy*factor)]
                               for x, y in corners], dtype=np.int32)
            cv2.polylines(view, [points.reshape(-1, 1, 2)], True,
                          (255, 0, 255), max(1, round(target/512)))
        output[name+'_bgr'] = np.ascontiguousarray(view)
        output[name+'_box_px'] = [left, top, right, bottom]
        output['clipped_edges'][name] = [edge for edge, clipped in zip(
            ('left', 'top', 'right', 'bottom'),
            (desired[0] < 0, desired[1] < 0, desired[2] > width_px, desired[3] > height_px))
            if clipped]
    return output
