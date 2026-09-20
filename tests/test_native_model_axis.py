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


#: Ingombro 20 x 18 centrato nell'origine.
FRAME = _Box(-10, 10, -9, 9)


@pytest.mark.parametrize("fraction,dx,dy,expected", [
    # Il modulo vero: residuo al 15,9%, spostato su Y di tre volte l'altro asse.
    (0.1586, 0.0567, -0.1808, (0, -1)),
    (0.30, 0.40, 0.0, (1, 0)),
    (0.30, -0.40, 0.05, (-1, 0)),
    (0.30, 0.0, 0.40, (0, 1)),
    # Il ricettacolo USB-C vero: residuo al 46%, spostato di mezzo semilato.
    (0.4599, -0.009, 0.514, (0, 1)),
    # Corpo simmetrico: il residuo e' rumore del boolean, non una feature.
    (0.004, 0.40, 0.0, None),
    (0.0, 0.0, 0.0, None),
    # Il SOT-223 vero: il residuo c'e' (4,9%) ma sta al centro, perche' si
    # divide in due lobi identici. L'asimmetria e' bilanciata fra i due capi e
    # non li distingue: la risposta onesta e' "non lo so", non il segno del
    # rumore numerico.
    (0.0485, -1e-6, 2e-6, None),
    (0.30, 0.0, 0.02, None),
    # Feature in un angolo: le due componenti si equivalgono e un angolo non si
    # confronta con un lato dichiarato.
    (0.30, 0.30, 0.28, None),
    (0.30, 0.20, -0.30, None),
])
def test_the_half_turn_residue_names_a_side_or_nothing(parts, fraction, dx, dy, expected):
    """@brief L'asimmetria e' cio' che non sopravvive a mezzo giro.

    Il baricentro del solido INTERO non basta: su un modulo schermato la massa
    simmetrica del PCB e dello schermo diluisce il connettore fino all'1,5%,
    sotto qualunque soglia onesta. Il residuo dello stesso modulo pesa il
    15,9%. E' una definizione, non una taratura: su un corpo simmetrico va a
    zero da solo.
    """
    assert parts._asymmetry_direction(fraction, dx, dy) == expected


class _Shape:
    """Solido finto: copia, ruota, sottrae. Quanto basta a provare il cablaggio."""

    def __init__(self, volume, residual_volume, residual_centre):
        self.Volume = volume
        self._residual_volume = residual_volume
        self._residual_centre = residual_centre

    def copy(self):
        return self

    def rotate(self, *args):
        return None

    def cut(self, other):
        return SimpleNamespace(Volume=self._residual_volume,
                               CenterOfMass=SimpleNamespace(*self._residual_centre))


def test_the_measurement_is_wired_to_the_residue_not_to_the_whole_solid(parts):
    # Residuo 16% del volume, spostato di -1.8 su un semilato di 9: -0.20.
    shape = _Shape(400.0, 64.0, SimpleNamespace(x=0.5, y=-1.8))
    shape.cut = lambda other: SimpleNamespace(
        Volume=64.0, CenterOfMass=SimpleNamespace(x=0.5, y=-1.8))
    assert parts._native_landmark_direction(shape, FRAME) == (0, -1)


def test_a_failed_boolean_does_not_take_down_the_generation(parts):
    class _Broken(_Shape):
        def cut(self, other):
            raise RuntimeError("boolean fallito")

    assert parts._native_landmark_direction(_Broken(400.0, 0, (0, 0)), FRAME) is None


def test_a_solid_without_volume_says_nothing(parts):
    shape = _Shape(0.0, 0.0, (0, 0))
    shape.cut = lambda other: SimpleNamespace(
        Volume=0.0, CenterOfMass=SimpleNamespace(x=0.0, y=0.0))
    assert parts._native_landmark_direction(shape, FRAME) is None


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
