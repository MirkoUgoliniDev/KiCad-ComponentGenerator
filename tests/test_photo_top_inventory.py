"""@brief Offline orchestration regression tests for geometry-first inventory."""
import json
import numpy as np
import pytest
from kicadgen.services import photo_top_inventory as top


def body(box):
    return dict(box=box,complete=True,confidence="high",evidence="Separate material body")


def fake_model(*, review_status="mounted_body", fail_review=False, add_residual=True):
    calls=[]
    def call(**kwargs):
        calls.append(kwargs)
        name=kwargs['tool']['function']['name']
        if name=='locate_mounted_bodies':
            if 'RESIDUAL SEARCH' in kwargs['system_prompt']:
                return {'components':[body([60,10,80,20])] if add_residual else []}
            return {'components':[body([20,10,40,20])]}
        if fail_review: raise ValueError('offline simulated failure')
        entries=json.loads(kwargs['content_blocks'][1]['text'])
        return {'components':[dict(id=c['id'],status=review_status,evidence='visible evidence') for c in entries]}
    return call,calls


def test_residual_recovers_new_body_and_runs_context_review():
    call,calls=fake_model()
    result=top.detect_top_bodies(np.zeros((50,100,3),np.uint8),'offline',call,lambda _:None)
    assert len(result['components'])==2
    assert result['failed_stages']==0 and result['completeness']=='not_certified'
    assert {tuple(c['_box']) for c in result['components']}=={(.2,.2,.4,.4),(.6,.2,.8,.4)}
    assert all(c['_objectness']=='mounted_body' for c in result['components'])
    assert all(len([b for b in k['content_blocks'] if b['type']=='image_url'])==1 for k in calls)
    assert any('Earlier local boxes' in k['content_blocks'][-1]['text'] for k in calls)


def test_failed_reviews_keep_bodies_uncertain_and_record_coverage():
    call,_=fake_model(fail_review=True)
    result=top.detect_top_bodies(np.zeros((50,100,3),np.uint8),'offline',call,lambda _:None,residual=False)
    assert len(result['components'])==1 and result['components'][0]['_uncertain']
    assert result['failed_stages']==1 and not result['rejected']


@pytest.mark.parametrize('status',['attached_part','pcb_feature'])
def test_explicit_nonbody_review_keeps_rejection_evidence(status):
    call,_=fake_model(review_status=status)
    result=top.detect_top_bodies(np.zeros((50,100,3),np.uint8),'offline',call,lambda _:None,residual=False)
    assert not result['components'] and len(result['rejected'])==1
    assert result['rejected'][0]['_review_evidence']=='visible evidence'


@pytest.mark.parametrize('rows',[
    [],[dict(id=1,status='mounted_body',evidence='')],
    [dict(id=1,status='mounted_body',evidence='a'),dict(id=1,status='pcb_feature',evidence='b')],
    [dict(id=True,status='pcb_feature',evidence='bool ID')],
    [dict(id=2,status='pcb_feature',evidence='foreign')]])
def test_ambiguous_votes_never_delete_candidate(rows):
    assert top._review_votes(rows,[1])[1]['status']=='uncertain'


def test_pixel_mapping_checks_both_axes_and_preserves_adjacent_bodies():
    rows=[body([10,20,30,40]),body([32,20,45,40]),body([0,20,10,40]),body([10,0,20,20])]
    decoded=top._decode({'components':rows},(100,200,200,300),(100,100),(400,400),'tile',lambda _:None)
    assert len(decoded)==2
    assert decoded[0]['_box']==(.275,.55,.325,.6)
    assert len(top._merge([decoded],lambda _:None))==2


@pytest.mark.parametrize('box',[[True,1,10,10],[0,float('nan'),10,10],[10,1,0,10],[0,0,101,10]])
def test_invalid_geometry_not_silently_clamped(box):
    assert top._decode({'components':[body(box)]},(0,0,100,100),(100,100),(100,100),'x',lambda _:None)==[]


def test_parallel_and_serial_have_same_stable_result():
    image=np.zeros((50,100,3),np.uint8)
    first,_=fake_model();second,_=fake_model()
    a=top.detect_top_bodies(image,'offline',first,lambda _:None,workers=1)
    b=top.detect_top_bodies(image,'offline',second,lambda _:None,workers=3)
    assert a==b


def test_replay_cached_calls_does_not_use_network(tmp_path):
    image=np.zeros((50,100,3),np.uint8)
    first,calls=fake_model()
    a=top.detect_top_bodies(image,'offline',first,lambda _:None,tmp_path)
    def forbidden(**kwargs):pytest.fail('Network on replay')
    b=top.detect_top_bodies(image,'offline',forbidden,lambda _:None,tmp_path)
    assert a==b and calls
