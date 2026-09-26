"""@brief The document screen: one card per file, several roles per file (2026-09-26)."""
from pathlib import Path

import pytest
from PIL import Image
from PyQt6.QtWidgets import QFileDialog, QGroupBox, QMessageBox
from test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa

from kicadgen.gui.guided_documents import ADD_KEY
from kicadgen.services.document_roles import ROLE_SHORT, ROLES, DocumentSet


def image_file(path, color='blue'):
    Image.new('RGB', (40, 30), color).save(path)
    return path


def add(panel, monkeypatch, source):
    """Aggiunge un file con il pulsante «Aggiungi…» come farebbe l'utente."""
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(source), ''))
    panel.add_button.click()
    return panel.cards[Path(source).name]


def start_analysis(window, monkeypatch):
    monkeypatch.setattr(window, '_selected_documents',
                        gui.MainWindow._selected_documents.__get__(window))
    monkeypatch.setattr(gui.ExtractWorker, 'start', lambda self: None)
    # Il rilevatore parte forzato su YOLO, che pretende pesi ONNX e runtime
    # installati: la scelta predefinita ha i suoi test in test_photo_detector_ui.py.
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData('ai'))
    window._extract_with_ai()


def test_a_new_file_has_no_role_until_the_user_ticks_one(window, monkeypatch, tmp_path):
    """Positivo e negativo: senza ruolo l'analisi non parte, con i ruoli spuntati parte."""
    panel = window.guided_docs
    assert window.docs_list.isHidden()
    source = image_file(tmp_path / 'random.png')
    card = add(panel, monkeypatch, source)
    assert not any(box.isChecked() for box in card['roles'].values())
    assert 'random.png' in panel.status.text()
    assert card['view'].isEnabled() and not card['preview'].pixmap().isNull()
    shown = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *a: shown.append(a))
    start_analysis(window, monkeypatch)
    assert window._extract_worker is None and 'random.png' in shown[-1][2]
    card['roles']['top'].setChecked(True)
    card['roles']['hires'].setChecked(True)
    start_analysis(window, monkeypatch)
    assert window._extract_worker.document_roles == {'random.png': ['top', 'hires']}
    assert window._extract_worker.doc_paths == [window._docs_dir() / 'random.png']
    assert source.exists()
    window._on_extract_finished()


def test_one_file_can_carry_every_role_it_contains(window, monkeypatch, tmp_path):
    panel = window.guided_docs
    card = add(panel, monkeypatch, image_file(tmp_path / 'sheet.png'))
    for role in ('pinout', 'top', 'land_pattern', 'side_view'):
        card['roles'][role].setChecked(True)
    assert DocumentSet(window._docs_dir()).selected()[1] == {
        'sheet.png': ['pinout', 'top', 'land_pattern', 'side_view']}
    covered = {role for role, badge in panel.role_badges.items() if badge.text().startswith('✓')}
    assert covered == {'pinout', 'top', 'land_pattern', 'side_view'}
    assert 'foto' in panel.role_badges['hires'].toolTip().lower()


def test_the_same_file_added_twice_is_not_copied_again(window, monkeypatch, tmp_path):
    panel = window.guided_docs
    card = add(panel, monkeypatch, image_file(tmp_path / 'a.png'))
    card['roles']['pinout'].setChecked(True)
    twin = tmp_path / 'copy'
    twin.mkdir()
    add(panel, monkeypatch, image_file(twin / 'a.png'))
    assert list(panel.cards) == ['a.png']
    assert 'già nel progetto' in panel.status.text()


def test_a_pdf_cannot_be_ticked_as_photo(window, tmp_path):
    pytest.importorskip('PyQt6.QtPdf')
    source = tmp_path / 'diagram.pdf'
    Image.new('RGB', (120, 80), 'blue').save(source, 'PDF')
    window.guided_docs.store().import_file(['pinout'], source)
    window.guided_docs.refresh()
    card = window.guided_docs.cards['diagram.pdf']
    assert not card['preview'].pixmap().isNull()
    assert not card['roles']['hires'].isEnabled()
    assert card['roles']['top'].isEnabled()


def test_cancel_replace_and_remove_keep_originals(window, monkeypatch, tmp_path):
    panel = window.guided_docs
    source = image_file(tmp_path / 'random.png')
    card = add(panel, monkeypatch, source)
    card['roles']['pinout'].setChecked(True)
    manifest = (window._docs_dir() / '.document-roles.json').read_bytes()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    card['replace'].click()
    card['remove'].click()
    assert (window._docs_dir() / '.document-roles.json').read_bytes() == manifest
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    card['remove'].click()
    assert not (window._docs_dir() / 'random.png').exists()
    assert list((window._docs_dir() / '.history').glob('*/random.png'))
    assert 'random.png' not in panel.cards
    assert source.exists()


def test_replace_keeps_the_ticked_roles(window, monkeypatch, tmp_path):
    panel = window.guided_docs
    card = add(panel, monkeypatch, image_file(tmp_path / 'old.png'))
    card['roles']['top'].setChecked(True)
    card['roles']['side_view'].setChecked(True)
    newer = image_file(tmp_path / 'new.png', 'red')
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(newer), ''))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    card['replace'].click()
    assert list(panel.cards) == ['new.png']
    assert DocumentSet(window._docs_dir()).selected()[1] == {'new.png': ['top', 'side_view']}


