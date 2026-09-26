"""@brief Contract of the document roles, manifest version 2 (one file, several roles)."""
import json
from pathlib import Path

from PIL import Image
import pytest

from kicadgen.services.document_roles import (DocumentError, DocumentSet, INVENTORY_ROLES,
                                              MANIFEST, PHOTO_ROLE, ROLE_HELP, ROLES,
                                              photo_paths, role_list, valid_document_name)


def picture(path, color='red'):
    Image.new('RGB', (32, 20), color).save(path)
    return path


def pdf(path, text=b'fixture'):
    path.write_bytes(b'%PDF-1.4\n' + text)
    return path


def test_every_role_has_a_label_and_a_help():
    assert set(ROLES) == set(ROLE_HELP) == {'pinout', 'top', 'hires', 'land_pattern', 'side_view'}
    assert INVENTORY_ROLES == (PHOTO_ROLE,) == ('hires',)


def test_copy_keeps_original_name_and_persists_the_role_list(tmp_path):
    source = picture(tmp_path / 'arbitrary-photo.png')
    original = source.read_bytes()
    store = DocumentSet(tmp_path / 'docs')
    target = store.import_file('hires', source)
    assert target.name == 'arbitrary-photo.png'
    assert source.read_bytes() == target.read_bytes() == original
    paths, roles = DocumentSet(store.directory).selected()
    assert paths == [target] and roles == {'arbitrary-photo.png': ['hires']}
    manifest = json.loads(store.path.read_text(encoding='utf-8'))
    assert manifest == {'version': 2, 'roles': {'arbitrary-photo.png': ['hires']},
                        'original_names': {'arbitrary-photo.png': 'arbitrary-photo.png'}}


def test_one_file_carries_several_roles_and_only_a_photo_goes_to_the_inventory(tmp_path):
    """Positivo: un datasheet e' pinout, quote e land pattern; negativo: non e' una foto."""
    store = DocumentSet(tmp_path / 'docs')
    sheet = store.import_file(['pinout', 'top', 'land_pattern'], pdf(tmp_path / 'datasheet.pdf'))
    photo = store.import_file([], picture(tmp_path / 'board.png'))
    store.set_roles(photo.name, ['hires', 'top'])
    paths, roles = store.selected()
    assert roles == {'datasheet.pdf': ['pinout', 'top', 'land_pattern'],
                     'board.png': ['top', 'hires']}
    assert photo_paths(paths, roles) == [photo]
    assert sheet not in photo_paths(paths, roles)


