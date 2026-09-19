# -*- coding: utf-8 -*-
"""Esito di calibrazione mostrato sotto ogni vista dall'alto."""

from pathlib import Path

import numpy as np
import pytest

from test_photo_inventory_gui import window, gui
from kicadgen.services.document_roles import INVENTORY_ROLES, ROLES
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS


def board_photo(tmp_path, name, scale):
    """Scheda sintetica 40x16 mm alla scala richiesta."""
    cv2 = pytest.importorskip("cv2")
    margin = int(6 * scale)
    w, h = int(40 * scale), int(16 * scale)
    img = np.full((h + 2 * margin, w + 2 * margin, 3), 235, np.uint8)
    cv2.rectangle(img, (margin, margin), (margin + w, margin + h), (22, 22, 22), -1)
    path = tmp_path / name
    cv2.imwrite(str(path), img)
    return path


def verdict_of(window, role):
    return window.guided_docs.cards[role]['verdict']


def test_only_the_top_down_roles_get_a_verdict(window):
    """Pinout e vista laterale non sono viste dall'alto: un esito confonderebbe."""
    panel = window.guided_docs
    for role in ROLES:
        if role in INVENTORY_ROLES:
            continue
        assert panel._calibration_verdict(role, Path('qualunque.png')) is None


def test_an_accepted_photo_shows_a_green_tick_with_the_resolution(window, tmp_path, monkeypatch):
    panel = window.guided_docs
    monkeypatch.setattr(panel, 'body_size', lambda: (40.0, 16.0))
    icon, text, colour = panel._calibration_verdict('hires', board_photo(tmp_path, 'a.png', 30))
    assert icon == '\u2713'
    assert 'px/mm' in text
    assert colour == '#a6e3a1'


def test_a_coarse_photo_is_amber_not_green(window, tmp_path, monkeypatch):
    """Calibra, ma a quella scala i passivi piccoli restano invisibili."""
    panel = window.guided_docs
    monkeypatch.setattr(panel, 'body_size', lambda: (40.0, 16.0))
    icon, text, colour = panel._calibration_verdict('top', board_photo(tmp_path, 'b.png', 8))
    assert icon == '\u26a0' and colour == '#f9e2af'
    assert 'piccoli' in text


def test_a_rejected_photo_shows_a_red_cross(window, tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    panel = window.guided_docs
    monkeypatch.setattr(panel, 'body_size', lambda: (40.0, 16.0))
    flat = tmp_path / 'c.png'
    cv2.imwrite(str(flat), np.full((300, 300, 3), 200, np.uint8))
    icon, _, colour = panel._calibration_verdict('hires', flat)
    assert icon == '\u2717' and colour == '#f38ba8'


def test_without_body_dimensions_it_says_so_instead_of_guessing(window, tmp_path, monkeypatch):
    panel = window.guided_docs
    monkeypatch.setattr(panel, 'body_size', lambda: None)
    icon, text, _ = panel._calibration_verdict('hires', board_photo(tmp_path, 'd.png', 30))
    assert icon == '\u00b7' and 'quote' in text


def test_the_expensive_check_is_cached(window, tmp_path, monkeypatch):
    """~150 ms su una foto da 4 Mpx: non va rifatto a ogni refresh."""
    panel = window.guided_docs
    monkeypatch.setattr(panel, 'body_size', lambda: (40.0, 16.0))
    path = board_photo(tmp_path, 'e.png', 30)
    calls = []
    original = panel._measure_calibration
    monkeypatch.setattr(panel, '_measure_calibration',
                        lambda *a: calls.append(a) or original(*a))
    panel._calibration_verdict('hires', path)
    panel._calibration_verdict('hires', path)
    assert len(calls) == 1


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_the_verdict_strings_are_translated(window, language):
    for source in ('utilizzabile:', 'calibrata, ma grossolana:', '(i passivi piccoli sfuggono)',
                   'non calibrabile: la scheda non \u00e8 riconoscibile qui',
                   'quote del corpo non ancora note'):
        entry = DEFAULT_TRANSLATIONS.get(f'ui:{source}')
        assert entry and entry.get(language), f'manca {language} per “{source}”'