def test_project_change_refreshes_cards_instead_of_reusing_previous_files(window, tmp_path):
    store = DocumentSet(window._docs_dir())
    store.import_file('top', image_file(tmp_path / 'a.png'))
    window._refresh_docs_list()
    assert list(window.guided_docs.cards) == ['a.png']
    old_name = window._component_name()
    window.field_name.setText('Other')
    assert window.guided_docs.cards == {}
    window.field_name.setText(old_name)
    assert list(window.guided_docs.cards) == ['a.png']


def test_guided_metadata_follows_document_migration(window, monkeypatch, tmp_path):
    source = image_file(tmp_path / 'a.png')
    store = DocumentSet(window._docs_dir())
    store.import_file(['top', 'hires'], source)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    window._migrate_docs_to_new_name('NewName')
    window.field_name.setText('NewName')
    paths, roles = gui.MainWindow._selected_documents(window)
    assert roles == {'a.png': ['top', 'hires']}
    assert paths == [window._docs_dir() / 'a.png']
    assert source.exists()


def test_guided_migration_conflict_leaves_both_sets_untouched(window, monkeypatch, tmp_path):
    store = DocumentSet(window._docs_dir())
    source = image_file(tmp_path / 'top.png')
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


@pytest.mark.parametrize('language', ['it', 'en', 'fr', 'de'])
def test_document_cards_retranslate_after_every_refresh(window, monkeypatch, tmp_path, language):
    """@brief Keep the dynamic document controls in the selected UI language."""
    card = add(window.guided_docs, monkeypatch, image_file(tmp_path / 'x.png'))
    window.language_combo.setCurrentIndex(window.language_combo.findData(language))
    window._refresh_docs_list()
    card = window.guided_docs.cards['x.png']
    assert [card['roles'][r].text() for r in ROLES] == [
        window.i18n.text(ROLE_SHORT[r]) for r in ROLES]
    # Il nome del file e' un dato: mai tradotto.
    assert card['group'].title() == 'x.png'
    assert window.guided_docs.add_button.text() == window.i18n.text('Aggiungi…')
    titles = {group.title() for group in window.findChildren(QGroupBox)}
    assert window.i18n.text('Documentazione (docs/)') in titles


def test_refresh_repairs_one_stale_role_box(window, monkeypatch, tmp_path):
    """@brief Prevent one document card from retaining a previous language."""
    card = add(window.guided_docs, monkeypatch, image_file(tmp_path / 'x.png'))
    window.language_combo.setCurrentIndex(window.language_combo.findData('fr'))
    stale = card['roles']['side_view']
    stale.setText('Vista laterale')
    window._refresh_docs_list()
    assert stale.text() == window.i18n.text('Vista laterale')


def test_load_dialog_reopens_on_the_last_source_folder(window, monkeypatch, tmp_path):
    """La cartella da cui si pescano i documenti viene ricordata."""
    panel = window.guided_docs
    sources = tmp_path / 'viste-esp32'
    sources.mkdir()
    opened = []
    names = iter(['pinout.png', 'hires.png'])

    def fake_dialog(*args):
        opened.append(args[2])
        return (str(image_file(sources / next(names), 'red' if len(opened) == 1 else 'green')), '')

    monkeypatch.setattr(QFileDialog, 'getOpenFileName', fake_dialog)
    panel.add_button.click()
    # Prima apertura: nessuna preferenza, si parte dalla cartella del progetto.
    assert Path(opened[0]) == Path(window._docs_dir())
    assert Path(window._config['last_document_dir']) == sources
    panel.add_button.click()
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
    panel.add_button.click()
    assert Path(opened[0]) == Path(window._docs_dir())


def test_the_add_card_shows_a_loading_state_while_the_file_is_read(window, monkeypatch, tmp_path):
    """Generare l'anteprima blocca la GUI: senza segnale sembra non aver fatto nulla."""
    panel = window.guided_docs
    seen = {}

    def fake_import(self, roles, source, replace=None):
        seen['text'] = panel.add_hint.text()
        seen['enabled'] = panel.add_button.isEnabled()
        return self.directory / Path(source).name

    monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                        lambda *a: (str(image_file(tmp_path / 'v.png')), ''))
    monkeypatch.setattr(DocumentSet, 'import_file', fake_import)
    panel.add_button.click()
    assert 'aricament' in seen['text'], 'la card non segnala il caricamento'
    assert seen['enabled'] is False, 'si puo\' accodare una seconda richiesta'
    assert panel.add_button.isEnabled()
    assert panel._busy_role is None


def test_cancelling_the_replace_dialog_leaves_the_cursor_alone(window, monkeypatch, tmp_path):
    """Ripristinare un cursore mai impostato toglierebbe quello di altri."""
    panel = window.guided_docs
    card = add(panel, monkeypatch, image_file(tmp_path / 'v.png'))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    card['replace'].click()
    assert panel._busy_role is None
    assert card['replace'].isEnabled()
    assert ADD_KEY != 'v.png'
