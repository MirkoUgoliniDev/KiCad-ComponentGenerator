"""Lead refinement must not overwrite the established body/terminal geometry."""
from copy import deepcopy
import json
from unittest.mock import Mock

import numpy as np
import pytest

from kicadgen.cad3d.lead_geometry import validated_lead_bbox
from kicadgen.services.photo_leads import refine_leads, _frame_box
from kicadgen.services.photo_terminals import _local_box
from kicadgen.services.openrouter import OpenRouterError

BODY = [.2,.2,.8,.8]
ENVELOPES = [[0,.3,.2,.5],[.8,.4,1,.6],[.4,0,.6,.2],[.3,.8,.5,1]]
LEADS = [[.05,.35,.2,.45],[.8,.45,.95,.55],[.45,.05,.55,.2],[.35,.8,.45,.95]]


def detail(box):
    return dict(bbox=box,confidence='high',source='photo_separation_v1')


def component(rotation=0):
    return dict(name='arbitrary component',kind='generic',x_mm=3,y_mm=-7,
        length_mm=8,width_mm=5,height_mm=1.4,rotation_deg=rotation,
        _box=[.1,.2,.9,.8], terminal_geometry=dict(body_bbox=BODY.copy(),confidence='high',
        source='photo_observation',coordinate_frame='target_image_v2',
        terminals=[dict(bbox=b.copy()) for b in ENVELOPES]))


@pytest.fixture
def png():
    cv2 = pytest.importorskip('cv2')
    return cv2.imencode('.png',np.zeros((100,200,3),np.uint8))[1].tobytes()


@pytest.mark.parametrize('envelope,lead',list(zip(ENVELOPES,LEADS)))
def test_distinct_face_connected_leads_stay_in_original_envelope(envelope,lead):
    assert validated_lead_bbox(BODY,envelope,detail(lead)) == lead


@pytest.mark.parametrize('bad', [
    dict(bbox=LEADS[0],confidence='low',source='photo_separation_v1'),
    dict(bbox=LEADS[0],confidence='high',source='guess'),
    detail([-.01,.35,.2,.45]), detail([.05,.25,.2,.45]),
    detail([.05,.35,.19,.45]), detail([0,.1,.2,.2]),
    detail([.2,.35,.3,.45]), detail([.05,.35,.2,float('nan')]),
    detail([True,.35,.2,.45]), detail([.05,.35,.05,.45]),
])
def test_untrusted_expanded_disconnected_or_degenerate_details_rejected(bad):
    with pytest.raises(ValueError):
        validated_lead_bbox(BODY,ENVELOPES[0],bad)


def test_round_trip_noise_snaps_but_real_gap_is_rejected():
    assert validated_lead_bbox(BODY,ENVELOPES[0],detail([.05,.35,.2-1e-12,.45]))[2] == .2
    with pytest.raises(ValueError):
        validated_lead_bbox(BODY,ENVELOPES[0],detail([.05,.35,.2-1e-5,.45]))


@pytest.mark.parametrize('rotation',[0,90,180,270])
def test_refinement_preserves_all_original_geometry_and_rotation(png,rotation,tmp_path):
    original = [component(rotation)]
    before = deepcopy(original)
    response = {'terminals':[dict(id=i+1,resolved=True,confidence='high',
        lead_bbox=_local_box(box,-rotation)) for i,box in enumerate(LEADS)]}
    call = Mock(return_value=response)
    result = refine_leads(png,original,'model',call,lambda _:None,tmp_path)
    for terminal,lead in zip(result[0]['terminal_geometry']['terminals'],LEADS):
        assert terminal.pop('lead_detail')['bbox'] == pytest.approx(lead)
    assert result == before == original
    reference = json.loads(call.call_args.kwargs['content_blocks'][0]['text'])
    assert len(reference['terminals']) == len(ENVELOPES)


