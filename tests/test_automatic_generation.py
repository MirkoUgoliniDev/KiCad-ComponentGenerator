"""Standard/advanced workflow: no network or real CAD subprocess required."""
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_photo_inventory_gui import window, gui, ExtractedParams, QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidgetItem, QMessageBox


def acquired(layout='matrix'):
    return ExtractedParams(name='AutoModule', description='', tags='', confidence='high', notes='notes',
        layout=layout, rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=7.62,
        pad_diameter_mm=1.7, drill_mm=1., body_length_mm=20, body_width_mm=10,
        body_height_mm=3, castellated_sides=[dict(edge='left', count=3, pitch_mm=2.54, first_pin=1)])


def begin(window, monkeypatch):
    monkeypatch.setattr(gui.ExtractWorker, 'start', lambda self: None)
    monkeypatch.setattr(gui.GenerateWorker, 'start', lambda self: None)
    row = QListWidgetItem('photo.png')
    row.setCheckState(Qt.CheckState.Checked)
    window.docs_list.addItem(row)
    window._extract_with_ai()


def test_standard_tabs_hidden_and_advanced_toggle_preserves_fields(window, monkeypatch):
    assert not window.tabs.isTabVisible(1) and not window.tabs.isTabVisible(2)
    assert window.tabs.isTabVisible(0) and window.tabs.isTabVisible(3) and window.tabs.isTabVisible(4)
    old_tab = window.tabs.widget(1)
    window.field_rows.setValue(3)
    generate = Mock()
    monkeypatch.setattr(window, '_generate', generate)
    window._config['advanced_user'] = True
    window._apply_user_mode()
    window.tabs.setCurrentIndex(1)
    window._config['advanced_user'] = False
    window._apply_user_mode()
    assert window.tabs.currentIndex() == 0
    assert window.tabs.widget(1) is old_tab and window.field_rows.value() == 3
    generate.assert_not_called()


def test_photo_review_is_advanced_only_and_preserves_acquired_photo(window):
    assert window.btn_photo_comparison.isHidden()
    assert not window.btn_photo_comparison.isEnabled()
    params = acquired('castellated')
    params.photo_png = b'photo fixture'
    window._on_extracted(params)
    assert window.btn_photo_comparison.isHidden()
    assert window.btn_photo_comparison.isEnabled()
    for advanced in (True, False, True):
        window._config['advanced_user'] = advanced
        window._apply_user_mode()
        assert window.btn_photo_comparison.isHidden() is (not advanced)
        assert window.btn_photo_comparison.isEnabled()
        assert window._photo_png == b'photo fixture'
        assert not window.btn_ai_log.isHidden()
        assert not window.btn_component_prompt.isHidden()


def test_3d_editors_follow_expert_mode_without_discarding_data(window):
    from kicadgen.cad3d.annotations import EdgeFilletAnnotation
    assert window.model_3d_settings_scroll.isHidden()
    assert window.group_annotations.isHidden()
    assert window.group_components.isHidden()
    window._on_extracted(acquired('castellated'))
    window.fillet_annotations.append(EdgeFilletAnnotation(edge='top-left', radius_mm=1))
    window._refresh_annotations_table()
    window._set_photo_components([dict(name='body', kind='generic', x_mm=0, y_mm=0,
                                      length_mm=2, width_mm=1, height_mm=1)])
    original = window._components_from_table()
    assert window.group_components.isHidden()  # Extraction must not reveal the editor.
    for advanced in (True, False, True):
        window._config['advanced_user'] = advanced
        window._apply_user_mode()
        assert window.model_3d_settings_scroll.isHidden() is (not advanced)
        assert window.group_annotations.isHidden() is (not advanced)
        assert window.group_components.isHidden() is (not advanced)
        assert window.table_annotations.rowCount() == 1
        assert window.fillet_annotations[0].radius_mm == 1
        assert window._components_from_table() == original
        assert not window.btn_regen_3d.isHidden()
        assert window.btn_terminal_detail.isHidden() and window.btn_lead_refinement.isHidden()
    window.group_castellated.setChecked(False)
    assert window.group_components.isHidden()
    assert not window.group_annotations.isHidden()


