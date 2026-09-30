"""Standard/advanced workflow: no network or real CAD subprocess required."""
from pathlib import Path
from unittest.mock import Mock

from kicadgen.services.document_roles import MANIFEST, ROLES
import pytest
from test_photo_inventory_gui import gui, ExtractedParams, QApplication
from qt_window_teardown import dispose_window
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidgetItem, QMessageBox


def acquired(layout='matrix'):
    return ExtractedParams(name='AutoModule', description='', tags='', confidence='high', notes='notes',
        layout=layout, rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=7.62,
        pad_diameter_mm=1.7, drill_mm=1., body_length_mm=20, body_width_mm=10,
        body_height_mm=3, castellated_sides=[dict(edge='left', count=3, pitch_mm=2.54, first_pin=1)])


def use_ai_detector(window):
    """Il rilevatore parte forzato su YOLO, che pretende pesi ONNX e runtime
    installati: senza, l'estrazione si ferma su un avviso e questi test
    fallirebbero per un motivo che col flusso standard/avanzato non c'entra.
    Qui si prova il flusso, non il rilevatore, quindi si sceglie quello privo
    di dipendenze esterne. La scelta predefinita resta verificata altrove, in
    tests/test_photo_detector_ui.py."""
    window.combo_photo_detector.setCurrentIndex(
        window.combo_photo_detector.findData('ai'))
    # Presupposto dichiarato di questi flussi: FreeCAD installato. Senza, il
    # gruppo «Modello 3D» resta spento per scelta (vedi
    # `_enable_3d_group_from_extraction`: accenderlo senza il CAD significa far
    # fallire ogni generazione alla fine), e qui si proverebbe la generazione
    # del solo footprint invece del flusso standard/avanzato completo. Il caso
    # «FreeCAD assente» ha i suoi test in tests/test_board_pipeline.py.
    window._freecad_available = True


def begin(window, monkeypatch):
    use_ai_detector(window)
    monkeypatch.setattr(gui.ExtractWorker, 'start', lambda self: None)
    monkeypatch.setattr(gui.GenerateWorker, 'start', lambda self: None)
    row = QListWidgetItem('photo.png')
    row.setCheckState(Qt.CheckState.Checked)
    window.docs_list.addItem(row)
    window._extract_with_ai()


def test_only_the_four_workflow_tabs_exist(window):
    """@brief I tab Footprint 2D e Genera sono stati cancellati (utente, 2026-09-26).

    Positivo: restano Progetto, Anteprima, Modello 3D e KiCad, negli indici
    delle costanti. Negativo: nessun widget dei parametri sopravvive; i
    parametri sono valori non grafici con gli stessi limiti degli editor.
    """
    from kicadgen.gui.component_state import BoundedValue
    assert window.tabs.count() == 4
    assert (gui.TAB_PROJECT, gui.TAB_PREVIEW, gui.TAB_MODEL_3D, gui.TAB_KICAD) == (0, 1, 2, 3)
    assert window.tabs.tabText(gui.TAB_MODEL_3D) == window.i18n.tr("tab.model3d.name")
    for name in ('footprint_subtabs', 'label_summary', 'btn_generate', 'table_sides',
                 'table_components', 'table_annotations', 'log_text',
                 'model_3d_settings_scroll'):
        assert not hasattr(window, name), name
    assert isinstance(window.field_rows, BoundedValue)
    # Dal 26/09 limiti fisici, non quelli dell'editor tolto (40 righe).
    assert window.field_rows.minimum() == 1 and window.field_rows.maximum() > 40


def test_the_advanced_mode_is_gone():
    """@brief Negativo: nessuna spunta, chiave o editor di traduzioni sopravvive."""
    from kicadgen.gui import app_config
    from kicadgen.gui.generation_options_dialog import GenerationOptionsDialog
    assert 'advanced_user' not in app_config.DEFAULTS
    assert not hasattr(GenerationOptionsDialog, 'advanced_user')
    assert not hasattr(gui.MainWindow, '_advanced_user')
    assert not hasattr(gui.MainWindow, '_edit_translations')


