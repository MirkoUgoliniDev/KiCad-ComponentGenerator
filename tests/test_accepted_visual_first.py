"""@brief Visual archive discovery precedes physical validation without bypassing it."""
import cv2
import numpy as np
import pytest
from test_accepted_models import library, _accepted, _body, R0603, R0805, SOT6
from kicadgen.services import accepted_models as am
from kicadgen.services.component_image_match import describe_crop, descriptor_bytes
from kicadgen.cad3d import kicad_models as km


def picture():
    crop = np.zeros((96, 96, 3), np.uint8)
    cv2.rectangle(crop, (15, 25), (70, 65), (100, 100, 100), -1)
    cv2.rectangle(crop, (10, 5), (24, 35), (255, 255, 255), -1)
    cv2.circle(crop, (65, 60), 8, (230, 230, 230), -1)
    return crop


def save(library, model=R0603, **extra):
    crop = picture()
    row = _accepted(library, model, **extra)
    row['_accepted_image_png'] = cv2.imencode('.png', crop)[1].tobytes()
    row['_accepted_image_descriptor'] = descriptor_bytes(describe_crop(crop))
    am.AcceptedModelStore().consolidate([row], [library])


@pytest.mark.parametrize('change', [dict(kind='capacitor'), dict(length=4),
    dict(pin_count=3, pin_count_confidence='high'), dict(bbox_basis='pads')])
def test_identical_crop_survives_conflicting_evidence_for_review(library, change):
    save(library, pin_count=2, pin_count_confidence='high')
    query = _body(**change)
    query['_accepted_image_descriptor'] = descriptor_bytes(describe_crop(picture()))
    result = am.AcceptedModelStore().find(query, [library])
    assert result['review_required'] and result['visual_conflicts']
    assert result['visual_first'] and result['visual_score'] > .99
    report = km._accepted_match(query, [library], lambda _: None)
    assert report['status'] == 'review_required'
    assert report['selected_path'] is None
    assert report['candidates'][0]['path'].endswith('R_0603_1608Metric.step')


def test_missing_terminal_observation_preserves_visual_candidate(library):
    save(library, SOT6, kind='ic', pin_count=6, pin_count_confidence='high')
    query = _body(kind='ic', _accepted_image_descriptor=descriptor_bytes(describe_crop(picture())))
    found = am.AcceptedModelStore().find(query, [library])
    assert found['review_required'] and not found['terminal_count_checked']


@pytest.mark.parametrize('turns', [0, 1, 2, 3])
def test_rotated_crop_with_compatible_evidence_can_match(library, turns):
    save(library)
    query = _body(_accepted_image_descriptor=descriptor_bytes(describe_crop(np.rot90(picture(), turns).copy())))
    result = am.AcceptedModelStore().find(query, [library])
    assert result['visual_first'] and not result['review_required']
    assert result['visual_score'] > .99


def test_identical_images_for_different_models_are_ambiguous(library):
    save(library, R0603)
    save(library, R0805)
    query = _body(_accepted_image_descriptor=descriptor_bytes(describe_crop(picture())))
    result = am.AcceptedModelStore().find(query, [library])
    assert result['review_required']
    assert any('ambigua' in reason for reason in result['visual_conflicts'])


def test_conflicting_visual_candidate_never_attaches_despite_library_estimate(library, monkeypatch):
    save(library)
    query = _body(kind='capacitor', _accepted_image_descriptor=descriptor_bytes(describe_crop(picture())))
    monkeypatch.setattr(km, '_library_search', lambda *args, **kwargs: {
        'status': 'auto_matched', 'selected_path': str(library / R0805), 'candidates': []})
    records = []
    with km.archive_progress_listener(lambda done, total, rows: records.append(rows)):
        assert km.attach_models([query], library=library) == 0
    assert not query.get('model_path')
    assert query['model_match']['status'] == 'review_required'
    assert records[-1][0]['status'] == 'review_required'
    assert records[-1][0]['conflicts']


def test_bad_query_descriptor_falls_back_to_mechanical_match(library):
    save(library)
    found = am.AcceptedModelStore().find(_body(_accepted_image_descriptor=b'broken'), [library])
    assert found and not found.get('visual_first')


def test_complete_example_of_same_model_wins_over_missing_pin_count(library):
    save(library, SOT6, kind='ic')
    save(library, SOT6, kind='ic', pin_count=6, pin_count_confidence='high')
    query = _body(kind='ic', pin_count=6, pin_count_confidence='high',
                  _accepted_image_descriptor=descriptor_bytes(describe_crop(picture())))
    found = am.AcceptedModelStore().find(query, [library])
    assert found['terminal_count_checked'] and not found['review_required']


def test_archive_prepass_caches_result_before_library_search(library, monkeypatch):
    save(library)
    query = _body(_accepted_image_descriptor=descriptor_bytes(describe_crop(picture())))
    calls = []
    original = km._accepted_match
    def match(*args, **kwargs):
        calls.append('archive')
        return original(*args, **kwargs)
    def search(*args, **kwargs):
        calls.append('library')
        return {'status': 'unresolved', 'candidates': [], 'selected_path': None}
    monkeypatch.setattr(km, '_accepted_match', match)
    monkeypatch.setattr(km, '_library_search', search)
    assert km.attach_models([query], library=library) == 1
    assert calls == ['archive', 'library']
