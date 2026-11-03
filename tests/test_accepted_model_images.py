import sqlite3

import cv2
import numpy as np
import pytest

from kicadgen.services import accepted_models as am
from kicadgen.services.component_image_match import describe_crop, descriptor_bytes


def _asymmetric_crop():
    crop = np.zeros((96, 96, 3), np.uint8)
    cv2.rectangle(crop, (15, 25), (70, 65), (100, 100, 100), -1)
    cv2.rectangle(crop, (10, 5), (24, 35), (255, 255, 255), -1)
    cv2.circle(crop, (65, 60), 8, (230, 230, 230), -1)
    return crop


@pytest.mark.parametrize('k', range(4))
def test_individual_angles_survive_grouping_and_transfer_rotation(tmp_path, k):
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / 'archive.sqlite3')
    crop = _asymmetric_crop()
    for turn in (0, 2):
        row = _component(root, paths[0], image=np.rot90(crop, turn).copy())
        row['model_selection'] = {'status': 'confirmed', 'rotation_deg': (270 + turn * 90) % 360}
        store.consolidate([row], [root])
    [entry] = store.entries()
    assert [e['model_rotation_deg'] for e in store.images(entry['id'])] == [270, 90]
    query = _component(root, paths[0], descriptor=describe_crop(np.rot90(crop, k).copy()))
    query['rotation_deg'] = 90  # Applied separately by CAD, never added again here.
    found = store.find(query, [root])
    assert found['orientation']['status'] == 'matched'
    assert found['orientation']['rotation_deg'] == (270 + k * 90) % 360


def test_symmetric_or_conflicting_examples_cannot_transfer_angle():
    from kicadgen.services.component_image_match import match_orientation
    crop = np.zeros((96, 96, 3), np.uint8)
    cv2.rectangle(crop, (20, 20), (75, 75), (255, 255, 255), -1)
    def example(image, angle, id):
        return dict(image_png=cv2.imencode('.png', image)[1].tobytes(), model_rotation_deg=angle, id=id)
    assert match_orientation(describe_crop(crop), [example(crop, 90, 1)])['status'] == 'unverified'
    crop = _asymmetric_crop()
    assert match_orientation(describe_crop(crop), [example(crop, 90, 1), example(crop, 270, 2)])['status'] == 'unverified'


@pytest.mark.parametrize('turn', range(4))
def test_photo_rotation_is_removed_before_transferring_local_angle(turn):
    from kicadgen.services.component_image_match import extract_component_crop, match_orientation
    board = np.full((240, 240, 3), 127, np.uint8)
    board[72:168, 72:168] = _asymmetric_crop()
    component = dict(x_mm=0, y_mm=0, length_mm=8, width_mm=8, rotation_deg=0)
    reference = extract_component_crop(board, component, 20, 20)
    rotated = np.rot90(board, turn).copy()
    query = extract_component_crop(rotated, dict(component, rotation_deg=turn * 90), 20, 20)
    example = dict(id=1, image_png=cv2.imencode('.png', reference)[1].tobytes(), model_rotation_deg=270)
    match = match_orientation(describe_crop(query), [example])
    assert match['status'] == 'matched'
    assert match['rotation_deg'] == 270


def test_v4_images_do_not_inherit_shared_angle(tmp_path):
    root, paths = _library(tmp_path)
    path = tmp_path / 'archive.sqlite3'
    store = am.AcceptedModelStore(path)
    row = _component(root, paths[0], image=_asymmetric_crop())
    row['model_selection'] = {'status': 'confirmed', 'rotation_deg': 270}
    store.consolidate([row], [root])
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE accepted_model_images DROP COLUMN model_rotation_deg')
        db.execute('PRAGMA user_version=4')
    found = store.find(row, [root])
    assert found['path'] == root / paths[0]
    assert found['orientation']['status'] == 'unverified'
    store.consolidate([row], [root])
    assert [e['model_rotation_deg'] for e in store.images(store.entries()[0]['id'])] == [None, 270]


