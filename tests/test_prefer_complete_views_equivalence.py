"""@brief The fast _prefer_complete_views keeps exactly what the all-pairs version kept."""

import copy
import glob
import json
import math
import random
from pathlib import Path

import pytest

from kicadgen.services import photo_yolo as py


def _reference(rows, complete, overlap=py._TILE_OVERLAP_FRACTION):
    """The all-pairs implementation, kept as the oracle (same-body check since 08/10)."""
    keep = []
    for row in rows:
        sides = row.get("clipped_sides")
        box = row["box"]
        size = max(box[2] - box[0], box[3] - box[1])
        covering = [other for other in complete if other is not row
                    and py._cover(box, other["box"]) >= py._FRAGMENT_COVERED
                    and py._same_body_as_fragment(row, other)]
        if sides and size > py.view_overlap_px(row.get("view"), overlap) and covering:
            if (row.get("rejection") is None
                    and not any(other.get("rejection") is None for other in covering)):
                best = max(covering, key=lambda other: other["confidence"])
                best["attests_presence"] = True
                best.setdefault("presence_attested_by", []).append(row["proposal_id"])
                best["presence_confidence"] = max(best.get("presence_confidence", 0.0),
                                                  row["confidence"])
            continue
        keep.append(row)
    return keep


def _run(function, rows, complete):
    """Run on a private deep copy (shared identities preserved); return a comparable state."""
    rows, complete = copy.deepcopy((rows, complete))
    universe = {id(row): row for row in rows + complete}
    order = {key: position for position, key in enumerate(universe)}
    kept = [order[id(row)] for row in function(rows, complete)]
    state = [json.dumps(row, sort_keys=True, default=str) for row in universe.values()]
    return kept, state


def _assert_same(rows, complete):
    expected = _run(_reference, rows, complete)
    assert _run(py._prefer_complete_views, rows, complete) == expected
    return expected


def _random_case(rng, count, width=4000, height=3000):
    views = ["overview"] + [f"tile{size}-{i}" for size in (640, 1280) for i in range(30)]
    rows = []
    for index in range(count):
        # Bodies near each other so covers of every size occur, ties included.
        w, h = rng.choice([rng.uniform(5, 80), rng.uniform(200, 900), 400.0]), rng.uniform(5, 700)
        x, y = rng.uniform(0, width - w), rng.uniform(0, height - h)
        if rng.random() < 0.3 and rows:
            base = rng.choice(rows)["box"]
            x, y = base[0] + rng.uniform(-50, 50), base[1] + rng.uniform(-50, 50)
        if rng.random() < 0.05:
            w = 0.0                                   # degenerate box
        rows.append({
            "proposal_id": f"p{index}",
            "view": rng.choice(views),
            "box": [x, y, x + w, y + h],
            "clipped_sides": rng.choice([[], [], ["left"], ["top", "right"]]),
            "rejection": rng.choice([None, "below_confidence", None]),
            "confidence": rng.choice([0.2, 0.5, round(rng.random(), 3)]),
        })
    return rows


@pytest.mark.parametrize("seed", range(12))
def test_randomized_boards_give_identical_rows_and_side_effects(seed):
    rng = random.Random(seed)
    rows = _random_case(rng, rng.randint(1, 250))
    complete = [row for row in rows if not row["clipped_sides"]] + _random_case(rng, 40)
    _assert_same(rows, complete)
    # Second pass on a subset, as detect_yolo_components does for review rows.
    _assert_same(rows[::2], complete)


def test_non_finite_and_integer_boxes_are_decided_by_the_scalar_rule():
    rows = [{"proposal_id": "nan", "view": "tile640-0", "box": [0, 0, math.nan, 500],
             "clipped_sides": ["left"], "rejection": None, "confidence": 0.9},
            {"proposal_id": "int", "view": "tile640-1", "box": [0, 0, 600, 3],
             "clipped_sides": ["top"], "rejection": None, "confidence": 0.9}]
    complete = [{"proposal_id": "w1", "box": [0, 0, 600, 600], "rejection": "below_confidence",
                 "confidence": 0.3},
                {"proposal_id": "w2", "box": [math.nan, 0, 700, 700],
                 "rejection": "below_confidence", "confidence": 0.3}]
    _assert_same(rows, complete)


def test_the_rule_still_drops_a_covered_fragment_and_keeps_a_small_one():
    """Positive and negative case of the rule itself, not just of the equivalence."""
    whole = {"proposal_id": "w", "view": "overview", "box": [0, 0, 1000, 1000],
             "clipped_sides": [], "rejection": "below_confidence", "confidence": 0.2}
    big = {"proposal_id": "big", "view": "tile640-0", "box": [0, 0, 600, 600],
           "clipped_sides": ["right"], "rejection": None, "confidence": 0.8}
    small = {"proposal_id": "small", "view": "tile640-0", "box": [0, 0, 100, 100],
             "clipped_sides": ["right"], "rejection": None, "confidence": 0.8}
    kept, _ = _assert_same([big, small, whole], [whole])
    assert kept == [1, 2]
    assert _run(py._prefer_complete_views, [big], [whole])[1][1].count("attests_presence") == 1


def _audits():
    root = Path(__file__).resolve().parents[1] / "generated"
    return sorted(glob.glob(str(root / "*" / "photo-cache" / "yolo-audit" / "yolo-*.json")))


def test_recorded_detector_audits_give_identical_results():
    audits = _audits()
    if not audits:
        pytest.skip("No recorded YOLO audit in generated/ (local artefacts only)")
    checked = 0
    for path in audits:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = data.get("raw_proposals") or []
        if not rows:
            continue
        overlap = (data.get("recipe") or {}).get("overlap_fraction", py._TILE_OVERLAP_FRACTION)
        whole = [row for row in rows if not row.get("clipped_sides")
                 and row.get("rejection") in (None, "below_confidence")]
        by_id = {row["proposal_id"]: row for row in rows}
        retained = [by_id.get(row["proposal_id"], row)
                    for row in data.get("retained_detector_results") or []]
        for candidate_rows in (rows, retained):
            expected = _run(lambda r, c: _reference(r, c, overlap), candidate_rows, whole)
            actual = _run(lambda r, c: py._prefer_complete_views(r, c, overlap),
                          candidate_rows, whole)
            assert actual == expected, path
        checked += 1
    if not checked:
        pytest.skip("Recorded audits carry no raw proposals")
