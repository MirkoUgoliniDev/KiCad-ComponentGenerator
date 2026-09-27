"""@brief Anteprima dei documenti: icona a occhio e zoom (utente, 2026-09-24).

Il pulsante delle schede apre un'anteprima nel programma, non sfoglia file:
occhio, non cartella. Nella finestra si zooma come nella revisione dei
componenti (rotella attorno al puntatore, +/-, F per adattare), sui pixel veri
del documento e non su una miniatura ingrandita.
"""
import pytest
from PIL import Image
from PyQt6.QtCore import QPoint, QPointF, QSize, Qt
from PyQt6.QtGui import QPixmap, QWheelEvent
from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog

from kicadgen.gui.guided_documents import document_pixmap
from kicadgen.gui.zoom_image import MAX_ZOOM, MIN_ZOOM, ZoomImagePanel
from tests.test_photo_inventory_gui import gui


def _panel(app, size=(800, 400)):
    pixmap = QPixmap(*size)
    pixmap.fill(Qt.GlobalColor.darkGreen)
    panel = ZoomImagePanel(pixmap)
    panel.resize(600, 500)
    panel.show()
    app.processEvents()
    return panel


def test_the_preview_opens_fitted_and_zooms_in_and_out(app):
    panel = _panel(app)
    view = panel.view
    assert view.auto_fit and view.zoom_percent() == 100
    assert panel.zoom_label.text() == "100%"
    panel.zoom_in_button.click()
    assert view.zoom_percent() > 100 and not view.auto_fit
    assert panel.zoom_label.text() == f"{view.zoom_percent()}%"
    panel.zoom_out_button.click()
    panel.zoom_out_button.click()
    assert view.zoom_percent() < 100
    panel.fit_button.click()
    assert view.auto_fit and view.zoom_percent() == 100
    _dispose(panel)


def test_the_wheel_zooms_like_the_review_canvas(app):
    panel = _panel(app)
    view = panel.view
    centre = QPointF(view.viewport().rect().center())
    event = QWheelEvent(centre, QPointF(view.viewport().mapToGlobal(centre.toPoint())),
                        QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), event)
    assert view.zoom_percent() > 100
    _dispose(panel)


def test_zoom_has_limits_both_ways(app):
    panel = _panel(app)
    panel.view.zoom(1e9)
    assert panel.view.zoom_percent() == round(100 * MAX_ZOOM)
    panel.view.zoom(1e-9)
    assert panel.view.zoom_percent() == round(100 * MIN_ZOOM)
    _dispose(panel)


def test_a_chosen_zoom_survives_a_resize_and_fit_follows_it(app):
    """Negativo: dopo uno zoom, ridimensionare non riadatta; dopo «adatta» si'."""
    panel = _panel(app)
    panel.view.zoom(2)
    scale = panel.view.transform().m11()
    panel.resize(900, 700)
    app.processEvents()
    assert panel.view.transform().m11() == pytest.approx(scale)
    panel.view.fit()
    panel.resize(500, 400)
    app.processEvents()
    assert panel.view.zoom_percent() == 100
    _dispose(panel)


def test_the_zoomable_preview_keeps_the_real_pixels(tmp_path, app):
    """Positivo: un'immagine piccola resta ai suoi pixel. Negativo: le card la ingrandiscono."""
    path = tmp_path / "small.png"
    Image.new("RGB", (120, 60), "blue").save(path)
    real = document_pixmap(path, QSize(8192, 8192), upscale=False)
    card = document_pixmap(path, QSize(240, 76))
    assert (real.width(), real.height()) == (120, 60)
    assert card.height() == 76
    big = tmp_path / "big.png"
    Image.new("RGB", (400, 200), "blue").save(big)
    capped = document_pixmap(big, QSize(100, 100), upscale=False)
    assert capped.width() == 100  # troppo grande: ridotta, mai oltre il limite


