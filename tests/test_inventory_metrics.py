"""Offline localization metrics; synthetic cases do not measure detector quality."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("inventory_metrics", ROOT / "tools" / "evaluate_photo_inventory.py")
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def reference(boxes, kinds=None):
    return {"schema_version": 1, "image": {"width_px": 300, "height_px": 200},
            "board": {"length_mm": 40, "width_mm": 10},
            "components": [{"id": f"M{i}", "bbox_original_px": box,
                            "kind": kinds[i] if kinds else "resistor",
                            "kind_uncertain": False, "package_uncertain": True}
                           for i, box in enumerate(boxes)]}


def prediction(box, crop=(100, 50, 300, 150), kind="resistor"):
    return {"kind": kind, "_box": [(box[0]-crop[0])/(crop[2]-crop[0]),
                                     (box[1]-crop[1])/(crop[3]-crop[1]),
                                     (box[2]-crop[0])/(crop[2]-crop[0]),
                                     (box[3]-crop[1])/(crop[3]-crop[1])]}


class InventoryMetricsTests(unittest.TestCase):
    crop = (100, 50, 300, 150)

    def test_crop_offset_and_independent_axes_in_original_pixels(self):
        self.assertEqual(metrics.normalized_to_original([.1, .2, .3, .6], self.crop),
                         (120, 70, 160, 110))

    def test_exact_threshold_is_inclusive(self):
        ref = reference([[100, 50, 120, 70]])
        result = metrics.evaluate_inventory(ref, [prediction([100, 50, 110, 70])], self.crop)
        self.assertEqual(result["matches"][0]["iou"], .5)
        self.assertEqual(result["recall"], 1)
        miss = metrics.evaluate_inventory(ref, [prediction([100, 50, 109, 70])], self.crop)
        self.assertEqual(miss["true_positives"], 0)

    def test_one_reference_cannot_match_two_predictions(self):
        box = [120, 70, 140, 90]
        result = metrics.evaluate_inventory(reference([box]), [prediction(box)]*2, self.crop)
        self.assertEqual((result["true_positives"], result["false_positives"], result["duplicate_count"]), (1, 1, 1))
        self.assertEqual(result["precision"], .5)
        self.assertEqual(result["recall"], 1)
        self.assertEqual(result["duplicates"][0]["duplicate_of_reference_ids"], ["M0"])

    def test_one_prediction_cannot_match_two_references(self):
        ref = reference([[100, 50, 110, 70], [110, 50, 120, 70]])
        result = metrics.evaluate_inventory(ref, [prediction([100, 50, 120, 70])], self.crop)
        self.assertEqual(result["true_positives"], 1)
        self.assertEqual(result["false_negatives"], 1)
        self.assertEqual(result["recall"], .5)

    def test_augmenting_path_avoids_greedy_match_loss(self):
        # P0 qualifies for both references; P1 qualifies only for R0.
        pairs = metrics.maximum_matching([[.9, .6], [.8, .1]], .5)
        self.assertEqual(pairs, [(0, 1), (1, 0)])

    def test_real_boxes_need_reassignment_to_recover_both(self):
        ref = reference([[100, 50, 120, 70], [110, 50, 130, 70]])
        result = metrics.evaluate_inventory(ref, [prediction([104, 50, 124, 70]),
                                                  prediction([100, 50, 120, 70])], self.crop)
        self.assertEqual(result["true_positives"], 2)
        self.assertEqual(result["duplicate_count"], 0)

    def test_deterministic_ties(self):
        self.assertEqual(metrics.maximum_matching([[1, 1], [1, 1]], .5), [(0, 0), (1, 1)])

    def test_centroid_errors_px_and_anisotropic_mm(self):
        result = metrics.evaluate_inventory(reference([[120, 70, 160, 110]]),
                                             [prediction([123, 74, 163, 114])], self.crop)
        self.assertEqual(result["matches"][0]["centroid_delta_px"], [3, 4])
        self.assertEqual(result["centroid_error_px"]["mean"], 5)
        self.assertAlmostEqual(result["centroid_error_mm"]["mean"], (.6**2+.4**2)**.5)

    def test_wrong_kind_does_not_hide_geometric_match(self):
        box = [120, 70, 140, 90]
        result = metrics.evaluate_inventory(reference([box]), [prediction(box, kind="capacitor")], self.crop)
        self.assertEqual(result["true_positives"], 1)
        self.assertEqual(result["kind_accuracy_on_certain_matches"], 0)

    def test_uncertain_kind_is_counted_for_recall_not_classification(self):
        box = [120, 70, 140, 90]
        ref = reference([box])
        ref["components"][0]["kind_uncertain"] = True
        result = metrics.evaluate_inventory(ref, [prediction(box, kind="anything")], self.crop)
        self.assertEqual(result["recall"], 1)
        self.assertIsNone(result["kind_accuracy_on_certain_matches"])

    def test_empty_denominators_and_error_distributions(self):
        box = [120, 70, 140, 90]
        result = metrics.evaluate_inventory(reference([box]), [], self.crop)
        self.assertIsNone(result["precision"])
        self.assertEqual(result["recall"], 0)
        self.assertTrue(all(v is None for v in result["centroid_error_px"].values()))
        result = metrics.evaluate_inventory(reference([]), [prediction(box)], self.crop)
        self.assertEqual(result["precision"], 0)
        self.assertIsNone(result["recall"])
        self.assertIsNone(metrics.evaluate_inventory(reference([]), [], self.crop)["f1"])

    def test_excluded_pad_predictions_still_are_false_positives(self):
        ref = reference([[120, 70, 140, 90]])
        ref["excluded_regions"] = [{"bbox_original_px": [180, 70, 200, 90]}]
        result = metrics.evaluate_inventory(ref, [prediction([180, 70, 200, 90])]*2, self.crop)
        self.assertEqual(result["false_positives"], 2)
        self.assertEqual(result["duplicate_count"], 0)  # no matched reference there
        self.assertEqual(len(result["potential_duplicate_prediction_pairs"]), 1)

    def test_invalid_predictions_fail_instead_of_improving_precision_by_discard(self):
        invalid = [None, {}, {"bbox": [0, 0, 1, 1]}, {"_box": [0, 0, 0, 1]},
                   {"_box": [-.01, 0, .1, .1]}, {"_box": [0, 0, 1.01, .1]},
                   {"_box": [float("nan"), 0, .1, .1]}, {"_box": [0, 0, float("inf"), .1]},
                   {"_box": [False, 0, .1, .1]}, {"_box": ["0", 0, .1, .1]}]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                metrics.evaluate_inventory(reference([]), [item], self.crop)

    def test_invalid_crop_and_threshold(self):
        for crop in ((0, 0, 0, 10), (-1, 0, 200, 100), (0, 0, 301, 200)):
            with self.subTest(crop=crop), self.assertRaises(ValueError):
                metrics.evaluate_inventory(reference([]), [], crop)
        for threshold in (0, -1, 1.1, float("nan"), float("inf"), True):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                metrics.evaluate_inventory(reference([]), [], self.crop, threshold)

    def test_reference_duplicate_ids_and_out_of_image_boxes_fail(self):
        ref = reference([[120, 70, 140, 90]]*2)
        ref["components"][1]["id"] = "M0"
        with self.assertRaises(ValueError):
            metrics.validate_reference(ref)
        with self.assertRaises(ValueError):
            metrics.validate_reference(reference([[0, 0, 301, 10]]))

    def test_fixture_coordinates_round_trip_synthetic_only(self):
        ref = json.loads(metrics.DEFAULT_REFERENCE.read_text(encoding="utf-8"))
        original = copy.deepcopy(ref)
        crop = ref["board"]["calibrated_crop_bbox_original_px"]
        preds = [prediction(r["bbox_original_px"], crop, r["kind"]) for r in ref["components"]]
        result = metrics.evaluate_inventory(ref, preds, crop)
        self.assertEqual(result["true_positives"], len(ref["components"]))
        self.assertEqual(result["duplicate_count"], 0)
        self.assertAlmostEqual(result["centroid_error_px"]["max"], 0)
        self.assertEqual(ref, original)

    def test_photo_verification_checks_dimensions_and_hash(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")
        ref = {"image": {"width_px": 1, "height_px": 1, "sha256": hashlib.sha256(png).hexdigest()}}
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "photo.png"
            photo.write_bytes(png)
            self.assertTrue(metrics.verify_photo(photo, ref)["reference_hash_verified"])
            ref["image"]["sha256"] = "bad"
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                metrics.verify_photo(photo, ref)
            ref["image"]["width_px"] = 2
            with self.assertRaisesRegex(ValueError, "dimensions"):
                metrics.verify_photo(photo, ref)

    def test_cli_requires_crop_provenance_and_runs_without_site_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "inventory.json"
            output = Path(tmp) / "metrics.json"
            inv.write_text('{"components": []}', encoding="utf-8")
            command = [sys.executable, "-S", str(ROOT / "tools" / "evaluate_photo_inventory.py"), str(inv)]
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("reference crop is not a fallback", failed.stderr)
            success = subprocess.run(command + ["--crop", "315", "117", "1435", "519", "--output", str(output)],
                                     capture_output=True, text=True)
            self.assertEqual(success.returncode, 0, success.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["recall"], 0)
            self.assertIsNone(result["precision"])

    def test_cli_accepts_crop_metadata_and_rejects_inconsistent_image_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "inventory.json"
            data = {"components": [], "crop_bbox_original_px": [315, 117, 1435, 519],
                    "image_width": 1120, "image_height": 402}
            command = [sys.executable, "-S", str(ROOT / "tools/evaluate_photo_inventory.py"), str(inv)]
            inv.write_text(json.dumps(data), encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["crop_source"], "inventory crop_bbox_original_px")
            data["image_width"] = 1597  # original image width, NOT calibrated-crop width
            inv.write_text(json.dumps(data), encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("differs from calibrated crop size", result.stderr)

    def test_cli_rejects_inventory_with_wrong_image_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "inventory.json"
            for hash_key in ("image_sha256", "source_sha256"):
                inv.write_text(json.dumps({"components": [], hash_key: "wrong",
                                           "crop_bbox_original_px": [315, 117, 1435, 519]}), encoding="utf-8")
                result = subprocess.run([sys.executable, "-S", str(ROOT / "tools/evaluate_photo_inventory.py"), str(inv)],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{hash_key} differs", result.stderr)


if __name__ == "__main__":
    unittest.main()
