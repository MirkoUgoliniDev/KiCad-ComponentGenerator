"""@brief Offscreen candidate review contracts, without AI or installed CAD."""

from copy import deepcopy
import math
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

    def thumbnail(path, rotation, *_):
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
    # The fourth argument is the sampled body colour: this component declares
    # none, so the renderer must fall back to its documented default.
    assert calls == [(report['candidates'][0]['path'], None, (3, 2), None)]


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


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Fixed library index served synchronously, so no library walk ever runs."""
    from kicadgen.services.model_library_search import LibraryModel
    folder = tmp_path / 'RF_Module.3dshapes'
    folder.mkdir()
    models = []
    for stem in ('ESP32-WROOM-32', 'ESP32-S3-WROOM-1', 'ESP32-S3-WROOM-1U', 'USB_C_Receptacle'):
        path = folder / f'{stem}.step'
        path.write_text('fixture')
        models.append(LibraryModel(path, 'RF_Module.3dshapes', stem))
    walks = []
    monkeypatch.setattr(gui, 'library_models', lambda *args, **kwargs: walks.append(True) or list(models))
    return models, walks


def _filter(app, dialog, text):
    dialog.library_filter.setText(text)
    _wait(app, lambda: not dialog._filter_timer.isActive() and dialog._index_worker is None
          and dialog.library_count_label.text() != 'Indicizzazione della libreria…')


def test_typing_filters_library_after_lazy_index(review, app, library):
    dialog, _, _ = review
    models, walks = library
    assert dialog.library_list.count() == 0
    assert walks == []
    _filter(app, dialog, 'wroom s3')
    assert walks == [True]
    assert [dialog.library_list.item(i).text() for i in range(dialog.library_list.count())] == [
        'RF_Module.3dshapes/ESP32-S3-WROOM-1', 'RF_Module.3dshapes/ESP32-S3-WROOM-1U']
    assert dialog.library_count_label.text() == '2 modelli'
    assert dialog.library_list.isVisibleTo(dialog)
    _filter(app, dialog, 'usb*recept')
    assert walks == [True]
    assert dialog.library_list.count() == 1
    assert dialog.library_count_label.text() == '1 modello'
    _filter(app, dialog, 'nothing-here')
    assert dialog.library_list.count() == 0
    assert dialog.library_count_label.text() == 'nessun modello'


def test_library_row_sets_path_enables_ok_and_is_returned(review, app, library, monkeypatch):
    dialog, _, _ = review
    models, _ = library
    previews = []
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: previews.append(True))
    _filter(app, dialog, 'wroom-1u')
    assert dialog.library_list.count() == 1
    dialog.library_list.setCurrentRow(0)
    expected = str(models[2].path)
    assert dialog._path == expected
    assert dialog.details_label.text().startswith(f'Dalla libreria KiCad: {expected}')
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert previews == [True]
    dialog.auto_rotation.setChecked(False)
    dialog.rotation.setValue(90)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_selection() == dict(status='confirmed', path=expected, rotation_deg=90.0)


def test_library_row_starts_the_same_preview_as_candidates(review, app, library, monkeypatch):
    dialog, _, _ = review
    models, _ = library
    calls = []
    monkeypatch.setattr(gui, '_thumbnail', lambda *args: calls.append(args))
    _filter(app, dialog, 'usb')
    dialog.library_list.setCurrentRow(0)
    _wait(app, lambda: not dialog._workers)
    assert calls == [(str(models[3].path), 37.0, None, None)]
    dialog.auto_rotation.setChecked(True)
    _wait(app, lambda: len(calls) == 2 and not dialog._workers)
    assert calls[-1] == (str(models[3].path), None, (3, 2), None)


def test_empty_filter_shows_nothing_and_keeps_candidate_selection(review, app, library, monkeypatch):
    dialog, _, report = review
    _, walks = library
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    dialog.candidate_list.setCurrentRow(2)
    _filter(app, dialog, '   ')
    assert walks == []
    assert dialog.library_list.count() == 0
    assert dialog.library_count_label.text() == ''
    assert not dialog.library_list.isVisibleTo(dialog)
    assert dialog.candidate_list.currentRow() == 2
    assert dialog._path == report['candidates'][2]['path']
    _filter(app, dialog, 'wroom')
    assert dialog.library_list.count() == 3
    assert dialog.candidate_list.currentRow() == 2
    _filter(app, dialog, '')
    assert dialog.library_list.count() == 0
    assert dialog.candidate_list.currentRow() == 2
    assert dialog._path == report['candidates'][2]['path']


def test_selecting_in_one_list_clears_the_other(review, app, library, monkeypatch):
    dialog, _, report = review
    models, _ = library
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    dialog.candidate_list.setCurrentRow(1)
    _filter(app, dialog, 'wroom')
    dialog.library_list.setCurrentRow(1)
    assert dialog.candidate_list.currentRow() == -1
    assert dialog._path == str(models[1].path)
    dialog.candidate_list.setCurrentRow(3)
    assert dialog.library_list.currentRow() == -1
    assert dialog._path == report['candidates'][3]['path']
    assert '12.0%' in dialog.details_label.text()
    # A manual file also releases the library row.
    dialog.library_list.setCurrentRow(0)
    manual = report['candidates'][6]['path']
    monkeypatch.setattr(gui.QFileDialog, 'getOpenFileName', lambda *args: (manual, ''))
    dialog.manual_button.click()
    assert dialog.library_list.currentRow() == -1
    assert dialog.candidate_list.currentRow() == -1
    assert dialog._path == manual


def test_library_without_models_and_failed_walk_are_reported(review, app, monkeypatch):
    dialog, _, _ = review
    monkeypatch.setattr(gui, 'library_models', lambda *args, **kwargs: [])
    _filter(app, dialog, 'sot')
    assert dialog.library_list.count() == 0
    assert dialog.library_count_label.text() == 'libreria KiCad non trovata'
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()

    def broken(*args, **kwargs):
        raise OSError('walk failed')

    monkeypatch.setattr(gui, 'library_models', broken)
    other = gui.ModelCandidatesDialog({}, {'candidates': []})
    _filter(app, other, 'sot')
    assert other.library_count_label.text() == 'libreria KiCad non trovata'
    other.reject()


def test_index_result_after_close_or_stale_token_is_ignored(review, app, monkeypatch):
    dialog, _, _ = review
    from kicadgen.services.model_library_search import LibraryModel
    entered, release = threading.Event(), threading.Event()

    def slow_walk(*args, **kwargs):
        entered.set()
        release.wait(2)
        return [LibraryModel(gui.Path('late.step'), 'Late.3dshapes', 'late')]

    monkeypatch.setattr(gui, 'library_models', slow_walk)
    dialog.library_filter.setText('late')
    _wait(app, lambda: dialog._index_worker is not None)
    assert entered.wait(1)
    assert dialog.library_count_label.text() == 'Indicizzazione della libreria…'
    try:
        dialog.reject()
        assert dialog.result() == QDialog.DialogCode.Rejected
    finally:
        release.set()
        QThreadPool.globalInstance().waitForDone(2000)
        app.processEvents()
    assert dialog.library_list.count() == 0
    assert dialog._library_models is None
    assert dialog.selected_selection() == dict(status='rejected', path='', rotation_deg=0.0)


@pytest.mark.parametrize('value,display', [(None, '?'), (float('nan'), '?'),
                                         (-0.00001, '0'), (2.0, '2'), (0.1254, '0.125')])
def test_dimension_display_handles_missing_and_small_values(value, display):
    assert gui._display_dimensions(value) == display


# --- Shaded STEP preview -----------------------------------------------------
#: Axis-aligned 10 mm cube; the isometric projection shows exactly three of its
#: faces, with three different orientations, which is what the shading must show.
_CUBE = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
         (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
_CUBE_FACES = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 5, 1), (0, 4, 5),
               (1, 6, 2), (1, 5, 6), (2, 7, 3), (2, 6, 7), (3, 4, 0), (3, 7, 4)]


def _render(monkeypatch, vertices, triangles, rotation=0.0, color=None, writer=None):
    """Render a synthetic mesh through _thumbnail, with no FreeCAD installed."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from kicadgen.cad3d import step_generator

    class Shape:
        def tessellate(self, tolerance):
            return ([SimpleNamespace(x=x, y=y, z=z) for x, y, z in vertices],
                    list(triangles))

    monkeypatch.setattr(step_generator, 'find_freecadcmd', lambda: 'fake-freecad')
    monkeypatch.setitem(sys.modules, 'Part', SimpleNamespace(
        read=lambda path: Shape(), makeCompound=lambda shapes: shapes[0]))

    def run(command, **kwargs):
        if writer is not None:
            writer(Path(command[1]).parent / 'mesh.json')
            return
        exec(compile(Path(command[1]).read_text(encoding='utf-8'), command[1], 'exec'), {})

    monkeypatch.setattr(gui.subprocess, 'run', run)
    return gui._thumbnail('example.step', rotation, None, color)


