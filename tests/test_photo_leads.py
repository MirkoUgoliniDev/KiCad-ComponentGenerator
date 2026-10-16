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


# --- Shared attachment helpers (MEDIO 16): face anchoring of observed leads ---

MM_BODY = [2, 1, 8, 5]   # millimetre frame, as the renderer anchors


@pytest.mark.parametrize('terminal,face', [
    ([0, 2, 2, 3], (0, -1)),        # left face, exact contact
    ([0, 2, 1.9, 3], (0, -1)),      # left face, short gap
    ([8.1, 2, 10, 3], (0, 1)),      # right face, short gap
    ([3, -1, 4, 1], (1, -1)),       # low Y face
    ([3, 5, 4, 7], (1, 1)),         # high Y face
    ([0, 0, 2, 1], (0, -1)),        # corner tie: X faces first, deterministic
    ([0, 0, 2, 2.5], (0, -1)),      # corner, protrudes more along X
    ([0, -2, 2.5, 1], (1, -1)),     # corner, protrudes more along Y
    ([1, 2, 4, 3], (0, -1)),        # overlapping into the body
])
def test_anchor_face_is_the_face_the_terminal_protrudes_from_most(terminal, face):
    from kicadgen.cad3d.lead_geometry import anchor_face
    assert anchor_face(MM_BODY, terminal) == face


def test_a_terminal_inside_the_body_has_no_anchor_face():
    from kicadgen.cad3d.lead_geometry import anchor_face
    assert anchor_face(MM_BODY, [3, 2, 4, 3]) is None


@pytest.mark.parametrize('terminal,expected', [
    ([0, 2, 1.9, 3], [0, 2, 2, 3]),            # gap closed by the inner edge only
    ([0, 2, 2.5, 3], [0, 2, 2.5, 3]),          # overlap kept as observed
    ([0, 0, 2, 1], [0, 0, 2, 1.12]),           # corner: root lengthened by lateral_min
    ([0, 5, 1.9, 6], [0, 4.88, 2, 6]),         # high corner with gap: both closures
    ([0, 2.4, 2, 2.5], [0, 2.4, 2, 2.5]),      # narrow lead inside the span: untouched
])
def test_anchored_terminal_grows_only_toward_the_body(terminal, expected):
    from kicadgen.cad3d.lead_geometry import anchor_face, anchored_terminal
    face = anchor_face(MM_BODY, terminal)
    result = anchored_terminal(MM_BODY, terminal, face, .12)
    assert result == pytest.approx(expected)
    assert result[0] <= terminal[0] and result[2] >= terminal[2]
    assert result[1] <= terminal[1] and result[3] >= terminal[3]


def test_lateral_overlap_is_capped_at_the_face_span():
    from kicadgen.cad3d.lead_geometry import anchored_terminal
    tiny = [2, 1, 8, 1.05]                       # face span .05 < lateral_min
    result = anchored_terminal(tiny, [0, 0, 2, 1], (0, -1), .12)
    assert result == pytest.approx([0, 0, 2, 1.05])


@pytest.mark.parametrize('terminal,attached', [
    ([0, .3, .2, .4], True), ([0, .3, .15, .4], True), ([0, .3, .13, .4], False),
    ([0, 0, .2, .2], True), ([0, 0, .13, .13], False), ([.9, .3, 1, .4], False)])
def test_is_attached_is_symmetric_in_the_two_axes(terminal, attached):
    from kicadgen.cad3d.lead_geometry import is_attached
    body = [.2, .2, .8, .8]
    swapped = [terminal[1], terminal[0], terminal[3], terminal[2]]
    assert is_attached(body, terminal) is attached
    assert is_attached(body, swapped) is attached


def test_lateral_growth_stops_at_a_neighbouring_lead():
    """Positive: a fine-pitch neighbour along the face caps the root growth at
    its edge, so the anchored lead never enters it; the outer end is kept."""
    from kicadgen.cad3d.lead_geometry import anchored_terminal
    neighbour = [0, 1.05, 2, 1.5]                # next lead, .05 mm past the corner
    result = anchored_terminal(MM_BODY, [0, 0, 2, 1], (0, -1), .12, [neighbour])
    assert result == pytest.approx([0, 0, 2, 1.05])
    assert min(result[3], neighbour[3]) - max(result[1], neighbour[1]) <= 1e-12


def test_a_neighbour_with_no_room_keeps_the_inner_closure_only():
    """A neighbour already at the face span edge leaves no lateral room: only
    the inner-edge closure along the face normal is applied."""
    from kicadgen.cad3d.lead_geometry import anchored_terminal
    neighbour = [0, 1, 2, 1.5]                   # touches the face span start
    result = anchored_terminal(MM_BODY, [0, 0, 1.9, 1], (0, -1), .12, [neighbour])
    assert result == pytest.approx([0, 0, 2, 1])


@pytest.mark.parametrize('obstacles', [
    [],                                          # isolated lead
    [[5, 1, 6, 1.5]],                            # far along X: not beside the lead
    [[0, 3, 2, 4]],                              # beyond the growth target
    [[0, -1, 2, 0]],                             # on the outer side: never in the way
])
def test_obstacles_out_of_the_growth_path_leave_the_growth_unchanged(obstacles):
    """Negative: leads that the lateral growth cannot reach do not change it."""
    from kicadgen.cad3d.lead_geometry import anchored_terminal
    result = anchored_terminal(MM_BODY, [0, 0, 2, 1], (0, -1), .12, obstacles)
    assert result == pytest.approx([0, 0, 2, 1.12])
