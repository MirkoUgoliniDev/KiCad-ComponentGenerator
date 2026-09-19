from kicadgen.services.document_roles import ROLES
import json
from pathlib import Path

from PIL import Image
import pytest
from kicadgen.services.document_roles import DocumentSet, DocumentError, MANIFEST


def picture(path, color='red'):
    Image.new('RGB', (32, 20), color).save(path)
    return path


def test_copies_standard_names_keeps_original_and_persists_roles(tmp_path):
    source = picture(tmp_path / 'arbitrary-photo.png')
    original = source.read_bytes()
    store = DocumentSet(tmp_path / 'docs')
    target = store.import_file('top', source)
    assert target.name == 'top.png'
    assert source.read_bytes() == target.read_bytes() == original
    store.clear('pinout', unavailable=True)
    store.clear('hires', unavailable=True)
    paths, roles = DocumentSet(store.directory).selected()
    assert paths == [target] and roles == {'top.png': 'top'}
    assert store.read()['roles']['top']['original_name'] == source.name


def test_replace_extension_and_remove_are_recoverable(tmp_path):
    source = picture(tmp_path / 'one.png')
    store = DocumentSet(tmp_path / 'docs')
    old = store.import_file('pinout', source)
    content = old.read_bytes()
    second = picture(tmp_path / 'two.jpg', 'blue')
    with pytest.raises(DocumentError, match='sostituzione'):
        store.import_file('pinout', second)
    target = store.import_file('pinout', second, replace=True)
    assert target.name == 'pinout.jpg' and not old.exists()
    assert next((store.directory / '.history').glob('*/pinout.png')).read_bytes() == content
    backup = store.clear('pinout', unavailable=True)
    assert not target.exists()
    assert (backup / 'pinout.jpg').read_bytes() == second.read_bytes()
    assert source.exists() and second.exists()


def test_manifest_failure_rolls_back_file_replacement(tmp_path, monkeypatch):
    store = DocumentSet(tmp_path / 'docs')
    old = store.import_file('top', picture(tmp_path / 'one.png'))
    previous = old.read_bytes(), store.path.read_bytes()
    def failed(data): raise OSError('disk unavailable')
    monkeypatch.setattr(store, '_write', failed)
    with pytest.raises(OSError):
        store.import_file('top', picture(tmp_path / 'two.png', 'blue'), replace=True)
    assert (old.read_bytes(), store.path.read_bytes()) == previous


@pytest.mark.parametrize('content', [b'not an image', b'%PDF-1.4\n'])
def test_rejects_misleading_image_extension(tmp_path, content):
    source = tmp_path / 'fake.png'
    source.write_bytes(content)
    with pytest.raises(DocumentError):
        DocumentSet(tmp_path / 'docs').import_file('top', source)
    assert source.read_bytes() == content


def test_pdf_keeps_its_extension(tmp_path):
    source = tmp_path / 'diagram.pdf'
    source.write_bytes(b'%PDF-1.4\nfixture')
    store = DocumentSet(tmp_path / 'docs')
    result = store.import_file('pinout', source)
    assert result.name == 'pinout.pdf' and result.read_bytes() == source.read_bytes()


def test_unassigned_legacy_files_are_never_silently_selected(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    store.directory.mkdir()
    old = picture(store.directory / 'old.png')
    with pytest.raises(DocumentError, match='Pinout'):
        store.selected()
    assert old.exists() and not store.path.exists()


def test_empty_and_missing_roles_are_explicit(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    for role in ROLES:
        store.clear(role, unavailable=True)
    with pytest.raises(DocumentError, match='almeno'):
        store.selected()
    file = store.import_file('top', picture(tmp_path / 'sample.png'))
    file.unlink()
    with pytest.raises(DocumentError, match='mancante'):
        store.selected()


def test_manifest_rejects_traversal_and_preserves_corrupt_source(tmp_path):
    store = DocumentSet(tmp_path / 'docs')
    store.directory.mkdir()
    raw = json.dumps({'version': 1, 'roles': {'top': {'filename': '../outside.png'}}})
    store.path.write_text(raw)
    with pytest.raises(DocumentError):
        store.read()
    assert store.path.read_text() == raw
