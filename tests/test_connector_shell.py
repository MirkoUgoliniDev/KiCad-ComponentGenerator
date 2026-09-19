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
        shell, cavity = boxes_for(cad, cx, 0.0, USB_C)[:2]
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
    shell, cavity = boxes_for(cad, -27.65, 0.0, USB_C)[:2]
    assert span(cavity, "z")[1] < span(shell, "z")[1], "la cavita' sfonda il cielo"
    assert span(cavity, "z")[0] > span(shell, "z")[0], "la cavita' sfonda la base"


def test_mating_axis_follows_the_shorter_horizontal_side(cad):
    """La profondita' di inserimento e' il lato orizzontale minore."""
    length, width, height = USB_C
    # Package ruotato: il lato lungo passa su X, quindi l'innesto va su Y.
    shell, cavity = boxes_for(cad, 0.0, -9.0, (width, length, height))[:2]
    assert span(cavity, "y")[0] < span(shell, "y")[0]
    assert span(cavity, "x")[0] > span(shell, "x")[0], "i fianchi devono restare pieni"


def test_a_package_too_small_for_walls_stays_solid(cad):
    """Nessun guscio degenere con le facce che si compenetrano."""
    assert len(boxes_for(cad, -5.0, 0.0, (1.0, 0.8, 0.5))) == 1


def test_the_shell_never_grows_beyond_the_component_envelope(cad):
    """La bocca non deve allargare l'ingombro dichiarato dall'inventario."""
    shell = boxes_for(cad, -27.65, 0.0, USB_C)[0]
    assert (shell[3], shell[4], shell[5]) == pytest.approx(USB_C)


def test_the_mouth_is_a_rounded_slot_not_a_square_hole(cad):
    """La bocca di un USB e' un'asola a stadio, non un foro squadrato."""
    module, _ = cad
    filleted = []
    module._fillet = lambda shape, radius, edges: filleted.append(radius) or shape
    boxes_for(cad, -27.65, 0.0, USB_C)
    # Raggio pari a meta' del lato corto della cavita': sezione a stadio.
    wall = max(0.2, min(USB_C) * 0.15)
    assert pytest.approx(min(USB_C[1] - 2 * wall, USB_C[2] - 2 * wall) / 2) in filleted


def test_a_flat_wide_mouth_gets_the_contact_tongue(cad):
    """La famiglia USB ha la linguetta; un jack squadrato no.

    La regola guarda la forma misurata, non il nome del componente.
    """
    assert len(boxes_for(cad, -27.65, 0.0, USB_C)) == 3      # guscio, cavita', linguetta
    # Jack tozzo da bordo: bocca quasi quadrata, nessuna linguetta. Non un
    # quadratino piccolo, che ora e' un coassiale verticale.
    assert len(boxes_for(cad, -5.0, 0.0, (5.0, 7.0, 6.0))) == 2


def test_the_tongue_faces_the_mouth_instead_of_hiding_at_the_back(cad):
    """Deve affacciarsi sull'asola, arretrata di poco: si vede in fondo."""
    shell, _cavity, tongue = boxes_for(cad, -27.65, 0.0, USB_C)
    mouth = span(shell, "x")[0]
    assert span(tongue, "x")[0] > mouth, "la linguetta sporge dalla bocca"
    assert span(tongue, "x")[0] - mouth < USB_C[0] * 0.25, "la linguetta e' in fondo"


def test_the_tongue_never_touches_the_cavity_walls(cad):
    _shell, cavity, tongue = boxes_for(cad, -27.65, 0.0, USB_C)
    for axis in ("y", "z"):
        assert span(tongue, axis)[0] > span(cavity, axis)[0]
        assert span(tongue, axis)[1] < span(cavity, axis)[1]


class _PickyShape:
    """makeFillet che rifiuta i raggi grandi, come fa OCC sul caso tangente."""

    def __init__(self, limit):
        self.limit = limit
        self.attempts = []

    def makeFillet(self, radius, edges):
        self.attempts.append(radius)
        if radius > self.limit:
            raise RuntimeError("radius too large for the edge")
        return f"filetto r={radius:.3f}"


def test_a_refused_fillet_retries_smaller_instead_of_giving_a_square_hole(cad):
    """Il raggio tangente veniva rifiutato e restava uno spigolo vivo.

    Era un fallimento silenzioso proprio nel caso che conta: l'asola
    diventava un foro squadrato senza che nulla lo segnalasse.
    """
    module, _ = cad
    shape = _PickyShape(limit=0.75)
    result = module._fillet_best(shape, 0.82, ["edge"])
    assert result is not shape, "ha rinunciato invece di riprovare piu' piccolo"
    assert len(shape.attempts) > 1, "non ha riprovato"
    assert max(shape.attempts) == pytest.approx(0.82)
    assert result.endswith(f"r={shape.attempts[-1]:.3f}")


def test_an_impossible_fillet_still_degrades_to_a_sharp_edge(cad):
    """Se nessun raggio passa, il pezzo resta spigoloso ma viene prodotto."""
    module, _ = cad
    shape = _PickyShape(limit=0.0)
    assert module._fillet_best(shape, 0.82, ["edge"]) is shape


