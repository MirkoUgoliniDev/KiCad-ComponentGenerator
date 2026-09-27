"""@brief The photo review reads the body fields of the photographed board (2026-09-26).

A correction of the body size after the extraction changes the scale (the
existing contract of test_photo_inventory_gui); a change of component type
must not, because its fields describe another body.
"""
import pytest
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QColor, QImage

from kicadgen.services.photo_inventory import save_review_bundle
from tests.test_photo_inventory_gui import gui


def _png(width=633, height=254):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#205020"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


@pytest.fixture
def opened(window, monkeypatch):  # noqa: F811
    """Capture the size the review dialog is built with, without showing it."""
    sizes = []

    class Review:
        def __init__(self, png, components, length, width, parent):
            sizes.append((length, width))
            self._items = []
            self.components_applied = type("Signal", (), {"connect": lambda *a: None})()

        def setWindowTitle(self, *args):  # noqa: N802 - Qt name
            pass

        def exec(self):
            return 0

    monkeypatch.setattr(gui, "PhotoReviewDialog", Review)
    monkeypatch.setattr(window.i18n, "retranslate_widgets", lambda *args: None)
    return sizes


def _board_then_matrix_type(window):  # noqa: F811
    """Board fields 63.3 x 25.4, then a type whose fields are the matrix ones (10 x 10)."""
    window.field_cast_body_length.setValue(63.3)
    window.field_cast_body_width.setValue(25.4)
    window.field_body_length.setValue(10.0)
    window.field_body_width.setValue(10.0)
    window.group_castellated.setChecked(False)
    assert not window._is_castellated()


def test_a_changed_type_does_not_change_the_photo_scale(window, opened):  # noqa: F811
    _board_then_matrix_type(window)
    window._photo_png, window._photo_body_fields = _png(), "cast"
    window._show_photo_comparison()
    assert opened == [(63.3, 25.4)]
    assert "la foto mostra la scheda di 63.30 x 25.40 mm" in window._log_plain_text()


def test_a_correction_of_the_board_size_is_followed(window, opened):  # noqa: F811
    """@brief Negativo: correggere la quota della scheda della foto cambia la scala."""
    _board_then_matrix_type(window)
    window._photo_png, window._photo_body_fields = _png(), "cast"
    window.field_cast_body_length.setValue(64.0)
    window._show_photo_comparison()
    assert opened == [(64.0, 25.4)]


def test_without_knowing_the_board_fields_the_current_type_is_used(window, opened):  # noqa: F811
    _board_then_matrix_type(window)
    window._photo_png, window._photo_body_fields = _png(), None
    window._show_photo_comparison()
    assert opened == [(10.0, 10.0)]


def test_a_reopened_project_finds_the_fields_matching_its_saved_photo(window):  # noqa: F811
    _board_then_matrix_type(window)
    project = window._project_dir()
    project.mkdir(parents=True, exist_ok=True)
    rows = [{"_id": 1, "_box": [0.1, 0.1, 0.2, 0.2], "name": "U1", "kind": "ic",
             "x_mm": 0.0, "y_mm": 0.0, "length_mm": 5.0, "width_mm": 5.0, "rotation_deg": 0}]
    save_review_bundle(project, rows, _png(), 63.3, 25.4, detector="yolo")
    window._photo_png, window._photo_body_fields = None, None
    window._restore_saved_photo()
    assert window._photo_png and window._photo_body_fields == "cast"
    # Negativo: nessun insieme di campi ha le quote del pacchetto -> ignoto.
    window.field_cast_body_length.setValue(40.0)
    window._photo_png, window._photo_body_fields = None, None
    window._restore_saved_photo()
    assert window._photo_body_fields is None


def test_the_3d_components_carry_the_scale_of_the_calibrated_photo(window):  # noqa: F811
    """@brief Il 3D ricava l'incertezza in mm dalla scala della foto, non da un ripiego."""
    _board_then_matrix_type(window)
    window._photo_png, window._photo_body_fields = _png(633, 254), "cast"
    window._photo_rows = [dict(name="U1", x_mm=0, y_mm=0, length_mm=5, width_mm=5, height_mm=1)]
    boxes = window._components_from_table()
    assert boxes[0].match_evidence["photo_scale_px_per_mm"] == pytest.approx(633 / 63.3)


def test_without_a_calibrated_photo_no_scale_is_invented(window):  # noqa: F811
    """@brief Negativo: senza foto calibrata nessuna scala; il 3D usa il suo ripiego dichiarato."""
    window._photo_png = None
    window._photo_rows = [dict(name="U1", x_mm=0, y_mm=0, length_mm=5, width_mm=5, height_mm=1)]
    boxes = window._components_from_table()
    assert "photo_scale_px_per_mm" not in boxes[0].match_evidence