def test_the_card_button_is_an_eye_that_opens_the_zoomable_preview(window, monkeypatch,  # noqa: F811
                                                                    tmp_path):
    panel = window.guided_docs
    source = tmp_path / "board.png"
    Image.new("RGB", (300, 150), "blue").save(source)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(source), ""))
    panel.add_button.click()
    view_button = panel.cards["board.png"]["view"]
    name, _, detail = view_button.toolTip().partition("\n")
    assert name == "Anteprima documento" and "zoom" in detail
    opened = []

    def fake_exec(dialog):
        opened.append(dialog)
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, "exec", fake_exec)
    view_button.click()
    assert len(opened) == 1
    zoom_panel = opened[0].zoom_panel
    assert zoom_panel.view._item.pixmap().width() == 300  # pixel veri
    _dispose(opened[0])


def test_buttons_zoom_around_the_centre_and_the_percentage_stays_true(app):
    """Il centro dell'immagine resta al centro; 2x fa 200%, anche con le barre comparse."""
    panel = _panel(app)
    view = panel.view
    centre_before = view.mapToScene(view.viewport().rect().center())
    view.zoom(2)
    app.processEvents()
    centre_after = view.mapToScene(view.viewport().rect().center())
    assert abs(centre_after.x() - centre_before.x()) < 8
    assert abs(centre_after.y() - centre_before.y()) < 8
    assert view.zoom_percent() == 200
    _dispose(panel)


def _dispose(widget):
    """Distruzione ordinata: un widget Qt lasciato al garbage collector puo'
    essere distrutto dopo la QApplication e far cadere il processo all'uscita
    (codice 139, visto il 2026-09-24)."""
    widget.close()
    widget.setParent(None)
    widget.deleteLater()
    QApplication.processEvents()


# ── Audit 2026-09-25 ────────────────────────────────────────────────────────

def test_a_pdf_page_is_shown_on_white_paper(tmp_path, app):
    """Negativo al difetto: QPdfDocument lascia il fondo trasparente, e sul
    visore scuro il testo nero diventava illeggibile."""
    pytest.importorskip("PyQt6.QtPdf")
    from PyQt6.QtGui import QPageSize, QPainter, QPdfWriter
    pdf = tmp_path / "datasheet.pdf"
    writer = QPdfWriter(str(pdf))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    painter = QPainter(writer)
    painter.drawText(200, 200, "PIN 1")
    painter.end()
    pixmap = document_pixmap(pdf, QSize(8192, 8192), upscale=False)
    corner = pixmap.toImage().pixelColor(2, 2)
    assert corner.alpha() == 255 and corner.lightness() > 240


@pytest.mark.parametrize("size, upscale", [((9000, 3000), False), ((9000, 3000), True)])
def test_a_huge_photo_is_reduced_while_read_not_refused(tmp_path, app, size, upscale):
    """Oltre ~8192 px Qt rifiutava la decodifica (limite 256 MB): anteprima e
    miniatura vuote. Ora la riduzione avviene in lettura."""
    path = tmp_path / "huge.png"
    Image.new("RGB", size, "blue").save(path)
    box = QSize(8192, 8192) if not upscale else QSize(240, 76)
    pixmap = document_pixmap(path, box, upscale=upscale)
    assert not pixmap.isNull()
    assert pixmap.width() <= box.width() and pixmap.height() <= box.height()


def test_a_rotated_huge_photo_keeps_its_orientation(tmp_path, app):
    """Il tag EXIF 6 ruota di 90 gradi: la riduzione tiene conto del verso."""
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (9000, 3000), "green").save(path, "JPEG", exif=exif.tobytes())
    pixmap = document_pixmap(path, QSize(8192, 8192), upscale=False)
    assert pixmap.height() > pixmap.width() and pixmap.height() <= 8192


def test_zoom_stays_within_limits_after_the_window_grows(app):
    """Zoom al minimo, poi finestra ingrandita: la percentuale non scende sotto
    il limite e «zoom indietro» non ingrandisce."""
    panel = _panel(app)
    panel.view.zoom(1e-9)
    panel.resize(1400, 1100)
    app.processEvents()
    assert panel.view.zoom_percent() >= round(100 * MIN_ZOOM)
    before = panel.view.transform().m11()
    panel.zoom_out_button.click()
    assert panel.view.transform().m11() <= before
    _dispose(panel)
