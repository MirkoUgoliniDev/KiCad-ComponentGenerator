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
from PyQt6.QtWidgets import QDialog, QDialogButtonBox

from kicadgen.gui import model_candidates_dialog as gui


@pytest.fixture
def review(app, tmp_path, monkeypatch):
    pool = QThreadPool.globalInstance()
    monkeypatch.setattr(gui, '_thumbnail', lambda *args, **kwargs: None)
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

    def thumbnail(path, rotation, *_, **kwargs):
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

    def thumbnail(*args, **kwargs):
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
    """@brief Ogni combinazione salvata/legacy produce la stessa scelta di prima.

    @note Contratto riscritto, non allentato: l'asserzione
    ``rotation.isEnabled() != automatic`` non esiste piu' perche' il campo non
    viene piu' disabilitato in modalita' automatica. Disabilitarlo era il
    difetto segnalato dall'utente: il campo restava visibile, con le frecce e
    un valore, e ingoiava in silenzio l'intenzione di chi lo girava. Al suo
    posto si asserisce cio' che il campo disabilitato avrebbe dovuto
    comunicare, cioe' quale delle due modalita' comanda, su un'etichetta
    leggibile senza passare il mouse. Il valore restituito da
    selected_selection() resta identico a prima per ogni combinazione.
    """
    _, component, report = review
    component['model_rotation_deg'] = legacy
    component['model_selection'] = dict(saved, status='confirmed', path=report['candidates'][0]['path'])
    before = deepcopy(component)
    monkeypatch.setattr(gui.QThreadPool, 'globalInstance', lambda: pytest.fail('Eager preview'))
    dialog = gui.ModelCandidatesDialog(component, report)
    assert dialog.auto_rotation.isChecked() == automatic
    assert dialog.rotation.isEnabled()
    assert ('Automatico' in dialog.rotation_state.text()) == automatic
    assert ('override esplicito' in dialog.rotation_state.text()) != automatic
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


def test_turning_the_rotation_takes_over_from_automatic_and_is_previewed(review, app, monkeypatch):
    """@brief Positivo: girare la manopola con «Automatico» attivo non e' un vicolo cieco.

    Il difetto segnalato: il campo restava visibile e con le frecce ma
    disabilitato, quindi l'utente lo girava due volte senza che accadesse
    nulla. L'unico significato possibile di quel gesto e' «voglio questo
    angolo», quindi la spunta automatica deve cadere da sola e l'anteprima
    deve essere ricostruita proprio con il valore chiesto.
    """
    dialog, _, report = review
    calls = []
    monkeypatch.setattr(gui, '_thumbnail', lambda *args, **kwargs: calls.append(args))
    dialog.auto_rotation.setChecked(True)
    dialog.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not dialog._workers and calls)
    assert calls[-1][1] is None
    # Il campo e' utilizzabile in entrambe le modalita': disabilitarlo era il difetto.
    assert dialog.rotation.isEnabled()
    dialog.rotation.setValue(45)
    _wait(app, lambda: not dialog._workers and len(calls) == 2)
    assert not dialog.auto_rotation.isChecked()
    assert dialog.rotation_state.text() == 'in uso: override esplicito'
    # Quinto argomento: la vista dell'anteprima. Contratto cambiato di proposito
    # quando e' stata introdotta la scelta pianta/isometrica: il worker dice al
    # renderer quale camera usare, e il dialogo si apre in pianta.
    assert calls[-1] == (report['candidates'][0]['path'], 45.0, None, None, gui.VIEW_PLAN)
    dialog.accept()
    assert dialog.selected_selection() == dict(
        status='confirmed', path=report['candidates'][0]['path'], rotation_deg=45.0)


