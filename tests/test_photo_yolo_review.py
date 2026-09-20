"""@brief Exercise reviewed YOLO geometry and paid-call boundaries without network."""
from copy import deepcopy
from pathlib import Path
import numpy as np
import pytest

from kicadgen.services import photo_yolo_review as review


def proposal(identity=1):
    """@brief Construct a synthetic component independent of the ESP32 example."""
    return dict(_id=identity,_box=[.2,.2,.4,.4],name='target',kind='generic',
                x_mm=-8,y_mm=-4,length_mm=8,width_mm=4,height_mm=1,
                rotation_deg=0,color_hex='#303030',_source='YOLO local')


def verdict(**updates):
    """@brief Construct a valid synthetic visual vote."""
    row=dict(id=1,present=True,confidence='high',kind='resistor',package='R_0603',package_evidence='two metal ends')
    row.update(updates)
    return row


def apply(vote):
    """@brief Apply a vote on a known crop transform."""
    return review.apply_review(proposal(),vote,[10,10,50,50],(100,100,3),40,20,['generic','resistor'],lambda _:None)


def test_refinement_converts_crop_pixels_not_guessed_package_dimensions():
    result=apply(verdict(refined_bbox=[.375,.375,.625,.625],bbox_basis='body'))
    assert result['_box']==pytest.approx([.25,.25,.35,.35])
    assert result['length_mm']==pytest.approx(4)
    assert result['width_mm']==pytest.approx(2)
    assert result['_yolo_original_box']==proposal()['_box']
    assert result['_package_verified'] is False
    assert result['_orientation_verified'] is False
    assert result['_uncertain']


@pytest.mark.parametrize('box',[[0,0,.1,.1],[.2,.2,.2,.3],[False,.2,.3,.4],[.2,.2,float('nan'),.4]])
def test_invalid_or_neighbour_refinement_does_not_retarget_or_import(box):
    result=apply(verdict(refined_bbox=box,bbox_basis='body'))
    assert result['_box']==proposal()['_box']
    assert result['package']==''
    assert result['bbox_basis']=='unknown'


def test_low_confidence_package_cannot_trigger_library_assignment():
    assert apply(verdict(confidence='low'))['package']==''


def test_only_clear_noncomponent_can_be_removed():
    assert apply(verdict(present=False)) is None
    assert apply(verdict(present=False,confidence='low')) is not None
    assert apply(None) is not None


def test_terminal_count_evidence_is_separate_from_global_confidence():
    result=apply(verdict(pin_count=3,pin_count_confidence='medium',pins_by_side={'left':2,'right':1}))
    assert result['pin_count_source']=='photo_observation'
    assert result['pin_count_confidence']=='medium'
    assert result['pins_by_side']=={'left':2,'right':1}
    assert 'pin_count' not in apply(verdict(pin_count=True,pin_count_confidence='high'))


def test_review_batches_keep_missing_and_duplicate_ids(tmp_path):
    calls=[]
    items=[proposal(i) for i in range(1,10)]
    def fake_call(**kwargs):
        calls.append(kwargs)
        return {'components':[verdict(id=1),verdict(id=1)]}
    before=deepcopy(items)
    result=review.review_yolo_components(np.zeros((100,100,3),np.uint8),items,40,20,'test/reviewer',fake_call,lambda _:None,tmp_path)
    assert len(calls)==2
    assert len(result)==9
    assert all(c['_uncertain'] for c in result)
    assert items==before
    assert all(c['model']=='test/reviewer' for c in calls)
    assert len(list((tmp_path/'yolo-audit').glob('reviewed-components-*.json')))==1
    # Incomplete or duplicate responses must not enter the reusable cache.
    assert not list(tmp_path.glob('*.json'))


def test_crop_review_uses_v3_contract_without_conflicting_profile_or_module_notes():
    calls=[]
    review.review_yolo_components(
        np.zeros((100,100,3),np.uint8), [proposal()], 40, 20, 'test/model',
        lambda **kwargs: calls.append(kwargs) or {'components':[verdict()]}, lambda _:None,
        verification_prompt='PRIVATE LEGACY PROFILE',
        extra_instructions='PRIVATE WHOLE MODULE NOTES')
    prompt=calls[0]['system_prompt']
    assert review.PROMPT_VERSION == 'yolo-package-review-3'
    assert 'target_estimate_mm' in prompt and 'complete chip and metal end caps' in prompt
    assert 'PRIVATE LEGACY PROFILE' not in prompt
    assert 'PRIVATE WHOLE MODULE NOTES' not in prompt


def test_explicit_high_confidence_rejection_applies_only_to_its_id():
    result=review.review_yolo_components(np.zeros((100,100,3),np.uint8),[proposal(1),proposal(2)],40,20,'test/model',lambda **_: {'components':[verdict(id=1,present=False),verdict(id=2)]},lambda _:None)
    assert [c['_id'] for c in result]==[2]


def test_no_proposals_make_no_external_calls():
    assert review.review_yolo_components(None,[],40,20,'test',lambda **_:pytest.fail('network'),lambda _:None)==[]


