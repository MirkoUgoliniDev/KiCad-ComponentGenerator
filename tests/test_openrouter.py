"""Test del client OpenRouter e delle tipologie di componente.

Nessuna chiamata di rete: si collauda il parsing dell'elenco modelli, la
lettura degli argomenti dello strumento da una risposta gia' pronta e la
gestione del blocco "tipologia componente" nel prompt specifico.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.services import component_types
from kicadgen.services.openrouter import (
    API_KEY_ENV, OpenRouterError, _model_from_payload, _parse_price, api_key,
    tool_arguments,
)


# ── Elenco modelli ──────────────────────────────────────────────────

def test_price_converted_to_dollars_per_million_tokens():
    assert _parse_price("0.000003") == pytest.approx(3.0)


def test_sentinel_negative_price_counts_as_free():
    # I modelli "auto" espongono -1000000 come prezzo: non e' un costo.
    assert _parse_price("-1000000") == 0.0


def test_model_parsed_with_capabilities():
    model = _model_from_payload({
        "id": "vendor/model-1",
        "name": "Vendor: Model 1",
        "context_length": 200000,
        "architecture": {"input_modalities": ["text", "image", "file"]},
        "pricing": {"prompt": "0.000002", "completion": "0.00001"},
        "supported_parameters": ["tools", "max_tokens"],
    })
    assert model.id == "vendor/model-1"
    assert model.prompt_usd_per_mtok == pytest.approx(2.0)
    assert model.completion_usd_per_mtok == pytest.approx(10.0)
    assert model.supports_images and model.supports_pdf and model.supports_tools
    assert model.usable_for_datasheets()


def test_model_without_pdf_is_not_usable_for_datasheets():
    model = _model_from_payload({
        "id": "vendor/text-only",
        "architecture": {"input_modalities": ["text"]},
        "pricing": {"prompt": "0", "completion": "0"},
        "supported_parameters": ["tools"],
    })
    assert not model.usable_for_datasheets()
    assert model.is_free


# ── Chiave e risposta ───────────────────────────────────────────────

def test_api_key_missing_raises(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    with pytest.raises(OpenRouterError, match=API_KEY_ENV):
        api_key()


def test_tool_arguments_decodes_json_string():
    payload = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {"name": "my_tool", "arguments": '{"rows": 2, "cols": 4}'}
                }]
            }
        }]
    }
    assert tool_arguments(payload, "my_tool") == {"rows": 2, "cols": 4}


def test_tool_arguments_without_tool_call_raises():
    payload = {"choices": [{"message": {"content": "non ho usato lo strumento"}}]}
    with pytest.raises(OpenRouterError, match="strumento"):
        tool_arguments(payload, "my_tool")


def test_tool_arguments_reports_api_error():
    payload = {"error": {"message": "credito esaurito"}}
    with pytest.raises(OpenRouterError, match="credito esaurito"):
        tool_arguments(payload, "my_tool")


# ── Tipologie di componente ─────────────────────────────────────────

def test_four_component_types_available():
    keys = [ct.key for ct in component_types.COMPONENT_TYPES]
    assert keys == [
        "single_tht", "single_smd", "aggregate_tht", "aggregate_castellated"
    ]


def test_apply_block_inserts_type_prompt():
    text = component_types.apply_block("", component_types.get("single_smd"))
    assert component_types.BLOCK_START in text
    assert component_types.BLOCK_END in text
    assert "SMD" in text


def test_changing_type_replaces_block_and_keeps_user_notes():
    first = component_types.apply_block("", component_types.get("single_tht"))
    with_notes = first + "\n\nIl case e' metallico."
    second = component_types.apply_block(
        with_notes, component_types.get("aggregate_castellated")
    )
    assert second.count(component_types.BLOCK_START) == 1
    assert "castellated" in second
    assert "Il case e' metallico." in second
    assert "FORI PASSANTI (THT)" not in second


def test_selecting_no_type_removes_block_only():
    text = component_types.apply_block("Nota mia.", component_types.get("single_tht"))
    cleaned = component_types.apply_block(text, None)
    assert cleaned == "Nota mia."
