"""@brief «Solo nome principale» wired into the main window (2026-11-13).

@details The preference is the config key "pin_primary_names" (default ON),
applied to the symbol tab at start-up and at every project load, changed from
the Pinout dialog (a toggle alone is a real change), and followed by the
footprint preview labels, whose tooltip keeps the full pinout text. The symbol
tab's per-pin notes reach the log. Positive and negative cases each.
"""

from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QDialog

from kicadgen.generators.symbol import primary_pin_names_enabled, set_primary_pin_names
from kicadgen.gui import main_window as gui


def _restore_default():
    """The switch is process-wide: put the default back for the following tests."""
    set_primary_pin_names(True)


def test_the_preference_reads_only_an_explicit_false_as_off(window):
    """@brief Positive: missing or True is ON; negative: exactly False is OFF, other values ON."""
    window._config.pop("pin_primary_names", None)
    assert window._primary_pin_names_preference() is True
    window._config["pin_primary_names"] = False
    assert window._primary_pin_names_preference() is False
    window._config["pin_primary_names"] = "no"
    assert window._primary_pin_names_preference() is True


def test_a_project_load_applies_the_saved_preference(window):
    """@brief Positive: OFF saved -> the symbol tab is OFF after a load; negative: ON stays ON."""
    try:
        window._config["pin_primary_names"] = False
        set_primary_pin_names(True)
        window._apply_project_state(window._project_state())
        assert window.symbol_tab.primary_pin_names() is False
        window._config["pin_primary_names"] = True
        window._apply_project_state(window._project_state())
        assert window.symbol_tab.primary_pin_names() is True
    finally:
        _restore_default()


class _PinoutStub:
    """Stand-in of PinoutDialog returning the given pinout and switch."""
    seen = {}
    rows = None
    primary = True

    def __init__(self, rows, name, parent, *, primary_names=True):
        _PinoutStub.seen = {"primary_names": primary_names}

    def exec(self):
        return QDialog.DialogCode.Accepted

    def edited_pinout(self):
        return [dict(row) for row in _PinoutStub.rows]

    def primary_names(self):
        return _PinoutStub.primary


def test_toggling_only_the_switch_in_the_pinout_dialog_is_applied(window, monkeypatch):
    """@brief Positive: the switch alone is saved, applied and redraws; negative: no change, no redraw."""
    saved = []
    monkeypatch.setattr(gui, "save_config", lambda config: saved.append(dict(config)))
    monkeypatch.setattr(gui, "PinoutDialog", _PinoutStub)
    drawn = []
    monkeypatch.setattr(window, "_draw_preview", lambda *a, **k: drawn.append(1))
    window.pinout = [{"pin": 1, "name": "GPIO14 (ADC2_CH3)"}]
    _PinoutStub.rows = window.pinout
    try:
        window._config["pin_primary_names"] = True
        _PinoutStub.primary = False
        window._show_pinout_dialog()
        assert _PinoutStub.seen == {"primary_names": True}
        assert window._config["pin_primary_names"] is False
        assert window.symbol_tab.primary_pin_names() is False and primary_pin_names_enabled() is False
        assert drawn and saved and saved[-1]["pin_primary_names"] is False
        drawn.clear()
        saved.clear()
        window._show_pinout_dialog()  # same switch, same rows: nothing to do
        assert _PinoutStub.seen == {"primary_names": False}
        assert drawn == [] and saved == []
    finally:
        _restore_default()


def test_the_preview_label_shows_the_primary_name_and_the_tooltip_the_full_text(window):
    """@brief Positive: ON draws «(GPIO14)»; negative: OFF draws the full text; tooltip always full."""
    full = "GPIO14 (ADC2_CH3, TOUCH14)"
    window.pinout = [{"pin": 1, "name": full}]
    bounds = QRectF(0, 0, 10, 10)
    try:
        set_primary_pin_names(True)
        label = window._add_preview_pin_function(1, "left", bounds)
        assert label.text() == "(GPIO14)" and label.toolTip() == full
        set_primary_pin_names(False)
        label = window._add_preview_pin_function(1, "left", bounds)
        assert label.text() == f"({full})" and label.toolTip() == full
    finally:
        _restore_default()
    assert window._pad_tooltip(1).endswith(full)


def test_the_symbol_tab_notes_reach_the_log(window):
    """@brief Positive: a note emitted by the symbol tab is logged; negative: nothing else is."""
    before = window._log_plain_text()
    window.symbol_tab.log_message.emit("nota di prova del simbolo")
    after = window._log_plain_text()
    assert "nota di prova del simbolo" in after and "nota di prova del simbolo" not in before


def test_colliding_primary_names_keep_their_full_footprint_label(window):
    """@brief Negative (review 2026-11-14): «GPIO0 (BOOT)» and «GPIO0 (STRAP)» would both read
    GPIO0, so the footprint keeps each full name, as the symbol does. Positive: an ordinary name
    is still split to its primary name."""
    window.pinout = [{"pin": 1, "name": "GPIO0 (BOOT)"}, {"pin": 2, "name": "GPIO0 (STRAP)"},
                     {"pin": 3, "name": "GPIO4 (ADC1_3, TOUCH4)"}]
    bounds = QRectF(0, 0, 10, 10)
    try:
        set_primary_pin_names(True)
        labels = {n: window._add_preview_pin_function(n, "left", bounds).text() for n in (1, 2, 3)}
        assert labels == {1: "(GPIO0 (BOOT))", 2: "(GPIO0 (STRAP))", 3: "(GPIO4)"}
    finally:
        _restore_default()