def test_photo_model_review_is_available_and_preserves_photo(window):
    """@brief La revisione foto/modelli resta raggiungibile senza tab dei parametri.

    Il comando diventa utilizzabile appena esiste una foto, e nascondere gli
    editor dei parametri non butta via la foto in memoria.

    Cambia solo cio' che la vecchia versione pretendeva sulla visibilita'.
    Prima il comando stava sempre visibile e restava spento finche' non c'era
    niente da aprire; su richiesta dell'utente («qui c'è troppa confusione con
    i pulsanti sotto. alcuni non hanno neppure senso») vale ora la disciplina
    di `_set_review_buttons_enabled`: cio' che non puo' agire si nasconde
    invece di stare acceso a vuoto. Quindi negativo = nascosto senza foto,
    positivo = visibile e abilitato appena la foto c'e'. La copia nella riga
    degli strumenti e' stata eliminata: il widget verificato e' quello della
    riga in fondo alla scheda Progetto.
    """
    review = window.btn_component_review
    assert review.isHidden() or not review.isVisibleTo(review.parentWidget())
    assert not review.isEnabled()
    params = acquired('castellated')
    params.photo_png = b'photo fixture'
    window._on_extracted(params)
    assert review.isVisibleTo(review.parentWidget())
    assert review.isEnabled()
    assert review.isVisibleTo(review.parentWidget())
    assert review.isEnabled()
    assert window._photo_png == b'photo fixture'
    assert not window.btn_ai_log.isHidden()
    assert not window.btn_component_prompt.isHidden()


def test_3d_data_is_kept_without_editors(window):
    """@brief Annotazioni e corpi della foto restano dati: nessun editor li mostra."""
    from kicadgen.cad3d.annotations import EdgeFilletAnnotation
    window._on_extracted(acquired('castellated'))
    window.fillet_annotations.append(EdgeFilletAnnotation(edge='top-left', radius_mm=1))
    window._set_photo_components([dict(name='body', kind='generic', x_mm=0, y_mm=0,
                                      length_mm=2, width_mm=1, height_mm=1)])
    original = window._components_from_table()
    assert window.fillet_annotations[-1].radius_mm == 1
    assert [c.name for c in original] == ['body']
    assert window._components_from_table() == original
    assert not window.btn_regen_3d.isHidden()
    assert window.btn_terminal_detail.isHidden() and window.btn_lead_refinement.isHidden()


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
    # Contratto del 2026-09-26 (utente): a lavoro riuscito la modale resta
    # aperta con tutti i passi spuntati e si chiude solo con «Chiudi».
    overlay = window.analysis_overlay
    assert overlay.is_holding_completed()
    assert overlay._step_current == len(overlay._step_labels) == 3
    assert "✓ 3" in overlay.steps_plain_text()
    assert not overlay.is_abortable()
    overlay.close_button.click()
    assert overlay.isHidden()
    assert window.tabs.isEnabled() and window.menuBar().isEnabled()
    assert window.btn_extract_ai.isEnabled()
    assert window.tabs.currentIndex() == gui.TAB_MODEL_3D


def test_explicit_pad_geometry_writes_the_footprint_only(window, monkeypatch):
    """@brief Positivo: i pad espliciti, prima solo manuali, ora si generano da soli.

    Il 3D non li supporta: la corsa scrive il solo footprint e lo dichiara.
    """
    begin(window, monkeypatch)
    params = acquired()
    params.package_geometry = {
        "family": "tssop", "body_length_mm": 5, "body_width_mm": 4,
        "pins": [
            {"number": "1", "x_mm": -2.5, "y_mm": -1.27,
             "pad_length_mm": 1.2, "pad_width_mm": .6, "side": "left"},
            {"number": "2", "x_mm": -2.5, "y_mm": 1.27,
             "pad_length_mm": 1.2, "pad_width_mm": .6, "side": "left"},
        ],
    }
    window._on_extracted(params)
    window._on_extract_finished()
    worker = window._worker
    assert worker is not None and worker.body is None
    assert isinstance(worker.params, gui.PackageGeometry)
    assert window._automatic_generation_active and not window._automatic_3d_expected