@pytest.mark.parametrize('layout', ['matrix', 'castellated'])
def test_standard_starts_exactly_once_after_extract_thread_finished(window, monkeypatch, layout):
    begin(window, monkeypatch)
    window._on_extracted(acquired(layout))
    assert window._worker is None
    window._on_extract_finished()
    worker = window._worker
    assert worker is not None and worker.body.height_mm == 3
    assert window._automatic_generation_active
    assert not window.analysis_overlay.isHidden()
    assert 'Generazione' in window.analysis_overlay.title.text()
    assert not window.tabs.isEnabled() and not window.menuBar().isEnabled()
    window._on_extract_finished()  # Duplicate finish must not restart/hide generation.
    assert window._worker is worker and not window.analysis_overlay.isHidden()
    monkeypatch.setattr(window, '_draw_preview', lambda: None)
    monkeypatch.setattr(window, '_draw_preview_3d', lambda: None)
    monkeypatch.setattr(window, '_show_generated_mesh', lambda p: None)
    worker.done.emit('footprint.kicad_mod')
    worker.cad3d_done.emit('model.step')
    worker.finished.emit()
    assert not window._automatic_generation_active
    assert window.analysis_overlay.isHidden()
    assert window.tabs.isEnabled() and window.menuBar().isEnabled()
    assert window.btn_extract_ai.isEnabled() and window.btn_generate.isEnabled()
    assert window.tabs.currentIndex() == 4
    assert not window.tabs.isTabVisible(1)


def test_advanced_keeps_manual_generation(window, monkeypatch):
    window._config['advanced_user'] = True
    window._apply_user_mode()
    begin(window, monkeypatch)
    window._on_extracted(acquired())
    window._on_extract_finished()
    assert window._worker is None
    assert window.tabs.isTabVisible(1) and window.tabs.isTabVisible(2)


@pytest.mark.parametrize('failure', ['missing_body', 'invalid_footprint', 'failed_extraction', 'unsupported_3d', 'unsafe_name'])
def test_bad_analysis_never_autogenerates_or_reuses_old_body(window, monkeypatch, failure):
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args))
    begin(window, monkeypatch)
    params = acquired()
    if failure == 'failed_extraction':
        window._on_extract_error('offline failure')
    else:
        if failure == 'missing_body': params.body_height_mm = None
        if failure == 'invalid_footprint': params.drill_mm = 2  # Greater than pad size.
        if failure == 'unsupported_3d': params.package_geometry = {'family': 'qfn'}
        if failure == 'unsafe_name': params.name = '../outside'
        window._on_extracted(params)
    window._on_extract_finished()
    assert window._worker is None
    assert window.btn_extract_ai.isEnabled() and window.btn_generate.isEnabled()
    assert not window._automatic_generation_active and window.analysis_overlay.isHidden()
    assert warnings


@pytest.mark.parametrize('overwrite', [False, True])
def test_existing_outputs_need_explicit_confirmation(window, monkeypatch, overwrite):
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a))
    begin(window, monkeypatch)
    window._on_extracted(acquired())
    path = window._project_dir() / 'AutoModule.3dshapes' / 'AutoModule.step'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('keep existing')
    questions = []
    def confirm(*args):
        questions.append(args)
        return QMessageBox.StandardButton.Yes if overwrite else QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, 'question', confirm)
    window._on_extract_finished()
    assert len(questions) == 1 and str(path) in questions[0][2]
    assert questions[0][-1] == QMessageBox.StandardButton.No
    assert path.read_text() == 'keep existing'  # Worker is mocked, even for acceptance.
    assert (window._worker is not None) is overwrite
    if overwrite:
        window._on_error('test stopped')
        window._on_generation_finished()


