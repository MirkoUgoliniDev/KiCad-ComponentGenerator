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
    c['model_path'] = 'C:/KiCad/3dmodels/R_0603.step'
    c['model_rotation_deg'] = 45
    c['render_3d'] = False
    c['model_selection'] = dict(status='rejected',path='',rotation_deg=0)
    path = save_choices(tmp_path, b'photo', [c])
    saved_choices = json.loads(path.read_text())['components'][0]
    assert saved_choices['model_selection']['status']=='rejected'
    assert saved_choices['model_path'].endswith('R_0603.step')
    assert saved_choices['model_rotation_deg'] == 45
    assert saved_choices['render_3d'] is False
    fresh=deepcopy(c); fresh.pop('model_selection')
    assert restore_choices(tmp_path,b'photo',[fresh])[0]['model_selection']['status']=='rejected'
    report=save_candidate_report(tmp_path,[c])
    saved_report = json.loads(report.read_text())['components'][0]
    assert saved_report['model_match']['candidates']
    assert saved_report['model_path'].endswith('R_0603.step')
    assert saved_report['model_rotation_deg'] == 45
    assert saved_report['render_3d'] is False
    assert saved_report['rotation_deg'] == 90


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


# ── eliminazione e rotazione manuali: devono tornare dal JSON ─────────

def test_manual_exclusion_and_rotation_survive_the_round_trip(tmp_path):
    """Sono scelte dell'utente come la selezione del modello: il giro dal
    JSON deve restituirle, altrimenti "Rigenera modello 3D" le perde."""
    original = dict(component(), render_3d_override=False, model_rotation_delta_deg=90.0)
    save_choices(tmp_path, b'photo', [original])
    fresh = deepcopy(component())
    restored = restore_choices(tmp_path, b'photo', [fresh])[0]
    assert restored['render_3d_override'] is False
    assert restored['model_rotation_delta_deg'] == 90.0
    assert 'render_3d_override' not in fresh, 'la sorgente non va toccata'


def test_a_photo_or_geometry_change_does_not_carry_the_choice_over(tmp_path):
    """Il negativo: le scelte restano legate a quella foto e a quel corpo."""
    save_choices(tmp_path, b'photo',
                 [dict(component(), render_3d_override=False, model_rotation_delta_deg=90.0)])
    other_photo = restore_choices(tmp_path, b'altra-foto', [deepcopy(component())])[0]
    assert 'render_3d_override' not in other_photo
    moved = deepcopy(component()); moved['length_mm'] = 3.2
    assert 'model_rotation_delta_deg' not in restore_choices(tmp_path, b'photo', [moved])[0]


def test_a_malformed_stored_choice_falls_back_to_the_automatic_behaviour(tmp_path):
    """Un JSON manomesso non deve imporre stati impossibili al costruttore."""
    save_choices(tmp_path, b'photo', [component()])
    target = tmp_path / 'kicad-model-choices.json'
    data = json.loads(target.read_text(encoding='utf-8'))
    data['components'][0]['render_3d_override'] = 'forse'
    data['components'][0]['model_rotation_delta_deg'] = 'novanta'
    target.write_text(json.dumps(data), encoding='utf-8')
    restored = restore_choices(tmp_path, b'photo', [deepcopy(component())])[0]
    assert 'render_3d_override' not in restored
    assert 'model_rotation_delta_deg' not in restored