@pytest.mark.parametrize('response', [
    {'terminals':[]}, {'terminals':[dict(id=1,resolved=False,confidence='low',lead_bbox=[0,0,1,1])]},
    {'terminals':[dict(id=True,resolved=True,confidence='high',lead_bbox=LEADS[0])]},
    {'terminals':[dict(id=99,resolved=True,confidence='high',lead_bbox=LEADS[0])]},
    {'terminals':[dict(id=1,resolved=True,confidence='high',lead_bbox=[0,0,1,1])]},
    {'terminals':[dict(id=1,resolved=True,confidence='high',lead_bbox=LEADS[0])]*2},
    {'terminals':[dict(id=1,resolved=True,confidence='high',lead_bbox=[.05,.35,.19,.45])]},
])
def test_ambiguous_or_invalid_response_keeps_previous_details(png,response):
    original = [component()]
    original[0]['terminal_geometry']['terminals'][0]['lead_detail'] = detail(LEADS[0])
    before = deepcopy(original)
    assert refine_leads(png,original,'m',Mock(return_value=response),lambda _:None) == before
    assert original == before


def test_network_failure_keeps_whole_input(png):
    original = [component()]
    messages = []
    result = refine_leads(png,original,'m',Mock(side_effect=OpenRouterError('offline')),messages.append)
    assert result == original
    assert any('non disponibile' in m for m in messages)


def test_no_known_terminals_means_no_extra_api_call(png):
    original = [dict(kind='generic',_box=[.1,.1,.9,.9])]
    call = Mock(side_effect=AssertionError('must not infer terminals'))
    assert refine_leads(png,original,'m',call,lambda _:None) == original
    call.assert_not_called()


def test_affine_mapping_round_trip_does_not_change_shape():
    target = [.103,.204,.902,.807]
    actual = [.1,.2,.91,.81]
    box = [.05,.35,.2,.45]
    assert _frame_box(_frame_box(box,target,actual),actual,target) == pytest.approx(box)


def test_detail_uses_cache_but_changed_body_invalidates_it(png,tmp_path):
    original = [component()]
    call = Mock(return_value={'terminals':[dict(id=1,resolved=True,confidence='high',lead_bbox=LEADS[0])]})
    for _ in range(2):
        refine_leads(png,original,'m',call,lambda _:None,tmp_path)
    assert call.call_count == 1
    original[0]['terminal_geometry']['body_bbox'] = [.19,.2,.8,.8]
    refine_leads(png,original,'m',call,lambda _:None,tmp_path)
    assert call.call_count == 2


@pytest.mark.parametrize('bad,reason', [
    (detail([-.01,.35,.2,.45]), 'invalid_rectangle'),
    (detail([.05,.35,.05,.45]), 'empty_rectangle'),
    (dict(bbox=LEADS[0],confidence='low',source='photo_separation_v1'), 'no_evidence'),
    (detail([.05,.25,.2,.45]), 'exceeds_envelope'),
    (detail([.05,.35,.19,.45]), 'not_one_face'),
])
def test_each_rejection_carries_a_stable_reason_code(bad, reason):
    from kicadgen.cad3d.lead_geometry import LeadDetailRejected, REJECTION_REASONS
    with pytest.raises(LeadDetailRejected) as info:
        validated_lead_bbox(BODY,ENVELOPES[0],bad)
    assert info.value.reason == reason and reason in REJECTION_REASONS


def test_discarded_detail_reason_is_fully_translated():
    from kicadgen.cad3d.lead_geometry import LeadDetailRejected
    from kicadgen.services.photo_leads import _rejection_text
    texts = {reason: _rejection_text(LeadDetailRejected(reason, 'english diagnostic'))
             for reason in ('invalid_rectangle', 'empty_rectangle', 'no_evidence',
                            'exceeds_envelope', 'not_one_face')}
    # Every code has its own text and none leaks the English diagnostic.
    assert len(set(texts.values())) == 5
    assert all('english diagnostic' not in text for text in texts.values())
    # Errors that are not a rejection (malformed answer) get the generic text.
    assert _rejection_text(KeyError('bbox')) not in texts.values()