def test_castellated_missing_overall_height_starts_generation(window, monkeypatch):
    begin(window, monkeypatch)
    window.field_body_height.setValue(99)
    params = acquired('castellated')
    params.body_height_mm = None
    window._on_extracted(params)
    window._on_extract_finished()
    assert window._worker is not None
    assert window._worker.body.height_mm == window.field_cast_pcb.value()


@pytest.mark.parametrize('failure', ['missing_body', 'invalid_footprint', 'failed_extraction', 'invalid_package', 'unsafe_name'])
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
        # Negativo della prova sui pad espliciti: una geometria scartata non deve
        # far generare il footprint a griglia al suo posto.
        if failure == 'invalid_package': params.package_geometry = {'family': 'qfn'}
        if failure == 'unsafe_name': params.name = '../outside'
        window._on_extracted(params)
    window._on_extract_finished()
    assert window._worker is None
    assert window.btn_extract_ai.isEnabled()
    assert not window._automatic_generation_active and window.analysis_overlay.isHidden()
    assert warnings


@pytest.mark.parametrize('overwrite', [False, True])
def test_existing_outputs_need_explicit_confirmation(window, monkeypatch, overwrite):
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a))
    begin(window, monkeypatch)
    window._on_extracted(acquired())
    # Gli output portano il nome in uso, che l'estrazione non riscrive piu':
    # propone il suo accanto al campo e aspetta un clic.
    name = window._component_name()
    path = window._project_dir() / f'{name}.3dshapes' / f'{name}.step'
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
        assert window.tabs.currentIndex() == gui.TAB_PREVIEW
        assert 'Footprint generato' in warnings[0][2]
    assert warnings
    assert window.tabs.isEnabled() and window.menuBar().isEnabled()
    assert window.btn_extract_ai.isEnabled()
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


def test_an_old_advanced_preference_is_ignored(window, monkeypatch):
    """@brief Negativo: un config.json scritto prima della rimozione non riapre i tab."""
    from kicadgen.gui import app_config
    app_config.save_config({'advanced_user': True})
    monkeypatch.setattr(gui, 'load_config', app_config.load_config)
    restored = gui.MainWindow()
    try:
        assert restored.tabs.count() == 4
    finally:
        # Destroyed now, not by a later GC pass inside another test.
        dispose_window(restored)


def test_extract_finish_inside_a_nested_event_loop_does_not_drop_autogeneration(window, monkeypatch):
    """Una modale apre un ciclo di eventi annidato mentre il worker lavora.

    Se il segnale finished arrivasse li' e venisse gestito subito, la
    generazione automatica partirebbe su campi applicati solo a meta'. Il test
    simula il ciclo annidato invece di dipendere da quale modale sopravvive
    sul percorso: le modali vanno e vengono, il meccanismo deve restare.
    """
    begin(window, monkeypatch)
    observed = []
    original = window._apply_extracted_result

    def apply_with_nested_loop(params):
        window._extract_worker.finished.emit()
        observed.append(window._worker is None)
        return original(params)

    monkeypatch.setattr(window, '_apply_extracted_result', apply_with_nested_loop)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)
    window._on_extracted(acquired())
    assert observed == [True]  # Never generate until all fields have been applied.
    assert window._worker is not None
    assert window._worker.body.height_mm == 3
    window._on_error('test stopped')
    window._on_generation_finished()