def test_untouched_automatic_still_asks_for_the_inferred_orientation(review, app, monkeypatch):
    """@brief Negativo: senza toccare la rotazione, l'anteprima resta automatica."""
    dialog, _, report = review
    calls = []
    monkeypatch.setattr(gui, '_thumbnail', lambda *args, **kwargs: calls.append(args))
    dialog.auto_rotation.setChecked(True)
    dialog.candidate_list.setCurrentRow(1)
    _wait(app, lambda: not dialog._workers and calls)
    # Nessuna rotazione esplicita si insinua: il worker riceve None e le quote.
    # Quinto argomento: la vista dell'anteprima. Contratto cambiato di proposito
    # quando e' stata introdotta la scelta pianta/isometrica: il worker dice al
    # renderer quale camera usare, e il dialogo si apre in pianta.
    assert calls == [(report['candidates'][1]['path'], None, (3, 2), None, gui.VIEW_PLAN)]
    assert dialog.auto_rotation.isChecked()
    assert dialog.rotation_state.text() == 'non in uso: comanda «Automatico»'
    dialog.accept()
    assert dialog.selected_selection() == dict(
        status='confirmed', path=report['candidates'][1]['path'], rotation_deg=None)


def test_explicit_zero_typed_over_automatic_zero_is_not_swallowed(review, app, monkeypatch):
    """@brief Il caso 0°: digitarlo mentre «Automatico» e' attivo deve valere.

    Con valore gia' a 0 e modalita' automatica, ``valueChanged`` non scatta:
    senza la via del testo digitato la richiesta sparirebbe in silenzio, ed e'
    esattamente la richiesta che distingue «automatico» da «esplicito 0°»,
    contratto dichiarato in rotation_hint.
    """
    from PyQt6.QtTest import QTest
    _, component, report = review
    component['model_selection'] = dict(status='confirmed', rotation_deg=None,
                                        path=report['candidates'][0]['path'])
    dialog = gui.ModelCandidatesDialog(component, report)
    calls = []
    monkeypatch.setattr(gui, '_thumbnail', lambda *args, **kwargs: calls.append(args))
    try:
        assert dialog.auto_rotation.isChecked() and dialog.rotation.value() == 0
        dialog.show()
        _wait(app, lambda: not dialog._workers and calls)
        assert calls[-1][1] is None
        dialog.rotation.lineEdit().selectAll()
        QTest.keyClicks(dialog.rotation.lineEdit(), '0')
        assert dialog.rotation.value() == 0
        assert not dialog.auto_rotation.isChecked()
        assert '0°' in dialog.rotation_hint.text()
        _wait(app, lambda: not dialog._workers and len(calls) == 2)
        # Zero esplicito, non None: l'inferenza degli assi resta disattivata.
        # Quinto argomento: la vista dell'anteprima. Contratto cambiato di proposito
        # quando e' stata introdotta la scelta pianta/isometrica: il worker dice al
        # renderer quale camera usare, e il dialogo si apre in pianta.
        assert calls[-1] == (report['candidates'][0]['path'], 0.0, None, None, gui.VIEW_PLAN)
        dialog.accept()
        assert dialog.selected_selection()['rotation_deg'] == 0.0
    finally:
        dialog.reject()


def test_a_step_swallowed_by_the_range_limit_still_claims_the_override(review):
    """@brief Negativo del meccanismo: nemmeno uno scatto senza effetto va perso."""
    from PyQt6.QtCore import QSignalBlocker
    dialog, _, _ = review
    dialog.auto_rotation.setChecked(True)
    # Il blocco evita che la preparazione del caso limite passi per la stessa
    # via che il test deve verificare.
    blocker = QSignalBlocker(dialog.rotation)
    try:
        dialog.rotation.setValue(360)
    finally:
        blocker.unblock()
    assert dialog.auto_rotation.isChecked()
    dialog.rotation.stepBy(1)
    assert dialog.rotation.value() == 360
    assert not dialog.auto_rotation.isChecked()
    assert dialog.rotation_state.text() == 'in uso: override esplicito'
    dialog.candidate_list.setCurrentRow(0)
    dialog.accept()
    assert dialog.selected_selection()['rotation_deg'] == 360.0


def test_restoring_a_saved_angle_never_claims_the_override_by_itself(review, monkeypatch):
    """@brief Negativo: costruire il dialogo non equivale a girare la manopola."""
    _, component, report = review
    component['model_selection'] = dict(status='confirmed', rotation_deg=None,
                                        path=report['candidates'][0]['path'])
    component['model_rotation_deg'] = 90
    monkeypatch.setattr(gui.QThreadPool, 'globalInstance', lambda: pytest.fail('Eager preview'))
    dialog = gui.ModelCandidatesDialog(component, report)
    try:
        assert dialog.auto_rotation.isChecked()
        assert dialog.rotation_state.text() == 'non in uso: comanda «Automatico»'
        dialog.accept()
        assert dialog.selected_selection()['rotation_deg'] is None
    finally:
        dialog.reject()


