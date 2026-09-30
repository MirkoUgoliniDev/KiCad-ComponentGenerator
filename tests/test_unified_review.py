"""@brief One review window for the detector's proposals and the AI inventory (HANDOFF 162).

The user's decision (2026-09-30): «unificare la vista YOLO con quella dell'AI;
i parametri che trova YOLO non vanno sovrascritti, vanno mantenuti per il
confronto; il passaggio a metà analisi resta; una sola icona». Each rule is
checked with a positive and a negative case that differ for a structural
reason, never by a tuned number.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QDialog, QDialogButtonBox

from kicadgen.gui.photo_review_dialog import (
    DETECTOR_DISAGREES_MARKER, PhotoReviewDialog, STAGE_DETECTOR, STAGE_INVENTORY,
    component_attribute_lines, detector_disagrees, left_out)
from kicadgen.services import photo_yolo
from kicadgen.services.photo_yolo import MANUAL_SOURCE

LENGTH, WIDTH = 40.0, 20.0


def _png():
    image = QImage(400, 100, QImage.Format.Format_RGB32)
    image.fill(0)
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _proposal(identity, box=(.2, .2, .4, .4), **extra):
    """A detector proposal as `detect_yolo_components` writes it (geometry in mm and `_box`)."""
    row = dict(_id=identity, _box=tuple(box), name=f"YOLO candidate {identity}", kind="generic",
               **photo_yolo.box_geometry_mm(box, LENGTH, WIDTH), height_mm=1.0,
               color_hex="#303030", confidence="low", _uncertain=True, _source="YOLO local",
               _yolo_class="resistor", _yolo_confidence=.6, _yolo_review_only=False,
               _yolo_origin="operating_threshold", _yolo_low_confidence=False,
               _yolo_class_ambiguous=False, _yolo_proposal_id=f"p{identity}")
    row.update(extra)
    return row


def _identity(text):
    return text


@pytest.fixture
def detector(qapp):
    """Detector stage: a strong, a weak and a sub-threshold proposal."""
    rows = [
        _proposal(1),
        _proposal(2, box=(.5, .2, .6, .4), _yolo_class="led", _yolo_confidence=.3,
                  _yolo_review_only=True, _yolo_origin="low_confidence_review",
                  _yolo_clipped_sides=["left", "right"], _yolo_clipped_recovery=True),
        _proposal(3, box=(.7, .5, .8, .7), _yolo_class="capacitor", _yolo_confidence=.1,
                  _yolo_review_only=True, _yolo_origin="low_confidence_review",
                  _yolo_low_confidence=True),
    ]
    dialog = PhotoReviewDialog(_png(), rows, LENGTH, WIDTH, stage=STAGE_DETECTOR)
    yield dialog
    dialog.close()
    qapp.processEvents()


# ── Services: the manual marking lives in photo_yolo ──────────────────────

def test_mark_manual_box_drops_only_the_side_the_handle_dragged():
    """@brief Positive: the dragged side lapses; negative: a move keeps every clipped side."""
    dragged = dict(_yolo_clipped_sides=["left", "right"], _yolo_clipped_recovery=True,
                   _source="YOLO local", _edges_measured=True, _yolo_review_only=True,
                   _yolo_origin="low_confidence_review")
    photo_yolo.mark_manual_box(dragged, (1, 0))
    assert dragged["_yolo_clipped_sides"] == ["left"] and dragged["_yolo_clipped_recovery"] is True
    assert dragged["_yolo_manual_box"] is True and dragged["_source"] == MANUAL_SOURCE
    assert dragged["_yolo_source_before_manual"] == "YOLO local"
    assert "_edges_measured" not in dragged
    assert dragged["_yolo_review_only"] is False
    assert dragged["_yolo_origin_before_manual"] == "low_confidence_review"
    moved = dict(_yolo_clipped_sides=["left", "right"], _yolo_clipped_recovery=True)
    photo_yolo.mark_manual_box(moved)
    assert moved["_yolo_clipped_sides"] == ["left", "right"]
    assert moved["_yolo_clipped_recovery"] is True
    low = dict(_yolo_low_confidence=True)
    photo_yolo.mark_manual_box(low, (0, -1))
    assert low["_yolo_user_kept"] is True and not photo_yolo.awaiting_user_keep(low)


def test_manual_proposal_is_a_detector_row_without_a_class():
    row = photo_yolo.manual_proposal((.1, .1, .3, .2), 7, LENGTH, WIDTH)
    assert row["_id"] == 7 and row["_yolo_origin"] == "user_added"
    assert row["_yolo_manual_box"] is True and row["_source"] == MANUAL_SOURCE
    assert row["_yolo_class"] == "" and row["kind"] == "generic"
    assert (row["x_mm"], row["y_mm"]) == pytest.approx((-12.0, -7.0))
    assert (row["length_mm"], row["width_mm"], row["rotation_deg"]) == pytest.approx((8.0, 2.0, 0))


def test_the_detector_evidence_carries_the_proposal_geometry_when_it_has_one():
    """@brief Positive: box and size travel with the class; negative: nothing invented."""
    from kicadgen.services import photo_global_inventory as inventory
    full = _proposal(27, box=(.79, .49, .85, .70), _yolo_class="transistor", _yolo_confidence=.9)
    bare = {"_id": 28, "_yolo_class": "diode", "_yolo_confidence": .8, "_yolo_class_ambiguous": False,
            "_yolo_origin": "operating_threshold", "_yolo_low_confidence": False,
            "_yolo_proposal_id": "p28"}
    proposals = {27: full, 28: bare}

    def row(source):
        return dict(component_id="C", grouping_basis="single_body", source_candidate_ids=[source],
                    **{"class": "semiconductor_unknown"}, bbox=[.79, .49, .85, .70],
                    bbox_basis="leads", rotation_deg=90, package="SOT-223", marking="",
                    terminal_count=4, terminal_layout="3 leads, tab", lead_count=3,
                    lead_count_complete=True, lead_count_confidence="medium", height_mm=None,
                    height_source="unknown", confidence="medium", evidence="black body",
                    presence_confidence="high", class_confidence="low", asymmetry_landmark=None)

    with_geometry = inventory._normalize(row(27), 1, LENGTH, WIDTH, [], proposals=proposals)
    evidence = with_geometry["_detector_evidence"]
    assert evidence["class"] == "transistor" and evidence["confidence"] == .9
    assert evidence["box"] == [.79, .49, .85, .70]
    assert evidence["length_mm"] == pytest.approx(full["length_mm"])
    assert evidence["width_mm"] == pytest.approx(full["width_mm"])
    assert "manual_box" not in evidence
    without = inventory._normalize(row(28), 1, LENGTH, WIDTH, [], proposals=proposals)
    assert without["_detector_evidence"]["class"] == "diode"
    assert not any(key in without["_detector_evidence"] for key in ("box", "length_mm", "width_mm"))


# ── Detector stage ────────────────────────────────────────────────────────

def test_the_detector_stage_names_itself_and_hands_over_only_the_kept_proposals(detector):
    """@brief Positive: OK sends the strong and the weak one; negative: the unkept sub-threshold one stays out."""
    assert detector.stage() == STAGE_DETECTOR
    assert detector.windowTitle() == "Revisione componenti — proposte del rilevatore"
    assert detector.count_label.text() == "Proposte del rilevatore: 3"
    assert detector.buttons.button(QDialogButtonBox.StandardButton.Apply).text() == "OK"
    assert detector.model_button.isHidden()
    assert [c["_id"] for c in detector.kept_components()] == [1, 2]
    assert [c["_id"] for c in detector.left_out_components()] == [3]
    assert "Il rilevatore ne ha proposte 3 (1 sicure, 2 deboli). Ne tieni 2" in detector.ai_only_label.text()
    # Keeping the sub-threshold one is the reviewer's decision (T), reversible.
    detector._select_row(2)
    assert detector.keep_button.isEnabled() and not detector.keep_button.isChecked()
    assert detector.toggle_keep() is True
    assert [c["_id"] for c in detector.kept_components()] == [1, 2, 3]
    assert detector.get_components()[2]["_yolo_user_kept"] is True
    assert detector.toggle_keep() is False
    assert [c["_id"] for c in detector.kept_components()] == [1, 2]
    # Negative: T on a strong proposal does nothing, it is not pending.
    detector._select_row(0)
    assert detector.toggle_keep() is None and not detector.keep_button.isEnabled()


def test_the_detector_rows_read_as_class_confidence_state_and_size(detector):
    rows = [detector.component_list.item(i).text() for i in range(3)]
    assert rows[0] == "1. resistor · 0.60 · 8.0×4.0 mm"
    assert rows[1].startswith("2. led · 0.30 · debole · 4.0×4.0 mm [!]")
    assert rows[2].startswith("3. capacitor · 0.10 · bassa confidenza · non tenuta · 4.0×4.0 mm")
    # The strong proposal carries no warning: no KiCad model, height or
    # generic «uncertain» warning belongs to this stage.
    assert "[!]" not in rows[0]
    detector._select_row(0)
    assert detector.warning_label.text() == "Nessun avviso per questo componente."
    # The filter vocabulary is the detector's classes, not the «generic» kind.
    data = [detector.category_filter.itemData(i) for i in range(detector.category_filter.count())]
    assert data == [None, "capacitor", "led", "resistor"]
    lines = dict(component_attribute_lines(detector.get_components()[1], _identity, STAGE_DETECTOR))
    assert lines == {"Classe": "led", "Confidenza": "0.30", "Riquadro": "4.0×4.0 mm",
                     "Provenienza": "sotto la soglia: da tenere a mano"}


def test_editing_a_detector_proposal_marks_it_as_the_reviewers_box(detector):
    """@brief Positive: a moved weak proposal becomes the reviewer's; negative: the untouched ones stay the detector's."""
    detector._select_row(1)
    detector.fields["x_mm"].setValue(3.0)
    moved = detector.get_components()[1]
    assert moved["_yolo_manual_box"] is True and moved["_source"] == MANUAL_SOURCE
    assert moved["_yolo_review_only"] is False, "the eye verified it"
    assert moved["_yolo_clipped_sides"] == ["left", "right"], "a move looks at no side"
    assert moved["_box"] == pytest.approx(detector.photo_transform.normalized_box(moved))
    assert "ritoccato" in detector.component_list.item(1).text()
    untouched = detector.get_components()[0]
    assert "_yolo_manual_box" not in untouched and untouched["_source"] == "YOLO local"


def test_a_resize_handle_names_the_photo_side_it_dragged():
    from kicadgen.gui.photo_review_dialog import _photo_handle
    assert _photo_handle(0, (1, 0)) == (1, 0)
    assert _photo_handle(0, (0, -1)) == (0, -1)
    # At 90 degrees the local u axis points down the photo, v points left.
    assert _photo_handle(90, (1, 0)) == (0, 1)
    assert _photo_handle(90, (0, 1)) == (-1, 0)
    assert _photo_handle(180, (1, 0)) == (-1, 0)
    assert _photo_handle(270, (1, 0)) == (0, -1)
    # Negative: an arbitrary angle names no photo side.
    assert _photo_handle(45, (1, 0)) == (0, 0)


def test_a_body_drawn_in_the_detector_stage_is_a_manual_proposal_with_no_question(detector):
    detector.add_component(geometry=(-10.0, 5.0, 4.0, 2.0))
    added = detector.get_components()[-1]
    assert added["_id"] == 4 and added["_yolo_origin"] == "user_added"
    assert added["_yolo_manual_box"] is True and added["_yolo_class"] == ""
    assert added["_box"] == pytest.approx(detector.photo_transform.normalized_box(added))
    assert not detector.parameters_dialog.isVisible(), "the model decides the class"
    assert detector.component_list.item(3).text().startswith("4. generico · aggiunto a mano")
    assert [c["_id"] for c in detector.kept_components()] == [1, 2, 4]
    # Negative: in the inventory stage a drawn body is the user's placeholder.
    inventory = PhotoReviewDialog(_png(), [], LENGTH, WIDTH)
    inventory.add_component(geometry=(0.0, 0.0, 4.0, 2.0))
    assert "_yolo_origin" not in inventory.get_components()[0]
    inventory.close()


def test_a_saved_bundle_row_without_millimetres_is_placed_from_its_box(qapp):
    """@brief Positive: `_box` alone gives the geometry; negative: the inventory stage keeps raising."""
    row = {"_id": 1, "_box": [0.1, 0.1, 0.4, 0.4], "name": "YOLO candidate 1", "kind": "generic",
           "_yolo_class": "ic", "_yolo_confidence": .8}
    dialog = PhotoReviewDialog(_png(), [row], LENGTH, WIDTH, stage=STAGE_DETECTOR)
    placed = dialog.get_components()[0]
    assert (placed["x_mm"], placed["y_mm"]) == pytest.approx((-10.0, -5.0))
    assert (placed["length_mm"], placed["width_mm"]) == pytest.approx((12.0, 6.0))
    dialog.close()
    with pytest.raises(ValueError):
        PhotoReviewDialog(_png(), [dict(row)], LENGTH, WIDTH)
    with pytest.raises(ValueError):
        PhotoReviewDialog(_png(), [], LENGTH, WIDTH, stage="somewhere")


def test_the_detector_stage_never_looks_for_kicad_models(detector):
    detector._select_row(0)
    detector.choose_model()
    assert detector._match_worker is None


# ── Inventory stage: the detector's reading is kept beside the AI's ───────

def _reconciled(identity, kind, evidence, box=(.2, .2, .4, .4)):
    row = dict(name=f"C{identity} ({kind})", kind=kind, **{"class": kind},
               **photo_yolo.box_geometry_mm(box, LENGTH, WIDTH), height_mm=1.0,
               _box=list(box), class_confidence="high", confidence="high")
    if evidence is not None:
        row["_detector_evidence"] = evidence
    return row


@pytest.fixture
def inventory(qapp):
    """Inventory stage: a disagreement, an agreement, a merged row, a same-box row."""
    rows = [
        _reconciled(1, "semiconductor_unknown",
                    dict(**{"class": "transistor"}, confidence=.9006, class_ambiguous=False,
                         origin="operating_threshold", low_confidence=False, proposal_id="p1",
                         box=[.2, .2, .45, .4], length_mm=10.0, width_mm=4.0)),
        _reconciled(2, "ic", dict(**{"class": "transistor"}, confidence=.88, class_ambiguous=False,
                                  origin="operating_threshold", low_confidence=False,
                                  proposal_id="p2", box=[.5, .2, .6, .4], length_mm=4.0,
                                  width_mm=4.0), box=(.5, .2, .6, .4)),
        _reconciled(3, "capacitor", None, box=(.7, .5, .8, .7)),
    ]
    dialog = PhotoReviewDialog(_png(), rows, LENGTH, WIDTH)
    yield dialog
    dialog.close()
    qapp.processEvents()


def test_the_inventory_stage_shows_the_detector_reading_and_flags_a_different_class(inventory):
    """@brief Positive: transistor vs semiconductor_unknown is flagged; negative: transistor vs ic is not."""
    assert inventory.stage() == STAGE_INVENTORY
    assert inventory.windowTitle() == "Revisione componenti — inventario AI"
    rows = inventory.get_components()
    disagreeing = dict(component_attribute_lines(rows[0], _identity))
    assert disagreeing["Classe"] == "transistor · diverso dal tipo AI"
    assert disagreeing["Confidenza"] == "0.90" and disagreeing["Riquadro"] == "10.0×4.0 mm"
    assert disagreeing["Provenienza"] == "sopra la soglia operativa del rilevatore"
    # The detector's section comes first, the AI's after it (HANDOFF 163).
    assert list(disagreeing)[:5] == ["Classe", "Confidenza", "Riquadro", "Provenienza", "Tipo"]
    assert detector_disagrees(rows[0]) is True
    assert DETECTOR_DISAGREES_MARKER in inventory.component_list.item(0).text()
    agreeing = dict(component_attribute_lines(rows[1], _identity))
    assert agreeing["Classe"] == "transistor" and agreeing["Confidenza"] == "0.88"
    assert detector_disagrees(rows[1]) is False
    assert DETECTOR_DISAGREES_MARKER not in inventory.component_list.item(1).text()
    # A merged row carries no single reading: no line is invented for it.
    merged = component_attribute_lines(rows[2], _identity)
    assert "Classe" not in dict(merged) and len(merged) == 5
    # The modal shows the two headed sections, the detector's first.
    inventory._select_row(2)
    text = inventory.attributes_plain_text()
    assert text.splitlines()[0] == "Rilevatore locale (YOLO)"
    assert "Lettura: nessuna proposta singola del rilevatore" in text
    assert text.index("Rilevatore locale") < text.index("Lettura AI") < text.index("Modello KiCad")


def test_the_corrections_exist_only_after_the_ai_inventory(detector, inventory):
    """@brief Positive: inventory stage shows the corrections and the KiCad command in the modal;
    negative: the detector stage hides them, and opening the modal focuses nothing."""
    inventory._select_row(0)
    assert not inventory.form_widget.isHidden() and not inventory.corrections_label.isHidden()
    assert not inventory.modal_model_button.isHidden()
    assert inventory.modal_model_button.isEnabled() == inventory.model_button.isEnabled()
    assert "Correzioni" in inventory.corrections_label.text()
    detector._select_row(0)
    assert detector.form_widget.isHidden() and detector.corrections_label.isHidden()
    assert detector.modal_model_button.isHidden()
    text = detector.attributes_plain_text()
    assert text.count("disponibile dopo l'inventario AI") == 2
    assert text.index("Lettura AI") < text.index("Modello KiCad")
    detector.open_parameters()
    assert detector.parameters_dialog.focusWidget() not in detector.text_fields.values()
    detector.parameters_dialog.close()
    assert DETECTOR_DISAGREES_MARKER not in inventory.component_list.item(2).text()


def test_the_detector_ghost_appears_only_where_the_boxes_differ(inventory):
    """@brief Positive: a different detector box is drawn under the selected body; negatives: same box, no reading, nothing selected."""
    inventory._select_row(0)
    assert inventory.detector_ghost.isVisible()
    ghost = inventory.detector_ghost.rect()
    assert (ghost.left(), ghost.top(), ghost.right(), ghost.bottom()) == pytest.approx((80, 20, 180, 40))
    inventory._select_row(1)
    assert not inventory.detector_ghost.isVisible(), "same box: a ghost would only thicken the outline"
    inventory._select_row(2)
    assert not inventory.detector_ghost.isVisible(), "no reading, nothing to compare"
    inventory._select_row(-1)
    assert not inventory.detector_ghost.isVisible()
    # Moving the agreeing body makes the boxes differ: the ghost shows the
    # detector's box where it was, and the reading itself is untouched.
    inventory._select_row(1)
    inventory.fields["x_mm"].setValue(8.0)
    assert inventory.detector_ghost.isVisible()
    moved = inventory.get_components()[1]
    assert moved["_detector_evidence"]["box"] == [.5, .2, .6, .4]
    assert moved["_detector_evidence"]["length_mm"] == 4.0
    assert "Grigio punteggiato" in inventory.legend.text()


def test_left_out_speaks_one_language_for_both_stages():
    assert left_out({"_ai_only_excluded": True}) is True
    assert left_out({"_ai_only_excluded": False}) is False
    assert left_out({"_yolo_low_confidence": True}) is True
    assert left_out({"_yolo_low_confidence": True, "_yolo_user_kept": True}) is False
    assert left_out({"_yolo_review_only": True}) is False, "weak is not excluded"


# ── The main window: one command, the stage that applies ──────────────────

def test_the_single_review_command_opens_the_stage_that_applies(window, monkeypatch):  # noqa: F811
    """@brief Positive: photo -> inventory, bundle only -> detector; negative: nothing -> no window."""
    pytest.importorskip("pyvistaqt")
    from PyQt6.QtCore import pyqtSignal
    from kicadgen.gui import photo_review_dialog as review_module
    opened = []

    class _Recorder(QDialog):
        components_applied = pyqtSignal(list)

        def __init__(self, png, components, length, width, parent=None, **kwargs):
            super().__init__(parent)
            self._items = []
            opened.append(kwargs.get("stage", STAGE_INVENTORY))

        def exec(self):
            return QDialog.DialogCode.Rejected

        def kept_components(self):
            return []

    # The detector stage reads the class from the module at each call, the
    # inventory stage through the name main_window imported: both are replaced.
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(review_module, "PhotoReviewDialog", _Recorder)
    monkeypatch.setattr(gui, "PhotoReviewDialog", _Recorder)
    window._photo_png = None
    window._photo_overlay_png = None
    window._set_review_buttons_enabled(False)
    window._open_component_review()
    assert opened == [] and window._component_review_stage() is None
    window.field_name.setText("Unificata")
    window.field_body_length.setValue(LENGTH)
    window.field_body_width.setValue(WIDTH)
    written = window._write_review_bundle([_proposal(1)], _png(), LENGTH, WIDTH)
    assert written is not None
    window._refresh_reopen_review_button()
    assert window._component_review_stage() == STAGE_DETECTOR
    window._open_component_review()
    assert opened == [STAGE_DETECTOR]
    window._photo_png = _png()
    window._refresh_component_review_button()
    assert window._component_review_stage() == STAGE_INVENTORY
    window._open_component_review()
    assert opened == [STAGE_DETECTOR, STAGE_INVENTORY]
    # Negative: the old pair of commands no longer exists.
    assert not hasattr(window, "btn_reopen_review") and not hasattr(window, "btn_edit_component_review")
    assert window.btn_photo_comparison is window.btn_component_review