@pytest.mark.parametrize('mode', ['explicit', 'rendered', 'preserved'])
def test_actual_model_base_and_manual_delta_are_stored_once(tmp_path, mode):
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / 'archive.sqlite3')
    row = _component(root, paths[0], image=_asymmetric_crop())
    row['model_rotation_delta_deg'] = 90
    if mode == 'explicit':
        row['model_selection'] = {'status': 'confirmed', 'rotation_deg': 270}
    elif mode == 'rendered':
        row['model_match'].update(rendered_local_rotation_deg=360, rendered_manual_delta_deg=90)
    else:
        row['model_rotation_deg'] = 270
        row['model_match'] = {'status': 'library_matched', 'accepted_entry': {'rotation_deg': 0},
                              'accepted_orientation': {'status': 'matched', 'rotation_deg': 0}}
    store.consolidate([row], [root])
    [example] = store.images(store.entries()[0]['id'])
    assert example['model_rotation_deg'] == 0


def _library(tmp_path):
    root = tmp_path / "3d"
    paths = ["Resistor_SMD.3dshapes/R_0603_1608Metric.step",
             "Resistor_SMD.3dshapes/R_0603_Other.step"]
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ISO-10303-21;\n", encoding="utf-8")
    return root, paths


def _component(root, relative, *, image=None, descriptor=None):
    component = {
        "name": "R1", "kind": "resistor", "length_mm": 1.6, "width_mm": .8,
        "bbox_basis": "body", "model_path": str(root / relative),
        "model_match": {"status": "auto_matched"},
    }
    if image is not None:
        ok, encoded = cv2.imencode(".png", image)
        assert ok
        component["_accepted_image_png"] = encoded.tobytes()
        component["_accepted_image_descriptor"] = descriptor_bytes(describe_crop(image))
    elif descriptor is not None:
        component["_accepted_image_descriptor"] = descriptor_bytes(descriptor)
    return component


@pytest.mark.parametrize('turn', range(4))
def test_c05_basis_change_recovers_only_with_strong_crop_and_matching_pins(tmp_path, turn):
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / 'archive.sqlite3')
    crop = _asymmetric_crop()
    saved = _component(root, paths[0], image=crop)
    saved.update(kind='ic', bbox_basis='total', length_mm=3.15, width_mm=2.79,
                 pin_count=6, pin_count_confidence='high')
    store.consolidate([saved], [root])
    query = dict(saved, bbox_basis='body',
                 _accepted_image_descriptor=descriptor_bytes(describe_crop(np.rot90(crop, turn).copy())))
    found = store.find(query, [root])
    assert found is not None
    assert found['basis_recovered_visually'] is True
    assert found['visual_score'] > .99
    # Visual discovery preserves these candidates, but none may attach automatically.
    for changes in (dict(pin_count=5), dict(pin_count=None), dict(kind='capacitor'), dict(length_mm=8)):
        review = store.find(dict(query, **changes), [root])
        assert review['review_required'] and review['visual_conflicts']
    assert store.find(dict(query, _accepted_image_descriptor=None), [root]) is None


def test_cross_basis_identical_images_for_different_models_are_ambiguous(tmp_path):
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / 'archive.sqlite3')
    for path in paths:
        store.consolidate([_component(root, path, image=_asymmetric_crop())], [root])
    query = _component(root, paths[0], image=_asymmetric_crop())
    query['bbox_basis'] = 'total'
    found = store.find(query, [root])
    assert found['review_required']
    assert any('ambigua' in reason for reason in found['visual_conflicts'])


def test_verified_component_crop_is_stored_and_deleted_with_its_entry(tmp_path):
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / "archive.sqlite3")
    crop = np.zeros((96, 96, 3), dtype=np.uint8)
    cv2.rectangle(crop, (20, 30), (76, 66), (255, 255, 255), 4)

    result = store.consolidate([_component(root, paths[0], image=crop)], [root])

    assert (result.created, result.images_saved, result.images_skipped) == (1, 1, 0)
    [entry] = store.entries()
    assert entry["image_count"] == 1
    [saved] = store.images(entry["id"])
    assert saved["image_png"].startswith(b"\x89PNG\r\n\x1a\n")
    assert len(saved["descriptor"]) == 4 * 4 * 9 * 4
    assert saved["component_name"] == "R1"
    assert store.delete([entry["id"]]) == 1
    assert store.images(entry["id"]) == []


