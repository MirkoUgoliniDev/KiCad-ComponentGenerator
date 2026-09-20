"""@brief Verify evidence invalidation only for edited photo rectangles, offline."""
from copy import deepcopy
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from kicadgen.gui.photo_review_dialog import PhotoReviewDialog


@pytest.fixture
def editor():
    app = QApplication.instance() or QApplication([])
    image = QImage(400, 100, QImage.Format.Format_RGB32)
    image.fill(0)
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    source = [dict(name="Body", kind="generic", x_mm=1., y_mm=2.,
                   length_mm=4., width_mm=2., height_mm=1., rotation_deg=0.,
                   package="VisiblePackage", mating="top", bbox_basis="pads",
                   package_evidence="Outer PCB pad edges", _box=(.475, .55, .575, .65),
                   terminal_geometry=dict(body_bbox=[.2, .2, .8, .8],
                                          terminals=[dict(bbox=[0, .3, .2, .4])]),
                   _source="detail", bbox=[.1, .2, .3, .4],
                   custom=dict(provenance=[1, 2]))]
    source.append(dict(deepcopy(source[0]), name="Other", x_mm=10.))
    before = deepcopy(source)
    dialog = PhotoReviewDialog(bytes(data), source, 40, 20)
    yield dialog, source, before
    dialog.close()
    app.processEvents()


def assert_invalidated(editor):
    dialog, source, before = editor
    result = dialog.get_components()
    changed = result[0]
    assert changed["bbox_basis"] == "unknown"
    assert "package_evidence" not in changed
    assert "terminal_geometry" not in changed
    for key in ("package", "mating", "_source", "bbox", "custom"):
        assert changed[key] == before[0][key]
    assert changed["_box"] == pytest.approx(dialog.photo_transform.normalized_box(changed))
    assert result[1] == before[1]
    assert source == before


@pytest.mark.parametrize("key,value", [
    ("x_mm", 3.), ("y_mm", -1.), ("length_mm", 2.), ("width_mm", 1.),
])
def test_numeric_rectangle_edits_discard_photo_evidence(editor, key, value):
    dialog, _, _ = editor
    dialog.fields[key].setValue(value)
    assert dialog.get_components()[0][key] == value
    assert_invalidated(editor)


@pytest.mark.parametrize("angle", [90., 37.])
def test_a_rotation_keeps_what_the_outline_encloses(editor, angle):
    """Spostare o ridimensionare ridisegna il contorno; ruotarlo no.

    `bbox_basis` risponde a "il contorno segue il corpo o il corpo coi
    reofori": girare lo stesso rettangolo non cambia quella risposta, e
    scriverci "unknown" non e' "non lo so piu'" ma un'affermazione diversa,
    che manda il componente sul ramo delle piazzole e gli nega il modello
    nativo di KiCad. Le posizioni dei terminali dentro il rettangolo invece
    dipendono da come il rettangolo e' posato, quindi quelle decadono.
    """
    dialog, _, before = editor
    dialog.fields["rotation_deg"].setValue(angle)
    changed = dialog.get_components()[0]
    assert changed["rotation_deg"] == angle
    assert changed["bbox_basis"] == before[0]["bbox_basis"]
    assert changed["package_evidence"] == before[0]["package_evidence"]
    assert "terminal_geometry" not in changed


def test_drag_position_change_discards_photo_evidence(editor):
    dialog, _, _ = editor
    item = dialog._items[0]
    item.setPos(item.pos() + QPointF(10, -5))
    assert dialog.get_components()[0]["x_mm"] == pytest.approx(2.)
    assert dialog.get_components()[0]["y_mm"] == pytest.approx(1.)
    assert_invalidated(editor)


def test_selection_zoom_and_unchanged_apply_preserve_evidence_exactly(editor):
    dialog, source, before = editor
    spy = QSignalSpy(dialog.components_applied)
    dialog._select_row(1)
    dialog._select_row(0)
    dialog.view.zoom(2)
    dialog.fit_photo()
    dialog.accept()
    assert len(spy) == 1 and spy[0][0] == before
    assert source == before
    spy[0][0][0]["terminal_geometry"]["body_bbox"][0] = .9
    assert dialog.get_components() == before


@pytest.mark.parametrize("key", PhotoReviewDialog._GEOMETRY_FIELDS)
def test_equal_numeric_callback_does_not_invalidate(editor, key):
    dialog, _, before = editor
    dialog._number_edited(key, before[0][key])
    assert dialog.get_components() == before


def test_unchanged_move_callback_preserves_exact_coordinates(editor):
    dialog, _, before = editor
    dialog._item_moved(dialog._items[0])
    assert dialog.get_components() == before


def test_height_and_text_edits_keep_rectangle_evidence(editor):
    dialog, _, before = editor
    dialog.fields["height_mm"].setValue(2.)
    dialog.text_fields["name"].setText("Edited name")
    dialog._text_edited("name")
    expected = deepcopy(before)
    expected[0].update(height_mm=2., name="Edited name")
    assert dialog.get_components() == expected


def test_cancel_after_geometry_edit_does_not_touch_source_or_emit(editor):
    dialog, source, before = editor
    spy = QSignalSpy(dialog.components_applied)
    dialog.fields["length_mm"].setValue(2.)
    assert_invalidated(editor)
    dialog.reject()
    assert len(spy) == 0 and source == before


def test_apply_delivers_detached_invalidated_metadata(editor):
    dialog, source, before = editor
    spy = QSignalSpy(dialog.components_applied)
    dialog.fields["width_mm"].setValue(3.)
    dialog.accept()
    assert len(spy) == 1
    assert spy[0][0] == dialog.get_components()
    assert_invalidated(editor)
    spy[0][0][0]["custom"]["provenance"].append(3)
    assert source == before
    assert dialog.get_components()[0]["custom"] == before[0]["custom"]
