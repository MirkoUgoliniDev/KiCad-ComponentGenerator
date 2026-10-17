"""@brief All'AI si manda la foto nel verso in cui la vedono analisi e anteprima (utente, 2026-09-25).

Nessuna chiamata di rete: si esamina solo il blocco di contenuto costruito.
"""
import base64
from io import BytesIO

import pytest
from PIL import Image

from kicadgen.services.datasheet_extractor import _file_to_content_block


def _photo(path, orientation=None, fmt="JPEG"):
    """Pixel salvati VERTICALI (20x40); con orientation=6 il verso vero e' orizzontale."""
    image = Image.new("RGB", (20, 40), "green")
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    image.save(path, fmt, exif=exif.tobytes())
    return path


def _sent(path):
    url = _file_to_content_block(path)["image_url"]["url"]
    return base64.b64decode(url.split(",", 1)[1])


def test_a_rotated_phone_photo_is_sent_upright(tmp_path):
    sent = Image.open(BytesIO(_sent(_photo(tmp_path / "rotated.jpg", 6))))
    assert sent.size == (40, 20)  # orizzontale, come la vede OpenCV
    assert sent.getexif().get(0x0112, 1) == 1  # il tag non ruota una seconda volta
    assert sent.format == "JPEG"


@pytest.mark.parametrize("orientation", [None, 1])
def test_a_photo_without_rotation_is_not_rotated_nor_reencoded(tmp_path, orientation):
    """Negative: no rotation tag -> same size and the same compressed pixels.

    The contract used to be «the original file, byte for byte»; since the
    privacy fix (audit 2026-10-15) the EXIF segment is removed, so only the
    pixels and the absence of re-encoding are compared (test_upload_privacy).
    """
    path = _photo(tmp_path / "plain.jpg", orientation)
    sent_bytes = _sent(path)
    sent = Image.open(BytesIO(sent_bytes))
    original = Image.open(path)
    assert sent.size == (20, 40)
    assert sent.tobytes() == original.tobytes()
    assert len(sent_bytes) <= len(path.read_bytes())


def test_an_undecodable_image_is_refused(tmp_path):
    """A file no decoder reads is refused, never sent raw (contract changed 2026-10-15).

    It used to leave as it was; its metadata could not be removed, so the
    privacy boundary now fails closed (see test_upload_privacy).
    """
    from kicadgen.services.datasheet_extractor import ExtractionError
    path = tmp_path / "strano.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nnon-una-immagine")
    with pytest.raises(ExtractionError, match="strano.png"):
        _sent(path)