def test_rapid_rotation_changes_end_on_the_last_value(review, app, monkeypatch):
    """@brief Coalescenza: l'ultimo valore chiesto e' quello disegnato.

    Con un worker gia' in corso ``_start_preview`` non ne avvia un secondo; la
    rientranza di ``_preview_ready`` deve quindi ripartire da sola sull'ultimo
    token, altrimenti l'utente vedrebbe l'anteprima di un angolo intermedio.
    """
    dialog, _, report = review
    entered, release = threading.Event(), threading.Event()
    calls = []

    def thumbnail(path, rotation, *_, **kwargs):
        calls.append((path, rotation))
        if len(calls) == 1:
            entered.set()
            release.wait(2)
        return None

    monkeypatch.setattr(gui, '_thumbnail', thumbnail)
    dialog.candidate_list.setCurrentRow(0)
    assert entered.wait(1)
    for value in (10, 20, 30, 40):
        dialog.rotation.setValue(value)
    # Un solo processo per volta: le richieste intermedie non ne aprono altri.
    assert len(calls) == 1
    release.set()
    _wait(app, lambda: not dialog._workers and len(calls) >= 2)
    assert calls[-1] == (report['candidates'][0]['path'], 40.0)
    assert len(calls) == 2
    assert dialog.rotation.value() == 40


def test_the_mode_in_force_is_readable_on_the_dialog_without_hovering(review, monkeypatch):
    """@brief Lo stato si legge sul dialogo, non in un tooltip da scoprire.

    Contratto cambiato di proposito (utente, 2026-09-24): il campo numerico e
    la casella «Automatico» sono diventati stato interno nascosto; cio' che si
    legge a vista e' l'angolo sotto le frecce, con chi l'ha scelto.
    """
    dialog, _, _ = review
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    label = dialog.rotation_angle_label
    assert label.isVisibleTo(dialog)
    assert not dialog.rotation_state.isVisibleTo(dialog)
    assert label.text() == '37°'
    dialog.auto_rotation.setChecked(True)
    assert label.text() == 'auto'
    assert dialog.rotate_auto_button.isChecked()
    dialog._auto_angle = 90.0
    # Angolo del modello mostrato ora (dal 25/09 vale solo per quello).
    dialog._auto_angle_path = getattr(dialog, '_path', '')
    dialog._update_rotation_readout()
    assert label.text() == '90° auto'
    # Stato interno ancora coerente con il modo in vigore.
    assert dialog.rotation_state.text() == 'non in uso: comanda «Automatico»'
    assert 'Modificando la rotazione' in dialog.rotation_hint.text()
    dialog.auto_rotation.setChecked(False)
    assert label.text() == '37°'
    assert not dialog.rotate_auto_button.isChecked()
    assert '0°' in dialog.rotation_hint.text()


def test_auto_worker_receives_component_dimensions(review, app, monkeypatch):
    dialog, _, report = review
    calls = []
    monkeypatch.setattr(gui, '_thumbnail', lambda *args, **kwargs: calls.append(args))
    dialog.auto_rotation.setChecked(True)
    dialog.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not dialog._workers)
    # The fourth argument is the sampled body colour: this component declares
    # none, so the renderer must fall back to its documented default.
    # Quinto argomento: la vista dell'anteprima. Contratto cambiato di proposito
    # quando e' stata introdotta la scelta pianta/isometrica: il worker dice al
    # renderer quale camera usare, e il dialogo si apre in pianta.
    assert calls == [(report['candidates'][0]['path'], None, (3, 2), None, gui.VIEW_PLAN)]


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
    monkeypatch.setattr(gui, '_thumbnail', lambda *args, **kwargs: calls.append(args))
    _filter(app, dialog, 'usb')
    dialog.library_list.setCurrentRow(0)
    _wait(app, lambda: not dialog._workers)
    # Quinto argomento: la vista dell'anteprima. Contratto cambiato di proposito
    # quando e' stata introdotta la scelta pianta/isometrica: il worker dice al
    # renderer quale camera usare, e il dialogo si apre in pianta.
    assert calls == [(str(models[3].path), 37.0, None, None, gui.VIEW_PLAN)]
    dialog.auto_rotation.setChecked(True)
    _wait(app, lambda: len(calls) == 2 and not dialog._workers)
    assert calls[-1] == (str(models[3].path), None, (3, 2), None, gui.VIEW_PLAN)


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