@pytest.mark.parametrize("cx, cy, rotation, expected", [
    # Bordo sinistro: la bocca deve finire a -X comunque sia ruotato il pezzo.
    (-27.65, 0.0, 0, -1.0), (-27.65, 0.0, 180, +1.0),
    # Bordo destro, speculare.
    (+27.65, 0.0, 0, +1.0), (+27.65, 0.0, 180, -1.0),
])
def test_the_mouth_compensates_the_rotation_applied_by_the_caller(cad, cx, cy, rotation,
                                                                  expected):
    """Il builder lavora nel sistema locale; e' il chiamante che ruota.

    Decidendo il verso dalla sola posizione globale, una rotazione di 180
    gradi ribaltava la bocca verso l'interno della scheda -- dove stanno i
    pin, non il cavo.
    """
    module, _ = cad
    assert module._outward_sign(cx, cy, 0, rotation) == expected


@pytest.mark.parametrize("rotation", [90, 270])
def test_a_connector_on_a_short_edge_also_faces_outwards(cad, rotation):
    module, _ = cad
    # Asse di innesto locale Y, componente sul bordo inferiore della scheda.
    assert module._outward_sign(0.0, -9.0, 1, rotation) == (-1.0 if rotation == 90 else 1.0)


def test_without_rotation_the_behaviour_is_unchanged(cad):
    """La rotazione e' opzionale: senza, vale la posizione come prima."""
    module, _ = cad
    assert module._outward_sign(-5.0, 0.0, 0, 0) == -1.0
    assert module._outward_sign(+5.0, 0.0, 0, 0) == +1.0


#: Connettore d'antenna U.FL misurato dall'inventario sulla foto reale.
UFL = (4.57, 4.05, 2.35)


def test_a_small_square_connector_mates_from_above_not_from_the_side(cad):
    """Un coassiale da circuito stampato accoglie il cavo dall'alto.

    Dargli la bocca laterale degli altri connettori lo rendeva
    irriconoscibile e apriva il guscio nella direzione sbagliata.
    """
    module, made = cad
    made.clear()
    parts = module._connector(23.76, -6.01, 0.96, *UFL, '#79582b')
    assert len(parts) == 2, 'base e barilotto devono restare pezzi distinti'
    base = made[0]
    # La base non occupa tutta l'altezza: sopra ci va il barilotto.
    assert base[5] < UFL[2]
    # Nessuna cavita' laterale: le scatole sono solo base e cilindri.
    assert len(made) == 1, 'una bocca laterale non deve essere scavata'


def test_the_barrel_is_metal_whatever_the_photo_sampled(cad):
    """Su un pezzo cosi' piccolo il colore campionato e' quello del PCB."""
    module, _ = cad
    (_, base_colour), (_, barrel_colour) = module._connector(
        23.76, -6.01, 0.96, *UFL, '#79582b')
    assert base_colour == '#79582b'
    assert barrel_colour == module.METAL


def test_an_edge_connector_keeps_the_side_mouth(cad):
    """La regola non deve rubare i connettori da bordo scheda."""
    module, made = cad
    for size in (USB_C, (5.0, 9.0, 3.0), (5.0, 7.0, 6.0)):
        made.clear()
        module._connector(-27.65, 0.0, 0.96, *size, '#d8d8d8')
        assert len(made) >= 2, f'{size}: manca la cavita\' laterale'


@pytest.mark.parametrize("size, vertical", [
    ((4.57, 4.05, 2.35), True),    # U.FL reale
    ((3.0, 3.0, 2.5), True),       # U.FL nominale
    ((7.61, 9.98, 2.35), False),   # USB-C: quadrato ma troppo grande
    ((5.0, 9.0, 3.0), False),      # da bordo: troppo allungato
    ((6.5, 6.0, 3.0), False),      # oltre il limite di dimensione
])
def test_the_rule_reads_the_measured_shape_not_the_name(cad, size, vertical):
    module, made = cad
    made.clear()
    module._connector(10.0, 0.0, 0.96, *size, '#79582b')
    # Il percorso verticale non scava nulla: una sola scatola fra le chiamate.
    assert (len(made) == 1) is vertical, f'{size}: percorso sbagliato'


@pytest.mark.parametrize("size, mating, coaxial", [
    (UFL,   'top',  True),    # coassiale, come lo vede il modello
    (UFL,   'edge', False),   # il campo vince sulle dimensioni
    (USB_C, 'edge', False),
    (USB_C, 'top',  True),    # anche al contrario: comanda la foto
])
def test_the_mating_direction_declared_by_the_model_wins(cad, size, mating, coaxial):
    """Dal solo ingombro il verso non e' deducibile: un U.FL e un jack da
    bordo possono misurare uguale. Dalla foto invece si vede."""
    module, _ = cad
    parts = module._connector(10.0, 0.0, 0.96, *size, '#79582b', mating=mating)
    is_coaxial = len(parts) == 2 and parts[1][1] == module.METAL
    assert is_coaxial is coaxial


@pytest.mark.parametrize("size, coaxial", [(UFL, True), (USB_C, False)])
def test_without_the_field_the_geometric_heuristic_still_answers(cad, size, coaxial):
    """Ripiego per le estrazioni vecchie e per un modello che non lo compila."""
    module, _ = cad
    parts = module._connector(10.0, 0.0, 0.96, *size, '#79582b', mating=None)
    assert (len(parts) == 2 and parts[1][1] == module.METAL) is coaxial


@pytest.mark.parametrize("bad", ['', 'sideways', 'TOP', 42, None])
def test_an_unusable_mating_value_falls_back_instead_of_breaking(cad, bad):
    module, _ = cad
    parts = module._connector(10.0, 0.0, 0.96, *UFL, '#79582b', mating=bad)
    assert len(parts) == 2 and parts[1][1] == module.METAL
