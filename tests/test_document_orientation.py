"""@brief L'anteprima dei documenti mostra la foto nel verso in cui la vede l'analisi.

Utente, 2026-09-24: «questa immagine perche' la vedo verticale? quella reale
e' orizzontale». I telefoni salvano i pixel in un verso e il verso giusto nel
tag EXIF di orientamento; OpenCV lo applica, QPixmap no.
"""
import pytest

pytest.importorskip("PyQt6")
from PIL import Image
from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QApplication

from kicadgen.gui.guided_documents import document_pixmap


def _photo(path, orientation=None):
    """Pixel salvati VERTICALI (20x40); con orientation=6 il verso vero e' orizzontale."""
    image = Image.new("RGB", (20, 40), "green")
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    image.save(path, "JPEG", exif=exif.tobytes())
    return path


def test_the_preview_applies_the_exif_orientation(tmp_path):
    """@brief Positivo: tag 6 -> anteprima orizzontale. Negativo: senza tag resta verticale."""
    app = QApplication.instance() or QApplication([])
    rotated = document_pixmap(_photo(tmp_path / "rotated.jpg", 6), QSize(240, 76))
    plain = document_pixmap(_photo(tmp_path / "plain.jpg"), QSize(240, 76))
    assert rotated.width() > rotated.height()
    assert plain.height() > plain.width()
    assert app is not None


def test_the_analysis_sees_the_same_orientation(tmp_path):
    """@brief La lettura dell'analisi (`photo_inventory.read_photo`) applica il tag.

    Passa dal codice del progetto, non da OpenCV direttamente (audit 25/09):
    se la lettura smettesse di applicare il tag, questo test fallirebbe.
    Negativo: senza tag la foto resta verticale.
    """
    pytest.importorskip("cv2")
    from kicadgen.services.photo_inventory import read_photo
    height, width = read_photo(_photo(tmp_path / "rotated.jpg", 6)).shape[:2]
    assert width > height
    height, width = read_photo(_photo(tmp_path / "plain.jpg")).shape[:2]
    assert height > width
