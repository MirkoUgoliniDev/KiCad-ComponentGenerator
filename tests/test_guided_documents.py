from pathlib import Path

from kicadgen.services.document_roles import ROLES
import pytest
from PIL import Image
from PyQt6.QtWidgets import QFileDialog, QGroupBox, QMessageBox
from test_photo_inventory_gui import window, gui
from kicadgen.services.document_roles import DocumentSet


def image_file(path):
    Image.new('RGB', (40, 30), 'blue').save(path)
    return path


def test_three_cards_load_unavailable_and_forward_roles(window, monkeypatch, tmp_path):
    panel = window.guided_docs
    assert list(panel.cards) == list(ROLES)
    assert window.docs_list.isHidden()
    source = image_file(tmp_path / 'random.png')
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(source), ''))
    panel.cards['top']['load'].click()
    assert panel.cards['top']['filename'].text() == 'top.png'
    assert panel.cards['top']['view'].isEnabled()
    assert not panel.cards['top']['preview'].pixmap().isNull()
    panel.cards['pinout']['unavailable'].setChecked(True)
    panel.cards['hires']['unavailable'].setChecked(True)
    monkeypatch.setattr(window, '_selected_documents', gui.MainWindow._selected_documents.__get__(window))
    monkeypatch.setattr(gui.ExtractWorker, 'start', lambda self: None)
    # Il rilevatore parte forzato su YOLO, che pretende pesi ONNX e runtime
    # installati: senza, l'estrazione si ferma su un avviso e questo test
    # fallirebbe per un motivo che col suo oggetto non c'entra. La scelta
    # predefinita resta verificata in tests/test_photo_detector_ui.py.
    window.combo_photo_detector.setCurrentIndex(
        window.combo_photo_detector.findData('ai'))
    window._extract_with_ai()
    assert window._extract_worker.document_roles == {'top.png': 'top'}
    assert window._extract_worker.doc_paths == [window._docs_dir() / 'top.png']
    assert source.exists()
    window._on_extract_finished()


def test_cancel_replace_and_remove_keep_originals(window, monkeypatch, tmp_path):
    panel = window.guided_docs
    source = image_file(tmp_path / 'random.png')
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(source), ''))
    panel.cards['pinout']['load'].click()
    manifest = (window._docs_dir() / '.document-roles.json').read_bytes()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    panel.cards['pinout']['load'].click()
    panel.cards['pinout']['remove'].click()
    assert (window._docs_dir() / '.document-roles.json').read_bytes() == manifest
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    panel.cards['pinout']['remove'].click()
    assert not (window._docs_dir() / 'pinout.png').exists()
    assert list((window._docs_dir() / '.history').glob('*/pinout.png'))
    assert source.exists()


def test_project_change_refreshes_roles_instead_of_reusing_previous_files(window, tmp_path):
    store = DocumentSet(window._docs_dir())
    store.import_file('top', image_file(tmp_path / 'a.png'))
    window._refresh_docs_list()
    assert window.guided_docs.cards['top']['filename'].text() == 'top.png'
    old_name = window._component_name()
    window.field_name.setText('Other')
    # Nessuna lineetta finta sotto un riquadro vuoto (PIANO-FIX, identita').
    assert window.guided_docs.cards['top']['filename'].text() == ''
    window.field_name.setText(old_name)
    assert window.guided_docs.cards['top']['filename'].text() == 'top.png'


def test_guided_metadata_follows_document_migration(window, monkeypatch, tmp_path):
    source = image_file(tmp_path / 'a.png')
    store = DocumentSet(window._docs_dir())
    store.import_file('top', source)
    store.clear('pinout', unavailable=True)
    store.clear('hires', unavailable=True)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    window._migrate_docs_to_new_name('NewName')
    window.field_name.setText('NewName')
    paths, roles = gui.MainWindow._selected_documents(window)
    assert roles == {'top.png': 'top'}
    assert paths == [window._docs_dir() / 'top.png']
    assert source.exists()


def test_guided_migration_conflict_leaves_both_sets_untouched(window, monkeypatch, tmp_path):
    store = DocumentSet(window._docs_dir())
    source = image_file(tmp_path / 'a.png')
    original = store.import_file('top', source)
    destination = window._project_dir().parent / 'NewName' / 'docs'
    destination.mkdir(parents=True)
    conflict = destination / 'top.png'
    conflict.write_bytes(b'existing destination')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    assert window._migrate_docs_to_new_name('NewName') == window._component_name()
    assert 'nessun documento spostato o sovrascritto' in window.ai_progress_text.toPlainText()
    assert original.exists() and store.path.exists()
    assert conflict.read_bytes() == b'existing destination'


def test_pdf_has_first_page_thumbnail(window, tmp_path):
    pytest.importorskip('PyQt6.QtPdf')
    source = tmp_path / 'diagram.pdf'
    Image.new('RGB', (120, 80), 'blue').save(source, 'PDF')
    window.guided_docs.store().import_file('pinout', source)
    window.guided_docs.refresh()
    card = window.guided_docs.cards['pinout']
    assert card['filename'].text() == 'pinout.pdf'
    assert not card['preview'].pixmap().isNull()