@pytest.mark.parametrize('declared,expected', [
    ('#112233', '#112233'),
    (' #112233 ', '#112233'),      # una spaziatura non deve costare il colore
    ('\tred\n', 'red'),
    ('   ', None),
    ('#12345g', None),             # caso negativo: resta il colore predefinito
    (None, None),
    (123456, None),
])
def test_declared_body_colour_survives_spacing_but_not_nonsense(app, declared, expected):
    """Il colore campionato dalla foto va usato, o scartato, per merito suo."""
    component = dict(name='U1', length_mm=3, width_mm=2)
    if declared is not None:
        component['color_hex'] = declared
    dialog = gui.ModelCandidatesDialog(component, {'candidates': []})
    try:
        assert dialog._body_color == expected
    finally:
        dialog.reject()


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


def _triangle_with_normal(normal):
    """Eye-space triangle whose geometric normal is exactly @p normal.

    Built from two vectors perpendicular to the normal, so the test states the
    orientation it means instead of hand-picking vertices that happen to give it.
    """
    nx, ny, nz = normal
    length = math.sqrt(nx*nx + ny*ny + nz*nz)
    nx, ny, nz = nx/length, ny/length, nz/length
    other = (1.0, 0.0, 0.0) if abs(nx) < 0.9 else (0.0, 1.0, 0.0)
    ux, uy, uz = (ny*other[2] - nz*other[1], nz*other[0] - nx*other[2],
                  nx*other[1] - ny*other[0])
    scale = math.sqrt(ux*ux + uy*uy + uz*uz)
    ux, uy, uz = ux/scale, uy/scale, uz/scale
    vx, vy, vz = ny*uz - nz*uy, nz*ux - nx*uz, nx*uy - ny*ux
    return (0.0, 0.0, 0.0), (ux, uy, uz), (vx, vy, vz)


def _dot_with_light(normal):
    length = math.sqrt(sum(value*value for value in normal))
    return sum(value*light for value, light in zip(normal, gui._LIGHT))/length


#: Eye-space normal of an undercut actually measured on a 10 mm box whose +X
#: flank is chamfered under the body: it points towards the camera (z > 0, so
#: the face is plainly visible) yet away from the light (dot < 0, so the face
#: must be in shadow). Connector shroud lips, draft angles under an IC body and
#: the chamfered underside of a shield all land in this family.
_UNDERCUT_NORMAL = (0.606, 0.770, 0.198)


def test_an_undercut_face_is_in_shadow_and_not_lit_like_one_facing_the_light():
    """Positive and negative case of the face a signed Lambert term must separate.

    Both normals are visible from the camera and differ only in which way they
    lean relative to the light; the shading must therefore separate them. The
    absolute value this module used before could not: it gave the undercut the
    intensity of its mirror image, 0.685 instead of 0.280, and the chamfer of a
    10 mm box then rendered at lightness 0.702 against the 0.657 of the lit
    flank next to it — brighter than a face that does point at the light.
    """
    away = _dot_with_light(_UNDERCUT_NORMAL)
    assert away < 0 and _UNDERCUT_NORMAL[2] > 0
    undercut = gui._face_intensity(*_triangle_with_normal(_UNDERCUT_NORMAL))
    assert undercut == pytest.approx(gui._AMBIENT, abs=1e-4)
    # Exactly the value the discarded abs() produced: naming it keeps the
    # regression readable instead of hiding behind an inequality.
    assert undercut != pytest.approx(
        gui._AMBIENT + (1.0 - gui._AMBIENT)*abs(away), abs=1e-3)
    # The same surface leaning the other way is lit, and by the full term.
    towards = tuple(-value for value in _UNDERCUT_NORMAL[:2]) + (_UNDERCUT_NORMAL[2],)
    lit = gui._face_intensity(*_triangle_with_normal(towards))
    assert lit == pytest.approx(
        gui._AMBIENT + (1.0 - gui._AMBIENT)*_dot_with_light(towards), abs=1e-4)
    assert lit - undercut > 0.5
    # Winding stays irrelevant: the normal is oriented against the camera, not
    # against the triangle order.
    for normal in (_UNDERCUT_NORMAL, towards):
        a, b, c = _triangle_with_normal(normal)
        assert gui._face_intensity(c, b, a) == pytest.approx(
            gui._face_intensity(a, b, c), abs=1e-9)


