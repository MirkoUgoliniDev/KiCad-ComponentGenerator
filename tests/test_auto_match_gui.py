"""@brief Automatic mechanical estimates remain distinct from manual confirmation."""
from kicadgen.gui import model_candidates_dialog as gui


def test_automatic_model_is_initial_selection_not_manual_confirmation(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, '_thumbnail', lambda *args: None)
    path = tmp_path/'R_0603.step'
    path.write_text('fixture')
    component = dict(name='R', length_mm=1.6, width_mm=.8, height_mm=.4)
    report = dict(status='auto_matched', selected_path=str(path),
                  candidates=[dict(path=str(path), package='0603')])
    dialog = gui.ModelCandidatesDialog(component, report)
    assert dialog.candidate_list.currentRow() == 0
    assert 'model_selection' not in component
    dialog.reject()
    assert 'model_selection' not in component
