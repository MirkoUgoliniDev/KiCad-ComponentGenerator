"""Optional lead metadata survives UI/STEP and changes only the intended solid."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_observed_terminals import cad, component, observation
from test_terminal_metadata_flow import window, step_components


def detail(box):
    return dict(bbox=box,confidence='high',source='photo_separation_v1')


def test_renderer_only_shrinks_selected_terminal_and_preserves_body_and_input(cad):
    renderer,_ = cad
    original = component(terminal_geometry=observation())
    base = renderer.build_component(original,1.6)
    refined = deepcopy(original)
    refined['terminal_geometry']['terminals'][0]['lead_detail'] = detail([.05,.32,.2,.38])
    before = deepcopy(refined)
    result = renderer.build_component(refined,1.6)
    assert result[0][0].boxes == base[0][0].boxes
    assert [s.boxes for s,_ in result[2:]] == [s.boxes for s,_ in base[2:]]
    assert result[1][0].boxes[0] == pytest.approx((5.5,-6.52,1.6,1.5,.24,.08))
    assert refined == before


@pytest.mark.parametrize('bad',[
    detail([.05,.32,.19,.38]), # Disconnected.
    detail([0,.1,.2,.5]), # Expands original envelope.
    dict(bbox=[.05,.32,.2,.38],confidence='low',source='photo_separation_v1'),
    {'bbox':None},
])
def test_bad_detail_keeps_entire_original_component(cad,bad,capsys):
    renderer,_ = cad
    payload = component(terminal_geometry=observation())
    baseline = renderer.build_component(payload,0)
    payload['terminal_geometry']['terminals'][0]['lead_detail'] = bad
    actual = renderer.build_component(payload,0)
    assert [s.boxes for s,_ in actual] == [s.boxes for s,_ in baseline]
    assert 'originale conservato' in capsys.readouterr().out


def test_gui_round_trip_and_step_preserve_optional_detail(window,monkeypatch,tmp_path):
    geometry = observation()
    geometry['terminals'][0]['lead_detail'] = detail([.05,.32,.2,.38])
    source = dict(name='test',kind='generic',x_mm=2,y_mm=-3,length_mm=4,
                  width_mm=2,height_mm=1,rotation_deg=90,terminal_geometry=geometry)
    window._set_photo_components([source])
    components = window._components_from_table()
    assert components[0].terminal_geometry == geometry
    assert components[0].rotation == 90
    payload = step_components(components,monkeypatch,tmp_path)
    assert payload[0]['terminal_geometry'] == geometry


def test_no_new_detail_does_not_invalidate_existing_mesh(window,monkeypatch):
    window._photo_png = b'unchanged'
    source = window._photo_components_from_table()
    window._terminal_worker = SimpleNamespace(only_leads=True,components=deepcopy(source),
        _model_revision=window._model_revision,_photo_snapshot=window._photo_png)
    def forbidden(*args):
        pytest.fail('Unchanged refinement must preserve table and preview')
    monkeypatch.setattr(window,'_set_photo_components',forbidden)
    monkeypatch.setattr(window,'_invalidate_generated_mesh',forbidden)
    window._on_terminal_detail_done(source)
    assert 'anteprima conservate' in window.label_3d_status.text()
    window._terminal_worker = None


def test_optional_worker_dispatches_only_separation_not_reextraction(monkeypatch):
    from kicadgen.gui.main_window import TerminalDetailWorker
    from kicadgen.services import photo_leads, photo_terminals
    calls = []
    def refine(photo,components,model,call,log,cache_dir):
        calls.append((photo,components,model))
        return components
    def forbidden(*args,**kwargs):
        pytest.fail('Lead-only mode must not reobserve body geometry')
    monkeypatch.setattr(photo_leads,'refine_leads',refine)
    monkeypatch.setattr(photo_terminals,'observe_terminals',forbidden)
    worker = TerminalDetailWorker(b'photo',[{'name':'original'}],'test',only_leads=True)
    done = []
    worker.done.connect(done.append)
    worker.run()
    assert calls == [(b'photo',[{'name':'original'}],'test')]
    assert done == [[{'name':'original'}]]
