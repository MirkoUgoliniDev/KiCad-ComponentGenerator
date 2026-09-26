"""Quarto ruolo: foto ad alta risoluzione per l'inventario dei componenti."""

from pathlib import Path

import numpy as np
import pytest

from kicadgen.services import photo_inventory as inv
from kicadgen.services.document_roles import INVENTORY_ROLES, ROLE_HELP, ROLES
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS


def test_the_new_role_is_declared_like_the_others():
    assert 'hires' in ROLES and 'hires' in ROLE_HELP
    assert set(ROLES) == set(ROLE_HELP)


def test_only_the_photograph_goes_to_the_inventory():
    """Dal 26/09 (manifest versione 2) un file ha piu' ruoli: all'inventario va
    SOLO quello spuntato come foto. Il disegno quotato che faceva da foto nei
    progetti vecchi riceve il ruolo foto dalla migrazione (test_document_roles)."""
    assert INVENTORY_ROLES == ('hires',)


def test_the_help_says_it_must_be_a_photograph_not_a_drawing():
    """Un disegno a tratto ad alta risoluzione non serve all'inventario."""
    assert 'fotografia' in ROLE_HELP['hires']
    assert 'disegno tecnico' in ROLE_HELP['hires']


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_the_card_label_is_translated_everywhere(language):
    assert DEFAULT_TRANSLATIONS["ui:Foto ad alta risoluzione"].get(language)


def photo_of(width, height=200):
    return np.full((height, width, 3), 30, np.uint8)


def test_the_sharpest_calibratable_view_wins(monkeypatch):
    """Fra due viste dall'alto vince quella con piu' pixel."""
    sizes = {'top.png': 400, 'hires.png': 1200}
    monkeypatch.setattr(inv, 'prepare_photo',
                        lambda path, *a, **k: photo_of(sizes[Path(path).name]))
    used = []
    result = inv.extract_inventory(
        [Path('top.png'), Path('hires.png')], 'mock', 40, 20, ['resistor'],
        lambda **kw: used.append(kw) or {"components": []},
        top_photo_index=[1, 0], grounding=False)
    assert result.photo == 'hires.png'


def test_an_uncalibratable_detail_shot_falls_back_to_the_top_view(monkeypatch):
    """Un primo piano parziale non deve lasciare l'inventario senza viste.

    E' il caso che la foto ad alta risoluzione rende probabile: non e' detto
    che inquadri tutta la scheda.
    """
    def prepare(path, *a, **k):
        return None if Path(path).name == 'hires.png' else photo_of(400)

    monkeypatch.setattr(inv, 'prepare_photo', prepare)
    result = inv.extract_inventory(
        [Path('top.png'), Path('hires.png')], 'mock', 40, 20, ['resistor'],
        lambda **kw: {"components": []}, top_photo_index=[1, 0], grounding=False)
    assert result.photo == 'top.png'
    assert not any('non calibrabile' in w for w in result.warnings)


def test_no_top_view_at_all_is_still_reported(monkeypatch):
    monkeypatch.setattr(inv, 'prepare_photo', lambda *a, **k: photo_of(400))
    result = inv.extract_inventory(
        [Path('top.png')], 'mock', 40, 20, ['resistor'],
        lambda **kw: {"components": []}, top_photo_index=[-1], grounding=False)
    assert not result.components
    assert any("Nessuna vista dall'alto" in w for w in result.warnings)


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_the_guided_help_describes_every_role(language):
    """La guida non deve restare indietro quando si aggiunge un ruolo."""
    text = DEFAULT_TRANSLATIONS["help.guided_documents"][language]
    for role in ROLES:
        assert role in text, f"{role} non citato nella guida in {language}"


@pytest.mark.parametrize("language", ["en", "it", "fr", "de"])
def test_every_role_label_has_a_translation(language):
    """Nessuna etichetta di ruolo deve restare in italiano nelle altre lingue."""
    for label in ROLES.values():
        entry = DEFAULT_TRANSLATIONS.get(f"ui:{label}")
        assert entry and entry.get(language), f"manca la traduzione {language} di “{label}”"
