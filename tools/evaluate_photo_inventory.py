"""@brief Offline evaluation of photo_inventory _box coordinates; never calls an API.

Examples (run from the repository root):
  python tools/evaluate_photo_inventory.py inventory.json --crop 315 117 1435 519
  python tools/evaluate_photo_inventory.py inventory.json --photo original.png
  python tools/evaluate_photo_inventory.py --review-only --photo original.png \
      --output generated/reference-evaluation/reference-review.json \
      --overlay generated/reference-evaluation/reference-overlay.png

Input: a component list, or {"components": [...], "crop_bbox_original_px":
[left, top, right, bottom]}. Every component must contain _box normalized to
the WHOLE calibrated PCB crop (not a tile, millimeters, or original image).
An explicit --crop takes precedence over input crop metadata. Otherwise --photo
recomputes the crop exactly as prepare_photo does. The fixture's recorded crop
is audit information, never a silent fallback. Record the actual crop with new
inventories: recomputing an old crop cannot establish historical provenance.

Scoring and explicit-crop CLI use only the standard library. --photo calibration
and --overlay need the project's optional OpenCV/numpy installation. Kind labels
do not affect localization. Matching maximizes the NUMBER of IoU-qualified pairs;
descending IoU orders candidate edges deterministically, but is not a global
maximum-sum-IoU optimizer. Unmatched predictions overlapping a matched reference
at the threshold are duplicates AND false positives. Centroid errors use matches
only. Zero-denominator precision/recall and empty error summaries are null.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import hashlib
import json
import math
from pathlib import Path
import statistics
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE = ROOT / "tests" / "fixtures" / "pcb_reference.json"


def checked_box(value, label="box", bounds=None):
    """@brief Validate a finite, non-empty (x0, y0, x1, y1) box.

    @param value Candidate sequence of four numbers.
    @param label Prefix used in error messages.
    @param bounds Optional (x0, y0, x1, y1) limits the box must lie within.
    @return The box as a tuple of floats.
    @throws ValueError When the box is malformed, inverted or out of bounds.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{label}: expected four coordinates")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise ValueError(f"{label}: coordinates must be numbers")
    box = tuple(float(v) for v in value)
    if not all(math.isfinite(v) for v in box):
        raise ValueError(f"{label}: non-finite coordinate")
    x0, y0, x1, y1 = box
    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"{label}: empty or inverted box")
    if bounds is not None:
        bx0, by0, bx1, by1 = bounds
        if not (bx0 <= x0 < x1 <= bx1 and by0 <= y0 < y1 <= by1):
            raise ValueError(f"{label}: outside bounds {bounds}")
    return box


def positive_number(value, label):
    """@brief Return `value` when it is a positive finite non-boolean number.

    @throws ValueError Otherwise, naming `label`.
    """
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{label}: expected a positive finite number")
    return value


def validate_reference(reference):
    """@brief Validate the manual reference annotation (schema 1) and return it unchanged.

    @param reference Parsed reference JSON.
    @return The same reference dictionary.
    @throws ValueError On unsupported schema, bad image/board sizes, or malformed or duplicate components.
    """
    if not isinstance(reference, dict) or reference.get("schema_version") != 1:
        raise ValueError("reference: unsupported schema_version")
    image = reference.get("image", {})
    w = positive_number(image.get("width_px"), "image.width_px")
    h = positive_number(image.get("height_px"), "image.height_px")
    if int(w) != w or int(h) != h:
        raise ValueError("image dimensions must be integer pixels")
    board = reference.get("board", {})
    for key in ("length_mm", "width_mm"):
        positive_number(board.get(key), f"board.{key}")
    if "calibrated_crop_bbox_original_px" in board:
        checked_box(board["calibrated_crop_bbox_original_px"], "reference crop", (0, 0, w, h))
    components = reference.get("components")
    if not isinstance(components, list):
        raise ValueError("reference.components must be a list")
    seen = set()
    for i, item in enumerate(components):
        if not isinstance(item, dict):
            raise ValueError(f"reference component {i}: expected object")
        label = item.get("id")
        if not isinstance(label, str) or not label or label in seen:
            raise ValueError(f"reference component {i}: missing or duplicate id")
        seen.add(label)
        checked_box(item.get("bbox_original_px"), label, (0, 0, w, h))
        if not isinstance(item.get("kind"), str) or not item["kind"]:
            raise ValueError(f"{label}: missing kind")
        for flag in ("kind_uncertain", "package_uncertain"):
            if not isinstance(item.get(flag), bool):
                raise ValueError(f"{label}: {flag} must be boolean")
    count = reference.get("annotation", {}).get("count")
    if count is not None and count != len(components):
        raise ValueError("reference annotation.count differs from component list")
    return reference


