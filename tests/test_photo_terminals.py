"""Photo-to-local terminal geometry and active extractor regression contracts."""
from copy import deepcopy
from unittest.mock import Mock

import numpy as np
import pytest

from kicadgen.services.photo_terminals import decode_terminal_geometry, observe_terminals
from kicadgen.services.openrouter import OpenRouterError


def observation(identifier=1):
    return dict(id=identifier, resolved=True, confidence='high', body_bbox=[.2,.2,.8,.8],
        terminals=[dict(bbox=[0,.3,.2,.4]), dict(bbox=[.8,.6,1,.7]),
                   dict(bbox=[.4,0,.5,.2])])


def component():
    return dict(name='Package', kind='generic', _detected_kind='ic',
        _box=[.1,.1,.9,.9], x_mm=1, y_mm=-2, length_mm=8,
        width_mm=4, height_mm=1, rotation_deg=0)


@pytest.fixture
def png():
    cv2 = pytest.importorskip('cv2')
    return cv2.imencode('.png', np.full((100,200,3), 70, dtype=np.uint8))[1].tobytes()


@pytest.mark.parametrize('rotation,expected', [
    (0,[0,.3,.2,.4]), (90,[.3,.8,.4,1]),
    (180,[.8,.6,1,.7]), (270,[.6,0,.7,.2]),
])
def test_asymmetric_terminal_transforms_to_local_frame_once(rotation, expected):
    entry = observation()
    before = deepcopy(entry)
    geometry = decode_terminal_geometry(entry, rotation)
    assert geometry['terminals'][0]['bbox'] == pytest.approx(expected)
    assert entry == before


@pytest.mark.parametrize('bad', [None, [], [0,0,0,1], [-.1,0,1,1],
    [0,0,2,1], [True,0,1,1], [0,0,float('nan'),1], [0,0,float('inf'),1]])
def test_invalid_rectangles_rejected(bad):
    entry = observation()
    entry['terminals'][0]['bbox'] = bad
    with pytest.raises(ValueError):
        decode_terminal_geometry(entry, 0)


@pytest.mark.parametrize('rotation', [True, float('nan'), 13, '90'])
def test_noncardinal_or_invalid_rotations_rejected(rotation):
    with pytest.raises(ValueError):
        decode_terminal_geometry(observation(), rotation)


def test_no_pin_count_inference_from_unresolved_low_confidence_or_body_marks():
    entry = observation()
    entry.update(resolved=False, pin_count=64)
    assert decode_terminal_geometry(entry, 0) is None
    entry.update(resolved=True, confidence='low')
    assert decode_terminal_geometry(entry, 0) is None
    entry.update(confidence='high')
    entry['terminals'][0]['bbox'] = [.3,.3,.4,.4]
    with pytest.raises(ValueError, match='dentro'):
        decode_terminal_geometry(entry, 0)


def test_duplicate_terminal_boxes_rejected():
    entry = observation()
    entry['terminals'][1] = deepcopy(entry['terminals'][0])
    with pytest.raises(ValueError, match='sovrapposti'):
        decode_terminal_geometry(entry, 0)


def test_call_cache_and_metadata_preserved(png, tmp_path):
    original = [component()]
    original[0]['_id'] = 99
    original[0]['pin_count'] = 123
    before = deepcopy(original)
    call = Mock(return_value={'components':[observation()]})
    messages = []
    first = observe_terminals(png, original, 'photo-model', call, messages.append, tmp_path)
    second = observe_terminals(png, original, 'photo-model', call, messages.append, tmp_path)
    assert first == second
    assert len(first[0]['terminal_geometry']['terminals']) == 3
    assert {k:v for k,v in first[0].items() if k != 'terminal_geometry'} == before[0]
    assert original == before
    assert call.call_count == 1
    # Model change must invalidate a previously reusable response.
    observe_terminals(png, original, 'other-model', call, messages.append, tmp_path)
    assert call.call_count == 2


@pytest.mark.parametrize('response', [
    {'components':[]}, {'components':[observation(),observation()]},
    {'components':[observation(9)]}, {'components':[dict(observation(),id=True)]},
    {'components':[dict(observation(),resolved=False)]},
])
def test_missing_duplicate_or_unresolved_ids_preserve_input(png, response):
    original = [component()]
    assert observe_terminals(png, original, 'm', Mock(return_value=response), lambda m:None) == original


def test_api_failure_preserves_geometry(png):
    original = [component()]
    messages = []
    actual = observe_terminals(png, original, 'm', Mock(side_effect=OpenRouterError('offline')), messages.append)
    assert actual == original
    assert any('fallita' in m for m in messages)


def test_active_datasheet_extraction_invokes_terminal_pass_and_retains_geometry(monkeypatch, png, tmp_path):
    from kicadgen.services import datasheet_extractor as ext, photo_inventory as inv
    path = tmp_path / 'photo.png'
    path.write_bytes(png)
    report = inv.InventoryResult(components=[component()], photo_png=png, photo='photo.png')
    monkeypatch.setattr(ext, 'api_key', lambda:'offline')
    monkeypatch.setattr(inv, 'extract_inventory', lambda *a,**kw: report)
    def call(**kw):
        if kw['tool']['function']['name'] == 'observe_package_terminals':
            assert kw['model'] == 'selected-photo-model'
            return {'components':[observation()]}
        return dict(layout='castellated', body_length_mm=40, body_width_mm=20,
                    castellated_sides=[dict(edge='left',count=2,pitch_mm=2.54)])
    monkeypatch.setattr(ext, 'call_tool', call)
    result = ext.extract_pin_header_params([path], vision_model='selected-photo-model', photo_cache_dir=tmp_path/'cache')
    assert len(result.components_3d[0]['terminal_geometry']['terminals']) == 3
    assert result.components_3d[0]['_detected_kind'] == 'ic'
    assert '1 con terminali osservati' in result.notes
    assert result.photo_png == png
