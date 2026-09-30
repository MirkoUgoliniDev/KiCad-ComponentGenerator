"""@brief Local OpenCV descriptors and crop extraction for accepted component examples.

This deliberately stays independent of the accepted-model database until
real PCB crops have been evaluated. Geometry and category filtering should
select candidates before these visual scores are considered.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import cv2
import numpy as np


_SIZE = 96


@dataclass(frozen=True)
class ImageMatch:
    """@brief One reference image ranked against a query crop."""

    reference: object
    score: float


def extract_component_crop(image, component: dict, board_length_mm: float,
                           board_width_mm: float, margin: float = 0.12):
    """@brief Extract and normalize a component crop from a calibrated board image.

    The image is a BGR/RGB-compatible OpenCV array. Coordinates and sizes are
    in the board's millimetre frame, with its origin at the centre and Y down.
    The component is deskewed using its recorded rotation. Only its measured
    body and a small physical margin are retained; the remaining square area
    is neutral padding so adjacent components cannot enter the descriptor.
    """
    if image is None or not isinstance(image, np.ndarray) or image.ndim < 2:
        raise ValueError("image must be a decoded OpenCV image")
    h, w = image.shape[:2]
    values = [board_length_mm, board_width_mm, component.get("x_mm"),
              component.get("y_mm"), component.get("length_mm"),
              component.get("width_mm")]
    if not all(np.isfinite(float(v)) for v in values):
        raise ValueError("board and component dimensions must be finite")
    board_l, board_w, x_mm, y_mm, length_mm, width_mm = map(float, values)
    if min(board_l, board_w, length_mm, width_mm) <= 0 or margin < 0:
        raise ValueError("dimensions must be positive and margin non-negative")

    sx, sy = w / board_l, h / board_w
    cx, cy = (x_mm / board_l + .5) * w, (y_mm / board_w + .5) * h
    angle = float(component.get("rotation_deg") or 0.0)
    radians = np.deg2rad(angle)
    cosine, sine = abs(float(np.cos(radians))), abs(float(np.sin(radians)))
    # Bounds of the rotated body plus margin, in the photo's unrotated axes.
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
    if crop.size == 0:
        raise ValueError("component crop falls outside the board image")
    # Rectify the photo's x/y pixel scales before rotation and final cropping.
    px_per_mm = (sx + sy) / 2.0
    rect_w = max(8, int(round(crop_w_mm * px_per_mm)))
    rect_h = max(8, int(round(crop_h_mm * px_per_mm)))
    crop = cv2.resize(crop, (rect_w, rect_h), interpolation=cv2.INTER_AREA)
    if angle:
        rotated_w = max(8, int(round((cosine * crop_w_mm + sine * crop_h_mm)
                                     * px_per_mm)))
        rotated_h = max(8, int(round((sine * crop_w_mm + cosine * crop_h_mm)
                                     * px_per_mm)))
        center = (rect_w / 2.0, rect_h / 2.0)
        matrix = cv2.getRotationMatrix2D(center, -angle, 1.0)
        matrix[0, 2] += (rotated_w - rect_w) / 2.0
        matrix[1, 2] += (rotated_h - rect_h) / 2.0
        crop = cv2.warpAffine(crop, matrix, (rotated_w, rotated_h),
                              flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT,
                              borderValue=(127, 127, 127))
    else:
        rotated_w, rotated_h = rect_w, rect_h
    target_w = max(8, int(round(length_mm * (1.0 + 2.0 * margin) * px_per_mm)))
    target_h = max(8, int(round(width_mm * (1.0 + 2.0 * margin) * px_per_mm)))
    x0 = (rotated_w - target_w) // 2
    y0 = (rotated_h - target_h) // 2
    # A conservative pad also handles tiny round-off at 90-degree rotations.
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


def describe_crop(crop) -> np.ndarray:
    """@brief Return a unit-length spatial gradient-histogram descriptor."""
    if crop is None or not isinstance(crop, np.ndarray) or crop.size == 0:
        raise ValueError("crop must be a non-empty OpenCV image")
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    normalized = cv2.resize(gray, (_SIZE, _SIZE), interpolation=cv2.INTER_AREA)
    normalized = cv2.equalizeHist(normalized.astype(np.uint8, copy=False))
    # HOG-like descriptor built from basic OpenCV operations: some packaged
    # cv2 builds omit HOGDescriptor even though Sobel and phase are available.
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


def descriptor_bytes(descriptor: np.ndarray) -> bytes:
    """@brief Serialize a descriptor for transient component metadata or SQLite."""
    vector = np.asarray(descriptor, dtype="<f4").reshape(-1)
    if vector.size != 4 * 4 * 9 or not np.isfinite(vector).all():
        raise ValueError("unexpected component-image descriptor")
    return vector.tobytes()


def descriptor_from_bytes(payload: bytes) -> np.ndarray:
    """@brief Read a descriptor serialized by :func:`descriptor_bytes`."""
    if isinstance(payload, str) and payload.startswith("base64:"):
        try:
            payload = base64.b64decode(payload[7:], validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("invalid base64 component-image descriptor") from exc
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValueError("descriptor must be bytes")
    vector = np.frombuffer(payload, dtype="<f4").copy()
    if vector.size != 4 * 4 * 9 or not np.isfinite(vector).all():
        raise ValueError("unexpected component-image descriptor")
    return vector


def similarity(first: np.ndarray, second: np.ndarray) -> float:
    """@brief Return cosine similarity in [0, 1] for component descriptors."""
    a, b = np.asarray(first, dtype=np.float32).reshape(-1), np.asarray(second, dtype=np.float32).reshape(-1)
    if a.shape != b.shape or not a.size:
        raise ValueError("descriptors must have the same non-empty shape")
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return float(np.clip(np.dot(a, b) / (na * nb), 0.0, 1.0))


def rank_references(query_descriptor: np.ndarray, references, category: str) -> list[ImageMatch]:
    """@brief Rank category-compatible references by visual score."""
    matches = [ImageMatch(reference, similarity(query_descriptor, descriptor))
               for reference, ref_category, descriptor in references
               if str(ref_category).casefold() == str(category).casefold()]
    return sorted(matches, key=lambda match: match.score, reverse=True)


def rotated_image_similarity(query_descriptor, image_png):
    """Compare crop appearance independently of the local quarter-turn angle."""
    try:
        crop = cv2.imdecode(np.frombuffer(image_png, np.uint8), cv2.IMREAD_COLOR)
        if crop is None:
            return 0.0
        return max(similarity(query_descriptor, describe_crop(np.rot90(crop, k).copy()))
                   for k in range(4))
    except (ValueError, TypeError, cv2.error):
        return 0.0


def match_orientation(query_descriptor, examples):
    """Transfer a verified local model angle only from an unambiguous crop match.

    Crops are already deskewed by the PCB component rotation. Rotating the
    reference counterclockwise by k quarter turns therefore adds k*90 to the
    LOCAL model angle; the CAD caller separately applies the PCB rotation.
    Old examples without an individual angle never vote. Near-equal votes for
    different angles (including symmetric crops) require manual review.
    """
    unresolved = {"status": "unverified", "rotation_deg": None}
    if not isinstance(query_descriptor, np.ndarray):
        return unresolved
    votes = []
    for example in examples:
        try:
            angle = float(example["model_rotation_deg"])
            if not np.isfinite(angle):
                continue
            crop = cv2.imdecode(np.frombuffer(example["image_png"], np.uint8), cv2.IMREAD_COLOR)
            if crop is None:
                continue
            for k in range(4):
                score = similarity(query_descriptor, describe_crop(np.rot90(crop, k).copy()))
                votes.append((score, (angle + k * 90) % 360, int(example["id"]), k * 90))
        except (ValueError, TypeError, KeyError, cv2.error):
            continue
    if not votes:
        return unresolved
    votes.sort(reverse=True)
    score, angle, image_id, delta = votes[0]
    conflicting = [v[0] for v in votes if abs((v[1] - angle + 180) % 360 - 180) > 1]
    margin = score - max(conflicting, default=0.0)
    if score < .85 or margin < .08:
        return dict(unresolved, score=score, margin=margin)
    return {"status": "matched", "rotation_deg": angle, "image_id": image_id,
            "relative_rotation_deg": delta, "score": score, "margin": margin}
