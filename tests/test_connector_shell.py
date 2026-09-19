"""Bocca del connettore sulla faccia di accoppiamento, senza FreeCAD.

Si controllano le scatole passate a makeBox, non la topologia OCC: bastano
a dimostrare su quale faccia si apre la cavita' e che il guscio resta chiuso
sugli altri lati. La cut e' un no-op nel mock, quindi il test descrive
l'intenzione geometrica, non la validita' del solido risultante.
"""

import pytest

from test_observed_terminals import cad


Z0 = 0.96
#: USB-C della ESP32-S3-DevKit, come misurato dall'inventario fotografico.
USB_C = (7.61, 9.98, 2.35)


def boxes_for(cad, cx, cy, size):
    module, made = cad
    made.clear()
    module._connector(cx, cy, Z0, *size, "#f4f1f0")
    return made


def span(box, axis):
    """Estensione (min, max) della scatola lungo l'asse richiesto."""
    index = {"x": 0, "y": 1, "z": 2}[axis]
    return box[index], box[index] + box[3 + index]


def test_cavity_opens_towards_the_board_edge_it_sits_on(cad):
    """Un connettore a sinistra si apre a sinistra, uno a destra a destra.

    E' il verso di innesto: la bocca guarda fuori dalla scheda, dalla parte
    in cui si infila il cavo.
    """
    for cx, edge in ((-27.65, "min"), (27.65, "max")):
        shell, cavity = boxes_for(cad, cx, 0.0, USB_C)
        shell_min, shell_max = span(shell, "x")
        cavity_min, cavity_max = span(cavity, "x")
        if edge == "min":
            assert cavity_min < shell_min, "la bocca non sfonda la faccia esterna"
            assert cavity_max < shell_max, "manca la parete di fondo"
        else:
            assert cavity_max > shell_max, "la bocca non sfonda la faccia esterna"
            assert cavity_min > shell_min, "manca la parete di fondo"


def test_the_lid_stays_closed(cad):
    """La cavita' non deve piu' uscire dal cielo del guscio.

    Scavata dall'alto, in anteprima il connettore si leggeva come una
    scatola con un incasso sul coperchio, indistinguibile da un package
    generico.
    """
    shell, cavity = boxes_for(cad, -27.65, 0.0, USB_C)
    assert span(cavity, "z")[1] < span(shell, "z")[1], "la cavita' sfonda il cielo"
    assert span(cavity, "z")[0] > span(shell, "z")[0], "la cavita' sfonda la base"


def test_mating_axis_follows_the_shorter_horizontal_side(cad):
    """La profondita' di inserimento e' il lato orizzontale minore."""
    length, width, height = USB_C
    # Package ruotato: il lato lungo passa su X, quindi l'innesto va su Y.
    shell, cavity = boxes_for(cad, 0.0, -9.0, (width, length, height))
    assert span(cavity, "y")[0] < span(shell, "y")[0]
    assert span(cavity, "x")[0] > span(shell, "x")[0], "i fianchi devono restare pieni"


def test_a_package_too_small_for_walls_stays_solid(cad):
    """Nessun guscio degenere con le facce che si compenetrano."""
    assert len(boxes_for(cad, -5.0, 0.0, (1.0, 0.8, 0.5))) == 1


def test_the_shell_never_grows_beyond_the_component_envelope(cad):
    """La bocca non deve allargare l'ingombro dichiarato dall'inventario."""
    shell, _ = boxes_for(cad, -27.65, 0.0, USB_C)
    assert (shell[3], shell[4], shell[5]) == pytest.approx(USB_C)
