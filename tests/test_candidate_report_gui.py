"""@brief Candidate-report corruption must not block real mesh preview loading."""
import json
from unittest.mock import Mock
from test_photo_inventory_gui import window


def test_corrupt_candidate_record_does_not_hide_mesh(window,tmp_path):
    shapes=tmp_path/'part.3dshapes'; shapes.mkdir()
    step=shapes/'part.step'; mesh=step.with_suffix('.preview.json')
    mesh.write_text('{}')
    (tmp_path/'kicad-model-candidates.json').write_text(json.dumps({'components':[None]}))
    window.preview_3d.render_mesh=Mock()
    window._show_generated_mesh(step)
    window.preview_3d.render_mesh.assert_called_once_with(mesh)
    assert 'Report candidati non caricato' in window._log_plain_text()


def test_report_source_row_not_filtered_index(window,tmp_path):
    window._set_photo_components([
        dict(name='first',x_mm=0,y_mm=0,length_mm=1,width_mm=1,height_mm=1),
        dict(name='second',x_mm=2,y_mm=0,length_mm=2,width_mm=1,height_mm=1)])
    shapes=tmp_path/'part.3dshapes'; shapes.mkdir()
    step=shapes/'part.step';step.with_suffix('.preview.json').write_text('{}')
    (tmp_path/'kicad-model-candidates.json').write_text(json.dumps({'components':[
        dict(source_row=1,name='second',package='',length_mm=2,width_mm=1,
             model_match={'status':'review_required','candidates':[]})]}))
    window.preview_3d.render_mesh=Mock()
    window._show_generated_mesh(step)
    rows=window._photo_components_from_table()
    assert not rows[0].get('model_match')
    assert rows[1]['model_match']['status']=='review_required'
