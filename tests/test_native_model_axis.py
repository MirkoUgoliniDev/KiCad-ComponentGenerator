"""@brief L'asse di un modello nativo non deve dipendere dalla scala della misura.

Il modulo ESP32 usciva ruotato di 90 gradi rispetto al vero: l'ingombro
fotografico era gonfiato del 28% e la scelta dell'asse, decisa sommando due
rapporti, si ribaltava con un margine dell'1%. Un errore di 90 gradi sposta una
feature asimmetrica -- il connettore d'antenna -- da un bordo corto a un bordo
lungo, ed e' il motivo per cui nessun valore fra 0 e 180 riusciva a rimetterlo
a posto.
"""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

CAD_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"


@pytest.fixture
def parts(monkeypatch):
    """Carica freecad_parts senza FreeCAD: qui si prova solo l'aritmetica."""
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(Vector=lambda *a: a))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("axis_only_parts", CAD_DIR / "freecad_parts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def box(x_length, y_length):
    return SimpleNamespace(XLength=x_length, YLength=y_length)


#: Lo STEP del WROOM-1U ha l'asse lungo su Y (19.2 contro 18.0). L'ingombro
#: fotografico ha l'asse lungo su X per contratto, quindi la risposta e' 90.
WROOM = box(18.0, 19.2)


@pytest.mark.parametrize("length,width", [
    (19.2, 18.0),    # misura esatta
    (24.40, 20.43),  # la misura vera del run: +27% e +14%
    (38.4, 36.0),    # il doppio: la scala non deve contare
    (9.6, 9.0),      # la meta'
])
def test_the_long_axis_decides_whatever_the_scale(parts, length, width):
    angle, reference = parts._native_axis_angle([WROOM], length, width)
    assert angle == 90
    assert reference is WROOM


def test_a_model_already_long_on_x_is_not_rotated(parts):
    """Caso negativo: stesso corpo, asse lungo gia' dalla parte giusta.

    Distingue dal positivo per una ragione strutturale -- quale lato e' il piu'
    lungo -- non per una differenza numerica fra due punteggi vicini.
    """
    angle, _ = parts._native_axis_angle([box(19.2, 18.0)], 24.40, 20.43)
    assert angle == 0


def test_the_reference_that_fits_best_wins_and_can_change_the_axis(parts):
    """Corpo e inviluppo dei reofori possono avere assi lunghi diversi.

    E' il caso che il commento nel codice cita (SOT-223): la scelta
    dell'ingombro e quella dell'asse sono la stessa decisione.
    """
    body, leads = box(6.5, 3.5), box(3.6, 7.0)
    angle, reference = parts._native_axis_angle([body, leads], 6.6, 3.6)
    assert (angle, reference) == (0, body)
    angle, reference = parts._native_axis_angle([body, leads], 7.1, 3.7)
    assert (angle, reference) == (90, leads)


def test_a_square_model_never_raises_and_stays_put(parts):
    angle, _ = parts._native_axis_angle([box(5.0, 5.0)], 5.1, 4.9)
    assert angle == 0


def test_only_an_explicit_choice_replaces_the_deduced_axis(parts):
    """La correzione manuale si somma, la scelta esplicita sostituisce."""
    assert parts._placement_angle({}, 90) == 90
    assert parts._placement_angle({"model_rotation_delta_deg": -90}, 90) == 0
    # La scelta esplicita nomina insieme il modello e l'angolo a cui metterlo.
    assert parts._placement_angle({"model_rotation_deg": 0}, 90) == 0
    assert parts._placement_angle(
        {"model_rotation_deg": 0, "model_rotation_delta_deg": 90}, 90) == 90


class _Box:
    """Ingombro finto: al calcolo del verso servono solo questi quattro numeri."""

    def __init__(self, x_min, x_max, y_min, y_max):
        self.XMin, self.XMax, self.YMin, self.YMax = x_min, x_max, y_min, y_max
        self.XLength, self.YLength = x_max - x_min, y_max - y_min


class _Solid:
    def __init__(self, cx, cy):
        self.CenterOfMass = SimpleNamespace(x=cx, y=cy)


#: Ingombro 20 x 18 centrato nell'origine.
FRAME = _Box(-10, 10, -9, 9)


@pytest.mark.parametrize("cx,cy,expected", [
    (2.0, 0.0, (1, 0)),    # il materiale pende a destra
    (-2.0, 0.0, (-1, 0)),
    (0.0, 2.0, (0, 1)),
    (0.0, -2.0, (0, -1)),
    (2.0, 0.4, (1, 0)),    # una sola direzione: vince la dominante
    (0.05, 0.02, None),    # corpo simmetrico: nessuna direzione
    (0.0, 0.0, None),
])
def test_the_centroid_says_where_the_material_hangs(parts, cx, cy, expected):
    """@brief Criterio fisico, non un nome di file.

    Una feature asimmetrica -- un connettore coassiale, un boss, una
    smussatura -- sposta il centro di massa rispetto al centro dell'ingombro.
    Su un pezzo simmetrico lo spostamento tende a zero, e li' la risposta
    onesta e' "non lo so", non una direzione qualsiasi.
    """
    assert parts._native_landmark_direction(_Solid(cx, cy), FRAME) == expected


def test_a_solid_without_mass_does_not_take_down_the_generation(parts):
    class _NoMass:
        @property
        def CenterOfMass(self):
            raise RuntimeError("compound senza massa")

    assert parts._native_landmark_direction(_NoMass(), FRAME) is None


@pytest.mark.parametrize("native,inferred,side,expected", [
    # Materiale a destra nel modello, riferimento visto a destra: gia' a posto.
    ((1, 0), 0, "right", 0),
    # Stesso modello, riferimento visto a sinistra: mezzo giro.
    ((1, 0), 0, "left", 180),
    # Con l'asse gia' ruotato di 90 il materiale guarda in alto (frame CAD).
    ((1, 0), 90, "top", 0),
    ((1, 0), 90, "bottom", 180),
    # Y della fotografia cresce verso il basso, quella del CAD verso l'alto.
    ((0, 1), 0, "top", 0),
    ((0, -1), 0, "top", 180),
])
def test_the_verse_follows_the_observed_side(parts, native, inferred, side, expected):
    assert parts._verse_correction(native, inferred, side) == expected


@pytest.mark.parametrize("native,inferred,side", [
    (None, 0, "right"),        # il modello non dice da che parte pende
    ((1, 0), 0, None),         # la fotografia non ha visto alcun riferimento
    ((1, 0), 0, "sopra"),      # lato fuori vocabolario
    ((1, 0), 0, "top"),        # asse e verso si contraddicono
    ((0, 1), 0, "right"),
])
def test_no_correction_when_the_two_sides_cannot_be_matched(parts, native, inferred, side):
    """@brief Meglio un verso non verificato che uno inventato.

    Si cercano solo 0 e 180 di proposito: l'asse l'ha gia' deciso la misura
    dell'ingombro, e una correzione di 90 lo smentirebbe. Quando nessuno dei
    due porta il riferimento dalla parte osservata, i dati si contraddicono.
    """
    assert parts._verse_correction(native, inferred, side) is None