@pytest.mark.parametrize(
    ('language', 'unavailable', 'empty', 'load', 'component_type', 'roles', 'groups'),
    (
        ('it', 'Non disponibile', 'Nessun file', 'Carica…',
         '(non specificata)',
         ('Pinout / disposizione contatti', 'Immagine con quote', 'Foto ad alta risoluzione'),
         ('Cartella componente', 'Documentazione (docs/)', 'Analisi componente', 'Strumenti AI')),
        ('en', 'Not available', 'No file', 'Load…',
         '(not specified)',
         ('Pinout / contact layout', 'Dimensioned drawing', 'High-resolution photo'),
         ('Component folder', 'Documentation (docs/)', 'Component analysis', 'AI tools')),
        ('fr', 'Indisponible', 'Aucun fichier', 'Charger…',
         '(non spécifié)',
         ('Brochage / disposition des contacts', 'Image cotée', 'Photo haute résolution'),
         ('Dossier composant', 'Documentation (docs/)', 'Analyse du composant', 'Outils IA')),
        ('de', 'Nicht verfügbar', 'Keine Datei', 'Laden …',
         '(nicht angegeben)',
         ('Pinbelegung / Kontaktanordnung', 'Bemaßte Zeichnung', 'Hochauflösendes Foto'),
         ('Komponentenordner', 'Dokumentation (docs/)', 'Komponentenanalyse', 'KI-Werkzeuge')),
    ),
)
def test_guided_document_cards_retranslate_after_every_refresh(
        window, language, unavailable, empty, load, component_type, roles, groups):
    """@brief Keep dynamic document controls in the selected UI language."""
    window.language_combo.setCurrentIndex(window.language_combo.findData(language))
    window._refresh_docs_list()

    cards = window.guided_docs.cards
    assert tuple(cards[role]['unavailable'].text() for role in cards) == (unavailable,) * len(ROLES)
    assert tuple(cards[role]['preview'].text() for role in cards) == (empty,) * len(ROLES)
    assert tuple(cards[role]['load'].text() for role in cards) == (load,) * len(ROLES)
    assert tuple(cards[role]['unavailable'].parentWidget().title() for role in cards) == roles
    assert window.combo_component_type.currentText() == component_type
    titles = {group.title() for group in window.findChildren(QGroupBox)}
    assert set(groups) <= titles


def test_refresh_repairs_one_stale_unavailable_checkbox(window):
    """@brief Prevent one document card from retaining a previous language."""
    window.language_combo.setCurrentIndex(window.language_combo.findData('fr'))
    stale = window.guided_docs.cards['hires']['unavailable']
    stale.setText('Non disponibile')

    window._refresh_docs_list()

    assert tuple(
        card['unavailable'].text() for card in window.guided_docs.cards.values()
    ) == ('Indisponible',) * len(ROLES)


def test_load_dialog_reopens_on_the_last_source_folder(window, monkeypatch, tmp_path):
    """La cartella da cui si pescano i documenti viene ricordata.

    Le viste di uno stesso componente stanno insieme: dopo la prima, il
    dialogo deve ripartire da li' invece che dalla cartella del progetto,
    che e' la destinazione delle copie e non contiene mai gli originali.
    """
    panel = window.guided_docs
    sources = tmp_path / 'viste-esp32'
    sources.mkdir()
    opened = []

    def fake_dialog(*args):
        opened.append(args[2])
        return (str(image_file(sources / 'pinout.png')), '')

    monkeypatch.setattr(QFileDialog, 'getOpenFileName', fake_dialog)
    panel.cards['pinout']['load'].click()
    # Prima apertura: nessuna preferenza, si parte dalla cartella del progetto.
    assert Path(opened[0]) == Path(window._docs_dir())
    assert Path(window._config['last_document_dir']) == sources

    panel.cards['hires']['load'].click()
    assert Path(opened[1]) == sources, 'il dialogo non e\' ripartito dall\'ultima cartella'


def test_forgotten_folder_falls_back_instead_of_opening_nowhere(window, monkeypatch, tmp_path):
    """Una cartella non piu' raggiungibile non deve aprire il dialogo nel vuoto."""
    panel = window.guided_docs
    window._config['last_document_dir'] = str(tmp_path / 'disco-scollegato')
    opened = []

    def fake_dialog(*args):
        opened.append(args[2])
        return ('', '')

    monkeypatch.setattr(QFileDialog, 'getOpenFileName', fake_dialog)
    panel.cards['top']['load'].click()
    assert Path(opened[0]) == Path(window._docs_dir())


def test_the_card_shows_a_loading_state_while_the_file_is_read(window, monkeypatch, tmp_path):
    """Generare l'anteprima blocca la GUI: senza segnale sembra non aver fatto nulla."""
    panel = window.guided_docs
    seen = {}

    def fake_import(self, role, source, replace=False):
        card = panel.cards[role]
        seen['text'] = card['preview'].text()
        seen['load_enabled'] = card['load'].isEnabled()

    monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                        lambda *a: (str(image_file(tmp_path / 'v.png')), ''))
    monkeypatch.setattr(DocumentSet, 'import_file', fake_import)
    panel.cards['top']['load'].click()
    assert 'aricament' in seen['text'], 'la card non segnala il caricamento'
    assert seen['load_enabled'] is False, 'si puo\' accodare una seconda richiesta'
    # Ripristino: la card torna operativa e il cursore non resta a clessidra.
    assert panel.cards['top']['load'].isEnabled()
    assert panel._busy_role is None


def test_cancelling_the_replace_dialog_leaves_the_cursor_alone(window, monkeypatch, tmp_path):
    """Ripristinare un cursore mai impostato toglierebbe quello di altri."""
    panel = window.guided_docs
    monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                        lambda *a: (str(image_file(tmp_path / 'v.png')), ''))
    panel.cards['top']['load'].click()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    panel.cards['top']['load'].click()
    assert panel._busy_role is None
    assert panel.cards['top']['load'].isEnabled()
