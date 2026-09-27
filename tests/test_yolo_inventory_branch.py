"""@brief The YOLO inventory branch goes straight to the global inventory, offline.

@note Moved from the removed tests/test_photo_yolo_review.py when the crop
    review and coverage recovery (dead code, HANDOFF 130 F4) were deleted: these
    two contracts belong to the live extraction path.
"""
from pathlib import Path

import numpy as np
import pytest


def proposal(identity=1):
    """@brief Construct a synthetic detector proposal independent of any real board."""
    return dict(_id=identity,_box=[.2,.2,.4,.4],name='target',kind='generic',
                x_mm=-8,y_mm=-4,length_mm=8,width_mm=4,height_mm=1,
                rotation_deg=0,color_hex='#303030',_source='YOLO local')


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
