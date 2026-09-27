"""@brief What the window uses when an extraction leaves a value out (decisions of 2026-09-26).

- PCB thickness: 1.6 mm for EVERY layout, reset at every extraction, the value
  actually used written in the log (medio 3).
- Board colour: sampled from the calibrated photo, otherwise the standard green
  solder mask, declared; reset at every extraction (medio 11).
- Limits: physical limits block, unusual values only warn (medio 10).

Every test pairs a positive and a negative case that differ for a structural
reason (declared or not, board or single component, possible or impossible).
"""
import cv2
import numpy as np
import pytest

from test_photo_inventory_gui import gui, ExtractedParams
from kicadgen.services import photo_refine


def castellated(**extra):
    values = dict(name='', description='', tags='', confidence='medium', notes='',
                  layout='castellated', body_length_mm=40, body_width_mm=20,
                  castellated_sides=[dict(edge='left', count=3, pitch_mm=2.54, first_pin=1,
                                          contact_style='half_hole')],
                  photo_source='', components_3d=[])
    values.update(extra)
    return ExtractedParams(**values)


def header(**extra):
    values = dict(name='', description='', tags='', confidence='medium', notes='',
                  layout='matrix', rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=2.54,
                  pad_diameter_mm=1.7, drill_mm=1.0, body_length_mm=10.2, body_width_mm=5.1,
                  body_height_mm=8.5, photo_source='', components_3d=[])
    values.update(extra)
    return ExtractedParams(**values)


@pytest.fixture
def logs(window, monkeypatch):
    lines = []
    monkeypatch.setattr(window, '_append_log', lines.append)
    return lines


def png_of(bgr):
    ok, encoded = cv2.imencode('.png', bgr)
    assert ok
    return encoded.tobytes()


# ── Spessore PCB ─────────────────────────────────────────────────────────


def test_a_board_without_thickness_uses_1_6_mm_and_says_so(window, logs):
    """Positivo: il valore del progetto precedente non sopravvive, il log dice quello usato."""
    window.field_cast_pcb.setValue(1.0)
    window._on_extracted(castellated())
    assert window.field_cast_pcb.value() == pytest.approx(gui.STANDARD_PCB_THICKNESS_MM)
    assert window._current_castellated_params().pcb_thickness == pytest.approx(1.6)
    assert any('usato 1.60 mm' in line for line in logs)
    assert not any('scheda THT' in line for line in logs)


def test_a_declared_thickness_is_used_and_not_called_an_estimate(window, logs):
    """Negativo: una quota dei documenti vince e non e' chiamata stima."""
    window._on_extracted(castellated(pcb_thickness_mm=0.8))
    assert window.field_cast_pcb.value() == pytest.approx(0.8)
    assert any('dai documenti: 0.80 mm' in line for line in logs)
    assert not any('usato 1.60' in line for line in logs)
    # La nuova estrazione senza quota non eredita 0.8 mm.
    window._on_extracted(castellated())
    assert window.field_cast_pcb.value() == pytest.approx(1.6)


def test_a_single_component_has_no_pcb_line_in_the_log(window, logs):
    """Negativo: un componente singolo non ha una scheda, niente riga sullo spessore."""
    window._on_extracted(header())
    assert not any('Spessore PCB' in line for line in logs)
    assert window._extracted_pcb_thickness is None


# ── Colore della scheda ──────────────────────────────────────────────────


def test_the_board_colour_is_sampled_from_the_calibrated_photo(window, logs, monkeypatch):
    seen = []
    monkeypatch.setattr(photo_refine, 'sample_board_color',
                        lambda crop: seen.append(crop.shape) or '#2A7F3B', raising=False)
    window.field_body_color.setText('#ff0000')
    window._on_extracted(castellated(photo_png=png_of(np.zeros((40, 80, 3), np.uint8))))
    assert seen == [(40, 80, 3)], 'il campionatore riceve il ritaglio BGR'
    assert window.field_body_color.text() == '#2a7f3b'
    assert any('campionato dalla foto' in line for line in logs)


@pytest.mark.parametrize('case', ['no_photo', 'no_sampler', 'bad_answer', 'sampler_fails'])
def test_without_a_usable_photo_the_standard_green_is_declared(window, logs, monkeypatch, case):
    if case == 'no_sampler':
        monkeypatch.delattr(photo_refine, 'sample_board_color', raising=False)
    elif case == 'bad_answer':
        monkeypatch.setattr(photo_refine, 'sample_board_color', lambda crop: 'green', raising=False)
    elif case == 'sampler_fails':
        def fail(crop):
            raise ValueError('scheda non trovata')
        monkeypatch.setattr(photo_refine, 'sample_board_color', fail, raising=False)
    photo = None if case == 'no_photo' else png_of(np.zeros((20, 40, 3), np.uint8))
    window.field_body_color.setText('#1a1a1a')
    window._on_extracted(castellated(photo_png=photo))
    assert window.field_body_color.text() == gui.STANDARD_SOLDER_MASK_HEX
    assert any('verde standard' in line for line in logs)