def test_matching_uses_similar_verified_crop_to_break_a_geometric_tie(tmp_path):
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / "archive.sqlite3")
    first = np.zeros((96, 96, 3), dtype=np.uint8)
    cv2.rectangle(first, (18, 28), (78, 68), (255, 255, 255), 5)
    second = np.zeros((96, 96, 3), dtype=np.uint8)
    cv2.line(second, (8, 8), (88, 88), (255, 255, 255), 8)
    store.consolidate([_component(root, paths[0], image=first)], [root])
    # Same category and dimensions, but the newer entry carries a different crop.
    store.consolidate([_component(root, paths[1], image=second)], [root])

    query = _component(root, paths[1], descriptor=describe_crop(first))
    found = store.find(query, [root])

    assert found["path"] == root / paths[0]
    assert found["visual_score"] > .9
    assert found["image_examples"] == 1


def test_schema_v2_is_migrated_without_losing_accepted_entries(tmp_path):
    root, paths = _library(tmp_path)
    path = tmp_path / "archive.sqlite3"
    store = am.AcceptedModelStore(path)
    store.consolidate([_component(root, paths[0])], [root])
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE accepted_model_images")
        db.execute("PRAGMA user_version=2")

    assert store.entries()[0]["image_count"] == 0
    store.consolidate([_component(root, paths[0], image=np.zeros((96, 96, 3), np.uint8))], [root])
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    assert store.entries()[0]["image_count"] == 1


def test_schema_v3_adds_component_ids_to_future_crop_examples(tmp_path):
    root, paths = _library(tmp_path)
    path = tmp_path / "archive.sqlite3"
    store = am.AcceptedModelStore(path)
    crop = np.zeros((96, 96, 3), np.uint8)
    store.consolidate([_component(root, paths[0], image=crop)], [root])
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=3")
        db.execute("ALTER TABLE accepted_model_images DROP COLUMN component_name")

    assert store.images(store.entries()[0]["id"])[0]["component_name"] == ""
    store.consolidate([dict(_component(root, paths[0], image=crop), name="C-02")], [root])
    examples = store.images(store.entries()[0]["id"])
    assert [example["component_name"] for example in examples] == ["", "C-02"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5


def test_new_crop_replaces_obsolete_crop_versions(tmp_path):
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / "archive.sqlite3")
    crop = np.zeros((96, 96, 3), np.uint8)
    first = _component(root, paths[0], image=crop)
    store.consolidate([first], [root])
    entry_id = store.entries()[0]["id"]
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE accepted_model_images SET crop_version=1 WHERE accepted_model_id=?",
                   (entry_id,))

    updated = dict(_component(root, paths[0], image=crop), name="C-02")
    store.consolidate([updated], [root])

    [saved] = store.images(entry_id)
    assert saved["crop_version"] == am.IMAGE_CROP_VERSION
    assert saved["component_name"] == "C-02"


# -- review G7 (2026-11-03): the declared visual thresholds, at their bounds ------------

def _scored(monkeypatch, scores):
    """Make every visual comparison return scores[i] for the i-th stored descriptor kind.

    `scores` maps a stored descriptor (bytes) to the score it gets; the rotated
    comparison adds nothing, so the declared threshold alone decides.
    """
    from kicadgen.services import component_image_match as cim

    def similarity(_query, stored):
        return scores.get(np.asarray(stored, np.float32).tobytes(), 0.0)
    monkeypatch.setattr(cim, "similarity", similarity)
    monkeypatch.setattr(cim, "rotated_image_similarity", lambda *_args: 0.0)


