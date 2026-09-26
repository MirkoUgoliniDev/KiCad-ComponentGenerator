"""@brief Su una scheda popolata a fori passanti tutti i pin stanno dentro la scheda.

Caso dell'utente del 2026-09-24 (TEST4, «il 3D è completamente sballato»):
l'AI ha dato `body_offset_y_mm = -12.7` (meta' larghezza, misurata dal centro
invece che dalla prima fila) e la fila 23-44 e' finita fuori dalla scheda.
La regola e' un vincolo fisico della famiglia dichiarata `aggregate_tht` (i
pin passano nei fori della scheda stessa), non una soglia tarata sul caso:
positivo e negativi la distinguono per struttura.
"""
from kicadgen.services.datasheet_extractor import ExtractedParams, fit_body_to_pin_field


def _params(**changes):
    base = dict(name="M", description="", tags="", confidence="medium", notes="",
                rows=2, cols=22, pitch_mm=2.54, row_spacing_mm=22.86,
                pad_diameter_mm=1.7, drill_mm=1.0,
                body_length_mm=63.3, body_width_mm=25.4, body_height_mm=3.2,
                body_offset_x_mm=-5.05, body_offset_y_mm=-12.7)
    base.update(changes)
    return ExtractedParams(**base)


def test_a_row_outside_the_board_is_recentred_and_declared_as_an_estimate():
    """@brief Positivo: i numeri di TEST4 -> offset Y ricentrato, X quotato intatto."""
    params = _params()
    messages = fit_body_to_pin_field(params, "aggregate_tht")
    assert params.body_offset_y_mm == -1.27
    assert params.body_offset_x_mm == -5.05
    assert len(messages) == 1 and "STIMATO" in messages[0] and "Y" in messages[0]
    assert "STIMATO" in params.notes
    # The recentred value is the program's estimate: its provenance says so.
    assert params.value_sources == {"body_offset_y_mm": "estimated"}
    assert params.values_to_review() == ["body_offset_y_mm"]


def test_consistent_offsets_are_left_alone():
    """@brief Negativo: pin gia' dentro (anche non centrati) -> nessun cambio, nessun messaggio."""
    params = _params(body_offset_y_mm=-2.0)
    assert fit_body_to_pin_field(params, "aggregate_tht") == []
    assert params.body_offset_y_mm == -2.0 and params.notes == ""
    assert params.value_sources is None


def test_a_single_component_may_have_legs_outside_its_body():
    """@brief Negativo: un componente singolo THT non e' una scheda: non si tocca."""
    params = _params(body_width_mm=5.0, body_offset_y_mm=-12.7)
    assert fit_body_to_pin_field(params, "single_tht") == []
    assert params.body_offset_y_mm == -12.7


def test_a_board_smaller_than_the_pin_field_is_only_reported():
    """@brief Negativo: nessun offset e' giusto se la scheda non contiene le file."""
    params = _params(body_width_mm=20.0)
    messages = fit_body_to_pin_field(params, "aggregate_tht")
    assert params.body_offset_y_mm == -12.7
    assert len(messages) == 1 and "incoerenti" in messages[0]


def test_the_extraction_applies_the_check_before_returning(tmp_path, monkeypatch):
    """@brief Integrazione offline: l'estrazione restituisce gia' l'offset corretto e lo dice.

    Nessuna chiamata vera: la risposta del modello e' quella di TEST4 per le
    quote della scheda, senza foto.
    """
    from kicadgen.services import datasheet_extractor as ext
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"%PDF-1.4 offline")
    payload = dict(layout="matrix", rows=2, cols=22, pitch_mm=2.54, row_spacing_mm=22.86,
                   pad_diameter_mm=1.7, drill_mm=1.0, confidence="medium",
                   body_length_mm=63.3, body_width_mm=25.4, body_height_mm=3.2,
                   body_offset_x_mm=-5.05, body_offset_y_mm=-12.7)
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    monkeypatch.setattr(ext, "call_tool", lambda **kw: dict(payload))
    progress = []
    result = ext.extract_pin_header_params([source], component_family="aggregate_tht",
                                           progress=progress.append)
    assert result.body_offset_y_mm == -1.27
    assert any("STIMATO" in line for line in progress)


