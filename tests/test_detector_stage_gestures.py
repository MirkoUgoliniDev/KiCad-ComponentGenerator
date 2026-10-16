"""@brief Real pointer gestures in the detector stage of the review window.

The detector stage (`PhotoReviewDialog(..., stage=STAGE_DETECTOR)`) turns a
reviewer's hand on a proposal into two facts that reach the paid AI call:
the box is the reviewer's (`_yolo_manual_box`, and a sub-threshold proposal
becomes kept), and of the sides resting on a tile seam
(`_yolo_clipped_sides`) only the one the reviewer actually dragged stops
being an artefact. These tests drive the view with real mouse presses,
moves and releases, so the path item -> `_item_moved` / `_item_resized` ->
`_geometry_changed` -> `mark_manual_box` is exercised end to end rather
than through the side fields. Every rule has a positive and a negative case
that differ for a structural reason (moved or not, which handle, which
rotation), never by a tuned number.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, Qt
from PyQt6.QtGui import QImage, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from kicadgen.gui.photo_review_dialog import PhotoReviewDialog, STAGE_DETECTOR
from kicadgen.services import photo_yolo
from kicadgen.services.photo_yolo import MANUAL_SOURCE

## Board size in mm; the 400 x 100 px photo gives 10 px/mm on X and 5 px/mm on Y.
LENGTH, WIDTH = 40.0, 20.0


def _png():
    """A plain 400 x 100 px PNG: the gestures need a photo, not its content."""
    image = QImage(400, 100, QImage.Format.Format_RGB32)
    image.fill(0)
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _proposal(identity, box, **extra):
    """A detector proposal as `detect_yolo_components` writes it (mm geometry and `_box`)."""
    row = dict(_id=identity, _box=tuple(box), name=f"YOLO candidate {identity}", kind="generic",
               **photo_yolo.box_geometry_mm(box, LENGTH, WIDTH), height_mm=1.0,
               color_hex="#303030", confidence="low", _uncertain=True, _source="YOLO local",
               _yolo_class="resistor", _yolo_confidence=.6, _yolo_review_only=False,
               _yolo_origin="operating_threshold", _yolo_low_confidence=False,
               _yolo_class_ambiguous=False, _yolo_proposal_id=f"p{identity}")
    row.update(extra)
    return row


def _drag(app, view, start, end):
    """Send a real held-left-button stroke, independent of the offscreen OS cursor."""
    button = Qt.MouseButton.LeftButton
    QTest.mousePress(view.viewport(), button, Qt.KeyboardModifier.NoModifier, start)
    event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(end),
                        QPointF(view.viewport().mapToGlobal(end)),
                        Qt.MouseButton.NoButton, button, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), event)
    QTest.mouseRelease(view.viewport(), button, Qt.KeyboardModifier.NoModifier, end)
    app.processEvents()


@pytest.fixture
def detector(qapp):
    """Detector stage with a strong, a clipped weak and a sub-threshold proposal.

    The boxes are wider than tall (10-12 x 8 mm, so rotation 0 without a
    tie between the sides) and well apart, so the interior of each is far
    wider than the 8 px grips that frame it. The «not kept» switch is on,
    as a reviewer turns it on to see the sub-threshold proposal at all.
    """
    rows = [
        _proposal(1, (.05, .1, .30, .5)),
        _proposal(2, (.35, .3, .65, .7), _yolo_class="led", _yolo_confidence=.3,
                  _yolo_review_only=True, _yolo_origin="low_confidence_review",
                  _yolo_clipped_sides=["left", "right"], _yolo_clipped_recovery=True),
        _proposal(3, (.70, .5, .95, .9), _yolo_class="capacitor", _yolo_confidence=.1,
                  _yolo_review_only=True, _yolo_origin="low_confidence_review",
                  _yolo_low_confidence=True),
    ]
    assert [row["rotation_deg"] for row in rows] == [0, 0, 0]
    dialog = PhotoReviewDialog(_png(), rows, LENGTH, WIDTH, stage=STAGE_DETECTOR)
    dialog.show()
    dialog.show_unkept.setChecked(True)
    qapp.processEvents()
    assert all(item.isVisible() for item in dialog._items)
    yield dialog
    dialog.close()
    qapp.processEvents()


def _centre(dialog, index):
    """Viewport pixel of the centre of outline `index`."""
    return dialog.view.mapFromScene(dialog._items[index].scenePos())


def _drag_grip(dialog, app, index, edge, offset):
    """Select outline `index` and drag its local grip `edge` by a viewport offset."""
    dialog._select_row(index, center=False)
    app.processEvents()
    item = dialog._items[index]
    assert item.handle(edge).isVisible()
    start = dialog.view.mapFromScene(item.handle(edge).scenePos())
    _drag(app, dialog.view, start, start + offset)


def _row(dialog, index):
    """The proposal with list index `index`, as the dialog currently holds it."""
    return dialog.get_components()[index]


# ── M1: a dragged proposal becomes the reviewer's box ─────────────────────

def test_dragging_a_sub_threshold_proposal_makes_it_the_reviewers_and_keeps_it(detector, qapp):
    """@brief Positive: a mouse drag on a not-kept weak proposal marks it manual and keeps it."""
    assert [c["_id"] for c in detector.left_out_components()] == [3]
    before = dict(_row(detector, 2))
    start = _centre(detector, 2)
    _drag(qapp, detector.view, start, start + QPoint(-15, -6))
    moved = _row(detector, 2)
    assert moved["x_mm"] != before["x_mm"], "the gesture really moved the outline"
    assert moved["_yolo_manual_box"] is True and moved["_source"] == MANUAL_SOURCE
    assert moved["_yolo_source_before_manual"] == "YOLO local"
    assert moved["_yolo_review_only"] is False, "the eye verified it"
    assert moved["_yolo_user_kept"] is True
    assert [c["_id"] for c in detector.kept_components()] == [1, 2, 3]
    assert detector.left_out_components() == []
    assert moved["_box"] == pytest.approx(detector.photo_transform.normalized_box(moved))
    # Negative: the proposals nobody touched are still the detector's.
    for index in (0, 1):
        untouched = _row(detector, index)
        assert "_yolo_manual_box" not in untouched and untouched["_source"] == "YOLO local"


def test_a_click_without_movement_leaves_the_proposal_the_detectors_and_not_kept(detector, qapp):
    """@brief Negative: press and release on the same pixel selects, but neither marks nor keeps."""
    before = dict(_row(detector, 2))
    point = _centre(detector, 2)
    _drag(qapp, detector.view, point, point)
    assert detector.component_list.currentRow() == 2, "the click did reach the outline"
    after = _row(detector, 2)
    assert after == before
    assert "_yolo_manual_box" not in after and after["_source"] == "YOLO local"
    assert "_yolo_user_kept" not in after
    assert [c["_id"] for c in detector.left_out_components()] == [3]


# ── M2: only the dragged side of a clipped proposal lapses ────────────────

def test_a_pure_move_keeps_every_clipped_side(detector, qapp):
    """@brief Negative: a move looks at no side, so every seam-resting side stays declared."""
    start = _centre(detector, 1)
    _drag(qapp, detector.view, start, start + QPoint(12, 5))
    moved = _row(detector, 1)
    assert moved["_yolo_manual_box"] is True
    assert moved["_yolo_clipped_sides"] == ["left", "right"]
    assert moved["_yolo_clipped_recovery"] is True


def test_dragging_the_right_grip_releases_only_the_right_side(detector, qapp):
    """@brief Positive: the right grip of an unrotated proposal releases «right», «left» stays."""
    before = dict(_row(detector, 1))
    _drag_grip(detector, qapp, 1, (1, 0), QPoint(15, 0))
    resized = _row(detector, 1)
    assert resized["length_mm"] > before["length_mm"], "the grip really resized"
    assert resized["_yolo_manual_box"] is True
    assert resized["_yolo_clipped_sides"] == ["left"]
    assert resized["_yolo_clipped_recovery"] is True, "one clipped side remains"


def test_dragging_the_left_grip_releases_only_the_left_side(detector, qapp):
    """@brief Positive mirror: the left grip releases «left», «right» stays declared."""
    _drag_grip(detector, qapp, 1, (-1, 0), QPoint(-15, 0))
    resized = _row(detector, 1)
    assert resized["_yolo_manual_box"] is True
    assert resized["_yolo_clipped_sides"] == ["right"]
    assert resized["_yolo_clipped_recovery"] is True


def test_a_grip_on_a_side_that_was_not_clipped_releases_nothing(detector, qapp):
    """@brief Negative: the bottom grip touches no seam-resting side, so both stay declared."""
    before = dict(_row(detector, 1))
    _drag_grip(detector, qapp, 1, (0, 1), QPoint(0, 10))
    resized = _row(detector, 1)
    assert resized["width_mm"] != before["width_mm"], "the grip really resized"
    assert resized["_yolo_manual_box"] is True
    assert resized["_yolo_clipped_sides"] == ["left", "right"]
    assert resized["_yolo_clipped_recovery"] is True


def test_dragging_both_clipped_sides_in_turn_ends_the_clipped_recovery(detector, qapp):
    """@brief Positive: once each clipped side was dragged, none remains and the recovery lapses."""
    _drag_grip(detector, qapp, 1, (1, 0), QPoint(15, 0))
    _drag_grip(detector, qapp, 1, (-1, 0), QPoint(-15, 0))
    resized = _row(detector, 1)
    assert resized["_yolo_clipped_sides"] == []
    assert resized["_yolo_clipped_recovery"] is False


def test_a_rotated_proposal_releases_the_photo_side_under_the_grip(qapp):
    """@brief The grip is named in the body frame, the clipped side in the photo frame.

    A tall proposal lies at 90 degrees (long side along Y): its screen-right
    grip is the local (0, -1) edge. Dragging it must release the photo's
    «right» side, not «top» (what the raw local signs would name); the
    clipped «top» side was not touched and stays declared.
    """
    row = _proposal(1, (.45, .1, .5, .9), _yolo_clipped_sides=["right", "top"],
                    _yolo_clipped_recovery=True)
    assert row["rotation_deg"] == 90
    dialog = PhotoReviewDialog(_png(), [row], LENGTH, WIDTH, stage=STAGE_DETECTOR)
    dialog.show()
    qapp.processEvents()
    try:
        dialog.view.zoom(2)
        dialog._select_row(0, center=False)
        qapp.processEvents()
        item = dialog._items[0]
        side_grips = [edge for edge in item.handles if 0 in edge]
        right_grip = max(side_grips, key=lambda edge: item.handle(edge).scenePos().x())
        assert right_grip == (0, -1)
        _drag_grip(dialog, qapp, 0, right_grip, QPoint(12, 0))
        resized = _row(dialog, 0)
        assert resized["_yolo_manual_box"] is True
        assert resized["_yolo_clipped_sides"] == ["top"]
        assert resized["_yolo_clipped_recovery"] is True
    finally:
        dialog.close()
        qapp.processEvents()
