"""A review's photograph, boxes and scale commit together, including on failure."""
import json

import pytest

from kicadgen.services import photo_inventory as inv


def _row(identity):
    return {'_id': identity, '_box': [.1, .1, .4, .4]}


def test_failed_manifest_replacement_keeps_previous_photo_boxes_and_scale(tmp_path, monkeypatch):
    inv.save_review_bundle(tmp_path, [_row(1)], b'old photo', 10, 5)
    old = inv.load_review_bundle(tmp_path)
    original = inv._atomic_write_bytes

    def fail_manifest(path, payload):
        if path.name == inv.REVIEW_BUNDLE_JSON:
            # Simulate another reader, or recovery after a crash, in the
            # interval between writing the new image and committing JSON.
            during = inv.load_review_bundle(tmp_path)
            assert during.photo_path.read_bytes() == b'old photo'
            assert during.proposals == [_row(1)]
            raise OSError('disk full')
        original(path, payload)

    monkeypatch.setattr(inv, '_atomic_write_bytes', fail_manifest)
    with pytest.raises(OSError, match='disk full'):
        inv.save_review_bundle(tmp_path, [_row(2)], b'new photo', 20, 10)
    after = inv.load_review_bundle(tmp_path)
    assert after.proposals == old.proposals
    assert (after.body_length_mm, after.body_width_mm) == (10, 5)
    assert after.photo_path == old.photo_path
    assert after.photo_path.read_bytes() == b'old photo'
    assert list(after.photo_path.parent.glob('*.png')) == [old.photo_path]


@pytest.mark.parametrize('explicit_reference', [False, True])
def test_legacy_photo_stays_readable_and_is_kept_when_only_boxes_change(tmp_path, explicit_reference):
    folder = inv.review_bundle_dir(tmp_path)
    folder.mkdir()
    document = dict(format=1, body_length_mm=10, body_width_mm=5, proposals=[_row(1)])
    if explicit_reference:
        document['photo'] = 'photo.png'
    (folder / 'photo.png').write_bytes(b'legacy photo')
    (folder / inv.REVIEW_BUNDLE_JSON).write_text(json.dumps(document), encoding='utf-8')
    assert inv.load_review_bundle(tmp_path).photo_path.read_bytes() == b'legacy photo'
    inv.save_review_bundle(tmp_path, [_row(2)], None, 10, 5)
    bundle = inv.load_review_bundle(tmp_path)
    assert bundle.proposals == [_row(2)]
    assert bundle.photo_path.name == 'photo.png'
    assert bundle.photo_path.read_bytes() == b'legacy photo'


def test_successful_replacement_commits_new_photo_and_removes_old_one(tmp_path):
    inv.save_review_bundle(tmp_path, [_row(1)], b'old photo', 10, 5)
    old = inv.load_review_bundle(tmp_path)
    inv.save_review_bundle(tmp_path, [_row(2)], b'new photo', 20, 10)
    new = inv.load_review_bundle(tmp_path)
    assert new.proposals == [_row(2)]
    assert (new.body_length_mm, new.body_width_mm) == (20, 10)
    assert new.photo_path != old.photo_path
    assert new.photo_path.read_bytes() == b'new photo'
    assert not old.photo_path.exists()


@pytest.mark.parametrize('reference', ['../outside.png', 'C:/outside.png', 'photo-other.png'])
def test_manifest_cannot_point_outside_the_bundle(tmp_path, reference):
    path = inv.save_review_bundle(tmp_path, [_row(1)], b'photo', 10, 5)
    document = json.loads(path.read_text(encoding='utf-8'))
    document['photo'] = reference
    path.write_text(json.dumps(document), encoding='utf-8')
    assert inv.load_review_bundle(tmp_path).photo_path is None


def test_failed_atomic_replace_removes_temporary_file(tmp_path, monkeypatch):
    from kicadgen import fsutil

    def fail(*args):
        raise OSError('disk busy')

    monkeypatch.setattr(fsutil, 'replace_file', fail)
    with pytest.raises(OSError, match='disk busy'):
        inv._atomic_write_bytes(tmp_path / 'target', b'data')
    assert list(tmp_path.iterdir()) == []


def test_explicit_empty_review_replaces_old_boxes_and_retains_photo_and_scale(tmp_path):
    inv.save_review_bundle(tmp_path, [_row(1)], b'photo', 10, 5)
    old = inv.load_review_bundle(tmp_path)
    inv.save_review_bundle(tmp_path, [], None, 10, 5, allow_empty=True)
    # A new read (including after restart) sees a real bundle, blocking the
    # caller's "bundle absent -> copy old cache" fallback.
    empty = inv.load_review_bundle(tmp_path)
    assert empty is not None and empty.proposals == [] and empty.reviewed
    assert (empty.body_length_mm, empty.body_width_mm) == (10, 5)
    assert empty.photo_path == old.photo_path
    assert empty.photo_path.read_bytes() == b'photo'
    document = json.loads(empty.json_path.read_text(encoding='utf-8'))
    assert document['review_empty'] is True and document['count'] == 0
    # Drawing a new box later replaces the empty decision normally.
    inv.save_review_bundle(tmp_path, [_row(2)], None, 10, 5)
    restored = inv.load_review_bundle(tmp_path)
    assert restored.proposals == [_row(2)]
    assert 'review_empty' not in json.loads(restored.json_path.read_text(encoding='utf-8'))


@pytest.mark.parametrize('rows, options', [
    ([], {}),
    ([], {'allow_empty': True, 'reviewed': False}),
    ([], {'allow_empty': True, 'reviewed': 1}),
    ([{'_id': 1}], {'allow_empty': True}),
])
def test_empty_permission_does_not_turn_invalid_data_into_a_review(tmp_path, rows, options):
    inv.save_review_bundle(tmp_path, [_row(1)], b'photo', 10, 5)
    with pytest.raises(ValueError, match='Nessuna proposta valida'):
        inv.save_review_bundle(tmp_path, rows, None, 10, 5, **options)
    assert inv.load_review_bundle(tmp_path).proposals == [_row(1)]


@pytest.mark.parametrize('changes', [
    {'review_empty': False},
    {'review_empty': 1},
    {'reviewed': False},
    {'reviewed': 1},
    {'proposals': [{'_id': 1}]},
    {'proposals': None},
    {'count': 1},
    {'count': False},
])
def test_loader_requires_an_explicit_valid_empty_review(tmp_path, changes):
    path = inv.save_review_bundle(tmp_path, [], b'photo', 10, 5, allow_empty=True)
    document = json.loads(path.read_text(encoding='utf-8'))
    document.update(changes)
    path.write_text(json.dumps(document), encoding='utf-8')
    assert inv.load_review_bundle(tmp_path) is None


@pytest.mark.parametrize('detector', ['yolo', 'yolo_smd'])
def test_relaunching_empty_proposals_cannot_trigger_paid_enumeration(detector):
    import numpy as np

    def forbidden(**kwargs):
        pytest.fail('Paid call for an empty reviewed selection')

    with pytest.raises(ValueError, match='Nessuna proposta valida'):
        inv.reconcile_saved_proposals(np.zeros((10, 20, 3), np.uint8), [], 10, 5,
                                      'offline/model', forbidden, lambda _: None,
                                      detector=detector)