def test_the_name_conflict_no_longer_opens_a_modal(window, monkeypatch):
    """Avviso, non domanda: non c'e' nulla da decidere e nulla e' stato toccato."""
    begin(window, monkeypatch)
    original_name = window._component_name()
    docs = window._docs_dir()
    docs.mkdir(parents=True, exist_ok=True)
    (docs / 'photo.png').write_bytes(b'offline fixture')
    (docs / MANIFEST).write_text('{"version": 1, "roles": {}}', encoding='utf-8')
    target = Path(window.field_library_root.text().strip()) / 'AutoModule' / 'docs'
    target.mkdir(parents=True, exist_ok=True)
    (target / 'photo.png').write_bytes(b'altro progetto')

    def refuse(*args, **kwargs):
        pytest.fail('Il conflitto di nomi non deve aprire una modale')

    monkeypatch.setattr(QMessageBox, 'information', refuse)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)
    window._on_extracted(acquired())
    # L'estrazione non rinomina mai: il nome proposto -- qui quello di un
    # altro progetto -- va solo nel log, e nulla viene toccato. Dal
    # 2026-09-24 (utente) non c'e' piu' il pulsante per adottarlo: si passa
    # da «Rinomina progetto…», che rifiuta i nomi di altri progetti.
    assert window._component_name() == original_name
    assert (target / 'photo.png').read_bytes() == b'altro progetto', 'documento sovrascritto'
    assert any('AutoModule' in line and 'Rinomina progetto' in line
               for line in window.ai_progress_text.toPlainText().splitlines())
    window._on_error('test stopped')
    window._on_generation_finished()


def test_the_proposed_name_waits_for_a_click_and_never_opens_a_modal(window, monkeypatch):
    """Scelta dell'utente: il nome lo decide chi lavora.

    L'estrazione lo propone accanto al campo e non tocca nulla finche' non si
    preme il pulsante; ne' prima ne' dopo compare una domanda a interrompere
    l'analisi. Prima si rinominava da soli, e ogni nome mai proposto lasciava
    una cartella in area di lavoro.
    """
    begin(window, monkeypatch)
    docs = window._docs_dir()
    docs.mkdir(parents=True, exist_ok=True)
    (docs / 'photo.png').write_bytes(b'offline fixture')
    chosen = window._component_name()
    def refuse(*args, **kwargs):
        pytest.fail("Il nome proposto non deve aprire una modale")
    monkeypatch.setattr(QMessageBox, 'question', refuse)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)

    window._on_extracted(acquired())
    assert window._component_name() == chosen
    # Contratto cambiato il 2026-09-24 (utente): la proposta non ha piu' una
    # riga col pulsante, sta in UNA riga del log; si adotta da «Rinomina
    # progetto…», e i documenti seguono il nome come prima.
    proposals = [line for line in window.ai_progress_text.toPlainText().splitlines()
                 if 'AutoModule' in line and 'Rinomina progetto' in line]
    assert len(proposals) == 1
    assert not hasattr(window, 'btn_use_suggested_name')

    _rename_to(window, monkeypatch, 'AutoModule')
    assert window._component_name() == 'AutoModule'
    assert any('Rinomina automatica' in line for line in
               window.ai_progress_text.toPlainText().splitlines())
    window._on_error('test stopped')
    window._on_generation_finished()


@pytest.mark.parametrize('rename_dialog', [False])
def test_real_qthread_handoff_generates_footprint_and_requests_3d(window, monkeypatch, rename_dialog):
    from PyQt6.QtCore import QEventLoop, QTimer
    monkeypatch.setattr(gui.ExtractWorker, 'run', lambda self: self.done.emit(acquired()))
    cad_calls = []
    warnings = []
    def cad(params, body, project, fillets, texts, progress=None, model_progress=None):
        cad_calls.append((params, body))
        path = project / f'{params.name}.3dshapes' / f'{params.name}.step'
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
    use_ai_detector(window)
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
    name = window._component_name()
    assert (window._project_dir() / f'{name}.pretty' / f'{name}.kicad_mod').is_file()
    assert (window._project_dir() / f'{name}.3dshapes' / f'{name}.step').is_file()
    assert window.tabs.currentIndex() == gui.TAB_MODEL_3D
    assert window.tabs.isEnabled() and window.analysis_overlay.is_holding_completed()
    window.analysis_overlay.close_button.click()
    assert window.analysis_overlay.isHidden()


