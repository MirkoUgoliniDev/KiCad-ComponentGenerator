"""@brief Live review-window behaviours a mutation review found unprotected.

Each test drives `PhotoReviewDialog` the way a reviewer does -- clicks,
drags, the lasso and the keyboard shortcuts -- and checks what the window
then says: the detector-stage summary sentence, the per-proposal warnings,
the lasso «Tieni» decision, the detector/AI disagreement and the «riquadro
ritoccato a mano» provenance. Every rule pairs a positive and a negative
case that differ for a structural reason (hand-drawn or not, weak or
strong, clipped or not, mixed group or uniform group, same family or a
different one), never by a tuned number.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, Qt
from PyQt6.QtGui import QImage, QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from kicadgen.gui.photo_review_dialog import (
    PhotoReviewDialog, STAGE_DETECTOR, STAGE_INVENTORY, component_attribute_lines,
    detector_disagrees)
from kicadgen.services import photo_yolo

## Board size in mm; the 400 x 100 px photo gives 10 px/mm on X and 5 px/mm on Y.
LENGTH, WIDTH = 40.0, 20.0

## Exact catalogue sentences (kicadgen/services/i18n.py, Italian source text).
WEAK_WARNING = ("Proposta debole: sotto la soglia operativa del rilevatore, il modello la "
                "rilegge.")
CLIPPED_WARNING = "Lati sul confine di una tessera del rilevatore, non misurati: {sides}."
NO_WARNING = "Nessun avviso per questo componente."
MANUAL_MARK = "riquadro ritoccato a mano"


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


def _weak(**extra):
    """Marks of a proposal under the operating threshold that the model still reads."""
    return dict(_yolo_confidence=.3, _yolo_review_only=True,
                _yolo_origin="low_confidence_review", **extra)


def _low(**extra):
    """Marks of a sub-threshold proposal left out until the reviewer keeps it."""
    return dict(_yolo_confidence=.1, _yolo_review_only=True,
                _yolo_origin="low_confidence_review", _yolo_low_confidence=True, **extra)


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
    """Detector stage covering the four weak/clipped combinations and two low proposals.

    Top row (y 10-30 % of the photo): 1 strong and whole, 2 weak with two
    clipped sides, 3 strong with one clipped side, 4 weak and whole. Bottom
    left: 5 and 6 sub-threshold, left out. The bottom right quarter is
    empty, so a stroke there draws a new body instead of grabbing one. The
    «not kept» switch is on, as a reviewer turns it on to see 5 and 6.
    """
    rows = [
        _proposal(1, (.05, .1, .20, .3)),
        _proposal(2, (.25, .1, .40, .3), **_weak(_yolo_clipped_sides=["left", "right"],
                                                  _yolo_clipped_recovery=True)),
        _proposal(3, (.45, .1, .60, .3), _yolo_clipped_sides=["top"],
                  _yolo_clipped_recovery=True),
        _proposal(4, (.65, .1, .80, .3), **_weak()),
        _proposal(5, (.05, .6, .20, .8), _yolo_class="capacitor", **_low()),
        _proposal(6, (.25, .6, .40, .8), _yolo_class="capacitor", **_low()),
    ]
    dialog = PhotoReviewDialog(_png(), rows, LENGTH, WIDTH, stage=STAGE_DETECTOR)
    dialog.show()
    dialog.activateWindow()
    dialog.show_unkept.setChecked(True)
    qapp.processEvents()
    assert all(item.isVisible() for item in dialog._items)
    yield dialog
    dialog.close()
    qapp.processEvents()


def _centre(dialog, index):
    """Viewport pixel of the centre of outline `index`."""
    return dialog.view.mapFromScene(dialog._items[index].scenePos())


def _click(dialog, app, index):
    """Click the centre of outline `index` (press and release on the same pixel)."""
    point = _centre(dialog, index)
    _drag(app, dialog.view, point, point)
    assert dialog.component_list.currentRow() == index, "the click reached the outline"


def _press(dialog, app, key):
    """Press a window shortcut key the way the reviewer does."""
    QTest.keyClick(dialog, key)
    app.processEvents()


def _ids(rows):
    return [row["_id"] for row in rows]


# ── R2M1: the detector summary counts hand-drawn bodies and kept low ones apart ─

def test_the_summary_counts_hand_drawn_bodies_apart_from_the_kept_proposals(detector, qapp):
    """@brief Positive: a drawn body is «più 1 aggiunte», not one more kept proposal.

    Negative first: before anything is drawn or kept the sentence says
    «più 0» and «ne tieni 0» of the two low-confidence proposals. Then the
    reviewer keeps one low proposal (click + T) and draws one body (R +
    stroke on an empty area): the proposals kept become 5 (four already
    kept plus the low one), the hand-drawn body is counted on its own, and
    of the two low proposals exactly one is kept. «sicure/deboli» is the
    detector's own split (user decision 2026-10-16): T flips the live
    `_yolo_review_only` of the kept proposal, a move flips the one of a weak
    proposal, and the split still reads «2 sicure, 4 deboli».
    """
    label = detector.ai_only_label
    split = "Il rilevatore ne ha proposte 6 (2 sicure, 4 deboli). "
    assert label.text() == (
        split + "Ne tieni 4, più 0 aggiunte a "
        "mano. Di 2 a bassa confidenza ne tieni 0: le altre non vanno al modello.")
    _click(detector, qapp, 4)
    _press(detector, qapp, Qt.Key.Key_T)
    assert _ids(detector.left_out_components()) == [6]
    assert detector.get_components()[4]["_yolo_review_only"] is False, "T kept it"
    assert label.text().startswith(split)
    _press(detector, qapp, Qt.Key.Key_R)
    assert detector.draw_mode()
    transform = detector.photo_transform
    start = detector.view.mapFromScene(transform.mm_to_pixel(8.0, 3.0))
    end = detector.view.mapFromScene(transform.mm_to_pixel(14.0, 7.0))
    _drag(qapp, detector.view, start, end)
    added = detector.get_components()[-1]
    assert added["_id"] == 7 and added["_yolo_origin"] == "user_added"
    assert _ids(detector.kept_components()) == [1, 2, 3, 4, 5, 7]
    text = label.text()
    assert text.startswith(split)
    assert "Ne tieni 5, più 1 aggiunte a mano." in text
    assert text.endswith("Di 2 a bassa confidenza ne tieni 1: le altre non vanno al modello.")
    # A move of the weak proposal 4: the eye verified it, the live mark goes
    # to False, the detector's split does not move.
    if detector.draw_mode():
        _press(detector, qapp, Qt.Key.Key_R)
    before = dict(detector.get_components()[3])
    start = _centre(detector, 3)
    _drag(qapp, detector.view, start, start + QPoint(-6, 4))
    moved = detector.get_components()[3]
    assert moved["x_mm"] != before["x_mm"], "the gesture really moved the outline"
    assert moved["_yolo_review_only"] is False
    assert label.text().startswith(split)
    assert "Ne tieni 5, più 1 aggiunte a mano." in label.text()


def test_leaving_a_kept_proposal_out_again_does_not_move_the_detector_split(detector, qapp):
    """@brief Negative: T twice restores the live mark and leaves «sicure/deboli» untouched.

    The detector's verdict is stored the first time and never overwritten:
    the second T writes `_yolo_review_only` True again, while the stored
    verdict stays the detector's True and «Ne tieni» goes back to 4.
    """
    label = detector.ai_only_label
    split = "Il rilevatore ne ha proposte 6 (2 sicure, 4 deboli). "
    _click(detector, qapp, 5)
    _press(detector, qapp, Qt.Key.Key_T)
    assert "Ne tieni 5," in label.text()
    _press(detector, qapp, Qt.Key.Key_T)
    row = detector.get_components()[5]
    assert row["_yolo_review_only"] is True
    assert row[photo_yolo.DETECTOR_REVIEW_ONLY_FIELD] is True
    assert label.text().startswith(split + "Ne tieni 4, più 0 aggiunte a mano.")


def test_the_detector_verdict_is_stored_once_and_read_with_a_declared_fallback():
    """@brief Unit contract of set_review_only / detector_review_only.

    Positive: an untouched proposal has no stored field and the reader falls
    back to the live `_yolo_review_only`, which is still the detector's (weak
    True, strong False). After two writes the stored verdict is the first
    live value, not the last write.
    """
    weak, strong = _proposal(1, (.1, .1, .2, .2), **_weak()), _proposal(2, (.3, .1, .4, .2))
    for row, expected in ((weak, True), (strong, False)):
        assert photo_yolo.DETECTOR_REVIEW_ONLY_FIELD not in row
        assert photo_yolo.detector_review_only(row) is expected
    photo_yolo.set_review_only(weak, False)
    photo_yolo.set_review_only(weak, True)
    photo_yolo.set_review_only(weak, False)
    assert weak["_yolo_review_only"] is False
    assert photo_yolo.detector_review_only(weak) is True
    # A resize through mark_manual_box goes through the same writer.
    photo_yolo.mark_manual_box(strong, (1, 0))
    assert photo_yolo.DETECTOR_REVIEW_ONLY_FIELD not in strong, "a strong one is not rewritten"
    other = _proposal(3, (.5, .1, .6, .2), **_weak())
    photo_yolo.mark_manual_box(other, (1, 0))
    assert other["_yolo_review_only"] is False
    assert photo_yolo.detector_review_only(other) is True


def test_the_detector_split_survives_the_saved_review_bundle(detector, qapp, tmp_path):
    """@brief The stored verdict survives save_review_bundle / load_review_bundle (the reopen path).

    Positive: the reviewer keeps the low proposal 5 and moves the weak 4,
    the kept rows are saved as `_reopen_detector_review` saves them, and the
    window reopened on the loaded rows still says «2 sicure, 3 deboli» of
    its 5 proposals (6 was left out, so it is not in the bundle). Negative:
    the same rows with the stored field stripped -- as a version older than
    the field saved them -- read the reviewer's live marks instead, which is
    the declared limit of the fallback.
    """
    from kicadgen.services import photo_inventory as inv
    _click(detector, qapp, 4)
    _press(detector, qapp, Qt.Key.Key_T)
    start = _centre(detector, 3)
    _drag(qapp, detector.view, start, start + QPoint(-6, 4))
    kept = detector.kept_components()
    assert [row["_yolo_review_only"] for row in kept] == [False, True, False, False, False]
    inv.save_review_bundle(tmp_path, kept, _png(), LENGTH, WIDTH)
    rows = inv.load_review_bundle(tmp_path).proposals
    assert {row["_id"]: row.get(photo_yolo.DETECTOR_REVIEW_ONLY_FIELD) for row in rows} == {
        1: None, 2: None, 3: None, 4: True, 5: True}
    expected = {True: "Il rilevatore ne ha proposte 5 (2 sicure, 3 deboli). ",
                False: "Il rilevatore ne ha proposte 5 (4 sicure, 1 deboli). "}
    for keep_field, prefix in expected.items():
        loaded = [dict(row) for row in rows]
        if not keep_field:
            for row in loaded:
                row.pop(photo_yolo.DETECTOR_REVIEW_ONLY_FIELD, None)
        reopened = PhotoReviewDialog(_png(), loaded, LENGTH, WIDTH, stage=STAGE_DETECTOR)
        try:
            assert reopened.ai_only_label.text().startswith(prefix)
        finally:
            reopened.close()
            qapp.processEvents()


# ── R2M2: weak and seam-clipped warnings belong to the right proposal ───────

@pytest.mark.parametrize("index, weak, sides", [
    (0, False, None),
    (1, True, "left, right"),
    (2, False, "top"),
    (3, True, None),
])
def test_the_weak_and_clipped_warnings_follow_the_proposals_own_marks(detector, qapp,
                                                                       index, weak, sides):
    """@brief Each warning appears exactly when its mark is on the clicked proposal.

    Weak (`_yolo_review_only` above the low-confidence band) and clipped
    (`_yolo_clipped_sides`) are independent marks, so the four combinations
    are checked: the strong whole proposal says nothing, the weak one says
    «Proposta debole», the clipped one names its seam sides, and neither
    sentence leaks onto a proposal that does not carry its mark.
    """
    _click(detector, qapp, index)
    text = detector.warning_label.text()
    assert (WEAK_WARNING in text) is weak
    clipped = CLIPPED_WARNING.split("{")[0]
    if sides is None:
        assert clipped not in text
    else:
        assert CLIPPED_WARNING.format(sides=sides) in text
    if not weak and sides is None:
        assert text == NO_WARNING


# ── R2M3: «Tieni» on a lasso group keeps the whole group if any is left out ──

def _lasso(dialog, app, indices):
    """Arm the lasso with L and trace, with a real stroke, a rectangle around `indices`."""
    _press(dialog, app, Qt.Key.Key_L)
    assert dialog.lasso_button.isChecked()
    items = [dialog._items[index] for index in indices]
    rect = items[0].photo_bounds()
    for item in items[1:]:
        rect = rect.united(item.photo_bounds())
    rect = rect.adjusted(-3, -3, 3, 3)
    _drag(app, dialog.view, dialog.view.mapFromScene(rect.topLeft()),
          dialog.view.mapFromScene(rect.bottomRight()))
    assert dialog.lasso_items() == items


def test_t_on_a_mixed_lasso_group_keeps_every_member(detector, qapp):
    """@brief Positive: one member kept, one left out -- T keeps both, not leaves both.

    The reviewer keeps 5 alone (click + T), then lassoes 5 and 6: the group
    is mixed, and a single decision for the gesture is «keep them all»
    because at least one is still left out.
    """
    _click(detector, qapp, 4)
    _press(detector, qapp, Qt.Key.Key_T)
    assert _ids(detector.left_out_components()) == [6]
    _lasso(detector, qapp, [4, 5])
    _press(detector, qapp, Qt.Key.Key_T)
    assert detector.left_out_components() == []
    assert all(detector.get_components()[i]["_yolo_user_kept"] is True for i in (4, 5))
    assert detector.keep_button.isChecked()


def test_t_on_a_lasso_group_with_none_left_out_leaves_every_member(detector, qapp):
    """@brief Negative: no member left out -- the same T now leaves the whole group out."""
    for index in (4, 5):
        _click(detector, qapp, index)
        _press(detector, qapp, Qt.Key.Key_T)
    assert detector.left_out_components() == []
    _lasso(detector, qapp, [4, 5])
    _press(detector, qapp, Qt.Key.Key_T)
    assert _ids(detector.left_out_components()) == [5, 6]
    assert not detector.keep_button.isChecked()


# ── R2M4: the detector disagrees on the family, not on the spelling ─────────

def _reconciled(kind, detector_class="resistor"):
    """An inventory row carrying the single source proposal's evidence, as `_normalize` writes it."""
    return dict(_id=1, name="R1", kind=kind, x_mm=0.0, y_mm=0.0, length_mm=2.0, width_mm=1.0,
                rotation_deg=0, height_mm=0.5,
                _detector_evidence={"class": detector_class, "confidence": .8,
                                    "class_ambiguous": False, "origin": "operating_threshold",
                                    "low_confidence": False})