# ── Generalita': schede di misure diverse, non solo TEST4 (utente, 2026-09-24) ──

import itertools

import pytest

from kicadgen.services.datasheet_extractor import _EXTRACTION_TOOL


@pytest.mark.parametrize("rows, cols, pitch, spacing, margin_x, margin_y", [
    (1, 5, 2.54, None, 1.0, 1.5),        # piccola, una fila
    (2, 8, 2.54, 7.62, 0.8, 0.6),        # DIP stretto
    (2, 30, 2.54, 45.72, 4.0, 3.0),      # grande, file lontane
    (3, 12, 2.0, 5.0, 2.2, 1.1),         # tre file, passo 2.0
    (2, 4, 5.08, 20.32, 0.0, 0.0),       # pin esattamente sul bordo
])
@pytest.mark.parametrize("wrong_offset_y", ["half_width", "positive", "too_negative", "right"])
def test_any_board_ends_with_every_pin_inside_or_untouched(rows, cols, pitch, spacing,
                                                          margin_x, margin_y, wrong_offset_y):
    """@brief Per qualunque misura: pin tutti dentro dopo la salvaguardia.

    Offset giusto -> nessun cambio; offset sbagliato in vari modi -> corretto
    solo l'asse sbagliato, e i centri dei pin cadono nella scheda.
    """
    span_x = (cols - 1) * pitch
    span_y = (rows - 1) * (spacing or pitch)
    length, width = span_x + 2 * margin_x, span_y + 2 * margin_y
    right_y = -margin_y
    offset_y = {"half_width": -width / 2, "positive": 1.0,
                "too_negative": -(width + 1.0), "right": right_y}[wrong_offset_y]
    params = _params(rows=rows, cols=cols, pitch_mm=pitch, row_spacing_mm=spacing,
                     body_length_mm=length, body_width_mm=width,
                     body_offset_x_mm=-margin_x, body_offset_y_mm=offset_y)
    messages = fit_body_to_pin_field(params, "aggregate_tht")
    y0 = params.body_offset_y_mm
    x0 = params.body_offset_x_mm
    tol = 1e-6
    assert x0 == -margin_x  # asse X giusto: mai toccato
    for r, c in itertools.product(range(rows), range(cols)):
        px, py = c * pitch, r * (spacing or pitch)
        assert x0 - tol <= px <= x0 + length + tol
        assert y0 - tol <= py <= y0 + width + tol
    inside_before = offset_y <= tol and offset_y + width >= span_y - tol
    assert (messages == []) == inside_before
    if inside_before:
        assert y0 == offset_y


def test_the_prompt_formula_and_the_safeguard_give_the_same_offset():
    """@brief La formula del prompt e la salvaguardia non si contraddicono.

    Positivo: la formula scritta nello schema, -(body_width_mm - distanza fra
    prima e ultima riga)/2, applicata alle quote della prova da' il valore che
    la salvaguardia ricava dal valore sbagliato. Negativo: lo schema non
    indica piu' «meta' della larghezza» come valore tipico, e non porta piu'
    l'esempio numerico della scheda campione (decisione dell'utente 26/09:
    niente numeri del campione nei prompt), solo la formula.
    """
    text = _EXTRACTION_TOOL["input_schema"]["properties"]["body_offset_y_mm"]["description"]
    assert "-(body_width_mm - distanza fra prima e ultima riga)/2" in text
    assert "PRIMA riga" in text
    assert "tipicamente meta' della larghezza" not in text
    assert not any(number in text for number in ("-1.27", "22.86", "25.4", "12.7"))
    params = _params()
    width, span = params.body_width_mm, params.row_spacing_mm * (params.rows - 1)
    fit_body_to_pin_field(params, "aggregate_tht")
    assert params.body_offset_y_mm == round(-(width - span) / 2, 3)


