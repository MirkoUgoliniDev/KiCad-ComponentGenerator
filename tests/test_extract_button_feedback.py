"""@brief «Estrai parametri con AI» shows at once that the click was taken (user, 2026-10-02).

The checks before the progress modal load YOLO and the SMD network on the GUI
thread (~7 s measured): the button must switch to «Preparazione…» with an
hourglass and be disabled before them, ignore a second click, and come back
to rest when the run does not start.
"""
import sys
from types import ModuleType

import pytest

from kicadgen.gui import main_window as gui


def _prepare(window, monkeypatch, validate):
    monkeypatch.setattr(window, "_component_name", lambda: "TEST")
    monkeypatch.setattr(window, "_extract_block_reason", lambda: None)
    module = ModuleType("kicadgen.services.photo_yolo")
    module.validate_yolo_setup = validate
    monkeypatch.setitem(sys.modules, module.__name__, module)
    smd = ModuleType("kicadgen.services.smd_component_classifier")
    smd.validate_smd_setup = lambda *a: None
    monkeypatch.setitem(sys.modules, smd.__name__, smd)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("yolo_smd"))


def test_the_button_shows_the_preparation_before_the_slow_checks(window, monkeypatch):
    seen = {}

    def validate(path):
        button = window.btn_extract_ai
        seen.update(enabled=button.isEnabled(), text=button.text())
        # A second click while the checks run is ignored.
        window._extract_with_ai()
    _prepare(window, monkeypatch, validate)
    calls = []
    monkeypatch.setattr(window, "_selected_documents",
                        lambda: calls.append(1) or (_ for _ in ()).throw(ValueError("nessun documento")))
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: None)
    window._extract_with_ai()
    assert seen["enabled"] is False
    assert seen["text"] == window.i18n.text("Preparazione…")
    assert calls == [1], "the second click did not start a second preparation"


def test_an_early_exit_puts_the_button_back_to_rest(window, monkeypatch):
    """@brief Negative: no documents -> idle text back, re-enabled after the short delay."""
    _prepare(window, monkeypatch, lambda path: None)
    monkeypatch.setattr(window, "_selected_documents",
                        lambda: (_ for _ in ()).throw(ValueError("nessun documento")))
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: None)
    idle_text = window.btn_extract_ai.text()
    window._extract_with_ai()
    assert window.btn_extract_ai.text() == idle_text
    assert not window.btn_extract_ai.isEnabled(), "queued clicks must still find it disabled"
    window._reenable_extract_button()
    assert window.btn_extract_ai.isEnabled()
    assert not window._extract_preparing


def test_the_button_is_brought_back_into_view_when_the_page_moved(window, monkeypatch):
    """@brief The hourglass must be seen: the Project page scrolls to the button (2026-10-02).

    @note Negative: with the button already in view nothing scrolls.
    """
    window.resize(1200, 500)
    window.show()
    scroll = window._project_scroll
    bar = scroll.verticalScrollBar()
    if bar.maximum() == 0:
        pytest.skip("page fits: nothing to scroll in this window size")
    bar.setValue(0)
    window._keep_extract_button_visible()
    moved = bar.value()
    assert moved > 0
    window._keep_extract_button_visible()
    assert bar.value() == moved


def test_a_click_stopped_by_the_checks_keeps_the_previous_run_detector(window, monkeypatch):
    """@brief Review B6 (2026-10-02): the detector of the last RUN, not of the last click.

    @note A relaunch reads _extraction_detector to know whether the global
        inventory is paid: a click that never started must not change it.
    """
    _prepare(window, monkeypatch, lambda path: None)
    window._extraction_detector = "yolo_smd"
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("ai"))
    monkeypatch.setattr(window, "_selected_documents",
                        lambda: (_ for _ in ()).throw(ValueError("nessun documento")))
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: None)
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a: None)
    window._extract_with_ai()
    assert window._extraction_detector == "yolo_smd"
