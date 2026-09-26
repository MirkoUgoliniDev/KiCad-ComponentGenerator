"""@brief Pin header pads must keep the minimum copper gap (punto 84 medio 9).

Un 2x10 a passo 1.27 con pad da 1.7 mm veniva accettato con le piazzole
sovrapposte. Il controllo guarda le piazzole DAVVERO emesse, quindi distingue
per struttura una griglia piena (piazzole vicine) da una mappa sparsa sulla
stessa griglia (piazzole lontane).
"""
import pytest

from kicadgen.generators import castellated_module as cm
from kicadgen.generators import pin_header as ph
from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header


def test_the_gap_is_the_castellated_minimum():
    assert ph.MIN_PAD_GAP_MM == cm.MIN_HOLE_WALL_MM == 0.2


def test_overlapping_pads_at_fine_pitch_are_refused_with_a_clear_message():
    params = PinHeaderParams(rows=2, cols=10, pitch=1.27, row_spacing=1.27,
                             pad_diameter=1.7, drill=1.0)
    with pytest.raises(ValueError) as error:
        params.validate()
    message = str(error.value)
    assert "1.70" in message and "1.27" in message and "0.20" in message


def test_a_standard_254_header_is_accepted():
    params = PinHeaderParams(rows=2, cols=20, pitch=2.54, row_spacing=2.54,
                             pad_diameter=1.7, drill=1.0)
    params.validate()
    assert len(build_pin_header(params).pads) == 40


def test_a_127_header_with_its_own_pads_is_accepted():
    """Positivo a passo fine: pad da 1.0 lasciano 0.27 mm di rame libero."""
    PinHeaderParams(rows=2, cols=10, pitch=1.27, row_spacing=1.27,
                    pad_diameter=1.0, drill=0.65).validate()


def test_row_spacing_is_checked_independently_of_the_pitch():
    """Passo largo ma file troppo vicine: rifiutato per la distanza fra righe."""
    with pytest.raises(ValueError):
        PinHeaderParams(rows=2, cols=4, pitch=2.54, row_spacing=1.6,
                        pad_diameter=1.7, drill=1.0).validate()
    # Una sola fila: la distanza fra righe non conta.
    PinHeaderParams(rows=1, cols=4, pitch=2.54, row_spacing=1.6,
                    pad_diameter=1.7, drill=1.0).validate()


def test_the_square_pin1_pad_reaches_further_on_the_diagonal():
    """Il pad 1 e' quadrato: in diagonale il suo spigolo arriva piu' vicino di un cerchio."""
    # Centri a (0,0) e (1.5,1.5): due cerchi da 1.7 lasciano 0.42 mm,
    # il quadrato contro il cerchio 1.5 - 0.85 = 0.65 per asse -> 0.069 mm.
    base = dict(rows=2, cols=2, pitch=1.5, row_spacing=1.5, pad_diameter=1.7, drill=1.0,
                pin_map={1: (0, 0), 4: (1, 1)})
    with pytest.raises(ValueError):
        PinHeaderParams(**base).validate()
    PinHeaderParams(**dict(base, pin_map={2: (0, 0), 4: (1, 1)})).validate()


def test_a_sparse_map_on_a_fine_grid_is_judged_on_the_emitted_pads():
    """Negativo strutturale: stessa griglia fine, ma le piazzole popolate sono lontane."""
    PinHeaderParams(rows=2, cols=8, pitch=1.27, row_spacing=5.0, pad_diameter=1.7,
                    drill=1.0, populated_pins={1, 6, 11, 16}).validate()
    with pytest.raises(ValueError):
        PinHeaderParams(rows=2, cols=8, pitch=1.27, row_spacing=5.0, pad_diameter=1.7,
                        drill=1.0, populated_pins={1, 3}).validate()