def test_the_fillet_edge_convention_points_corners_to_vertical_edges():
    """@brief Gli angoli visti dall'alto sono spigoli verticali, e lo schema lo dice.

    Coerenza col codice (convenzione 2, 2026-09-25): front = lato -Y del
    modello 3D, la faccia «Fronte» del cubo, cioe' il lato in basso della
    vista dall'alto.
    """
    from kicadgen.cad3d.annotations import EDGE_NAMES
    fillet = _EXTRACTION_TOOL["input_schema"]["properties"]["edge_fillets"]
    text = fillet["items"]["properties"]["edge"]["description"]
    vertical = [name for name in EDGE_NAMES
                if not name.startswith(("top-", "bottom-"))]
    assert sorted(vertical) == ["back-left", "back-right", "front-left", "front-right"]
    for name in vertical:
        assert name in text
    # Convenzione 2 (utente, 2026-09-25): i nomi del cubo 3D.
    assert "front = lato in BASSO" in text
    assert "NON usarli per gli angoli visti dall'alto" in " ".join(text.split())


# ── Pin presenti, non matrice dichiarata; asse X; vista dal basso (audit 2026-09-25) ──

def test_an_empty_end_column_is_not_a_pin_outside_the_board():
    """Negativo: 1x10 con i soli pin 1..6 (x 0..12.7), scheda 25 mm da -4.0 -> giusto, intatto."""
    params = _params(rows=1, cols=10, row_spacing_mm=None, populated_pins=[1, 2, 3, 4, 5, 6],
                     body_length_mm=25.0, body_width_mm=5.0,
                     body_offset_x_mm=-4.0, body_offset_y_mm=-2.5)
    assert fit_body_to_pin_field(params, "aggregate_tht") == []
    assert params.body_offset_x_mm == -4.0


def test_a_partial_field_outside_the_board_is_recentred_on_the_real_pins():
    """Positivo: stessi pin, offset sbagliato -> centrato sui pin PRESENTI (0..12.7)."""
    params = _params(rows=1, cols=10, row_spacing_mm=None, populated_pins=[1, 2, 3, 4, 5, 6],
                     body_length_mm=16.0, body_width_mm=5.0,
                     body_offset_x_mm=5.0, body_offset_y_mm=-2.5)
    messages = fit_body_to_pin_field(params, "aggregate_tht")
    assert len(messages) == 1 and "X" in messages[0]
    assert params.body_offset_x_mm == pytest.approx(-(16.0 - 12.7) / 2)


def test_an_explicit_pin_map_decides_the_field():
    """pin_map con la sola colonna 3 occupata: il campo e' x = 7.62, non 0..7.62."""
    params = _params(rows=1, cols=4, row_spacing_mm=None, body_length_mm=4.0, body_width_mm=4.0,
                     body_offset_x_mm=5.62, body_offset_y_mm=-2.0,
                     pin_map=[{"pin": 1, "row": 0, "col": 3}])
    assert fit_body_to_pin_field(params, "aggregate_tht") == []


def test_the_x_axis_is_checked_like_the_y_axis():
    """Asse X: offset positivo (scheda a destra del pin 1) -> ricentrato, Y intatto."""
    params = _params(body_offset_x_mm=2.0, body_offset_y_mm=-1.27)
    messages = fit_body_to_pin_field(params, "aggregate_tht")
    assert len(messages) == 1 and "X" in messages[0]
    assert params.body_offset_x_mm == pytest.approx(-(63.3 - 21 * 2.54) / 2)
    assert params.body_offset_y_mm == -1.27


@pytest.mark.parametrize("view, expected", [("bottom", -5.05), ("top", -3.0), (None, -3.0)])
def test_a_bottom_view_offset_is_mirrored_with_the_columns(view, expected):
    """Margini 3.0 (sinistra) e 5.05 (destra) nel disegno: dal basso, visto dall'alto
    il margine sinistro e' 5.05. Vista dall'alto o sconosciuta: invariato."""
    from kicadgen.services.datasheet_extractor import _offset_x_seen_from_top
    length = 21 * 2.54 + 3.0 + 5.05
    assert _offset_x_seen_from_top(-3.0, length, 22, 2.54, view) == pytest.approx(expected)
    assert _offset_x_seen_from_top(None, length, 22, 2.54, view) is None