#: 10 mm box whose +X flank is chamfered under the body from z=0 to z=6: the
#: undercut of the previous test as a whole solid, the shape a shield or a
#: connector shroud presents to this preview.
_CHAMFER_Z, _CHAMFER_SLOPE = 6.0, 0.6
_UNDERCUT = [(0, 0, 0), (10-_CHAMFER_SLOPE*_CHAMFER_Z, 0, 0),
             (10-_CHAMFER_SLOPE*_CHAMFER_Z, 10, 0), (0, 10, 0),
             (10, 0, _CHAMFER_Z), (10, 10, _CHAMFER_Z),
             (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
_UNDERCUT_FACES = [
    (6, 7, 8), (6, 8, 9),                     # top
    (4, 7, 8), (4, 8, 5),                     # +X flank above the chamfer
    (1, 4, 5), (1, 5, 2),                     # the chamfer itself
    # Visible faces depend on the camera: the isometric camera sits above
    # (+1, -1, +1) since the mirrored projection was fixed, so the lit flank
    # is now -Y and +Y is hidden. The chamfer normal is the same in both.
    (3, 2, 5), (3, 5, 8), (3, 8, 9),          # +Y flank, hidden
    (0, 1, 4), (0, 4, 7), (0, 7, 6),          # -Y flank, lit
    (0, 3, 9), (0, 9, 6),                     # -X flank, hidden
    (0, 1, 2), (0, 2, 3),                     # bottom, hidden
]


def test_an_undercut_solid_adds_no_tone_brighter_than_its_lit_flank(app, monkeypatch):
    """A chamfer cut under a flank can only lose light, never gain it."""
    cube = {round(QColor.fromRgb(rgb).lightnessF(), 3)
            for rgb in _regions(_render(monkeypatch, _CUBE, _CUBE_FACES))}
    regions = _regions(_render(monkeypatch, _UNDERCUT, _UNDERCUT_FACES))
    tones = {round(QColor.fromRgb(rgb).lightnessF(), 3) for rgb in regions}
    # Turning a surface further from the light cannot invent a shade the plain
    # box does not have: before the fix the chamfer painted 0.702, a fourth tone
    # brighter than the 0.657 flank it cuts into, and the chamfer disappeared.
    assert tones <= cube, (tones, cube)
    assert max(tones) == max(cube)
    # The face pointing at the light stays the brightest, and it is the top one.
    brightest = max(regions, key=lambda rgb: QColor.fromRgb(rgb).lightnessF())
    assert regions[brightest][1] < min(rows for rgb, (_, rows) in regions.items()
                                       if rgb != brightest)
    flipped = _regions(_render(monkeypatch, _UNDERCUT,
                               [tuple(reversed(face)) for face in _UNDERCUT_FACES]))
    assert set(flipped) == set(regions)


#: Coplanar fan: one flat disc cut into 180 triangles that all share the centre
#: vertex. Every triangle has the same normal, so the whole disc must read as a
#: single colour; only the seams between the triangles can break it.
_FAN_SIDES = 180
_FAN = [(0.0, 0.0, 0.0)] + [
    (5*math.cos(2*math.pi*index/_FAN_SIDES), 5*math.sin(2*math.pi*index/_FAN_SIDES), 0.0)
    for index in range(_FAN_SIDES)]
_FAN_FACES = [(0, 1+index, 1+(index+1) % _FAN_SIDES) for index in range(_FAN_SIDES)]


def _interior_deviation(image):
    """Worst lightness deviation from the flat fill, away from the silhouette.

    Only pixels at least three from the background are examined, so the
    antialiased outline of the disc — which is not a seam — cannot be mistaken
    for one.
    """
    background = QColor(gui.PREVIEW_BACKGROUND).rgb()
    solid = [[image.pixel(x, y) != background for x in range(image.width())]
             for y in range(image.height())]
    values = []
    for y in range(3, image.height()-3):
        for x in range(3, image.width()-3):
            if not solid[y][x]:
                continue
            if all(solid[y+dy][x+dx] for dx, dy in
                   ((3, 0), (-3, 0), (0, 3), (0, -3), (2, 2), (2, -2), (-2, 2), (-2, -2))):
                values.append(QColor.fromRgb(image.pixel(x, y)).lightnessF())
    assert len(values) > 10_000, len(values)
    flat = max(set(values), key=values.count)
    return flat, max(abs(value - flat) for value in values)


def test_coplanar_triangles_are_painted_without_visible_seams(app, monkeypatch):
    """The same-colour outline must close the gaps antialiasing leaves.

    @note Without it the file's own assertions stay green — _regions drops the
    seam as too small — so this is the only test that holds the pen in place.
    The tolerance is the 0.05 the cube test already uses to call two surfaces
    different: a seam may not open a gap as wide as the difference between two
    real faces. Measured worst deviation 0.020 with the pen and 0.247 without,
    so neither side of that limit is close to it.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QPen

    flat, deviation = _interior_deviation(_render(monkeypatch, _FAN, _FAN_FACES))
    assert deviation <= 0.05, (flat, deviation)
    # Caso negativo: senza la penna le cuciture riaprono, e si vedono.
    monkeypatch.setattr(gui, 'QPen', lambda *args: QPen(Qt.PenStyle.NoPen))
    seamed_flat, seamed = _interior_deviation(_render(monkeypatch, _FAN, _FAN_FACES))
    assert seamed_flat == flat
    assert seamed > 0.05, seamed


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


# ── Frecce di rotazione sull'anteprima (utente, 2026-09-24) ─────────────────

def test_arrows_turn_from_the_automatic_angle_and_claim_the_override(review, monkeypatch):
    """@brief Positivo: dall'automatico il primo clic parte dall'angolo scelto dal programma."""
    dialog, _, _ = review
    starts = []
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: starts.append(
        None if dialog.auto_rotation.isChecked() else dialog.rotation.value()))
    dialog.auto_rotation.setChecked(True)
    dialog._auto_angle = 90.0
    # Angolo del modello mostrato ora (dal 25/09 vale solo per quello).
    dialog._auto_angle_path = getattr(dialog, '_path', '')
    starts.clear()
    dialog.rotate_ccw_button.click()
    assert not dialog.auto_rotation.isChecked()
    assert dialog.rotation.value() == 180
    assert starts[-1] == 180
    dialog.rotate_cw_button.click()
    dialog.rotate_cw_button.click()
    assert dialog.rotation.value() == 0
    dialog.rotate_cw_button.click()
    assert dialog.rotation.value() == 270
    assert dialog.rotation_angle_label.text() == '270°'


def test_arrows_without_a_known_automatic_angle_start_from_zero(review, monkeypatch):
    """@brief Negativo: angolo automatico ignoto, il giro parte da 0 e non dal valore salvato."""
    dialog, _, _ = review
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    dialog.auto_rotation.setChecked(True)
    dialog._auto_angle = None
    # Angolo del modello mostrato ora (dal 25/09 vale solo per quello).
    dialog._auto_angle_path = getattr(dialog, '_path', '')
    dialog.rotate_ccw_button.click()
    assert dialog.rotation.value() == 90 and not dialog.auto_rotation.isChecked()


def test_an_arrow_landing_on_the_stored_value_still_leaves_automatic(review, monkeypatch):
    """@brief Stesso valore del campo nascosto: la scelta esplicita vale comunque."""
    dialog, _, _ = review
    starts = []
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: starts.append(dialog.rotation.value()))
    dialog.rotation.setValue(180)
    dialog.auto_rotation.setChecked(True)
    dialog._auto_angle = 90.0
    # Angolo del modello mostrato ora (dal 25/09 vale solo per quello).
    dialog._auto_angle_path = getattr(dialog, '_path', '')
    starts.clear()
    dialog.rotate_ccw_button.click()
    assert not dialog.auto_rotation.isChecked() and dialog.rotation.value() == 180
    assert starts == [180]
    # Il valore salvato passa da accept(), non da una formula ripetuta nel test.
    dialog.candidate_list.setCurrentRow(0)
    dialog.rotation.setValue(180)
    dialog.accept()
    assert dialog.selected_selection()['rotation_deg'] == 180.0


def test_the_automatic_icon_restores_the_inferred_orientation(review, monkeypatch):
    """@brief L'icona «automatico» comanda la stessa modalita' della casella nascosta."""
    dialog, _, _ = review
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    dialog.auto_rotation.setChecked(False)
    dialog.rotate_auto_button.click()
    assert dialog.auto_rotation.isChecked() and dialog.rotate_auto_button.isChecked()
    dialog.rotate_ccw_button.click()
    assert not dialog.rotate_auto_button.isChecked()


def test_rotation_icons_explain_themselves_in_the_tooltip(review):
    """@brief Ogni icona ha nome e spiegazione al passaggio del mouse."""
    dialog, _, _ = review
    for button in (dialog.rotate_ccw_button, dialog.rotate_cw_button, dialog.rotate_auto_button):
        tip = button.toolTip()
        assert '\n' in tip and tip.split('\n')[0].strip()
    assert 'antiorario' in dialog.rotate_ccw_button.toolTip()
    assert 'antiorario' not in dialog.rotate_cw_button.toolTip()


@pytest.mark.parametrize('output, expected', [
    ('KICADGEN_IMPORT_OK:a.step; parts=1; local_rotation_deg=270', 270.0),
    ('KICADGEN_IMPORT_OK:a.step; parts=1; local_rotation_deg=-90', 270.0),
    ('local_rotation_deg=360', 0.0),
    ('local_rotation_deg=1e-05', 0.0),
    ('KICADGEN_IMPORT_OK:a.step; parts=1', None),
    ('', None),
])
def test_the_automatic_angle_is_read_from_the_importer_line(output, expected):
    assert gui._reported_auto_angle(output) == expected


def test_only_the_automatic_preview_carries_the_chosen_angle(monkeypatch):
    """@brief Positivo in automatico, negativo con angolo esplicito."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from kicadgen.cad3d import step_generator

    class Shape:
        def tessellate(self, tolerance):
            points = [(0, 0, 0), (8, 0, 0), (0, 2, 1)]
            return [SimpleNamespace(x=x, y=y, z=z) for x, y, z in points], [(0, 1, 2)]

    monkeypatch.setattr(step_generator, 'find_freecadcmd', lambda: 'fake-freecad')
    monkeypatch.setitem(sys.modules, 'freecad_parts', SimpleNamespace(
        _library_model=lambda *args: [(Shape(), '#ffffff')]))
    monkeypatch.setitem(sys.modules, 'Part', SimpleNamespace(
        read=lambda path: Shape(), makeCompound=lambda shapes: shapes[0]))

    def run(command, **kwargs):
        old_path = sys.path[:]
        try:
            exec(compile(Path(command[1]).read_text(encoding='utf-8'), command[1], 'exec'), {})
        finally:
            sys.path[:] = old_path
        return SimpleNamespace(stdout='KICADGEN_IMPORT_OK:x; parts=1; local_rotation_deg=90\n')

    monkeypatch.setattr(gui.subprocess, 'run', run)
    automatic = gui._thumbnail('example.step', None, (8, 2))
    explicit = gui._thumbnail('example.step', 90)
    assert gui._image_auto_angle(automatic) == 90.0
    assert gui._image_auto_angle(explicit) is None


def test_the_automatic_preview_updates_the_readout(review, app, monkeypatch):
    """@brief L'angolo scelto dall'automatico arriva sotto le frecce."""
    dialog, _, _ = review
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.setText(gui.AUTO_ROTATION_KEY, '90')
    # Un'anteprima manuale che portasse un angolo DIVERSO: se la guardia
    # «solo le anteprime automatiche» sparisse, `_auto_angle` diventerebbe 0 e
    # il negativo qui sotto fallirebbe (audit 25/09: prima era sempre 90).
    manual = QImage(4, 4, QImage.Format.Format_RGB32)
    manual.setText(gui.AUTO_ROTATION_KEY, '0')
    monkeypatch.setattr(gui, '_thumbnail',
                        lambda path, rotation, *rest, **kwargs: image if rotation is None else manual)
    dialog.auto_rotation.setChecked(True)
    dialog.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not dialog._workers)
    assert dialog._auto_angle == 90.0
    assert dialog.rotation_angle_label.text() == '90° auto'
    # Negativo: una miniatura manuale non tocca l'angolo automatico ricordato.
    dialog.rotate_ccw_button.click()
    _wait(app, lambda: not dialog._workers)
    assert dialog._auto_angle == 90.0 and dialog.rotation_angle_label.text() == '180°'


# ── Audit 2026-09-25: angolo automatico del modello giusto, anteprima come il CAD ──

def test_the_previous_models_automatic_angle_is_not_used_for_the_next(review, monkeypatch):
    """Negativo: A in automatico a 90; si sceglie B, la sua anteprima non e' ancora
    arrivata -> nessun «90° auto» e la prima freccia parte da 0, non da 90."""
    dialog, _, _ = review
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    dialog.auto_rotation.setChecked(True)
    dialog._auto_angle, dialog._auto_angle_path = 90.0, 'A.step'
    dialog._path = 'B.step'
    dialog._update_rotation_readout()
    assert dialog.rotation_angle_label.text() == 'auto'
    dialog.rotate_ccw_button.click()
    assert dialog.rotation.value() == 90


def test_leaving_automatic_from_the_icon_keeps_the_model_where_it_is(review, monkeypatch):
    """Positivo: automatico a 90 per il modello mostrato -> manuale a 90, nessun salto."""
    dialog, _, _ = review
    monkeypatch.setattr(dialog, '_start_preview', lambda *args: None)
    dialog.rotation.setValue(0)
    dialog.auto_rotation.setChecked(True)
    dialog._auto_angle = 90.0
    dialog._auto_angle_path = getattr(dialog, '_path', '')
    dialog.rotate_auto_button.click()
    assert not dialog.auto_rotation.isChecked() and dialog.rotation.value() == 90


def test_the_automatic_preview_gets_the_same_placement_as_the_final_cad(monkeypatch):
    """Verso osservato e delta del menu 3D arrivano all'importatore dell'anteprima."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from kicadgen.cad3d import step_generator
    seen = []

    class Shape:
        def tessellate(self, tolerance):
            points = [(0, 0, 0), (8, 0, 0), (0, 2, 1)]
            return [SimpleNamespace(x=x, y=y, z=z) for x, y, z in points], [(0, 1, 2)]

    def library_model(component, *args):
        seen.append(component)
        return [(Shape(), '#ffffff')]
    monkeypatch.setattr(step_generator, 'find_freecadcmd', lambda: 'fake-freecad')
    monkeypatch.setitem(sys.modules, 'freecad_parts', SimpleNamespace(_library_model=library_model))
    monkeypatch.setitem(sys.modules, 'Part', SimpleNamespace(
        read=lambda path: Shape(), makeCompound=lambda shapes: shapes[0]))

    def run(command, **kwargs):
        exec(compile(Path(command[1]).read_text(encoding='utf-8'), command[1], 'exec'), {})
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(gui.subprocess, 'run', run)
    worker = gui._Preview(1, 'x.step', None, (8, 2), placement={
        'landmark_side': 'left', 'model_rotation_delta_deg': 90})
    worker.run()
    assert seen and seen[0]['landmark_side'] == 'left'
    assert seen[0]['model_rotation_delta_deg'] == 90


def test_an_explicit_angle_is_previewed_with_the_3d_menu_delta(review, app, monkeypatch):
    """Il CAD finale somma il delta del menu 3D anche a un angolo scelto: l'anteprima pure."""
    dialog, _, _ = review
    calls = []
    monkeypatch.setattr(gui, '_thumbnail', lambda *args, **kwargs: calls.append(args[1]))
    dialog._placement = {'model_rotation_delta_deg': 90}
    dialog.auto_rotation.setChecked(False)
    dialog.rotation.setValue(180)
    dialog.candidate_list.setCurrentRow(0)
    _wait(app, lambda: not dialog._workers and calls)
    assert calls[-1] == 270
