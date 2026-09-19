# -*- coding: utf-8 -*-
"""Sigle dei pin incise nel modello 3D: opzione, spenta di default."""

import pytest

from test_photo_inventory_gui import window, gui
from kicadgen.gui.app_config import DEFAULTS
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS


def test_the_option_is_off_by_default():
    """Su un modulo a passo fitto le sigle si sovrappongono: meglio niente."""
    assert DEFAULTS['silk_pin_labels'] is False


def test_no_labels_are_emitted_when_the_option_is_off(window):
    window._config['silk_pin_labels'] = False
    window.pinout = [{'pin': '1', 'name': 'GPIO38'}, {'pin': '2', 'name': 'GPIO37'}]
    assert window._silk_labels() == {}


def test_labels_come_from_the_pinout_when_the_option_is_on(window):
    window._config['silk_pin_labels'] = True
    window.pinout = [{'pin': '1', 'name': 'GPIO38 (input only)'},
                     {'pin': '2', 'name': 'GND'}]
    labels = window._silk_labels()
    assert labels == {'1': 'GPIO38', '2': 'GND'}


def test_the_option_survives_a_round_trip_through_the_dialog(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    captured = {}

    def fake_exec(self):
        captured['dialog'] = self
        self.silk_labels_checkbox.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui.GenerationOptionsDialog, 'exec', fake_exec)
    monkeypatch.setattr(gui, 'save_config', lambda config: None)
    window._config['silk_pin_labels'] = False
    window._edit_generation_options()
    assert window._config['silk_pin_labels'] is True
    assert window._silk_pin_labels() is True


def test_the_dialog_opens_showing_the_current_value(window, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    seen = {}

    def fake_exec(self):
        seen['checked'] = self.silk_labels_checkbox.isChecked()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui.GenerationOptionsDialog, 'exec', fake_exec)
    window._config['silk_pin_labels'] = True
    window._edit_generation_options()
    assert seen['checked'] is True


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_the_new_labels_are_translated(language):
    for source in ('Serigrafia del modello 3D', 'Incidi le sigle dei pin'):
        entry = DEFAULT_TRANSLATIONS.get(f'ui:{source}')
        assert entry and entry.get(language), f'manca {language} per «{source}»'
