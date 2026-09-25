"""@brief Modale «Parametri YOLO» e loro percorso fino ai worker (utente, 2026-09-25)."""
from pathlib import Path

import pytest

from kicadgen.gui import main_window as gui
from kicadgen.gui.yolo_settings_dialog import YoloSettingsDialog
from kicadgen.services.photo_yolo import YOLO_SETTING_DEFAULTS

from test_photo_inventory_gui import window  # noqa: F401 - fixture


def test_the_dialog_opens_with_the_given_values_and_restores_the_defaults(window):  # noqa: F811
    # `window` porta con se' la QApplication: senza, un QDialog abbatte il processo.
    dialog = YoloSettingsDialog({"confidence": 0.46, "nms_iou": 0.7, "tile_sizes": [],
                                 "tile_overlap": 0.3}, window)
    assert dialog.values() == {"confidence": 0.46, "nms_iou": 0.7, "tile_sizes": [],
                               "tile_overlap": 0.3}
    assert not dialog.spin_overlap.isEnabled()  # senza tessere non significa nulla
    dialog.btn_defaults.click()
    assert dialog.values() == YOLO_SETTING_DEFAULTS
    assert dialog.spin_overlap.isEnabled()


def test_the_button_sits_next_to_the_threshold_and_follows_the_detector(window):  # noqa: F811
    combo = window.combo_photo_detector
    for detector, enabled in (("ai", False), ("vlm", False), ("yolo", True), ("yolo_only", True)):
        combo.setCurrentIndex(combo.findData(detector))
        assert window.btn_yolo_settings.isEnabled() is enabled
    assert window.btn_yolo_settings.toolTip().startswith("Parametri YOLO")


def test_confirming_the_dialog_updates_settings_and_summary(window, monkeypatch):  # noqa: F811
    def accept(dialog):
        dialog.spin_confidence.setValue(0.46)
        dialog.spin_nms.setValue(0.7)
        dialog.check_640.setChecked(False)
        return gui.QDialog.DialogCode.Accepted
    monkeypatch.setattr("kicadgen.gui.yolo_settings_dialog.YoloSettingsDialog.exec", accept)
    window._open_yolo_settings()
    assert window._chosen_yolo_settings() == {"confidence": 0.46, "nms_iou": 0.7,
                                             "tile_sizes": [1280], "tile_overlap": 0.25}
    summary = window.label_yolo_summary.text()
    assert "0.46" in summary and "0.70" in summary and "1280 px" in summary
    # Salvati con il progetto e rimessi da uno stato salvato.
    state = window._project_state()
    assert state["yolo_settings"]["confidence"] == 0.46
    window._yolo_settings = None
    window._apply_project_state(state)
    assert window._chosen_yolo_settings()["tile_sizes"] == [1280]
    assert "0.46" in window.label_yolo_summary.text()


def test_cancelling_the_dialog_changes_nothing(window, monkeypatch):  # noqa: F811
    before = window._chosen_yolo_settings()
    monkeypatch.setattr("kicadgen.gui.yolo_settings_dialog.YoloSettingsDialog.exec",
                        lambda dialog: gui.QDialog.DialogCode.Rejected)
    window._open_yolo_settings()
    assert window._chosen_yolo_settings() == before


def test_a_project_without_settings_gets_the_defaults(window):  # noqa: F811
    window._yolo_settings = {"confidence": 0.5, "nms_iou": 0.7, "tile_sizes": [], "tile_overlap": 0.3}
    state = window._project_state()
    del state["yolo_settings"]
    window._apply_project_state(state)
    assert window._chosen_yolo_settings() == YOLO_SETTING_DEFAULTS


def test_a_project_saved_with_the_old_threshold_field_keeps_its_threshold(window):  # noqa: F811
    """@brief Progetti salvati il 25/09 con il campo «Soglia YOLO»: la soglia non si perde."""
    state = window._project_state()
    del state["yolo_settings"]
    state["widgets"]["field_yolo_confidence"] = 0.46
    window._apply_project_state(state)
    assert window._chosen_yolo_settings()["confidence"] == pytest.approx(0.46)


def test_the_dialog_buttons_follow_the_interface_language(window):  # noqa: F811
    """@brief Utente, 25/09: «Cancel» in inglese in un'interfaccia italiana."""
    from PyQt6.QtWidgets import QDialogButtonBox
    dialog = YoloSettingsDialog(None, window)
    box = dialog.findChild(QDialogButtonBox)
    assert box.button(QDialogButtonBox.StandardButton.Cancel).text() == "Annulla"


@pytest.mark.parametrize("detector", ["yolo", "yolo_only"])
def test_both_yolo_modes_receive_the_same_settings(window, monkeypatch, detector):  # noqa: F811
    """@brief Utente: «la scelta dei parametri rimane quando scelgo YOLO + AI»."""
    import sys
    from types import ModuleType
    module = ModuleType("kicadgen.services.photo_yolo")
    module.validate_yolo_setup = lambda path: None
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("hires.jpg")], {"hires.jpg": "hires"}))
    monkeypatch.setattr(gui.QThread, "start", lambda self: None)
    monkeypatch.setattr(window, "_chosen_yolo_settings",
                        lambda: {"confidence": 0.4, "nms_iou": 0.7, "tile_sizes": [],
                                 "tile_overlap": 0.25})
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData(detector))
    window._extract_with_ai()
    assert window._extract_worker.yolo_settings == {"confidence": 0.4, "nms_iou": 0.7,
                                                    "tile_sizes": [], "tile_overlap": 0.25}
    window._on_extract_finished() if detector == "yolo" else window._hide_analysis_overlay()
    window.btn_extract_ai.setEnabled(True)
