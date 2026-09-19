"""@brief Regression tests for photo-scoped manual CAD decisions."""
from copy import deepcopy
import json
from kicadgen.services.model_choices import save_choices, restore_choices, save_candidate_report


def component():
    return dict(name='R1', kind='generic', _detected_kind='resistor', package='0603',
                x_mm=1, y_mm=2, length_mm=1.6, width_mm=.8, rotation_deg=90,
                bbox_basis='body', model_selection=dict(status='confirmed', path='R_0603.step', rotation_deg=90),
                model_match=dict(status='review_required', candidates=[{'path':'R_0603.step'}]))


def test_restore_same_photo_not_other_body_or_photo(tmp_path):
    original = component()
    save_choices(tmp_path, b'photo', [original])
    fresh = deepcopy(original)
    fresh.pop('model_selection'); fresh.pop('model_match')
    assert restore_choices(tmp_path, b'photo', [fresh])[0]['model_selection'] == original['model_selection']
    assert 'model_selection' not in fresh
    assert 'model_selection' not in restore_choices(tmp_path, b'other', [fresh])[0]
    fresh['length_mm'] = 3.2
    assert 'model_selection' not in restore_choices(tmp_path, b'photo', [fresh])[0]


def test_rejected_choice_survives_and_report_is_json(tmp_path):
    c = component()
    c['model_selection'] = dict(status='rejected',path='',rotation_deg=0)
    path = save_choices(tmp_path, b'photo', [c])
    assert json.loads(path.read_text())['components'][0]['model_selection']['status']=='rejected'
    fresh=deepcopy(c); fresh.pop('model_selection')
    assert restore_choices(tmp_path,b'photo',[fresh])[0]['model_selection']['status']=='rejected'
    report=save_candidate_report(tmp_path,[c])
    assert json.loads(report.read_text())['components'][0]['model_match']['candidates']


def test_reordering_does_not_reassign_selection(tmp_path):
    first=component(); other=deepcopy(first); other['x_mm']=9
    other['model_selection']=dict(status='rejected',path='',rotation_deg=0)
    save_choices(tmp_path,b'photo',[first,other])
    fresh=[deepcopy(other),deepcopy(first)]
    for c in fresh:c.pop('model_selection')
    restored=restore_choices(tmp_path,b'photo',fresh)
    assert [c['model_selection']['status'] for c in restored]==['rejected','confirmed']


def test_duplicate_geometry_does_not_transfer_choices(tmp_path):
    c=component()
    save_choices(tmp_path,b'photo',[c])
    fresh=deepcopy(c); fresh.pop('model_selection')
    result=restore_choices(tmp_path,b'photo',[fresh,deepcopy(fresh)])
    assert all('model_selection' not in row for row in result)
