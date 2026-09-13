from pathlib import Path

import pytest
from PIL import Image
from PyQt6.QtWidgets import QFileDialog, QMessageBox
from test_photo_inventory_gui import window, gui
from kicadgen.services.document_roles import DocumentSet


def image_file(path):
    Image.new('RGB', (40, 30), 'blue').save(path)
    return path


def test_three_cards_load_unavailable_and_forward_roles(window, monkeypatch, tmp_path):
    panel = window.guided_docs
    assert list(panel.cards) == ['pinout', 'top', 'front']
    assert window.docs_list.isHidden()
    source = image_file(tmp_path / 'random.png')
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(source), ''))
    panel.cards['top']['load'].click()
    assert panel.cards['top']['filename'].text() == 'top.png'
    assert panel.cards['top']['view'].isEnabled()
    assert not panel.cards['top']['preview'].pixmap().isNull()
    panel.cards['pinout']['unavailable'].setChecked(True)
    panel.cards['front']['unavailable'].setChecked(True)
    monkeypatch.setattr(window, '_selected_documents', gui.MainWindow._selected_documents.__get__(window))
    monkeypatch.setattr(gui.ExtractWorker, 'start', lambda self: None)
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
    assert window.guided_docs.cards['top']['filename'].text() == '—'
    window.field_name.setText(old_name)
    assert window.guided_docs.cards['top']['filename'].text() == 'top.png'


def test_guided_metadata_follows_document_migration(window, monkeypatch, tmp_path):
    source = image_file(tmp_path / 'a.png')
    store = DocumentSet(window._docs_dir())
    store.import_file('top', source)
    store.clear('pinout', unavailable=True)
    store.clear('front', unavailable=True)
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