def test_inventory_yolo_branch_bypasses_ai_localizer(monkeypatch,tmp_path):
    from kicadgen.services import photo_inventory as inv, photo_yolo
    import kicadgen.services.photo_grounding as grounding
    photo=np.zeros((100,100,3),np.uint8)
    monkeypatch.setattr(inv,'prepare_photo',lambda *args:photo)
    seen=[]
    monkeypatch.setattr(photo_yolo,'detect_yolo_components',lambda *args,**kwargs:[proposal()])
    monkeypatch.setattr(grounding,'grounded_components',lambda *args,**kwargs:pytest.fail('legacy localizer called'))
    response = dict(components=[dict(component_id='R1',source_candidate_ids=[1],
        **{'class':'resistor'},bbox=[.2,.2,.4,.4],bbox_basis='body',rotation_deg=0,
        package='R_0603',marking='?',terminal_count=2,terminal_layout='two endcaps',
        height_mm=None,height_source='unknown',confidence='high',evidence='complete body',
        # Campi obbligatori dello schema: omessi su ogni riga farebbero scattare
        # un secondo tentativo, e questo test conta una chiamata sola.
        presence_confidence='high',class_confidence='high',lead_count=2,lead_count_complete=True,
        lead_count_confidence='high',grouping_basis='single_body',asymmetry_landmark=None)],
        candidate_decisions=[dict(candidate_id=1,status='keep',component_id='R1',reason='body')],uncertainties=[])
    result=inv.extract_inventory([tmp_path/'image.png'],'photo/model',40,20,['generic','resistor'],lambda **kw: seen.append(kw) or response,detector='yolo',classifier_model='verify/model',top_photo_index=0)
    assert result.components[0]['package']=='R_0603'
    assert seen[0]['model']=='verify/model'
    assert len(seen)==1
    assert seen[0]['tool']['function']['name']=='report_global_inventory'
    assert result.photo_png and result.overlay_png
    assert result.calibration['detector']=='yolo'


def test_extractor_preflight_failure_precedes_paid_calls(monkeypatch):
    from kicadgen.services import datasheet_extractor as extractor, photo_yolo
    def fail(*args): raise ValueError('invalid ONNX')
    monkeypatch.setattr(photo_yolo,'validate_yolo_setup',fail)
    monkeypatch.setattr(extractor,'call_tool',lambda **kwargs:pytest.fail('paid request'))
    with pytest.raises(extractor.ExtractionError,match='invalid ONNX'):
        extractor.extract_pin_header_params([Path('photo.jpg')],photo_detector='yolo')


def test_coverage_keeps_whole_assembly_and_deduplicates_same_envelope(tmp_path):
    items=[proposal()]
    response={'components':[
        {'box_2d':[200,200,400,400],'kind':'resistor','label':'duplicate'},
        {'box_2d':[100,100,900,900],'kind':'ic','label':'whole module','package':'unverified'},
        {'box_2d':[False,200,400,400],'kind':'ic','label':'invalid'}]}
    result=review.recover_coverage(np.zeros((100,100,3),np.uint8),items,40,20,'reviewer',
                                 lambda **_:response,lambda _:None,tmp_path)
    assert [c['_id'] for c in result]==[1,2]
    assert result[0]==items[0]
    assert result[1]['_box']==pytest.approx([.1,.1,.9,.9])
    assert result[1]['package']=='' and result[1]['kind']=='generic'
    assert result[1]['_source']=='AI coverage'
    assert list((tmp_path/'yolo-audit').glob('coverage-*.json'))


def test_empty_yolo_can_be_rescued_by_coverage():
    response={'components':[{'box_2d':[200,200,400,400],'kind':'resistor','label':'body'}]}
    result=review.recover_coverage(np.zeros((100,100,3),np.uint8),[],40,20,'reviewer',lambda **_:response,lambda _:None)
    assert len(result)==1 and result[0]['_id']==1


def test_coverage_failure_preserves_local_proposals():
    def fail(**kwargs): raise ValueError('invalid API response')
    items=[proposal()]
    assert review.recover_coverage(np.zeros((100,100,3),np.uint8),items,40,20,'reviewer',fail,lambda _:None)==items


def test_coverage_expansion_survives_high_iou_with_clipped_box():
    response={'components':[{'box_2d':[200,200,450,400],'kind':'resistor','label':'complete'}]}
    result=review.recover_coverage(np.zeros((100,100,3),np.uint8),[proposal()],40,20,'reviewer',lambda **_:response,lambda _:None)
    assert len(result)==2


@pytest.mark.parametrize('vote',[
    {'present':False,'confidence':'high'},
    verdict(present=False,package_evidence=''),
    verdict(present=False,kind='nonexistent')])
def test_incomplete_rejection_does_not_delete(vote):
    assert apply(vote) is not None


def test_huge_numeric_values_do_not_crash_review():
    result=apply(verdict(refined_bbox=[0,0,10**400,.5],bbox_basis='body',height_mm=10**400))
    assert result['_box']==proposal()['_box'] and result['height_mm']==1


@pytest.mark.parametrize('box',[[.7,.2,.3,.4],[.3,.2,float('nan'),.4]])
def test_invalid_refinement_is_not_reusable_and_audit_is_standard_json(tmp_path,box):
    import json
    review.review_yolo_components(np.zeros((100,100,3),np.uint8),[proposal()],40,20,'reviewer',
        lambda **_: {'components':[verdict(refined_bbox=box,bbox_basis='body')]},lambda _:None,tmp_path)
    assert not list(tmp_path.glob('*.json'))
    for path in (tmp_path/'yolo-audit').glob('*.json'):
        json.loads(path.read_text(encoding='utf-8'),parse_constant=lambda s:pytest.fail('Nonstandard '+s))
