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
                 dict(layout='matrix', rows=2, cols=4),
                 # Contratto aggiornato (fix A2): passo e distanza fra le file
                 # non bastano, perche' non dicono DOVE sta la griglia sulla
                 # scheda. Prima la si centrava d'ufficio e si cancellavano
                 # corpi veri al centro di moduli con l'header su un bordo.
                 dict(layout='matrix', rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=10),
                 dict(layout='matrix', rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=10,
                      body_offset_x_mm=-5.0)):
        assert contact_positions(data, *BOARD) == []
    assert contact_proposals([_proposal(0, 12.7, 1.6, .8)], [], *BOARD) == set()


def test_a_pad_sized_box_on_a_contact_goes_and_a_component_elsewhere_stays():
    """@brief La piazzola e un 0603 misurano uguale: separa la POSIZIONE.

    La striscia ha una profondita' perche' il rilevatore annota la piazzola
    dorata e la sigla serigrafata, non il centro del semi-foro: senza
    profondita' le file di piazzole passavano quasi tutte.
    """
    positions = contact_positions(_declared(), *BOARD)
    assert positions[0]['depth_mm'] > 0
    pad = _proposal(0, 12.7, 1.6, .8)            # sul contatto, al bordo
    label = _proposal(0, 12.7 - 2.0, 1.0, .9)    # la sigla, appena piu' dentro
    elsewhere = _proposal(0, 0, 1.6, .8)         # stesso ingombro, in mezzo alla scheda
    deep = _proposal(0, 12.7 - 6.0, 1.6, .8)     # vicino alla fila ma fuori dalla striscia
    dropped = contact_proposals([pad, label, elsewhere, deep], positions, *BOARD)
    assert dropped == {0, 1}


def test_a_stretch_of_the_row_goes_and_an_edge_connector_stays():
    """@brief Coppia positivo/negativo sul secondo criterio: la contenzione.

    Un riquadro che copre piu' contatti ED E' CONTENUTO nella striscia e' un
    pezzo di fila: su un modulo castellated non c'e' nulla montato sopra i
    contatti. Un connettore montato sul bordo copre anch'esso piu' contatti, ma
    entra nella scheda molto piu' della striscia -- ed e' quella differenza a
    salvarlo, non la sua dimensione.
    """
    positions = contact_positions(_declared(), *BOARD)
    depth = positions[0]['depth_mm']
    strip = _proposal(0, 12.7 - depth / 2, 19.0, depth * .8)
    connector = _proposal(0, 12.7 - 4.0, 19.0, 9.0)
    dropped = contact_proposals([strip, connector], positions, *BOARD)
    assert dropped == {0}


def test_on_a_through_hole_matrix_nothing_is_removed_for_covering_pins():
    """@brief Caso negativo strutturale: li' un connettore sta proprio sopra i pin.

    La regola sulla copertura vale solo sui contatti di bordo. Su una matrice
    il corpo del pin header copre tutti i suoi pin per definizione, e toglierlo
    significherebbe perdere il componente piu' importante della scheda.
    """
    # Offset dichiarati che centrano la griglia 8x2 sulla scheda (fix A2: senza
    # offset la griglia non ha posizione e non si filtra nulla).
    positions = contact_positions(
        dict(layout='matrix', rows=2, cols=8, pitch_mm=2.54, row_spacing_mm=7.62,
             body_offset_x_mm=-(BOARD[0] - 7 * 2.54) / 2,
             body_offset_y_mm=-(BOARD[1] - 7.62) / 2), *BOARD)
    assert len(positions) == 16
    header = _proposal(0, 0, 8 * 2.54, 7.62 + 2.54)
    assert contact_proposals([header], positions, *BOARD) == set()


#: Griglia 2x4 il cui pin 1 sta a (-3.81, -5.0) dal centro della scheda:
#: offset = distanza (negativa) fra il bordo del corpo e il pin 1 / la prima fila.
_CENTRED_MATRIX = dict(layout='matrix', rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=10,
                       body_offset_x_mm=-(BOARD[0] / 2 - 3.81),
                       body_offset_y_mm=-(BOARD[1] / 2 - 5.0))


def test_a_matrix_of_through_holes_is_recognized_the_same_way():
    positions = contact_positions(_CENTRED_MATRIX, *BOARD)
    assert len(positions) == 8
    on_pin = _proposal(2.54*1.5, 5.0, 1.7, 1.7)
    assert contact_proposals([on_pin], positions, *BOARD) == {0}


def test_the_matrix_sits_where_the_declared_offsets_put_it_not_at_the_centre():
    """@brief Fix A2: stessa griglia, stessi corpi; cambia SOLO la posizione dichiarata.

    Con l'header lungo il bordo sinistro (pin 1 a 1.27 mm dai bordi), un corpo
    al centro della scheda non sta su nessun pin e deve restare; un corpo sul
    pin 1 reale viene invece riconosciuto. Prima la griglia veniva centrata e
    il corpo centrale spariva prima della revisione.
    """
    edge_header = dict(_CENTRED_MATRIX, body_offset_x_mm=-1.27, body_offset_y_mm=-1.27)
    positions = contact_positions(edge_header, *BOARD)
    assert len(positions) == 8
    pin1 = min(positions, key=lambda p: (p['x_mm'], p['y_mm']))
    assert pin1['x_mm'] == pytest.approx(-BOARD[0] / 2 + 1.27)
    assert pin1['y_mm'] == pytest.approx(-BOARD[1] / 2 + 1.27)
    centre_body = _proposal(2.54 * 1.5, 5.0, 1.7, 1.7)   # su un pin della griglia CENTRATA
    on_real_pin = _proposal(pin1['x_mm'], pin1['y_mm'], 1.7, 1.7)
    assert contact_proposals([centre_body, on_real_pin], positions, *BOARD) == {1}


def test_only_the_pins_that_exist_are_contacts_and_a_bottom_view_is_mirrored():
    """@brief populated_pins e pin_map contano come li disegna il footprint.

    Positivo: un pin non popolato non e' un contatto, quindi un corpo sopra la
    sua posizione resta. Negativo strutturale: lo stesso pin_map letto su una
    BOTTOM VIEW ribalta il campo in X, come fa l'estrazione.
    """
    sparse = dict(_CENTRED_MATRIX, populated_pins=[1, 2, 3, 4, 5, 6])  # colonna 3 vuota
    xs = sorted({round(p['x_mm'], 3) for p in contact_positions(sparse, *BOARD)})
    assert xs == pytest.approx([-3.81, -1.27, 1.27])
    one_side = dict(_CENTRED_MATRIX, body_offset_x_mm=-1.27, body_offset_y_mm=-1.27,
                    pin_map=[{'pin': 1, 'row': 0, 'col': 0}])
    top = contact_positions(one_side, *BOARD)
    bottom = contact_positions(dict(one_side, pin_map_view='bottom'), *BOARD)
    assert [p['x_mm'] for p in top] == pytest.approx([-BOARD[0] / 2 + 1.27])
    assert [p['x_mm'] for p in bottom] == pytest.approx([BOARD[0] / 2 - 1.27])
    assert contact_positions(dict(one_side, pin_map=[{'pin': 1}]), *BOARD) == []