def test_the_same_bytes_add_a_role_instead_of_a_copy(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    first = store.import_file('pinout', pdf(tmp_path / 'a.pdf'))
    again = store.import_file('top', pdf(tmp_path / 'renamed.pdf'))
    assert again == first
    assert store.selected()[1] == {'a.pdf': ['pinout', 'top']}
    # Negativo: stesso nome, contenuto diverso -> un altro documento, non una fusione.
    other_dir = tmp_path / 'other'
    other_dir.mkdir()
    different = store.import_file('side_view', pdf(other_dir / 'a.pdf', b'another drawing'))
    assert different.name == 'a-2.pdf'
    assert store.selected()[1] == {'a.pdf': ['pinout', 'top'], 'a-2.pdf': ['side_view']}


def test_a_pdf_cannot_be_declared_a_photograph(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    with pytest.raises(DocumentError, match='foto'):
        store.import_file(['hires'], pdf(tmp_path / 'scan.pdf'))
    drawing = store.import_file(['top'], pdf(tmp_path / 'scan.pdf'))
    with pytest.raises(DocumentError, match='foto'):
        store.set_roles(drawing.name, ['top', 'hires'])
    assert store.selected()[1] == {'scan.pdf': ['top']}


def test_unknown_roles_are_refused(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    with pytest.raises(DocumentError):
        store.import_file('schematic', picture(tmp_path / 'x.png'))
    target = store.import_file([], picture(tmp_path / 'x.png'))
    with pytest.raises(DocumentError):
        store.set_roles(target.name, ['top', 'schematic'])


def test_replace_keeps_roles_and_remove_is_recoverable(tmp_path):
    source = picture(tmp_path / 'one.png')
    store = DocumentSet(tmp_path / 'docs')
    old = store.import_file(['pinout', 'top'], source)
    content = old.read_bytes()
    second = picture(tmp_path / 'two.jpg', 'blue')
    target = store.import_file([], second, replace=old.name)
    assert target.name == 'two.jpg' and not old.exists()
    assert store.selected()[1] == {'two.jpg': ['pinout', 'top']}
    assert next((store.directory / '.history').glob('*/one.png')).read_bytes() == content
    backup = store.remove(target.name)
    assert not target.exists()
    assert (backup / 'two.jpg').read_bytes() == second.read_bytes()
    assert source.exists() and second.exists()
    with pytest.raises(DocumentError, match='almeno'):
        store.selected()


def test_manifest_failure_rolls_back_file_replacement(tmp_path, monkeypatch):
    store = DocumentSet(tmp_path / 'docs')
    old = store.import_file('top', picture(tmp_path / 'one.png'))
    previous = old.read_bytes(), store.path.read_bytes()

    def failed(data):
        raise OSError('disk unavailable')
    monkeypatch.setattr(store, '_write', failed)
    with pytest.raises(OSError):
        store.import_file([], picture(tmp_path / 'one.png', 'blue'), replace=old.name)
    assert (old.read_bytes(), store.path.read_bytes()) == previous


@pytest.mark.parametrize('content', [b'not an image', b'%PDF-1.4\n'])
def test_rejects_misleading_image_extension(tmp_path, content):
    source = tmp_path / 'fake.png'
    source.write_bytes(content)
    with pytest.raises(DocumentError):
        DocumentSet(tmp_path / 'docs').import_file('top', source)
    assert source.read_bytes() == content


@pytest.mark.parametrize(('original', 'local'), [
    ('Scheda ESP32 (vista).PNG', 'Scheda ESP32 _vista.png'),
    ('è.png', 'documento.png'),
])
def test_local_names_are_safe(tmp_path, original, local):
    source = picture(tmp_path / 'src.png')
    named = tmp_path / 'named'
    named.mkdir()
    renamed = named / original
    source.rename(renamed)
    target = DocumentSet(tmp_path / 'docs').import_file('top', renamed)
    assert target.name == local and valid_document_name(target.name)


def test_reserved_device_names_are_never_used():
    """CON.png non si puo' creare su Windows: il nome locale lo evita."""
    from kicadgen.services.document_roles import _local_name
    assert _local_name('CON.png') == 'documento_CON.png'
    assert not valid_document_name('CON.png') and not valid_document_name('.hidden.png')


def test_a_document_without_role_blocks_the_analysis(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    store.import_file('top', picture(tmp_path / 'a.png'))
    store.import_file([], picture(tmp_path / 'b.png', 'blue'))
    with pytest.raises(DocumentError, match='b.png'):
        store.selected()
    store.set_roles('b.png', ['pinout'])
    assert store.selected()[1] == {'a.png': ['top'], 'b.png': ['pinout']}


def test_unassigned_legacy_files_are_never_silently_selected(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    store.directory.mkdir()
    old = picture(store.directory / 'old.png')
    with pytest.raises(DocumentError, match='almeno'):
        store.selected()
    assert old.exists() and not store.path.exists()


def test_missing_file_is_explicit(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    file = store.import_file('top', picture(tmp_path / 'sample.png'))
    file.unlink()
    with pytest.raises(DocumentError, match='mancante'):
        store.selected()


@pytest.mark.parametrize('raw', [
    {'version': 1, 'roles': {'top': {'filename': '../outside.png'}}},
    {'version': 2, 'roles': {'../outside.png': ['top']}},
    {'version': 2, 'roles': {'a.png': ['schematic']}},
    {'version': 2, 'roles': {'a.png': 'top'}},
    {'version': 3, 'roles': {}},
])
def test_manifest_rejects_bad_content_and_preserves_the_source(tmp_path, raw):
    store = DocumentSet(tmp_path / 'docs')
    store.directory.mkdir()
    text = json.dumps(raw)
    store.path.write_text(text)
    with pytest.raises(DocumentError):
        store.read()
    assert store.path.read_text() == text


def _v1(tmp_path, rows, files):
    store = DocumentSet(tmp_path / 'docs')
    store.directory.mkdir()
    for name in files:
        (pdf if name.endswith('.pdf') else picture)(store.directory / name)
    store.path.write_text(json.dumps({'version': 1, 'roles': rows}))
    return store


def test_v1_top_view_doubles_as_photo_when_no_photo_was_loaded(tmp_path):
    """Positivo della migrazione: il comportamento di prima, ora dichiarato."""
    store = _v1(tmp_path, {'pinout': {'filename': 'pinout.pdf', 'unavailable': False},
                           'top': {'filename': 'top.png', 'original_name': 'x.png',
                                   'unavailable': False},
                           'hires': {'unavailable': True}}, ['pinout.pdf', 'top.png'])
    paths, roles = store.selected()
    assert roles == {'pinout.pdf': ['pinout'], 'top.png': ['top', 'hires']}
    assert photo_paths(paths, roles) == [store.directory / 'top.png']
    assert store.documents()[1][2] == 'x.png'


def test_v1_top_view_is_not_a_photo_when_a_photo_exists(tmp_path):
    """Negativo: con una foto dichiarata il disegno resta un disegno."""
    store = _v1(tmp_path, {'top': {'filename': 'top.png', 'unavailable': False},
                           'hires': {'filename': 'hires.jpg', 'unavailable': False}},
                ['top.png', 'hires.jpg'])
    assert store.selected()[1] == {'top.png': ['top'], 'hires.jpg': ['hires']}


def test_v1_pdf_top_view_never_becomes_a_photo(tmp_path):
    """Negativo strutturale: un PDF non e' mai stato una foto per l'inventario."""
    store = _v1(tmp_path, {'top': {'filename': 'top.pdf', 'unavailable': False}}, ['top.pdf'])
    assert store.selected()[1] == {'top.pdf': ['top']}


def test_a_change_writes_the_migrated_manifest(tmp_path):
    store = _v1(tmp_path, {'top': {'filename': 'top.png', 'unavailable': False}}, ['top.png'])
    store.set_roles('top.png', ['top', 'hires', 'side_view'])
    data = json.loads(store.path.read_text(encoding='utf-8'))
    assert data['version'] == 2 and data['roles'] == {'top.png': ['top', 'hires', 'side_view']}


def test_role_helpers_accept_the_single_strings_of_version_1():
    assert role_list('top') == ['top']
    assert role_list(['hires', 'pinout', 'hires', 'bogus']) == ['pinout', 'hires']
    assert role_list(None) == []
    paths = [Path('a.png'), Path('b.png')]
    assert photo_paths(paths, {'a.png': 'hires', 'b.png': ['top']}) == [Path('a.png')]
    assert photo_paths(paths, None) == []
