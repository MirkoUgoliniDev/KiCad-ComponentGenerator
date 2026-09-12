"""Test per la logica di validazione di kicadgen.services.datasheet_extractor.

Non chiamano l'API Anthropic reale: verificano solo i controlli locali
(chiave mancante, file non supportati, troppo grandi, troppi file).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.services.datasheet_extractor import (
    MAX_FILES, ExtractionError, extract_pin_header_params, _file_to_content_block,
)


def test_extract_requires_at_least_one_file():
    with pytest.raises(ExtractionError, match="Nessun file"):
        extract_pin_header_params([])


def test_extract_rejects_too_many_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    files = []
    for i in range(MAX_FILES + 1):
        f = tmp_path / f"doc{i}.pdf"
        f.write_bytes(b"%PDF-1.4")
        files.append(f)
    with pytest.raises(ExtractionError, match="Troppi file"):
        extract_pin_header_params(files)


def test_extract_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    with pytest.raises(ExtractionError, match="ANTHROPIC_API_KEY"):
        extract_pin_header_params([f])


def test_content_block_rejects_unsupported_extension(tmp_path):
    f = tmp_path / "doc.docx"
    f.write_bytes(b"fake")
    with pytest.raises(ExtractionError, match="non supportato"):
        _file_to_content_block(f)


def test_content_block_rejects_oversized_file(tmp_path, monkeypatch):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        "kicadgen.services.datasheet_extractor.MAX_FILE_BYTES", 1
    )
    with pytest.raises(ExtractionError, match="troppo grande"):
        _file_to_content_block(f)


def test_content_block_builds_pdf_document_block(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake content")
    block = _file_to_content_block(f)
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"


def test_content_block_builds_image_block(tmp_path):
    f = tmp_path / "drawing.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    block = _file_to_content_block(f)
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"
