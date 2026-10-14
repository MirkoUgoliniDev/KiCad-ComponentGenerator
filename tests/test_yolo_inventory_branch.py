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
    # Contratto aggiornato (HANDOFF 144): dopo l'UNICA chiamata d'inventario
    # ogni corpo tenuto si rilegge due volte dal suo ritaglio ('observe_body').
    # Qui le letture non dicono nulla, cosi' la riga resta quella dell'inventario.
    # Osservazione (HANDOFF 148) di un corpo opaco a due terminali senza forma
    # riconoscibile: sulla riga «resistor» ridà la classe dell'inventario.
    # v5 contract (review D3): every required field, class triple included.
    blank=dict(visual_class='unknown',visual_class_confidence='low',visual_class_evidence='',contacts_complete=True,
               bodies_in_frame=1,metal_contacts_total=2,two_end_terminals_only=True,
               body_shape='other',top_translucent=False,something_visible_inside=False,
               coloured_patch_at_end=False,band_at_end=False,printed_text='none',marking='',
               tab=False,size_family='',
               leads_by_side=dict(left=None,right=None,top=None,bottom=None))
    def call(**kw):
        seen.append(kw)
        return blank if kw['tool']['function']['name']=='observe_body' else response
    result=inv.extract_inventory([tmp_path/'image.png'],'photo/model',40,20,['generic','resistor'],call,detector='yolo',classifier_model='verify/model',top_photo_index=0)
    assert result.components[0]['package']=='R_0603'
    assert {kw['model'] for kw in seen}=={'verify/model'}
    names=[kw['tool']['function']['name'] for kw in seen]
    assert names==['report_global_inventory','observe_body','observe_body']
    assert result.photo_png and result.overlay_png
    assert result.calibration['detector']=='yolo'


def test_extractor_preflight_failure_precedes_paid_calls(monkeypatch):
    from kicadgen.services import datasheet_extractor as extractor, photo_yolo
    def fail(*args): raise ValueError('invalid ONNX')
    monkeypatch.setattr(photo_yolo,'validate_yolo_setup',fail)
    monkeypatch.setattr(extractor,'call_tool',lambda **kwargs:pytest.fail('paid request'))
    with pytest.raises(extractor.ExtractionError,match='invalid ONNX'):
        extractor.extract_pin_header_params([Path('photo.jpg')],photo_detector='yolo')