def test_a_declared_colour_wins_and_is_not_sampled(window, logs, monkeypatch):
    """Negativo: un colore dichiarato dall'analisi non si campiona."""
    monkeypatch.setattr(photo_refine, 'sample_board_color',
                        lambda crop: pytest.fail('campionato senza bisogno'), raising=False)
    window._on_extracted(castellated(body_color_hex='#0b3d91',
                                     photo_png=png_of(np.zeros((20, 40, 3), np.uint8))))
    assert window.field_body_color.text() == '#0b3d91'
    assert not any('Colore' in line for line in logs)


def test_a_single_component_falls_back_to_the_declared_body_colour(window, logs):
    """Un componente singolo non e' una scheda: niente verde, ma nemmeno il colore precedente."""
    window.field_body_color.setText('#ff0000')
    window._on_extracted(header())
    assert window.field_body_color.text() == gui.DEFAULT_BODY_COLOR_HEX
    assert any('nero predefinito' in line for line in logs)


# ── Limiti fisici e avvisi ───────────────────────────────────────────────


@pytest.mark.parametrize('extra', [
    dict(rows=2, cols=60, body_length_mm=160.0),          # piu' di 40 colonne
    dict(pitch_mm=0.4, cols=10, body_length_mm=5.0),      # passo sotto 0.5 mm
    dict(rows=2, row_spacing_mm=60.0, body_width_mm=65.0),  # interasse oltre 50 mm
    dict(body_height_mm=0.2),                             # corpo piu' basso di 0.5 mm
])
def test_values_the_old_editors_refused_are_now_accepted(window, extra):
    window._validate_automatic_input(header(**extra))


@pytest.mark.parametrize(('extra', 'field'), [
    (dict(body_length_mm=400.0), 'body_length_mm'),
    (dict(body_height_mm=0.01), 'body_height_mm'),
    (dict(rows=200, row_spacing_mm=2.54, body_width_mm=290.0), 'rows'),
    (dict(cols=4.5), 'cols'),
])
def test_impossible_values_are_still_refused_with_the_field_named(window, extra, field):
    with pytest.raises(ValueError, match=field):
        window._validate_automatic_input(header(**extra))


def test_a_module_side_with_many_contacts_is_left_to_the_generator(window):
    """Il tetto di 100 contatti dell'editor non c'e' piu': se stanno lo dice il generatore."""
    many = castellated(body_length_mm=200.0, castellated_sides=[
        dict(edge='top', count=150, pitch_mm=1.27, first_pin=1, contact_style='half_hole')])
    window._validate_automatic_input(many)
    too_long = castellated(castellated_sides=[
        dict(edge='top', count=400, pitch_mm=1.27, first_pin=1, contact_style='half_hole')])
    with pytest.raises(ValueError, match='impossibile'):
        window._validate_automatic_input(too_long)


def test_unusual_values_warn_instead_of_blocking(window, logs):
    """Una griglia piu' larga del corpo (un DIP) e un passo fuori dal consueto: solo avvisi."""
    params = header(rows=2, row_spacing_mm=7.62, body_width_mm=6.35, pitch_mm=0.2, cols=4,
                    body_length_mm=5.0, pad_diameter_mm=0.15, drill_mm=0.1)
    window._validate_automatic_input(params)
    warnings = window._plausibility_warnings(params)
    assert any('lungo Y' in w for w in warnings)
    assert any('0.2 mm' in w and 'unità di misura' in w for w in warnings)
    window._on_extracted(params)
    assert sum(line.startswith('ATTENZIONE') for line in logs) >= 2


def test_ordinary_values_raise_no_warning(window):
    """Negativo: un pin header comune non produce avvisi."""
    assert window._plausibility_warnings(header()) == []


def test_an_inventory_relaunched_on_the_fields_keeps_thickness_and_colour(window, logs):
    """Negativo: il rilancio dell'inventario senza nuova analisi non azzera i valori del progetto."""
    window.field_cast_pcb.setValue(0.8)
    window.field_body_color.setText('#123456')
    window._relaunch_base_synthesized = True
    window._apply_extracted_pcb_thickness(castellated())
    window._apply_extracted_body_color(castellated())
    assert window.field_cast_pcb.value() == pytest.approx(0.8)
    assert window.field_body_color.text() == '#123456'
    assert not any('Spessore' in line or 'Colore' in line for line in logs)
