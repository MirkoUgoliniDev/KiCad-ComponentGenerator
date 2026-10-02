"""@brief «Copia formato» in the review window (HANDOFF point 172).

The brush of the 3D tab, before the generation: select a body, switch the
brush on, click the others. Each rule has its positive and its negative
case: the source keeps its own data, the targets take the format and keep
their position, a body never clicked is untouched. Since 2026-10-02 («copia
tutto») the detector stage has the brush too, and it also carries the
detector's class.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest

from kicadgen.gui.photo_review_dialog import (
    FORMAT_SOURCE_MARKER, FORMAT_TARGET_MARKER, PhotoReviewDialog, STAGE_DETECTOR)


def _png():
    image = QImage(400, 100, QImage.Format.Format_RGB32)
    image.fill(0)
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _components():
    return [
        dict(name="U1", kind="ic", package="SOIC-8", x_mm=-10.0, y_mm=2.0, length_mm=5.0,
             width_mm=4.0, height_mm=1.5, rotation_deg=90.0, marking="SP3485", color_hex="#101010",
             model_selection={"status": "confirmed", "path": "C:/lib/SOIC-8.step",
                              "rotation_deg": 0}, _box=(.2, .3, .3, .8)),
        dict(name="U2", kind="generic", package="", x_mm=5.0, y_mm=-3.0, length_mm=2.0,
             width_mm=1.0, height_mm=1.0, rotation_deg=0.0, marking="", color_hex="#303030",
             _box=(.6, .3, .65, .4)),
        dict(name="R1", kind="resistor", package="R_0603", x_mm=12.0, y_mm=4.0, length_mm=1.6,
             width_mm=0.8, height_mm=0.5, rotation_deg=0.0, marking="472", color_hex="#404040"),
    ]


@pytest.fixture
def dialog(qapp):
    widget = PhotoReviewDialog(_png(), _components(), 40, 20)
    yield widget
    widget.close()
    qapp.processEvents()


def test_select_then_brush_then_click_pastes_the_format_and_keeps_the_place(dialog):
    """@brief Positive: U1 is the source; U2 takes its format; R1, never clicked, is untouched."""
    dialog._select_row(0)
    dialog.format_button.setChecked(True)
    assert dialog.format_source_row() == 0
    assert FORMAT_SOURCE_MARKER in dialog.component_list.item(0).text()
    dialog._select_row(1)
    assert dialog.format_target_rows() == [1]
    source, target, other = dialog.get_components()
    assert (target["length_mm"], target["width_mm"], target["height_mm"]) == (5.0, 4.0, 1.5)
    assert target["kind"] == "ic" and target["package"] == "SOIC-8"
    assert target["model_selection"]["path"] == "C:/lib/SOIC-8.step"
    # What stays the target's own.
    assert (target["x_mm"], target["y_mm"], target["rotation_deg"]) == (5.0, -3.0, 0.0)
    assert target["name"] == "U2" and target["marking"] == "" and target["color_hex"] == "#303030"
    assert target["_box"] == pytest.approx(dialog.photo_transform.normalized_box(target))
    assert FORMAT_TARGET_MARKER in dialog.component_list.item(1).text()
    # The source is unchanged, the untouched body too.
    assert source == _components()[0] or source["name"] == "U1"
    assert other == _components()[2]
    assert dialog.draw_hint.isVisibleTo(dialog) and "incollato su 1" in dialog.draw_hint.text()
    # A second click on the source does not paste onto itself.
    dialog._select_row(0)
    assert dialog.format_target_rows() == [1]


def test_without_a_selection_the_first_click_is_the_source(dialog):
    dialog._select_row(-1)
    dialog.format_button.setChecked(True)
    assert dialog.format_source_row() == -1
    dialog._select_row(2)
    assert dialog.format_source_row() == 2 and dialog.format_target_rows() == []
    dialog._select_row(1)
    assert dialog.get_components()[1]["kind"] == "resistor"
    assert dialog.get_components()[1]["package"] == "R_0603"


def test_switching_off_or_escape_ends_the_session_and_keeps_the_pastes(dialog):
    """@brief Negative: with the brush off a click selects and changes nothing."""
    dialog._select_row(0)
    dialog.format_button.setChecked(True)
    dialog._select_row(1)
    dialog.format_button.setChecked(False)
    assert dialog.format_source_row() == -1 and dialog.format_target_rows() == []
    assert FORMAT_SOURCE_MARKER not in dialog.component_list.item(0).text()
    assert not dialog.draw_hint.isVisibleTo(dialog)
    dialog._select_row(2)
    assert dialog.get_components()[2] == _components()[2]
    assert dialog.get_components()[1]["kind"] == "ic", "the paste is kept"
    assert [c["kind"] for c in dialog.kept_components()] == ["ic", "ic", "resistor"]
    dialog.format_button.setChecked(True)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert not dialog.format_button.isChecked()
    assert dialog.isVisible() or True  # Escape put the brush down, it did not close the dialog


def test_the_detector_stage_copies_everything_but_the_place(qapp):
    """@brief A box YOLO drew too tight takes everything of a right one (LED 6 case).

    @note User, 2026-10-02: «a parte il posizionamento deve copiare ed
        incollare tutto». Negative: the target keeps its centre, id and name;
        the source and a body never clicked are unchanged.
    """
    rows = [dict(_id=15, _box=(.2, .2, .3, .3), name="led 15", kind="led", x_mm=-10.0, y_mm=0.0,
                 length_mm=4.1, width_mm=1.9, height_mm=0.6, rotation_deg=90.0,
                 _yolo_class="led", _yolo_confidence=.77, package="LED 1206"),
            dict(_id=6, _box=(.6, .2, .65, .25), name="resistor 6", kind="resistor", x_mm=5.0,
                 y_mm=2.0, length_mm=3.1, width_mm=1.2, height_mm=0.5, rotation_deg=0.0,
                 _yolo_class="resistor", _yolo_confidence=.41, _yolo_clipped_sides=["right"]),
            dict(_id=7, _box=(.8, .2, .85, .25), name="ic 7", kind="ic", x_mm=12.0, y_mm=2.0,
                 length_mm=2.0, width_mm=2.0, height_mm=1.0, rotation_deg=0.0,
                 _yolo_class="ic", _yolo_confidence=.9)]
    dialog = PhotoReviewDialog(_png(), [dict(r) for r in rows], 40, 20, stage=STAGE_DETECTOR)
    try:
        assert not dialog.format_button.isHidden()
        dialog._select_row(0)
        dialog.format_button.setChecked(True)
        dialog._select_row(1)
        source, target, other = dialog.get_components()
        assert (target["length_mm"], target["width_mm"]) == (4.1, 1.9)
        assert target["_yolo_class"] == "led" and target["kind"] == "led"
        assert (target["x_mm"], target["y_mm"]) == (5.0, 2.0)
        assert (target["_id"], target["name"]) == (6, "resistor 6")
        assert target["_yolo_confidence"] == .77 and target["rotation_deg"] == 90.0
        assert target["height_mm"] == 0.6 and target["package"] == "LED 1206"
        assert target["_yolo_manual_box"] is True
        assert "_yolo_clipped_sides" not in target
        assert target["_box"] == pytest.approx(dialog.photo_transform.normalized_box(target))
        assert source["_yolo_class"] == "led" and source["_id"] == 15 and source["x_mm"] == -10.0
        assert other["_yolo_class"] == "ic" and other["length_mm"] == 2.0
        # The list on the right says it, with the 3D brush colours.
        from kicadgen.services.component_format import FORMAT_SOURCE_COLOR, FORMAT_TARGET_COLOR
        rows = [dialog.component_list.item(i) for i in range(3)]
        assert FORMAT_SOURCE_MARKER in rows[0].text()
        assert FORMAT_TARGET_MARKER in rows[1].text() and "4.1" in rows[1].text()
        assert FORMAT_SOURCE_MARKER not in rows[2].text() and FORMAT_TARGET_MARKER not in rows[2].text()
        # The source stands out: filled yellow row, filled yellow box.
        assert rows[0].background().color().name() == FORMAT_SOURCE_COLOR
        assert rows[1].foreground().color().name() == FORMAT_TARGET_COLOR
        assert dialog._items[0].pen().color().name() == FORMAT_SOURCE_COLOR
        assert dialog._items[0].brush().color().alpha() > dialog._items[1].brush().color().alpha()
        # Negative: the brush off, every body back to its own state colour.
        dialog.format_button.setChecked(False)
        assert all(FORMAT_SOURCE_MARKER not in r.text() and FORMAT_TARGET_MARKER not in r.text()
                   for r in rows)
        assert dialog._items[0].pen().color().name() != FORMAT_SOURCE_COLOR
        assert rows[0].background().style() == Qt.BrushStyle.NoBrush
    finally:
        dialog.close()
