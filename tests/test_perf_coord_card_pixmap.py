"""@brief Document cards are drawn from disk once per file version (2026-10-17)."""
import os

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QSize  # noqa: E402

from kicadgen.gui import guided_documents as gd  # noqa: E402


@pytest.fixture
def picture(qapp, tmp_path, monkeypatch):
    from PIL import Image
    path = tmp_path / "foto.png"
    Image.new("RGB", (40, 30), "red").save(path)
    monkeypatch.setattr(gd, "_CARD_PIXMAPS", {})
    calls = []
    real = gd.document_pixmap
    monkeypatch.setattr(gd, "document_pixmap", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    return path, calls


def test_an_unchanged_file_is_drawn_once(picture):
    path, calls = picture
    first = gd.card_pixmap(path, QSize(100, 80))
    for _ in range(5):
        again = gd.card_pixmap(path, QSize(100, 80))
    assert len(calls) == 1 and again.cacheKey() == first.cacheKey()


def test_a_rewritten_file_or_another_box_is_drawn_again(picture):
    """@brief Negative: the memo never hides a new version of the file."""
    from PIL import Image
    path, calls = picture
    gd.card_pixmap(path, QSize(100, 80))
    Image.new("RGB", (60, 30), "blue").save(path)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    gd.card_pixmap(path, QSize(100, 80))
    gd.card_pixmap(path, QSize(50, 40))
    assert len(calls) == 3


def test_an_unreadable_document_is_tried_again(picture, tmp_path):
    path = tmp_path / "rotto.png"
    path.write_bytes(b"not an image")
    _, calls = picture
    assert gd.card_pixmap(path, QSize(100, 80)).isNull()
    gd.card_pixmap(path, QSize(100, 80))
    assert len(calls) == 2