def test_guided_name_collision_keeps_documents_and_generates_after_real_thread(window, monkeypatch, tmp_path):
    """Regression: completed AI used to be discarded by a docs-name collision."""
    from PIL import Image
    from PyQt6.QtCore import QEventLoop, QTimer
    from kicadgen.services.document_roles import DocumentSet

    original_name = window._component_name()
    original_project = window._project_dir()
    store = DocumentSet(window._docs_dir())
    # Colori diversi: dal 26/09 lo stesso contenuto caricato due volte diventa
    # UN documento con piu' ruoli, qui servono cinque documenti distinti.
    for index, role in enumerate(ROLES):
        source = tmp_path / f'source-{role}.png'
        Image.new('RGB', (30, 20), (10 * index, 40, 200)).save(source)
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
    def cad(params, body, project, fillets, texts, progress=None, model_progress=None):
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
    use_ai_detector(window)
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
    assert window._extract_worker.document_roles == {f'source-{role}.png': [role] for role in ROLES}
    assert window.tabs.currentIndex() == gui.TAB_MODEL_3D


def test_the_modal_says_which_tool_is_working_and_how_far_it_is(window, monkeypatch):
    """@brief Il segnale di fase dei worker deve arrivare davvero alla modale.

    Senza questo collegamento la scheda resta muta sullo strumento e mostra al
    suo posto i messaggi di servizio, che era il difetto segnalato: si vedeva
    "AI_CALL_PROMPT_END ... sha256=..." e non si capiva chi stesse lavorando
    ne' quanto mancasse.
    """
    begin(window, monkeypatch)
    overlay = window.analysis_overlay

    window._extract_worker.phase.emit('llm', 'anthropic/claude-opus-5', 0, 0)
    assert 'anthropic/claude-opus-5' in overlay.tool.text()
    # Una chiamata al modello non ha un conteggio: nessuna percentuale finta.
    assert not overlay.progress_row.isVisibleTo(overlay)

    window._on_extracted(acquired())
    window._generate()
    window._worker.phase.emit('freecad', 'abbinamento modelli KiCad', 9, 45)
    assert 'FreeCAD' in overlay.tool.text()
    assert overlay.progress_label.text() == '20% · 9/45 componenti'


def _rename_to(window, monkeypatch, name):
    """Rinomina come fa l'utente: File > Rinomina progetto… (dal 2026-09-24 il
    pulsante «Usa questo nome» non c'e' piu')."""
    from PyQt6.QtWidgets import QDialog
    from kicadgen.gui.project_dialogs import ProjectNameDialog

    def accept(self):
        # Come l'utente: con OK spento (nome non valido) non si accetta.
        self.field.setText(name)
        ok = self.buttons.button(self.buttons.StandardButton.Ok)
        return QDialog.DialogCode.Accepted if ok.isEnabled() else QDialog.DialogCode.Rejected
    monkeypatch.setattr(ProjectNameDialog, "exec", accept)
    window._rename_project()


def test_an_incomplete_generation_does_not_hold_the_card(window, monkeypatch):
    """@brief Negativo: un 3D fallito non lascia la modale aperta come «Completato»."""
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a))
    begin(window, monkeypatch)
    window._on_extracted(acquired())
    window._on_extract_finished()
    monkeypatch.setattr(window, '_draw_preview', lambda: None)
    monkeypatch.setattr(window, '_draw_preview_3d', lambda: None)
    window._worker.done.emit('footprint.kicad_mod')
    window._worker.cad3d_error.emit('FreeCAD unavailable')
    window._worker.finished.emit()
    assert warnings
    assert window.analysis_overlay.isHidden()
    assert not window.analysis_overlay.is_holding_completed()
