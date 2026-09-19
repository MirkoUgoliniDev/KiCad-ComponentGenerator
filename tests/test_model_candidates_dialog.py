"""@brief Offscreen candidate review contracts, without AI or installed CAD."""

from copy import deepcopy
import os
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('PyQt6')
from PyQt6.QtCore import QThreadPool
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from kicadgen.gui import model_candidates_dialog as gui


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def review(app, tmp_path, monkeypatch):
    pool = QThreadPool.globalInstance()
    monkeypatch.setattr(gui, '_thumbnail', lambda *args: None)
    candidates = []
    for index in range(7):
        path = tmp_path / f'{index}.step'
        path.write_text('test fixture')
        candidates.append(dict(path=str(path), package=f'Package {index}',
                               dimensions_mm=[3, 2, 1], relative_xy_error=.12,
                               reasons=['Compatible outline', 'Height uncertain']))
    component = dict(name='U1', length_mm=3, width_mm=2, height_mm=1,
                     rotation_deg=90, model_rotation_deg=37)
    report = dict(candidates=candidates)
    dialog = gui.ModelCandidatesDialog(component, report)
    yield dialog, component, report
    dialog.reject()
    pool.waitForDone(2000)
    app.processEvents()


def _wait(app, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


def test_confirm_one_of_five_without_mutating_inputs(review):
    dialog, component, report = review
    before = deepcopy((component, report))
    assert dialog.candidate_list.count() == 5
    assert dialog.rotation.value() == 37
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.candidate_list.setCurrentRow(1)
    assert '12.0%' in dialog.details_label.text()
    assert 'Compatible outline' in dialog.details_label.text()
    dialog.rotation.setValue(-45.5)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    expected = dict(status='confirmed', path=report['candidates'][1]['path'], rotation_deg=-45.5)
    assert dialog.selected_selection() == expected
    dialog.selected_selection()['path'] = 'changed'
    assert dialog.selected_selection() == expected
    assert (component, report) == before


def test_cancel_and_none_match_have_different_dialog_results(review):
    dialog, component, report = review
    before = deepcopy((component, report))
    dialog.candidate_list.setCurrentRow(0)
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.selected_selection() == dict(status='rejected', path='', rotation_deg=0.0)
    other = gui.ModelCandidatesDialog(component, report)
    other.none_button.click()
    assert other.result() == QDialog.DialogCode.Accepted
    assert other.selected_selection() == dict(status='rejected', path='', rotation_deg=0.0)
    assert (component, report) == before


def test_manual_step_and_file_picker_cancel(review, monkeypatch):
    dialog, _, report = review
    path = report['candidates'][6]['path']
    monkeypatch.setattr(gui.QFileDialog, 'getOpenFileName', lambda *args: (path, ''))
    dialog.manual_button.click()
    assert 'STEP manuale' in dialog.details_label.text()
    monkeypatch.setattr(gui.QFileDialog, 'getOpenFileName', lambda *args: ('', ''))
    dialog.manual_button.click()
    dialog.accept()
    assert dialog.selected_selection()['path'] == path


def test_missing_geometry_does_not_disable_existing_step(review, app):
    dialog, _, _ = review
    dialog.candidate_list.setCurrentRow(0)
    _wait(app, lambda: 'non disponibile' in dialog.preview_label.text())
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_missing_file_cannot_be_confirmed(review):
    dialog, _, report = review
    from pathlib import Path
    Path(report['candidates'][0]['path']).unlink()
    dialog.candidate_list.setCurrentRow(0)
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_qpixmap_crop_and_missing_metadata(app):
    pixmap = QPixmap(20, 10)
    pixmap.fill(QColor('red'))
    dialog = gui.ModelCandidatesDialog({}, {'candidates': [None, {'path': 'absent.step'}]}, pixmap)
    assert not dialog.photo_label.pixmap().isNull()
    dialog.candidate_list.setCurrentRow(0)
    assert 'non disponibili' in dialog.details_label.text()
    dialog.reject()


def test_preview_is_nonblocking_coalesces_and_ignores_late_result(review, app, monkeypatch):
    dialog, _, report = review
    entered, release = threading.Event(), threading.Event()
    calls = []

    def thumbnail(path, rotation):
        calls.append((path, rotation))
        entered.set()
        release.wait(2)
        image = QImage(10, 10, QImage.Format.Format_RGB32)
        image.fill(QColor('blue'))
        return image

    monkeypatch.setattr(gui, '_thumbnail', thumbnail)
    dialog.candidate_list.setCurrentRow(0)
    assert entered.wait(1)
    dialog.candidate_list.setCurrentRow(1)
    dialog.rotation.setValue(180)
    assert len(calls) == 1
    release.set()
    _wait(app, lambda: len(calls) == 2 and not dialog._workers)
    assert calls[-1] == (report['candidates'][1]['path'], 180)
    assert not dialog.preview_label.pixmap().isNull()
    dialog.reject()
    dialog._preview_ready(dialog._token, QImage())
    assert dialog.selected_selection()['status'] == 'rejected'


def test_cancel_while_worker_runs_is_immediate(review, app, monkeypatch):
    dialog, _, _ = review
    entered, release = threading.Event(), threading.Event()

    def thumbnail(*args):
        entered.set()
        release.wait(2)
        return None

    monkeypatch.setattr(gui, '_thumbnail', thumbnail)
    dialog.candidate_list.setCurrentRow(0)
    assert entered.wait(1)
    try:
        dialog.reject()
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert not release.is_set()
        dialog.deleteLater()
        app.processEvents()
    finally:
        release.set()
        QThreadPool.globalInstance().waitForDone(2000)
        app.processEvents()


@pytest.mark.parametrize('index', [1, 6])
def test_reopen_restores_path_rotation_and_limits_list(review, monkeypatch, index):
    _, component, report = review
    component['model_selection'] = dict(status='confirmed', path=report['candidates'][index]['path'], rotation_deg=-90)
    before = deepcopy((component, report))
    calls = []
    monkeypatch.setattr(gui.QThreadPool, 'globalInstance', lambda: pytest.fail('Constructor started preview'))
    dialog = gui.ModelCandidatesDialog(component, report)
    assert dialog.candidate_list.count() == 5
    assert dialog._path == component['model_selection']['path']
    assert dialog.rotation.value() == -90
    assert not dialog.auto_rotation.isChecked()
    assert '3 × 2 × 1' in dialog.details_label.text()
    if index == 6:
        assert dialog.candidate_list.currentRow() == 0
    # Instance hooks can be installed after construction and before show/exec.
    monkeypatch.setattr(dialog, '_start_preview', lambda: calls.append(True))
    dialog.show()
    assert calls == [True]
    dialog.accept()
    assert dialog.selected_selection() == component['model_selection']
    assert (component, report) == before


def test_reopen_manual_path_missing_from_report(review, monkeypatch, tmp_path):
    _, component, report = review
    path = tmp_path / 'manual.stp'
    path.write_text('fixture')
    component['model_selection'] = dict(status='confirmed', path=str(path), rotation_deg=0)
    monkeypatch.setattr(gui.QThreadPool, 'globalInstance', lambda: pytest.fail('Eager preview'))
    dialog = gui.ModelCandidatesDialog(component, report)
    assert dialog.candidate_list.count() == 5
    assert dialog.candidate_list.currentRow() == 0
    assert dialog._path == str(path)
    assert not dialog.auto_rotation.isChecked()
    assert dialog.rotation.value() == 0
    dialog.accept()
    assert dialog.selected_selection() == component['model_selection']


@pytest.mark.parametrize('saved,legacy,automatic,angle', [
    ({'rotation_deg': None}, 37, True, 0),
    ({'rotation_deg': 0}, 37, False, 0),
    ({}, 37, False, 37),
    ({}, None, True, 0),
])
def test_rotation_auto_and_explicit_zero_contract(review, monkeypatch, saved, legacy, automatic, angle):
    _, component, report = review
    component['model_rotation_deg'] = legacy
    component['model_selection'] = dict(saved, status='confirmed', path=report['candidates'][0]['path'])
    before = deepcopy(component)
    monkeypatch.setattr(gui.QThreadPool, 'globalInstance', lambda: pytest.fail('Eager preview'))
    dialog = gui.ModelCandidatesDialog(component, report)
    assert dialog.auto_rotation.isChecked() == automatic
    assert dialog.rotation.isEnabled() != automatic
    assert dialog.rotation.value() == angle
    dialog.accept()
    assert dialog.selected_selection()['rotation_deg'] == (None if automatic else angle)
    assert component == before


def test_switch_auto_to_explicit_zero_and_back(review, monkeypatch):
    _, component, report = review
    component['model_rotation_deg'] = None
    dialog = gui.ModelCandidatesDialog(component, report)
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    dialog.candidate_list.setCurrentRow(0)
    dialog.auto_rotation.setChecked(False)
    assert dialog.rotation.isEnabled()
    assert '0°' in dialog.rotation_hint.text()
    dialog.accept()
    assert dialog.selected_selection()['rotation_deg'] == 0.0
    other = gui.ModelCandidatesDialog(component, report)
    monkeypatch.setattr(other, '_start_preview', lambda *args: None)
    other.candidate_list.setCurrentRow(0)
    other.auto_rotation.setChecked(False)
    other.rotation.setValue(90)
    other.auto_rotation.setChecked(True)
    other.accept()
    assert other.selected_selection()['rotation_deg'] is None


def test_auto_worker_receives_component_dimensions(review, app, monkeypatch):
    dialog, _, report = review
    calls = []
    monkeypatch.setattr(gui, '_thumbnail', lambda *args: calls.append(args))
    dialog.auto_rotation.setChecked(True)
    dialog.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not dialog._workers)
    assert calls == [(report['candidates'][0]['path'], None, (3, 2))]


