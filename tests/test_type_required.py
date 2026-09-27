"""@brief Without a component type the analysis does not start (user, 2026-09-26: «nada»)."""
import types

import pytest
from PyQt6.QtWidgets import QMessageBox

from tests.test_photo_inventory_gui import gui


@pytest.fixture
def real_gate(window, monkeypatch):  # noqa: F811
    """The shared window with the real rule back (the fixture relaxes it)."""
    monkeypatch.setattr(window, "_extract_block_reason",
                        types.MethodType(gui.MainWindow._extract_block_reason, window))
    return window


def _choose(window, key):
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData(key))
    assert (combo.currentData() or "") == key


def test_no_type_no_analysis(real_gate, monkeypatch):
    window = real_gate
    started, shown = [], []
    monkeypatch.setattr(window, "_begin_ai_run", lambda title: started.append(title))
    monkeypatch.setattr(QMessageBox, "information", lambda *args: shown.append(args[2]))
    _choose(window, "")
    window._apply_component_name_gate()
    assert not window.btn_extract_ai.isEnabled()
    assert "tipologia" in window.btn_extract_ai.toolTip().lower()
    window._extract_with_ai()
    assert started == [] and shown and "tipologia" in shown[0].lower()


def test_choosing_a_type_unlocks_the_analysis(real_gate):
    """@brief Negativo: scelta la tipologia il pulsante si riaccende, tolta si rispegne."""
    window = real_gate
    _choose(window, "")
    window._apply_component_name_gate()
    assert not window.btn_extract_ai.isEnabled()
    key = window.combo_component_type.itemData(1)
    assert key
    _choose(window, key)
    assert window.btn_extract_ai.isEnabled()
    _choose(window, "")
    assert not window.btn_extract_ai.isEnabled()


def test_the_end_of_a_run_does_not_unlock_an_untyped_project(real_gate):
    window = real_gate
    _choose(window, "")
    window.btn_extract_ai.setEnabled(False)
    window._refresh_extract_enabled()
    assert not window.btn_extract_ai.isEnabled()
