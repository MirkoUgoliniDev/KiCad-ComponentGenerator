"""@brief Heights that are not measured read «stimata» in the review and in the editor (medio 13).

The pipeline declares the provenance of a height (`_height_estimated`,
`_height_source`). A spin box shows an estimate exactly like a reading, so the
review dialog and the component editor say which one it is, and a value typed
by hand becomes the user's reading.
"""
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QColor, QImage

from kicadgen.gui.component_editor_dialog import ComponentEditorDialog
from kicadgen.gui.component_state import (height_is_estimated, mark_height_measured,
                                          mark_height_placeholder)
from kicadgen.gui.photo_review_dialog import PhotoReviewDialog
from kicadgen.services.datasheet_extractor import COMPONENT_KINDS


@pytest.fixture
def png(app):
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(QColor("#243b28"))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def body(name, **extra):
    base = dict(name=name, kind="ic", x_mm=0.0, y_mm=0.0, length_mm=4.0, width_mm=3.0,
                height_mm=1.2, rotation_deg=0.0)
    base.update(extra)
    return base


@pytest.mark.parametrize(("component", "estimated"), [
    (body("A", _height_estimated=True, _height_source="measured"), True),   # il flag vince
    (body("B", _height_estimated=False, _height_source="x_not_measured"), False),
    (body("C", _height_source="coarse_visual_prior_not_measured"), True),
    (body("D", _height_source="visual_package_estimate"), True),
    (body("E", _height_source="measured"), False),
    (body("F", _height_source="user"), False),
    (body("G"), False),                                                     # provenienza ignota
    ({k: v for k, v in body("H").items() if k != "height_mm"}, True),        # segnaposto
])
def test_the_rule_reads_the_declared_attributes(component, estimated):
    assert height_is_estimated(component) is estimated


def test_marking_helpers():
    component = body("X")
    mark_height_placeholder(component)
    assert height_is_estimated(component)
    mark_height_measured(component)
    assert not height_is_estimated(component) and component["_height_source"] == "user"


def label_of(dialog):
    return dialog._form.labelForField(dialog.fields["height_mm"]).text()


def test_review_marks_the_estimated_height_and_a_typed_value_clears_it(png):
    dialog = PhotoReviewDialog(png, [
        body("U1", _height_estimated=True, _height_source="coarse_visual_prior_not_measured"),
        body("U2", _height_source="measured")], 40, 20)
    dialog._select_row(0)
    assert label_of(dialog) == "Altezza (stimata)"
    assert "stimata" in dialog.warning_label.text().lower()
    dialog._select_row(1)
    assert label_of(dialog) == "Altezza", "una misura non va chiamata stima"
    assert "stimata" not in dialog.warning_label.text().lower()
    dialog._select_row(0)
    dialog.fields["height_mm"].setValue(2.5)
    edited = dialog.get_components()[0]
    assert edited["height_mm"] == 2.5 and not height_is_estimated(edited)
    assert label_of(dialog) == "Altezza"
    dialog.close()


def test_review_other_edits_keep_the_estimate_mark(png):
    """Negativo: spostare un corpo non rende la sua altezza misurata."""
    dialog = PhotoReviewDialog(png, [body("U1", _height_estimated=True)], 40, 20)
    dialog._select_row(0)
    dialog.fields["x_mm"].setValue(3.0)
    assert height_is_estimated(dialog.get_components()[0])
    dialog.close()


def test_a_body_added_in_the_review_has_a_placeholder_height(png):
    dialog = PhotoReviewDialog(png, [], 40, 20)
    dialog.add_component()
    assert height_is_estimated(dialog.get_components()[0])
    assert label_of(dialog) == "Altezza (stimata)"
    dialog.close()


def test_editor_marks_the_estimated_cell_and_a_changed_height_becomes_a_reading(app):
    dialog = ComponentEditorDialog(
        [body("U1", _height_estimated=True), body("U2", _height_source="measured")],
        COMPONENT_KINDS)
    estimated_cell, measured_cell = dialog.table.cellWidget(0, 6), dialog.table.cellWidget(1, 6)
    assert estimated_cell.suffix() == " ~" and "stimata" in estimated_cell.toolTip()
    assert measured_cell.suffix() == "" and not measured_cell.toolTip()
    first, second = dialog.components()
    assert height_is_estimated(first), "non toccata: resta una stima"
    estimated_cell.setValue(3.3)
    first, second = dialog.components()
    assert first["height_mm"] == 3.3 and not height_is_estimated(first)
    assert second["_height_source"] == "measured"


def test_editor_new_row_is_a_placeholder_until_typed(app):
    dialog = ComponentEditorDialog([], COMPONENT_KINDS)
    dialog.btn_add.click()
    assert height_is_estimated(dialog.components()[0])
    dialog.table.cellWidget(0, 6).setValue(2.0)
    assert not height_is_estimated(dialog.components()[0])