@pytest.mark.parametrize('dimensions', [(8, 2), (2, 8)])
def test_auto_thumbnail_delegates_to_actual_cad_importer(monkeypatch, dimensions):
    """Auto consumes positioned CAD shapes, without applying a second rotation."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from kicadgen.cad3d import step_generator

    calls = []
    native = [(0, 0, 0), (8, 0, 0), (0, 2, 1)]
    angle = 90 if dimensions == (2, 8) else 0

    class Shape:
        def __init__(self, vertices):
            self.vertices = vertices

        def tessellate(self, tolerance):
            return [SimpleNamespace(x=x, y=y, z=z) for x, y, z in self.vertices], [(0, 1, 2)]

    def library_model(component, *args):
        calls.append(component)
        vertices = [(-y, x, z) for x, y, z in native] if angle else native
        return [(Shape(vertices), '#ffffff')]

    monkeypatch.setattr(step_generator, 'find_freecadcmd', lambda: 'fake-freecad')
    monkeypatch.setitem(sys.modules, 'freecad_parts', SimpleNamespace(_library_model=library_model))
    monkeypatch.setitem(sys.modules, 'Part', SimpleNamespace(
        read=lambda path: Shape(native), makeCompound=lambda shapes: shapes[0]))

    def run(command, **kwargs):
        old_path = sys.path[:]
        try:
            exec(compile(Path(command[1]).read_text(encoding='utf-8'), command[1], 'exec'), {})
        finally:
            sys.path[:] = old_path

    monkeypatch.setattr(gui.subprocess, 'run', run)
    automatic = gui._thumbnail('example.step', None, dimensions)
    explicit = gui._thumbnail('example.step', angle)
    assert automatic == explicit
    assert calls == [dict(model_path='example.step', length_mm=dimensions[0], width_mm=dimensions[1])]
    assert 'model_rotation_deg' not in calls[0]


@pytest.mark.parametrize('dimensions', [None, (0, 2), (2, float('nan')), (None, 3)])
def test_auto_missing_dimensions_never_silently_previews_zero(monkeypatch, dimensions):
    from kicadgen.cad3d import step_generator
    monkeypatch.setattr(step_generator, 'find_freecadcmd', lambda: 'fake-freecad')
    monkeypatch.setattr(gui.subprocess, 'run', lambda *a, **k: pytest.fail('Unexpected CAD launch'))
    with pytest.raises(ValueError, match='positive component dimensions'):
        gui._thumbnail('example.step', None, dimensions)


def test_dimensions_are_readable_without_changing_evidence(review, monkeypatch):
    from PyQt6.QtWidgets import QLabel
    _, component, report = review
    component.update(length_mm=5.7919500000000002, height_mm=1.7000000000000002)
    report['candidates'][0]['dimensions_mm'] = {'body': [5.7919500000000002, 1.7000000000000002, 2]}
    before = deepcopy((component, report))
    dialog = gui.ModelCandidatesDialog(component, report)
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    dialog.candidate_list.setCurrentRow(0)
    assert 'body: 5.792 × 1.7 × 2' in dialog.details_label.text()
    title = next(label.text() for label in dialog.findChildren(QLabel) if 'dimensioni stimate' in label.text())
    assert '5.792 × 2 × 1.7' in title
    assert (component, report) == before
    dialog.reject()


@pytest.mark.parametrize('value,display', [(None, '?'), (float('nan'), '?'),
                                         (-0.00001, '0'), (2.0, '2'), (0.1254, '0.125')])
def test_dimension_display_handles_missing_and_small_values(value, display):
    assert gui._display_dimensions(value) == display