def normalized_to_original(box, crop):
    """@brief Map a crop-normalized box to original-photo pixel coordinates.

    @param box Box normalized to the whole calibrated crop.
    @param crop Crop rectangle in original-photo pixels.
    @return Box in original-photo pixels.
    """
    x0, y0, x1, y1 = checked_box(box, "prediction _box", (0, 0, 1, 1))
    cx0, cy0, cx1, cy1 = checked_box(crop, "crop")
    return (cx0 + x0 * (cx1-cx0), cy0 + y0 * (cy1-cy0),
            cx0 + x1 * (cx1-cx0), cy0 + y1 * (cy1-cy0))


def box_iou(a, b):
    """@brief Return the intersection-over-union of two (x0, y0, x1, y1) boxes."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x1-x0) * max(0, y1-y0)
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - intersection
    return intersection / union


def maximum_matching(scores, threshold):
    """@brief Match predictions to references one-to-one with maximum cardinality above an IoU threshold.

    Iterative augmenting paths: maximum cardinality, no recursion/dependencies.
    """
    edges = [sorted((r for r, score in enumerate(row) if score >= threshold),
                    key=lambda r: (-row[r], r)) for row in scores]
    pred_to_ref, ref_to_pred = {}, {}
    for root in range(len(scores)):
        queue, seen_predictions, parent = deque([root]), {root}, {}
        free_reference = None
        while queue and free_reference is None:
            prediction = queue.popleft()
            for ref in edges[prediction]:
                if ref in parent:
                    continue
                parent[ref] = prediction
                if ref not in ref_to_pred:
                    free_reference = ref
                    break
                other = ref_to_pred[ref]
                if other not in seen_predictions:
                    seen_predictions.add(other)
                    queue.append(other)
        ref = free_reference
        while ref is not None:
            prediction = parent[ref]
            previous_ref = pred_to_ref.get(prediction)
            pred_to_ref[prediction] = ref
            ref_to_pred[ref] = prediction
            ref = previous_ref
    return sorted(pred_to_ref.items())


def error_summary(values):
    """@brief Summarize errors as mean, median, RMSE and maximum; all None for an empty list."""
    if not values:
        return {key: None for key in ("mean", "median", "rmse", "max")}
    return {"mean": statistics.mean(values), "median": statistics.median(values),
            "rmse": math.sqrt(statistics.mean(v*v for v in values)), "max": max(values)}


def evaluate_inventory(reference, predictions, crop, iou_threshold=0.5):
    """@brief Compare predicted inventory boxes with the manual reference and report precision, recall and errors.

    Return auditable localization metrics; reject malformed input, never drop it.
    """
    validate_reference(reference)
    positive_number(iou_threshold, "IoU threshold")
    if iou_threshold > 1:
        raise ValueError("IoU threshold must be <= 1")
    im = reference["image"]
    crop = checked_box(crop, "crop", (0, 0, im["width_px"], im["height_px"]))
    if not isinstance(predictions, list):
        raise ValueError("predictions must be a list")
    boxes = []
    for index, prediction in enumerate(predictions):
        if not isinstance(prediction, dict) or "_box" not in prediction:
            raise ValueError(f"prediction {index}: missing normalized _box")
        try:
            boxes.append(normalized_to_original(prediction["_box"], crop))
        except ValueError as exc:
            raise ValueError(f"prediction {index}: {exc}") from exc
    refs = reference["components"]
    scores = [[box_iou(box, ref["bbox_original_px"]) for ref in refs] for box in boxes]
    pairs = maximum_matching(scores, iou_threshold)
    matched_predictions = {p for p, _ in pairs}
    matched_references = {r for _, r in pairs}
    sx = reference["board"]["length_mm"] / (crop[2]-crop[0])
    sy = reference["board"]["width_mm"] / (crop[3]-crop[1])
    matches = []
    for p, r in pairs:
        a, b = boxes[p], refs[r]["bbox_original_px"]
        dx, dy = (a[0]+a[2]-b[0]-b[2])/2, (a[1]+a[3]-b[1]-b[3])/2
        known_kind = not refs[r]["kind_uncertain"] and refs[r]["kind"] != "generic"
        matches.append({"prediction_index": p, "reference_id": refs[r]["id"],
                        "iou": scores[p][r], "centroid_delta_px": [dx, dy],
                        "centroid_error_px": math.hypot(dx, dy),
                        "centroid_error_mm": math.hypot(dx*sx, dy*sy),
                        "reference_kind": refs[r]["kind"],
                        "prediction_kind": predictions[p].get("kind"),
                        "kind_correct": predictions[p].get("kind") == refs[r]["kind"] if known_kind else None})
    false_positives, duplicates = [], []
    for p in range(len(predictions)):
        if p in matched_predictions:
            continue
        nearest = max(range(len(refs)), key=lambda r: scores[p][r]) if refs else None
        duplicate_refs = [r for r in sorted(matched_references) if scores[p][r] >= iou_threshold]
        entry = {"prediction_index": p, "name": predictions[p].get("name"),
                 "bbox_original_px": list(boxes[p]),
                 "best_reference_id": refs[nearest]["id"] if nearest is not None else None,
                 "best_iou": scores[p][nearest] if nearest is not None else None,
                 "duplicate_of_reference_ids": [refs[r]["id"] for r in duplicate_refs]}
        false_positives.append(entry)
        if duplicate_refs:
            duplicates.append(entry)
    potential_pairs = [{"prediction_indices": [p, q], "iou": box_iou(boxes[p], boxes[q])}
                       for p in range(len(boxes)) for q in range(p+1, len(boxes))
                       if box_iou(boxes[p], boxes[q]) >= iou_threshold]
    kind_checks = [m["kind_correct"] for m in matches if m["kind_correct"] is not None]
    tp, npred, nref = len(pairs), len(predictions), len(refs)
    return {
        "evaluation_only": True, "iou_threshold": iou_threshold,
        "matching": "maximum cardinality; geometry only; one-to-one; IoU >= threshold",
        "crop_bbox_original_px": list(crop),
        "reference_count": nref, "prediction_count": npred,
        "true_positives": tp, "false_positives": npred-tp, "false_negatives": nref-tp,
        "precision": tp/npred if npred else None, "recall": tp/nref if nref else None,
        "f1": 2*tp/(npred+nref) if npred+nref else None,
        "duplicate_count": len(duplicates), "duplicates": duplicates,
        "duplicate_definition": "Unmatched prediction with IoU >= threshold to an already matched reference; included in false positives.",
        "potential_duplicate_prediction_pairs": potential_pairs,
        "potential_duplicate_note": "Prediction-to-prediction overlap only; may include adjacent legitimate parts and is not the duplicate_count.",
        "centroid_error_px": error_summary([m["centroid_error_px"] for m in matches]),
        "centroid_error_mm": error_summary([m["centroid_error_mm"] for m in matches]),
        "centroid_scope": "Matched pairs only; mm uses independent x/y calibrated-crop scales. No perspective correction.",
        "kind_accuracy_on_certain_matches": sum(kind_checks)/len(kind_checks) if kind_checks else None,
        "certain_kind_match_count": len(kind_checks),
        "reference_counts_by_kind": dict(sorted(Counter(r["kind"] for r in refs).items())),
        "matches": matches, "unmatched_predictions": false_positives,
        "unmatched_references": [r for i, r in enumerate(refs) if i not in matched_references],
        "prediction_boxes_original_px": [list(b) for b in boxes],
        "excluded_regions_are_ignore_masks": False,
    }


def verify_photo(path, reference):
    """@brief Check that a file is the reference PNG (size and optional SHA-256) and describe it.

    Verify this fixture's original PNG, without image or network dependencies.
    """
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR" or len(data) < 24:
        raise ValueError("reference photo must be an original PNG")
    width, height = struct.unpack(">II", data[16:24])
    expected = reference["image"]
    if (width, height) != (expected["width_px"], expected["height_px"]):
        raise ValueError("photo dimensions differ from reference")
    digest = hashlib.sha256(data).hexdigest()
    if expected.get("sha256") and digest != expected["sha256"]:
        raise ValueError("photo SHA-256 differs from reference")
    return {"path": str(Path(path).resolve()), "width_px": width, "height_px": height,
            "sha256": digest, "reference_hash_verified": bool(expected.get("sha256"))}


def calibrate_crop(path, reference):
    """@brief Recompute the calibrated board crop of the reference photo with find_board.

    @return Crop rectangle in original-photo pixels.
    @throws ValueError When the photo does not match the reference, cannot be decoded or calibrated.
    """
    import cv2
    import numpy as np
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from kicadgen.services.photo_refine import find_board

    verify_photo(path, reference)
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("cannot decode original photo")
    frame = find_board(image, reference["board"]["length_mm"], reference["board"]["width_mm"])
    if frame is None:
        raise ValueError("find_board could not calibrate this photo")
    crop = [round(frame.cx-frame.width_px/2), round(frame.cy-frame.height_px/2),
            round(frame.cx+frame.width_px/2), round(frame.cy+frame.height_px/2)]
    return checked_box(crop, "calibrated crop", (0, 0, image.shape[1], image.shape[0]))


def draw_overlay(photo, reference, report, output):
    """@brief Draw references, excluded regions and predictions on a double-size photo and save it.

    Generated review output only; labels are annotation IDs, not PCB identities.
    """
    import cv2
    import numpy as np
    verify_photo(photo, reference)
    canvas = cv2.imdecode(np.fromfile(str(photo), dtype=np.uint8), cv2.IMREAD_COLOR)
    if canvas is None:
        raise ValueError("cannot decode overlay photo")
    # Double-size nearest-neighbor review preserves original pixel boundaries.
    canvas = cv2.resize(canvas, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    def rectangle(box, label, color, above=True):
        x0, y0, x1, y1 = [round(v*2) for v in box]
        cv2.rectangle(canvas, (x0, y0), (x1, y1), color, 2)
        pos = (x0, max(16, y0-4) if above else min(canvas.shape[0]-5, y1+17))
        cv2.putText(canvas, label, pos, cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 0, 0), 4)
        cv2.putText(canvas, label, pos, cv2.FONT_HERSHEY_SIMPLEX, .55, color, 1)
    for ref in reference["components"]:
        rectangle(ref["bbox_original_px"], ref["id"], (40, 220, 40))
    for i, item in enumerate(reference.get("excluded_regions", []), 1):
        rectangle(item["bbox_original_px"], f"excluded {i}", (180, 180, 180), False)
    for i, box in enumerate(report.get("prediction_boxes_original_px", [])):
        rectangle(box, f"P{i}", (0, 140, 255), False)
    cv2.putText(canvas, "Green: manual reference | Orange: predictions (zero-based) | Gray: excluded pads",
                (30, 40), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", canvas)
    if not ok:
        raise ValueError("cannot encode overlay")
    encoded.tofile(str(output))


def main(argv=None):
    """@brief Run the evaluation or reference-review CLI and print or save the JSON report.

    @param argv Optional argument list; None uses sys.argv.
    @return Process exit code 0; argument and input errors exit through argparse.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").replace("@brief ", "", 1), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inventory", type=Path, nargs="?")
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--crop", type=float, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    parser.add_argument("--photo", type=Path, help="Original reference PNG; verify hash and optionally calibrate crop")
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--review-only", action="store_true", help="Review reference without claiming detector performance")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overlay", type=Path)
    args = parser.parse_args(argv)
    if args.review_only == (args.inventory is not None):
        parser.error("provide either inventory or --review-only")
    if args.overlay and not args.photo:
        parser.error("--overlay requires --photo")
    try:
        reference = validate_reference(json.loads(args.reference.read_text(encoding="utf-8")))
        photo_info = verify_photo(args.photo, reference) if args.photo else None
        if args.review_only:
            report = {"evaluation_only": True, "mode": "manual_reference_review",
                      "reference_count": len(reference["components"]),
                      "counts_by_kind": dict(sorted(Counter(r["kind"] for r in reference["components"]).items())),
                      "uncertain_kind_count": sum(r["kind_uncertain"] for r in reference["components"]),
                      "annotation": reference.get("annotation"), "board": reference["board"],
                      "detector_performance": "Not measured in review-only mode."}
        else:
            raw = json.loads(args.inventory.read_text(encoding="utf-8"))
            metadata = raw if isinstance(raw, dict) else {}
            predictions = metadata.get("components") if isinstance(raw, dict) else raw
            for hash_key in ("image_sha256", "source_sha256"):
                if metadata.get(hash_key) and metadata[hash_key] != reference["image"].get("sha256"):
                    raise ValueError(f"inventory {hash_key} differs from reference")
            for key, reference_key in (("body_length_mm", "length_mm"), ("body_width_mm", "width_mm")):
                if key in metadata and not math.isclose(positive_number(metadata[key], key), reference["board"][reference_key], rel_tol=1e-6):
                    raise ValueError(f"inventory {key} differs from reference")
            crop, source = args.crop, "explicit --crop"
            if crop is None:
                crop, source = metadata.get("crop_bbox_original_px"), "inventory crop_bbox_original_px"
            if crop is None and args.photo:
                crop, source = calibrate_crop(args.photo, reference), "recomputed find_board / prepare_photo rounding"
            if crop is None:
                raise ValueError("supply --crop, inventory crop_bbox_original_px, or --photo; reference crop is not a fallback")
            checked_box(crop, "crop")
            for key, size in (("image_width", crop[2]-crop[0]), ("image_height", crop[3]-crop[1])):
                if key in metadata and positive_number(metadata[key], key) != size:
                    raise ValueError(f"inventory {key} differs from calibrated crop size")
            report = evaluate_inventory(reference, predictions, crop, args.iou)
            report["inventory_path"] = str(args.inventory.resolve())
            report["inventory_sha256"] = hashlib.sha256(args.inventory.read_bytes()).hexdigest()
            report["crop_source"] = source
            report["inventory_image_hash_verified"] = bool(metadata.get("image_sha256") or metadata.get("source_sha256"))
            report["provenance_note"] = "Explicit/recomputed crop must describe the crop used to produce _box. A verified original photo does not prove a legacy inventory used that same crop."
        report["reference_path"] = str(args.reference.resolve())
        report["reference_sha256"] = hashlib.sha256(args.reference.read_bytes()).hexdigest()
        report["photo_verification"] = photo_info
        if args.overlay:
            draw_overlay(args.photo, reference, report, args.overlay)
        serialized = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
            print(f"Saved {args.output}")
        else:
            print(serialized, end="")
    except (ValueError, OSError, ImportError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
