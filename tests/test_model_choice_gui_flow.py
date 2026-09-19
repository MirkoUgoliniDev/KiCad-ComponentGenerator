"""@brief Exercise candidate choices across photo review, table and CAD payload."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from copy import deepcopy
from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication
from kicadgen.gui.photo_review_dialog import PhotoReviewDialog
from kicadgen.generators.castellated_module import ComponentBox, CastellatedModuleParams, CastellatedSide
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d import step_generator as step


def photo():
    pixmap=QPixmap(200,100); pixmap.fill()
    buffer=QBuffer(); buffer.open(QIODevice.OpenModeFlag.WriteOnly); pixmap.save(buffer,'PNG')
    return bytes(buffer.data())


def test_geometry_edit_invalidates_choice_cancel_keeps_original():
    app=QApplication.instance() or QApplication([])
    source=[dict(name='R',kind='generic',x_mm=0,y_mm=0,length_mm=2,width_mm=1,height_mm=1,
                 model_selection=dict(status='confirmed',path='R.step',rotation_deg=90),
                 model_match={'candidates':[]})]
    before=deepcopy(source)
    dialog=PhotoReviewDialog(photo(),source,20,10)
    dialog.fields['length_mm'].setValue(3)
    assert 'model_selection' not in dialog.get_components()[0]
    dialog.reject()
    assert source==before


def test_castellated_payload_preserves_user_choice(monkeypatch,tmp_path):
    from kicadgen.cad3d import kicad_models
    monkeypatch.setattr(kicad_models,'attach_models',lambda *args,**kwargs:0)
    captured={}
    def run(*args,**kwargs):
        captured.update(kwargs['extra_payload'])
        return {'warnings':[]}
    monkeypatch.setattr(step,'_run_freecad',run)
    choice={'status':'confirmed','path':'chosen.step','rotation_deg':90}
    params=CastellatedModuleParams(name='test',body_length=20,body_width=10,
        sides=[CastellatedSide('top',2,2.54,1)],
        components=[ComponentBox(0,0,2,1,1,model_selection=choice)])
    step.generate_castellated_3d_model(params,Body3DParams(20,10,1.6),tmp_path)
    assert captured['components'][0]['model_selection']==choice
    assert (tmp_path/'kicad-model-candidates.json').is_file()


def test_skipped_component_retains_original_source_row(monkeypatch,tmp_path):
    from kicadgen.cad3d import kicad_models
    monkeypatch.setattr(kicad_models,'attach_models',lambda *args,**kwargs:0)
    captured={}
    def run(*args,**kwargs):
        captured.update(kwargs['extra_payload'])
        return {'warnings':[]}
    monkeypatch.setattr(step,'_run_freecad',run)
    params=CastellatedModuleParams(name='test',body_length=20,body_width=10,
        sides=[CastellatedSide('top',2,2.54,1)],
        components=[ComponentBox(0,0,0,1,1),ComponentBox(0,0,2,1,1)])
    step.generate_castellated_3d_model(params,Body3DParams(20,10,1.6),tmp_path)
    assert len(captured['components'])==1
    assert captured['components'][0]['source_row']==1


def test_failed_import_downgrades_report_not_user_choice(tmp_path):
    import json
    mesh=tmp_path/'mesh.json'
    mesh.write_text(json.dumps({'parts':[{'source_row':3,'model_import_failed':True}]}))
    component={'source_row':3,'name':'Q1','model_match':{'status':'confirmed'},
               'model_selection':{'status':'confirmed','path':'broken.step'}}
    result={'mesh_path':mesh,'warnings':[]}
    step._reconcile_model_imports(result,[component],tmp_path)
    assert component['model_match']['status']=='import_failed'
    assert component['model_selection']['status']=='confirmed'
    assert result['warnings']
    assert json.loads((tmp_path/'kicad-model-candidates.json').read_text())['components'][0]['model_match']['status']=='import_failed'


def test_cancel_candidate_dialog_keeps_import_failure(monkeypatch):
    from kicadgen.gui import model_candidates_dialog as module
    from PyQt6.QtWidgets import QDialog
    app=QApplication.instance() or QApplication([])
    source=[dict(name='Q',kind='generic',x_mm=0,y_mm=0,length_mm=2,width_mm=1,height_mm=1,
                 model_selection=dict(status='confirmed',path='broken.step',rotation_deg=0),
                 model_match={'status':'import_failed','error':'broken'})]
    editor=PhotoReviewDialog(photo(),source,20,10)
    monkeypatch.setattr(module.ModelCandidatesDialog,'exec',lambda self:QDialog.DialogCode.Rejected)
    item=editor._items[0]
    editor._show_model_candidates(item,deepcopy(item.component),{'status':'review_required','candidates':[]})
    assert editor.get_components()==source
    editor.reject()
