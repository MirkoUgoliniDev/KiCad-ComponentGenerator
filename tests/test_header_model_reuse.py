"""Header-only regeneration reuses proven choices, never stale board data."""

from copy import deepcopy
from dataclasses import replace

import pytest

from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.header_reuse import HeaderModelReuse, REUSE_PROGRESS
from kicadgen.cad3d import step_generator as sg
from kicadgen.generators.pin_header import PinHeaderParams
from tests.test_host_mounting import module


@pytest.mark.parametrize('binary', [bytes, bytearray])
def test_photo_descriptor_is_keyed_and_header_change_can_reuse(tmp_path, binary):
    from kicadgen.generators.castellated_module import ComponentBox
    params = module()
    params.components = [ComponentBox(x=0, y=0, length=2, width=1, height=1,
        match_evidence={'_accepted_image_descriptor': binary(b'\x00\xffphoto descriptor')})]
    body = Body3DParams(30, 20, 3, show_pins_3d=False)
    output = tmp_path / 'result.step'
    output.write_text('solid')
    cache = HeaderModelReuse(tmp_path, params, body, [])
    cache.save([], {'step_path': output})
    target = replace(body, show_pins_3d=True)
    assert cache.path.exists()
    assert HeaderModelReuse(tmp_path, params, target, []).load() == []
    params.components[0].match_evidence['_accepted_image_descriptor'] = binary(b'changed photo')
    assert HeaderModelReuse(tmp_path, params, target, []).load() is None


@pytest.mark.parametrize('castellated', [True, False])
def test_header_change_skips_matching_but_rebuilds_step_with_same_components(tmp_path, monkeypatch, castellated):
    params = module() if castellated else PinHeaderParams(name='Board')
    body = Body3DParams(30, 20, 3, populated_board=True, show_pins_3d=False)
    generator = sg.generate_castellated_3d_model if castellated else sg.generate_3d_model
    searches, builds, messages = [], [], []
    source = tmp_path / 'selected.step'
    source.write_text('existing model')
    def match(components, *args, **kwargs):
        searches.append(True)
        components.append(dict(name='Known', model_path=str(source), render_3d=False))
    def build(name, pins, body, output_dir, **kwargs):
        builds.append((body.show_pins_3d, deepcopy(kwargs['extra_payload']['components'])))
        path = tmp_path / 'Board.step'
        path.write_text(f'new solid, headers={body.show_pins_3d}')
        return dict(step_path=path, warnings=[])
    monkeypatch.setattr(sg, 'attach_library_models', match)
    monkeypatch.setattr(sg, '_run_freecad', build)
    generator(params, body, tmp_path, progress=messages.append)
    generator(params, replace(body, show_pins_3d=True), tmp_path, progress=messages.append)
    assert len(searches) == 1 and len(builds) == 2
    assert builds[0][1] == builds[1][1]
    assert [item[0] for item in builds] == [False, True]
    assert REUSE_PROGRESS in messages
    assert 'headers=True' in (tmp_path / 'Board.step').read_text()
    # Ordinary regeneration must still allow a fresh library search.
    generator(params, replace(body, show_pins_3d=True), tmp_path)
    assert len(searches) == 2


@pytest.mark.parametrize('change', ['body', 'contacts', 'annotations', 'source', 'output', 'corrupt', 'same'])
def test_changed_inputs_or_files_do_not_reuse(tmp_path, change):
    params = module()
    body = Body3DParams(30, 20, 3, show_pins_3d=False)
    source = tmp_path / 'source.step'
    output = tmp_path / 'result.step'
    source.write_text('source')
    output.write_text('result')
    cache = HeaderModelReuse(tmp_path, params, body, [])
    cache.save([{'model_path': str(source)}], {'step_path': output})
    target = replace(body, show_pins_3d=True)
    context = []
    if change == 'body':
        target = replace(target, height_mm=4)
    elif change == 'contacts':
        params.sides[0].pitch = 3
    elif change == 'annotations':
        context = ['fillet']
    elif change == 'source':
        source.write_text('modified source')
    elif change == 'output':
        output.unlink()
    elif change == 'corrupt':
        cache.path.write_text('{}')
    elif change == 'same':
        target = body
    assert HeaderModelReuse(tmp_path, params, target, context).load() is None


def test_failed_generation_cannot_publish_new_reuse_state(tmp_path, monkeypatch):
    params, body = module(), Body3DParams(30, 20, 3, show_pins_3d=False)
    monkeypatch.setattr(sg, 'attach_library_models', lambda *a, **k: None)
    def fail(*a, **k):
        raise sg.Cad3DError('cancelled')
    monkeypatch.setattr(sg, '_run_freecad', fail)
    with pytest.raises(sg.Cad3DError):
        sg.generate_castellated_3d_model(params, body, tmp_path)
    assert not (tmp_path / '.header-model-reuse.json').exists()


def test_reuse_progress_shows_only_solid_step(window):
    from kicadgen.gui.workers import Cad3DWorker
    worker = Cad3DWorker(module(), Body3DParams(30, 20, 3), window._project_dir(), [], [])
    worker.phase.connect(window._on_phase)
    window.analysis_overlay.set_steps(['archive', 'library', 'solid'])
    worker._cad_progress(REUSE_PROGRESS)
    assert len(window.analysis_overlay._step_labels) == 1
    assert window._generation_step_of == {'freecad': 0}


def test_gui_model_report_feedback_keeps_header_reuse_valid(window, tmp_path, monkeypatch):
    window._set_photo_components([dict(name='R1', package='R_0603', kind='resistor',
        x_mm=0, y_mm=0, length_mm=1.6, width_mm=.8, height_mm=.5)])
    params = module()
    params.components = window._components_from_table()
    body = Body3DParams(30, 20, 3, show_pins_3d=False)
    calls = []
    def match(components, *a, **k):
        calls.append(True)
        components[0].update(model_match={'status': 'matched'},
                             model_rotation_deg=90, rotation_deg=90)
    def build(name, *a, **k):
        step = tmp_path / (name + '.3dshapes') / (name + '.step')
        step.parent.mkdir(exist_ok=True)
        step.write_text('solid')
        return dict(step_path=step, warnings=[])
    monkeypatch.setattr(sg, 'attach_library_models', match)
    monkeypatch.setattr(sg, '_run_freecad', build)
    result = sg.generate_castellated_3d_model(params, body, tmp_path)
    window._show_generated_mesh(result['step_path'])
    params.components = window._components_from_table()
    assert params.components[0].rotation == 90
    sg.generate_castellated_3d_model(params, replace(body, show_pins_3d=True), tmp_path)
    assert len(calls) == 1
    params.components[0].model_rotation_delta_deg = 15
    sg.generate_castellated_3d_model(params, body, tmp_path)
    assert len(calls) == 2
