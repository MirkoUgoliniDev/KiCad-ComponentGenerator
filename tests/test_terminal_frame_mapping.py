"""Affine crop/target/local frame contracts, independent of a PCB or package."""
import base64
from copy import deepcopy
from unittest.mock import Mock

import numpy as np
import pytest

from kicadgen.services import photo_terminals as service


def observation():
    return dict(id=1, resolved=True, confidence='high', body_bbox=[.2,.2,.8,.8],
        terminals=[{'bbox':[0,.3,.2,.4]}, {'bbox':[.8,.6,1,.7]}, {'bbox':[.4,0,.5,.2]}])


@pytest.mark.parametrize('rotation', [0,90,180,270])
@pytest.mark.parametrize('target', [[.101,.203,.697,.811], [.223,.159,.617,.487]])
def test_subpixel_crop_mapping_and_rotation_preserve_photo_locations(target, rotation):
    image = np.zeros((107,193,3), dtype=np.uint8)
    _, actual = service.target_crop(image,target)
    entry = observation()
    before = deepcopy(entry)
    mapped = service._to_target_frame(entry, actual, target)
    geometry = service.decode_terminal_geometry(mapped,rotation)
    body_photo_frame = service._local_box(geometry['body_bbox'], -rotation)
    expected_full = [actual[0]+.2*(actual[2]-actual[0]), actual[1]+.2*(actual[3]-actual[1]),
                     actual[0]+.8*(actual[2]-actual[0]), actual[1]+.8*(actual[3]-actual[1])]
    actual_full = [target[0]+body_photo_frame[0]*(target[2]-target[0]),
                   target[1]+body_photo_frame[1]*(target[3]-target[1]),
                   target[0]+body_photo_frame[2]*(target[2]-target[0]),
                   target[1]+body_photo_frame[3]*(target[3]-target[1])]
    assert actual_full == pytest.approx(expected_full)
    assert entry == before


def test_pixel_fringe_does_not_expand_envelope():
    target = [.105,.205,.795,.805]
    _, actual = service.target_crop(np.zeros((100,100,3),np.uint8),target)
    mapped = service._to_target_frame(observation(),actual,target)
    assert mapped['terminals'][0]['bbox'][0] == 0
    assert mapped['terminals'][1]['bbox'][2] == 1


def test_model_receives_only_target_pixels_and_explicit_id(tmp_path):
    cv2 = pytest.importorskip('cv2')
    pixels = np.arange(60*100*3,dtype=np.uint8).reshape(60,100,3)
    png = cv2.imencode('.png',pixels)[1].tobytes()
    component = dict(kind='ic',_box=[.1,.2,.8,.9],rotation_deg=90)
    original = deepcopy(component)
    call = Mock(return_value={'components':[observation()]})
    output = service.observe_terminals(png,[component],'test',call,lambda _:None,tmp_path)
    blocks = call.call_args.kwargs['content_blocks']
    assert blocks[0]['type'] == 'text' and 'Target ID: 1' in blocks[0]['text']
    encoded = blocks[1]['image_url']['url'].split(',',1)[1]
    sent = cv2.imdecode(np.frombuffer(base64.b64decode(encoded),np.uint8),cv2.IMREAD_COLOR)
    np.testing.assert_array_equal(sent,pixels[12:54,10:80])
    assert output[0]['terminal_geometry']['coordinate_frame'] == 'target_image_v2'
    assert component == original


def test_changed_coordinate_prompt_does_not_reuse_previous_response(monkeypatch,tmp_path):
    cv2 = pytest.importorskip('cv2')
    png = cv2.imencode('.png',np.zeros((100,100,3),np.uint8))[1].tobytes()
    call = Mock(return_value={'components':[observation()]})
    args = (png,[dict(kind='ic',_box=[.1,.1,.9,.9])],'model',call,lambda _:None,tmp_path)
    prompt = service.PROMPT
    monkeypatch.setattr(service,'PROMPT','Previous ambiguous frame prompt')
    service.observe_terminals(*args)
    monkeypatch.setattr(service,'PROMPT',prompt)
    service.observe_terminals(*args)
    service.observe_terminals(*args)
    assert call.call_count == 2


def test_invalid_target_preserves_existing_geometry_without_calling_api():
    cv2 = pytest.importorskip('cv2')
    png = cv2.imencode('.png',np.zeros((100,100,3),np.uint8))[1].tobytes()
    component = dict(kind='ic',_box=[.4,.3,.2,.7],terminal_geometry={'existing':True})
    call = Mock(side_effect=AssertionError('Invalid crops must not reach API'))
    assert service.observe_terminals(png,[component],'m',call,lambda _:None) == [component]
    call.assert_not_called()
