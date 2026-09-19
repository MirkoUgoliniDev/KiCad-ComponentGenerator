"""@brief Preserve reviewed native placement without moving edge connectors."""
import pytest
from kicadgen.cad3d import step_generator as cad, kicad_models
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.castellated_module import CastellatedModuleParams, CastellatedSide, ComponentBox
from kicadgen.generators.pin_header import PinHeaderParams


@pytest.mark.parametrize('layout',['castellated','matrix'])
def test_explicit_native_orientation_and_count_evidence_survive_payload(monkeypatch,tmp_path,layout):
    component=ComponentBox(x=-19,y=0,length=8,width=6,height=3,mating='edge',
        model_rotation_deg=-90,model_z_offset_mm=.2,pin_count=6,
        pin_count_confidence='medium',pin_count_source='photo_observation')
    captured={}
    monkeypatch.setattr(kicad_models,'find_library',lambda:None)
    def build(*args,**kwargs):
        captured.update(kwargs['extra_payload'])
        return {'warnings':[]}
    monkeypatch.setattr(cad,'_run_freecad',build)
    body=Body3DParams(length_mm=40,width_mm=20,height_mm=5,populated_board=True,components=[component])
    if layout=='castellated':
        params=CastellatedModuleParams(name='Placement',body_length=40,body_width=20,
            sides=[CastellatedSide(edge='left',count=2,pitch=2.54)],components=[component])
        cad.generate_castellated_3d_model(params,body,tmp_path)
        assert captured['components'][0]['x']==-19
    else:
        cad.generate_3d_model(PinHeaderParams(),body,tmp_path)
    result=captured['components'][0]
    assert result['model_rotation_deg']==-90 and result['model_z_offset_mm']==.2
    assert result['pin_count_source']=='photo_observation'
    assert result['pin_count_confidence']=='medium'
    assert 'model_path' not in result
    assert result['model_match']['status']=='unresolved'
    assert result['color_hex']=='#FF00FF'


@pytest.mark.parametrize('rotation',[None,True,float('nan'),float('inf'),10**400])
def test_unknown_or_invalid_native_rotation_does_not_override_inference(rotation):
    component=ComponentBox(0,0,1,1,1,model_rotation_deg=rotation)
    assert 'model_rotation_deg' not in cad._model_placement(component)


@pytest.mark.parametrize('layout', ['castellated', 'matrix'])
@pytest.mark.parametrize('selection_status', [None, 'confirmed', 'rejected', 'missing'])
def test_review_selection_contract_reaches_cad_payload(monkeypatch, tmp_path, layout, selection_status):
    model = tmp_path / 'Resistor_SMD.3dshapes' / 'R_0603.step'
    model.parent.mkdir()
    model.touch()
    monkeypatch.setattr(kicad_models, 'find_library', lambda: tmp_path)
    monkeypatch.setattr(kicad_models, 'model_sizes', lambda *a, **k: {str(model): {'body': [1.6, .8, .4]}})
    monkeypatch.setattr(kicad_models, 'footprint_sizes', lambda *a, **k: {})
    selection = None if selection_status is None else {
        'status': 'confirmed' if selection_status == 'missing' else selection_status,
        'path': str(model.with_name('missing.step') if selection_status == 'missing' else model),
        'rotation_deg': -90,
    }
    component = ComponentBox(0, 0, 1.6, .8, .4, kind='resistor', package='R_0603',
        bbox_basis='body', rotation=37, model_rotation_deg=45, model_selection=selection)
    captured = {}
    def build(*args, **kwargs):
        captured.update(kwargs['extra_payload'])
        return {'warnings': []}
    monkeypatch.setattr(cad, '_run_freecad', build)
    body = Body3DParams(length_mm=40, width_mm=20, height_mm=5, populated_board=True, components=[component])
    if layout == 'castellated':
        params = CastellatedModuleParams(name='Selection', body_length=40, body_width=20,
            sides=[CastellatedSide(edge='left', count=2, pitch=2.54)], components=[component])
        cad.generate_castellated_3d_model(params, body, tmp_path)
    else:
        cad.generate_3d_model(PinHeaderParams(), body, tmp_path)
    result = captured['components'][0]
    assert result['rotation_deg'] == 37
    assert result['model_selection'] == selection
    if selection_status == 'confirmed':
        assert result['model_path'] == str(model)
        assert result['model_rotation_deg'] == -90
        assert result['model_match']['status'] == 'confirmed'
    else:
        assert 'model_path' not in result
        assert result['model_rotation_deg'] == 45
        assert result['color_hex'] == '#FF00FF'
        expected = 'review_required' if selection_status is None else 'unresolved'
        assert result['model_match']['status'] == expected
        if selection_status is None:
            assert result['model_match']['candidates'][0]['dimensions_mm'] == [1.6, .8, .4]