def _regions(image, minimum=2000):
    """Flat areas of the render: rgb -> (pixel count, mean row).

    Only areas far larger than any one-pixel outline can be are kept: a visible
    face of the 280 px object covers order 10^4 pixels, while a stroked edge or
    an antialiased blend covers at most order 10^3, so the cut needs no tuning
    against a particular render.
    """
    background = QColor(gui.PREVIEW_BACKGROUND).rgb()
    stats = {}
    for y in range(image.height()):
        for x in range(image.width()):
            rgb = image.pixel(x, y)
            if rgb == background:
                continue
            count, rows = stats.get(rgb, (0, 0))
            stats[rgb] = (count + 1, rows + y)
    return {rgb: (count, rows / count) for rgb, (count, rows) in stats.items()
            if count >= minimum}


def test_cube_shows_three_shades_brightest_towards_the_light(app, monkeypatch):
    """Each visible side of a box must read as its own surface, not as a blob."""
    image = _render(monkeypatch, _CUBE, _CUBE_FACES)
    assert (image.width(), image.height()) == (340, 340)
    all_colors = {image.pixel(x, y) for x in range(0, 340, 3) for y in range(0, 340, 3)}
    assert len(all_colors) > 1
    regions = _regions(image)
    # Exactly the three visible faces: an outline stroked in a foreign colour
    # would add a fourth large area, which is what made the old render a wireframe.
    assert len(regions) == 3
    lightnesses = sorted(QColor.fromRgb(rgb).lightnessF() for rgb in regions)
    assert len(set(round(value, 3) for value in lightnesses)) == 3
    assert lightnesses[1] - lightnesses[0] > 0.05
    assert lightnesses[2] - lightnesses[1] > 0.05
    # The light comes from above: the top face faces it most directly, and in this
    # isometric projection that face is the upper rhombus of the cube.
    brightest = max(regions, key=lambda rgb: QColor.fromRgb(rgb).lightnessF())
    assert regions[brightest][1] < min(rows for rgb, (_, rows) in regions.items()
                                       if rgb != brightest)
    # Every face stays clear of the background lightness, so the body is visible.
    assert lightnesses[0] > QColor(gui.PREVIEW_BACKGROUND).lightnessF() + 0.1


