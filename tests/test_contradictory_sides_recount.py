"""@brief One paid recount when the castellated sides of an answer contradict themselves.

Difetto trovato dall'utente il 2026-09-26: con gli stessi documenti il modello
aveva risposto una volta 22 contatti per lato, un'altra 28 con 22 nomi, su un
bordo di 63.3 mm dove 28 contatti a 2.54 mm non stanno. Il controllo
dell'ingombro rifiutava giustamente la risposta, ma l'analisi finiva li' e la
spesa era persa. Decisione dell'utente: una sola chiamata di ricontrollo,
solo quando la risposta si contraddice.

Le contraddizioni sono strutturali (conteggio contro nomi dello stesso lato,
centri dei contatti contro il bordo che li porta), mai soglie tarate sul pezzo.
Ogni caso positivo ha il suo negativo: una risposta coerente non paga nulla.
"""
from unittest.mock import Mock

import pytest

from kicadgen.services.datasheet_extractor import (
    ExtractionError, _params_from_tool_arguments, _side_contradictions, _validate_main_response,
)
from kicadgen.services.openrouter import OpenRouterError


def side(edge, count, first_pin, names=None, **extra):
    entry = dict(edge=edge, count=count, pitch_mm=2.54, first_pin=first_pin,
                 contact_style="half_and_through", **extra)
    if names is not None:
        entry["pin_names"] = [f"P{i}" for i in range(names)]
    return entry


def answer(sides, length=63.3, width=25.4):
    return dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=length, body_width_mm=width, castellated_sides=sides)


def validate(data, call, messages=None):
    _validate_main_response(data, [], call, None, None, False,
                            messages.append if messages is not None else None)


# ── Le contraddizioni ───────────────────────────────────────────────────


def test_count_against_names_and_against_the_edge_are_both_reported():
    problems = _side_contradictions([side("top", 28, 1, names=22)], 63.3, 25.4)
    assert any("count=28 ma pin_names ha 22" in p for p in problems)
    assert any("68.58" in p and "63.3" in p for p in problems)


def test_a_coherent_side_has_no_contradiction():
    """Negativo: 22 contatti, 22 nomi, 53.34 mm di centri su 63.3 mm."""
    assert _side_contradictions([side("top", 22, 1, names=22)], 63.3, 25.4) == []
    # Senza nomi non c'e' nulla da confrontare, e un bordo ignoto non limita.
    assert _side_contradictions([side("top", 28, 1)], None, None) == []


def test_left_and_right_edges_are_measured_along_the_width():
    """Stessa riga, bordo diverso: 11 contatti a 2.54 (25.4 mm) stanno in 63.3, non in 20."""
    assert _side_contradictions([side("top", 11, 1)], 63.3, 20.0) == []
    assert _side_contradictions([side("left", 11, 1)], 63.3, 20.0) != []


# ── La chiamata di ricontrollo ──────────────────────────────────────────


def test_a_contradictory_answer_is_recounted_once_and_the_recount_is_used():
    data = answer([side("top", 28, 1, names=22), side("bottom", 28, 29, names=23)])
    recount = dict(drawing_unit="mm", castellated_sides=[
        side("top", 22, 1, names=22, value_sources=dict(pitch_mm="read")),
        side("bottom", 22, 23, names=22, value_sources=dict(pitch_mm="read"))])
    call = Mock(return_value=recount)
    messages = []
    validate(data, call, messages)
    assert call.call_count == 1
    prompt = call.call_args.kwargs["content_blocks"][-1]["text"]
    # Il modello riceve i numeri esatti e il bordo, non un generico «riprova».
    assert "count=28 ma pin_names ha 22" in prompt and "body_length_mm=63.3" in prompt
    params = _params_from_tool_arguments(data)
    assert [s["count"] for s in params.castellated_sides] == [22, 22]
    assert [len(s["pin_names"]) for s in params.castellated_sides] == [22, 22]
    assert params.value_sources["castellated_sides.top.pitch_mm"] == "read"
    assert "ricontati" in params.notes
    assert any("una chiamata in piu'" in m for m in messages)


def test_a_coherent_answer_costs_no_extra_call():
    """Negativo: stessi lati ma coerenti -> nessuna chiamata."""
    call = Mock(side_effect=AssertionError("nessuna chiamata attesa"))
    data = answer([side("top", 22, 1, names=22), side("bottom", 22, 23, names=22)])
    validate(data, call)
    assert call.call_count == 0


def test_a_recount_still_too_long_is_an_error_without_a_third_call():
    data = answer([side("top", 28, 1, names=22)])
    call = Mock(return_value=dict(castellated_sides=[side("top", 27, 1, names=27)]))
    with pytest.raises(ExtractionError, match="27 contatti a passo 2.54 mm"):
        validate(data, call)
    assert call.call_count == 1


def test_names_still_of_the_wrong_length_are_dropped_as_before():
    """Il ricontrollo non e' riuscito sui nomi ma i contatti stanno: nomi scartati, niente errore."""
    data = answer([side("top", 22, 1, names=21)])
    call = Mock(return_value=dict(castellated_sides=[side("top", 22, 1, names=20)]))
    validate(data, call)
    params = _params_from_tool_arguments(data)
    assert call.call_count == 1
    assert "pin_names" not in params.castellated_sides[0]
    assert "nomi scartati" in params.notes


@pytest.mark.parametrize("recount", [
    dict(drawing_unit="inch", castellated_sides=[side("top", 22, 1)]),   # altra unita'
    dict(castellated_sides=[]),                                            # nessun lato
    "testo libero",                                                        # non strutturata
])
def test_an_unusable_recount_is_refused(recount):
    data = answer([side("top", 28, 1)])
    with pytest.raises(ExtractionError, match="non ha restituito lati utilizzabili"):
        validate(data, Mock(return_value=recount))


def test_a_failed_recount_call_is_an_extraction_error():
    data = answer([side("top", 28, 1)])
    with pytest.raises(ExtractionError, match="Ricontrollo dei contatti non riuscito"):
        validate(data, Mock(side_effect=OpenRouterError("rete")))
