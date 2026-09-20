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


def test_the_automatic_verse_correction_adds_to_the_axis(parts):
    """@brief 90 gradi d'asse piu' 180 di verso fanno 270, non 180.

    E' il difetto che teneva storto il modulo: la correzione automatica veniva
    scritta come angolo assoluto e cancellava l'asse dedotto. Un solo campo
    resta assoluto, ed e' la scelta esplicita dell'utente, che nomina insieme
    il modello e l'angolo a cui metterlo.
    """
    assert parts._placement_angle({"model_rotation_native_delta_deg": 180}, 90) == 270
    assert parts._placement_angle({}, 90) == 90
    assert parts._placement_angle({"model_rotation_delta_deg": -90}, 90) == 0
    assert parts._placement_angle(
        {"model_rotation_native_delta_deg": 180, "model_rotation_delta_deg": 90}, 90) == 360
    # La scelta esplicita sostituisce la deduzione, e i delta si sommano a lei.
    assert parts._placement_angle({"model_rotation_deg": 0}, 90) == 0
    assert parts._placement_angle(
        {"model_rotation_deg": 0, "model_rotation_native_delta_deg": 180}, 90) == 180