@pytest.mark.parametrize("kind", ["Resistor", "RESISTOR", " resistor "])
def test_a_kind_differing_from_the_detector_only_by_case_is_not_a_disagreement(kind):
    """@brief Negative: «Resistor» and the detector's «resistor» are the same family."""
    row = _reconciled(kind)
    assert detector_disagrees(row) is False
    lines = dict(component_attribute_lines(row, lambda text: text, STAGE_INVENTORY))
    assert "diverso dal tipo AI" not in lines["Classe"]


@pytest.mark.parametrize("kind", ["capacitor", "Capacitor"])
def test_a_kind_naming_another_family_is_a_disagreement(kind):
    """@brief Positive: a capacitor where the detector saw a resistor is flagged, whatever the case."""
    row = _reconciled(kind)
    assert detector_disagrees(row) is True
    lines = dict(component_attribute_lines(row, lambda text: text, STAGE_INVENTORY))
    assert lines["Classe"] == "resistor · diverso dal tipo AI"


# ── R2M5: the provenance says «ritoccato a mano» once the reviewer moved it ─

def test_the_attribute_panel_says_the_box_was_retouched_only_after_a_drag(detector, qapp):
    """@brief Negative before, positive after: a real drag adds the «ritoccato» provenance."""
    _click(detector, qapp, 0)
    before = detector.attributes_label.text()
    assert "sopra la soglia operativa del rilevatore" in before
    assert MANUAL_MARK not in before
    start = _centre(detector, 0)
    _drag(qapp, detector.view, start, start + QPoint(12, 4))
    assert detector.get_components()[0]["_yolo_manual_box"] is True
    after = detector.attributes_label.text()
    assert "sopra la soglia operativa del rilevatore · " + MANUAL_MARK in after
    # Negative on a body the reviewer drew: «tracciato a mano» already says
    # it, the retouch mark would repeat it.
    lines = dict(component_attribute_lines(
        photo_yolo.manual_proposal((.6, .6, .7, .7), 9, LENGTH, WIDTH),
        lambda text: text, STAGE_DETECTOR))
    assert lines["Provenienza"] == "tracciato a mano in revisione"