@pytest.mark.parametrize('failure', ['footprint', '3d', 'start'])
def test_generation_failure_releases_ui_and_reports_partial_success(window, monkeypatch, failure):
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a))
    begin(window, monkeypatch)
    window._on_extracted(acquired())
    if failure == 'start':
        def broken(self): raise RuntimeError('cannot start')
        monkeypatch.setattr(gui.GenerateWorker, 'start', broken)
    window._on_extract_finished()
    if failure == 'footprint':
        window._worker.error.emit('cannot write footprint')
        window._worker.finished.emit()
    elif failure == '3d':
        monkeypatch.setattr(window, '_draw_preview', lambda: None)
        monkeypatch.setattr(window, '_draw_preview_3d', lambda: None)
        window._worker.done.emit('footprint.kicad_mod')
        window._worker.cad3d_error.emit('FreeCAD unavailable')
        window._worker.finished.emit()
        assert window.tabs.currentIndex() == 3
        assert 'Footprint generato' in warnings[0][2]
    assert warnings
    assert window.tabs.isEnabled() and window.menuBar().isEnabled()
    assert window.btn_generate.isEnabled() and window.btn_extract_ai.isEnabled()
    assert window.analysis_overlay.isHidden()


def test_no_overlapping_generation_or_extraction(window, monkeypatch):
    begin(window, monkeypatch)
    window._on_extracted(acquired())
    window._on_extract_finished()
    worker = window._worker
    extraction = window._extract_worker
    window._extract_with_ai()
    window._generate()
    window._regenerate_3d()
    assert window._worker is worker and window._extract_worker is extraction
    assert window._cad3d_worker is None
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: None)
    window._on_generation_finished()


def test_mode_preference_survives_restart(window, monkeypatch):
    from kicadgen.gui import app_config
    app_config.save_config({'advanced_user': True})
    monkeypatch.setattr(gui, 'load_config', app_config.load_config)
    restored = gui.MainWindow()
    try:
        assert restored._advanced_user()
        assert restored.tabs.isTabVisible(1) and restored.tabs.isTabVisible(2)
    finally:
        restored.close()


@pytest.mark.parametrize('answer', [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No])
def test_extract_finish_during_rename_modal_does_not_drop_autogeneration(window, monkeypatch, answer):
    from PyQt6.QtCore import QTimer
    begin(window, monkeypatch)
    original_name = window._component_name()
    docs = window._docs_dir()
    docs.mkdir(parents=True, exist_ok=True)
    (docs / 'photo.png').write_bytes(b'offline fixture')
    observed = []
    def question(parent, title, text, *args):
        box = QMessageBox(parent)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        def finish_thread_inside_modal():
            window._extract_worker.finished.emit()
            observed.append(window._worker is None)
            box.done(answer)
        QTimer.singleShot(0, finish_thread_inside_modal)
        return box.exec()
    monkeypatch.setattr(QMessageBox, 'question', question)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)
    window._on_extracted(acquired())
    assert observed == [True]  # Never generate until all fields have been applied.
    assert window._worker is not None
    assert window._worker.params.name == ('AutoModule' if answer == QMessageBox.StandardButton.Yes else original_name)
    assert window._worker.body.height_mm == 3
    window._on_error('test stopped')
    window._on_generation_finished()


