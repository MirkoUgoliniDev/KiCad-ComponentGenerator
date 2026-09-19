# -*- coding: utf-8 -*-
"""Esito mostrato sotto ogni documento: icona in card, spiegazione nel tooltip."""

from pathlib import Path

import numpy as np
import pytest

from test_photo_inventory_gui import window, gui
from kicadgen.services.document_roles import DocumentSet, INVENTORY_ROLES, ROLES
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


def verdict_for(window, role, path, dims=(40.0, 16.0)):
    panel = window.guided_docs
    panel.body_size = (lambda: dims) if dims else (lambda: None)
    panel._calibration_cache.clear()
    return panel._calibration_verdict(role, path)


def test_the_roles_that_are_not_calibrated_still_say_something(window):
    """Nessun segno non distingue «non serve» da «non l'ho guardata»."""
    for role in ROLES:
        if role in INVENTORY_ROLES:
            continue
        icon, summary, explanation, _ = verdict_for(window, role, Path('qualunque.png'))
        assert icon == '\u2713' and summary == ''
        assert 'calibrazione' in explanation


def test_no_verdict_without_a_file(window):
    assert verdict_for(window, 'hires', None) is None


def test_an_accepted_photo_is_green_and_keeps_the_resolution_visible(window, tmp_path):
    icon, summary, explanation, colour = verdict_for(
        window, 'hires', board_photo(tmp_path, 'a.png', 30))
    assert icon == '\u2713' and colour == '#a6e3a1'
    assert 'px/mm' in summary, 'il dato che conta deve restare in card'
    assert 'utilizzabile' in explanation, 'la spiegazione appartiene al tooltip'


def test_a_coarse_photo_is_amber_not_green(window, tmp_path):
    """Calibra, ma a quella scala i passivi piccoli restano invisibili."""
    icon, summary, explanation, colour = verdict_for(
        window, 'top', board_photo(tmp_path, 'b.png', 8))
    assert icon == '\u26a0' and colour == '#f9e2af'
    assert 'px/mm' in summary and 'piccoli' in explanation


def test_a_rejected_photo_is_a_red_cross(window, tmp_path):
    cv2 = pytest.importorskip("cv2")
    flat = tmp_path / 'c.png'
    cv2.imwrite(str(flat), np.full((300, 300, 3), 200, np.uint8))
    icon, _, _, colour = verdict_for(window, 'hires', flat)
    assert icon == '\u2717' and colour == '#f38ba8'


def test_without_body_dimensions_it_says_so_instead_of_guessing(window, tmp_path):
    icon, _, explanation, _ = verdict_for(
        window, 'hires', board_photo(tmp_path, 'd.png', 30), dims=None)
    assert icon == '\u00b7' and 'quote' in explanation


def test_the_expensive_check_is_cached(window, tmp_path, monkeypatch):
    """~150 ms su una foto da 4 Mpx: non va rifatto a ogni refresh."""
    panel = window.guided_docs
    monkeypatch.setattr(panel, 'body_size', lambda: (40.0, 16.0))
    panel._calibration_cache.clear()
    path = board_photo(tmp_path, 'e.png', 30)
    calls = []
    original = panel._measure_calibration
    monkeypatch.setattr(panel, '_measure_calibration',
                        lambda *a: calls.append(a) or original(*a))
    panel._calibration_verdict('hires', path)
    panel._calibration_verdict('hires', path)
    assert len(calls) == 1


def test_the_explanation_goes_to_the_tooltip_not_into_the_card(window, tmp_path, monkeypatch):
    """La card e' stretta: per esteso il testo andava a capo su due righe."""
    panel = window.guided_docs
    monkeypatch.setattr(panel, 'body_size', lambda: (40.0, 16.0))
    DocumentSet(window._docs_dir()).import_file('hires', board_photo(tmp_path, 'src.png', 30))
    panel._calibration_cache.clear()
    panel.refresh()
    label = panel.cards['hires']['verdict']
    assert 'px/mm' in label.text()
    assert 'utilizzabile' not in label.text()
    assert 'utilizzabile' in label.toolTip()


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_the_verdict_strings_are_translated(window, language):
    for source in ('utilizzabile:', 'calibrata, ma grossolana:', '(i passivi piccoli sfuggono)',
                   'non calibrabile: la scheda non \u00e8 riconoscibile qui',
                   'quote del corpo non ancora note',
                   'documento caricato; non richiede calibrazione.'):
        entry = DEFAULT_TRANSLATIONS.get(f'ui:{source}')
        assert entry and entry.get(language), f'manca {language} per «{source}»'