def _two_crops():
    first = np.zeros((96, 96, 3), dtype=np.uint8)
    cv2.rectangle(first, (18, 28), (78, 68), (255, 255, 255), 5)
    second = np.zeros((96, 96, 3), dtype=np.uint8)
    cv2.line(second, (8, 8), (88, 88), (255, 255, 255), 8)
    return first, second


@pytest.mark.parametrize("delta, older_wins", [(0.01, True), (-0.01, False)])
def test_images_break_a_tie_only_above_the_declared_minimum(tmp_path, monkeypatch, delta,
                                                            older_wins):
    """Positive: just above VISUAL_TIEBREAK_MIN the older, similar crop wins the tie.
    Negative: just below it the recency rule decides (the newer entry)."""
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / "archive.sqlite3")
    first, second = _two_crops()
    store.consolidate([_component(root, paths[0], image=first)], [root])
    store.consolidate([_component(root, paths[1], image=second)], [root])
    _scored(monkeypatch, {describe_crop(first).astype(np.float32).tobytes():
                          am.VISUAL_TIEBREAK_MIN + delta})
    found = store.find(_component(root, paths[1], descriptor=describe_crop(first)), [root])
    assert found["path"] == root / (paths[0] if older_wins else paths[1])


@pytest.mark.parametrize("delta, recovered", [(0.005, True), (-0.005, False)])
def test_a_basis_change_is_recovered_only_above_the_declared_minimum(tmp_path, monkeypatch,
                                                                    delta, recovered):
    """Positive: just above VISUAL_BASIS_RECOVERY_MIN the entry recorded on the other size
    basis is recovered. Negative: just below it nothing is recovered."""
    root, paths = _library(tmp_path)
    store = am.AcceptedModelStore(tmp_path / 'archive.sqlite3')
    crop = _asymmetric_crop()
    saved = _component(root, paths[0], image=crop)
    saved.update(kind='ic', bbox_basis='total', length_mm=3.15, width_mm=2.79,
                 pin_count=6, pin_count_confidence='high')
    store.consolidate([saved], [root])
    _scored(monkeypatch, {describe_crop(crop).astype(np.float32).tobytes():
                          am.VISUAL_BASIS_RECOVERY_MIN + delta})
    found = store.find(dict(saved, bbox_basis='body'), [root])
    if recovered:
        assert found is not None and found['basis_recovered_visually'] is True
    else:
        assert found is None


def _orientation_votes(monkeypatch, best, other):
    """Two stored examples at 0 and 90 degrees whose first quarter turn scores best / other."""
    from kicadgen.services import component_image_match as cim
    monkeypatch.setattr(cim, "_quarter_turn_descriptors",
                        lambda png: ([png + b"0", png + b"1", png + b"2", png + b"3"], True))
    table = {b"a0": best, b"b0": other}
    monkeypatch.setattr(cim, "similarity", lambda _q, d: table.get(d, 0.0))
    return cim.match_orientation(np.zeros(4, np.float32),
                                 [{"id": 1, "image_png": b"a", "model_rotation_deg": 0},
                                  {"id": 2, "image_png": b"b", "model_rotation_deg": 90}])


@pytest.mark.parametrize("delta, status", [(0.01, "matched"), (-0.01, "unverified")])
def test_an_angle_is_transferred_only_above_the_declared_score(monkeypatch, delta, status):
    """Positive/negative around ORIENTATION_MIN_SCORE, with no competing angle."""
    from kicadgen.services.component_image_match import ORIENTATION_MIN_SCORE
    assert _orientation_votes(monkeypatch, ORIENTATION_MIN_SCORE + delta, 0.0)["status"] == status


@pytest.mark.parametrize("delta, status", [(0.01, "matched"), (-0.01, "unverified")])
def test_an_angle_is_transferred_only_with_the_declared_margin(monkeypatch, delta, status):
    """Positive/negative around ORIENTATION_MIN_MARGIN over a vote for another angle."""
    from kicadgen.services.component_image_match import ORIENTATION_MIN_MARGIN
    best = 0.97
    result = _orientation_votes(monkeypatch, best, best - ORIENTATION_MIN_MARGIN - delta)
    assert result["status"] == status