@pytest.mark.parametrize('rename_dialog', [False, True])
def test_real_qthread_handoff_generates_footprint_and_requests_3d(window, monkeypatch, rename_dialog):
    from PyQt6.QtCore import QEventLoop, QTimer
    monkeypatch.setattr(gui.ExtractWorker, 'run', lambda self: self.done.emit(acquired()))
    cad_calls = []
    warnings = []
    def cad(params, body, project, fillets, texts):
        cad_calls.append((params, body))
        path = project / 'AutoModule.3dshapes' / 'AutoModule.step'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('offline CAD fixture')
        return {'step_path': path}
    monkeypatch.setattr(gui, '_generate_3d', cad)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a))
    modal_observations = []
    if rename_dialog:
        docs = window._docs_dir()
        docs.mkdir(parents=True, exist_ok=True)
        (docs / 'photo.png').write_bytes(b'offline fixture')
        def confirm_rename(parent, title, text, *args):
            box = QMessageBox(parent)
            box.setText(text)
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            def accept():
                modal_observations.append((window._extract_finish_pending, window._worker is None))
                box.done(QMessageBox.StandardButton.Yes)
            QTimer.singleShot(50, accept)
            return box.exec()
        monkeypatch.setattr(QMessageBox, 'question', confirm_rename)
    row = QListWidgetItem('photo.png')
    row.setCheckState(Qt.CheckState.Checked)
    window.docs_list.addItem(row)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: loop.quit() if window._worker is not None
                          and not window._worker.isRunning() and not window._automatic_generation_active else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    timer.start()
    deadline.start(5000)
    window._extract_with_ai()
    loop.exec()
    timer.stop()
    deadline.stop()
    for worker in (window._extract_worker, window._worker):
        if worker is not None:
            worker.wait(5000)
    QApplication.processEvents()
    assert len(cad_calls) == 1
    if rename_dialog:
        assert modal_observations == [(True, True)]
    assert not warnings
    assert (window._project_dir() / 'AutoModule.pretty' / 'AutoModule.kicad_mod').is_file()
    assert (window._project_dir() / 'AutoModule.3dshapes' / 'AutoModule.step').is_file()
    assert window.tabs.currentIndex() == 4
    assert window.tabs.isEnabled() and window.analysis_overlay.isHidden()


def test_guided_name_collision_keeps_documents_and_generates_after_real_thread(window, monkeypatch, tmp_path):
    """Regression: completed AI used to be discarded by a docs-name collision."""
    from PIL import Image
    from PyQt6.QtCore import QEventLoop, QTimer
    from kicadgen.services.document_roles import DocumentSet

    original_name = window._component_name()
    original_project = window._project_dir()
    store = DocumentSet(window._docs_dir())
    for role in ('pinout', 'top', 'front'):
        source = tmp_path / f'source-{role}.png'
        Image.new('RGB', (30, 20), 'blue').save(source)
        store.import_file(role, source)
    before = {p: p.read_bytes() for p in store.directory.iterdir() if p.is_file()}
    destination = original_project.parent / 'AutoModule' / 'docs'
    destination.mkdir(parents=True)
    conflict = destination / 'top.png'
    conflict.write_bytes(b'other project: must never change')
    monkeypatch.setattr(window, '_selected_documents', gui.MainWindow._selected_documents.__get__(window))
    monkeypatch.setattr(gui.ExtractWorker, 'run', lambda self: self.done.emit(acquired('castellated')))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a))
    cad_calls = []
    def cad(params, body, project, fillets, texts):
        cad_calls.append((params.name, project))
        path = project / f'{params.name}.3dshapes' / f'{params.name}.step'
        path.parent.mkdir(parents=True)
        path.write_text('offline CAD fixture')
        return {'step_path': path}
    monkeypatch.setattr(gui, '_generate_3d', cad)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: loop.quit() if window._worker is not None
                          and not window._worker.isRunning() and not window._automatic_generation_active else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    timer.start()
    deadline.start(5000)
    window._extract_with_ai()
    loop.exec()
    timer.stop()
    deadline.stop()
    for worker in (window._extract_worker, window._worker):
        if worker is not None:
            worker.wait(5000)
    QApplication.processEvents()
    assert not warnings
    assert window._component_name() == original_name
    assert cad_calls == [(original_name, original_project)]
    assert (original_project / f'{original_name}.pretty' / f'{original_name}.kicad_mod').is_file()
    assert (original_project / f'{original_name}.3dshapes' / f'{original_name}.step').is_file()
    assert all(path.read_bytes() == content for path, content in before.items())
    assert conflict.read_bytes() == b'other project: must never change'
    assert window._extract_worker.document_roles == {'pinout.png': 'pinout', 'top.png': 'top', 'front.png': 'front'}
    assert window.tabs.currentIndex() == 4