def test_faces_keep_one_material_and_follow_the_declared_body_colour(app, monkeypatch):
    """Only lightness varies; the body colour, when known, wins over the default."""
    default = _regions(_render(monkeypatch, _CUBE, _CUBE_FACES))
    red = _regions(_render(monkeypatch, _CUBE, _CUBE_FACES, color='#b01818'))
    assert len(red) == 3
    assert set(default) != set(red)
    for source, reference in ((default, gui.DEFAULT_BODY_COLOR), (red, '#b01818')):
        expected = QColor(reference).hueF()
        for rgb in source:
            assert abs(QColor.fromRgb(rgb).hueF() - expected) < 0.03
    # An unusable declared colour must not paint an invalid colour.
    fallback = _regions(_render(monkeypatch, _CUBE, _CUBE_FACES, color='not-a-colour'))
    assert set(fallback) == set(default)


def test_shading_ignores_the_winding_order_of_the_tessellation(app, monkeypatch):
    """Reversed triangles keep their brightness: no face may go black or vanish."""
    reversed_faces = [tuple(reversed(face)) for face in _CUBE_FACES]
    direct = _regions(_render(monkeypatch, _CUBE, _CUBE_FACES))
    flipped = _regions(_render(monkeypatch, _CUBE, reversed_faces))
    assert set(direct) == set(flipped)
    mixed = _regions(_render(monkeypatch, _CUBE,
                             [face if index % 2 else tuple(reversed(face))
                              for index, face in enumerate(_CUBE_FACES)]))
    assert set(mixed) == set(direct)


