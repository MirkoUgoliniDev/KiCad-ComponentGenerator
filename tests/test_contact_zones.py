"""@brief I contatti dichiarati non sono corpi montati, e non si indovinano."""
import pytest
from kicadgen.services.contact_zones import contact_positions, contact_proposals

BOARD = (63.3, 25.4)


def _declared(**changes):
    row = dict(layout='castellated', castellated_sides=[
        dict(edge='top', count=22, pitch_mm=2.54), dict(edge='bottom', count=22, pitch_mm=2.54)])
    row.update(changes)
    return row


def _proposal(x_mm, y_mm, length_mm, width_mm):
    L, W = BOARD
    return {'_box': (x_mm/L + .5 - length_mm/2/L, y_mm/W + .5 - width_mm/2/W,
                     x_mm/L + .5 + length_mm/2/L, y_mm/W + .5 + width_mm/2/W)}


def test_the_contacts_come_from_the_declared_sides_not_from_the_photo():
    positions = contact_positions(_declared(), *BOARD)
    assert len(positions) == 44
    assert {p['edge'] for p in positions} == {'top', 'bottom'}
    assert all(abs(p['y_mm']) == pytest.approx(12.7) for p in positions)
    # Passo dichiarato, distribuzione centrata: i due estremi sono simmetrici.
    xs = sorted(p['x_mm'] for p in positions if p['edge'] == 'top')
    assert xs[0] == pytest.approx(-xs[-1]) and xs[1] - xs[0] == pytest.approx(2.54)


def test_without_declared_quotes_nothing_is_filtered():
    """@brief Indovinare dove sono i contatti cancellerebbe componenti veri."""
    for data in (None, {}, dict(layout='castellated', castellated_sides=[]),
                 dict(layout='castellated', castellated_sides=[dict(edge='top', count=22)]),
                 dict(layout='matrix', rows=2, cols=4)):
        assert contact_positions(data, *BOARD) == []
    assert contact_proposals([_proposal(0, 12.7, 1.6, .8)], [], *BOARD) == set()


def test_a_pad_sized_box_on_a_contact_goes_and_a_body_over_the_row_stays():
    """@brief Coppia positivo/negativo: due criteri, entrambi su quote dichiarate.

    La piazzola e un condensatore 0603 misurano uguale, quindi la dimensione da
    sola non separa i due casi: separa la POSIZIONE. E un corpo lungo che
    attraversa piu' passi non e' una piazzola, anche se ci sta sopra.
    """
    positions = contact_positions(_declared(), *BOARD)
    pad = _proposal(0, 12.7, 1.6, .8)            # sul contatto centrale
    body = _proposal(0, 12.7, 8.0, 3.0)          # sopra la fila, ma lungo 3 passi
    elsewhere = _proposal(0, 0, 1.6, .8)         # stesso ingombro, in mezzo alla scheda
    dropped = contact_proposals([pad, body, elsewhere], positions, *BOARD)
    assert dropped == {0}


def test_a_matrix_of_through_holes_is_recognized_the_same_way():
    positions = contact_positions(
        dict(layout='matrix', rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=10), *BOARD)
    assert len(positions) == 8
    on_pin = _proposal(2.54*1.5, 5.0, 1.7, 1.7)
    assert contact_proposals([on_pin], positions, *BOARD) == {0}