@pytest.mark.parametrize('triangle', [
    ((0., 0., 0.), (1., 1., 1.), (2., 2., 2.)),      # collinear
    ((3., 1., 2.), (3., 1., 2.), (3., 1., 2.)),      # coincident
    ((0., 0., 0.), (0., 0., 0.), (5., 4., 3.)),      # zero-length edge
])
def test_degenerate_triangle_falls_back_to_the_ambient_floor(triangle):
    value = gui._face_intensity(*triangle)
    assert math.isfinite(value)
    assert value == pytest.approx(gui._AMBIENT)
    color = gui._shaded_color(QColor(gui.DEFAULT_BODY_COLOR), value)
    assert color.isValid()


def test_face_intensity_separates_a_lit_face_from_an_edge_on_face():
    """Positive and negative case of the Lambert term, in eye space."""
    light = gui._LIGHT
    # Normal parallel to the light: the term must saturate at 1.
    facing = ((0., 0., 0.), (light[1], -light[0], 0.),
              (light[0]*light[2], light[1]*light[2], -(light[0]**2 + light[1]**2)))
    assert gui._face_intensity(*facing) == pytest.approx(1.0, abs=1e-4)
    # Normal perpendicular to the light: only the ambient floor remains.
    edge_on = ((0., 0., 0.), (light[0], light[1], light[2]), (light[1], -light[0], 0.))
    assert gui._face_intensity(*edge_on) == pytest.approx(gui._AMBIENT, abs=1e-4)


def test_non_finite_intensity_never_produces_an_invalid_colour():
    for intensity in (float('nan'), float('inf')):
        color = gui._shaded_color(QColor(gui.DEFAULT_BODY_COLOR), intensity)
        assert color.isValid()
        assert 0.0 <= color.lightnessF() <= 1.0


def test_degenerate_mesh_renders_without_crashing_or_black_pixels(app, monkeypatch):
    """A tessellation with flat triangles must still produce a usable image."""
    vertices = [(0., 0., 0.), (5., 5., 5.), (10., 10., 10.), (0., 0., 0.), (0., 10., 4.)]
    faces = [(0, 1, 2), (0, 1, 3), (0, 3, 3), (0, 4, 2)]
    image = _render(monkeypatch, vertices, faces)
    assert not image.isNull()
    background = QColor(gui.PREVIEW_BACKGROUND).rgb()
    darkest = QColor(gui.PREVIEW_BACKGROUND).lightnessF()
    for y in range(0, 340, 2):
        for x in range(0, 340, 2):
            rgb = image.pixel(x, y)
            if rgb == background:
                continue
            # An invalid or NaN colour paints pure black; every real pixel is
            # either a shaded face or an antialiased blend towards the canvas,
            # so it can never be darker than the canvas itself.
            assert rgb != 0xff000000
            assert QColor.fromRgb(rgb).lightnessF() >= darkest - 0.01


@pytest.mark.parametrize('vertices,faces,message', [
    (_CUBE, [], 'Preview geometry unavailable or too complex'),
    ([], _CUBE_FACES, 'Preview geometry unavailable or too complex'),
    (_CUBE, [(0, 1, 2)] * 100_001, 'Preview geometry unavailable or too complex'),
    (_CUBE, [(0, 1, 2, 3)], 'Invalid mesh triangle'),
    ([(0., 0., 0.), (1., 0., 0.), (0., float('nan'), 1.)], [(0, 1, 2)],
     'Invalid mesh coordinates'),
])
def test_mesh_guards_still_reject_unusable_previews(app, monkeypatch, vertices, faces, message):
    with pytest.raises(ValueError, match=message):
        _render(monkeypatch, vertices, faces)


def test_oversized_mesh_file_is_rejected_before_parsing(app, monkeypatch):
    def huge(output):
        output.write_bytes(b'x' * 20_000_001)

    with pytest.raises(ValueError, match='Preview mesh too large'):
        _render(monkeypatch, _CUBE, _CUBE_FACES, writer=huge)
