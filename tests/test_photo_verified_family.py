"""@brief Offline family-before-package verification and stored-prompt contracts."""
from copy import deepcopy
import json
from unittest.mock import Mock

import numpy as np
import pytest

from kicadgen.services import photo_grounding as grounding, openrouter
from kicadgen.services.datasheet_extractor import COMPONENT_KINDS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Family tests must not use paid APIs")
    monkeypatch.setattr(openrouter, "call_tool", forbidden)
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", forbidden)


def run(verdict, *, prompt=None, original=None, cache=None):
    original = original or dict(_id=7, kind="generic", _detected_kind="diode", name="prior",
        _box=(.2, .2, .4, .4), length_mm=2.5, width_mm=1.2)
    call = Mock(return_value={"components": [dict(id=7, present=True, confidence="high",
                                                  package="", **verdict)]})
    logs = []
    result = grounding.mechanical_envelopes(np.zeros((100, 200, 3), np.uint8), [original],
        "offline", call, logs.append, cache, verification_prompt=prompt)
    return result[0], call.call_args.kwargs if call.called else None, logs


@pytest.mark.parametrize("kind", COMPONENT_KINDS)
def test_explicit_supported_family_overrides_hint_and_public_kind(kind):
    result, request, logs = run(dict(kind=kind, package_evidence="Visible physical family features"))
    assert result["kind"] == result["_detected_kind"] == kind
    assert result["name"] == "Ingombro 7" and result["marking"] == ""
    assert "_type_verified" not in result
    assert result["_box"] == (.2, .2, .4, .4)
    rows = json.loads(request["content_blocks"][1]["text"].split("\nMeasurements:\n")[1])
    assert rows[0]["tentative_kind"] == "diode"
    assert any("evidenza=Visible physical family features" in message for message in logs)


@pytest.mark.parametrize("verdict", [{}, {"kind": None}, {"kind": []}, {"kind": True}, {"kind": "invented"}])
def test_omitted_or_invalid_family_remains_generic_and_prior_is_only_hint(verdict):
    result, _, logs = run(verdict)
    assert result["kind"] == "generic" and result["_detected_kind"] == "diode"
    assert any("tipo fisico=sconosciuto; ipotesi diode non verificata" in message for message in logs)
    assert any("package=sconosciuto" in message for message in logs)


def test_family_schema_optional_and_reasoning_budget_is_explicit():
    _, request, logs = run(dict(kind="resistor"))
    schema = request["tool"]["function"]["parameters"]["properties"]["components"]["items"]
    assert schema["properties"]["kind"]["enum"] == list(COMPONENT_KINDS)
    assert set(schema["required"]) == {"id", "present", "confidence", "package"}
    assert request["max_tokens"] == grounding.MECHANICAL_MAX_TOKENS == 16384
    assert any("16384 token" in message for message in logs)
    prompt = request["system_prompt"]
    assert prompt.index("FIRST classify") < prompt.index("THEN combine")
    assert "Do NOT identify electrical type" not in prompt
    assert "A marking alone cannot establish electrical identity" in prompt
    assert "override it when visible evidence disagrees" in request["content_blocks"][1]["text"]


def test_exact_legacy_clause_migrates_without_rewriting_other_custom_instructions():
    clause = "Do NOT identify electrical type, part number, markings, values or circuit function."
    selected = "Custom prefix. " + clause + " Keep custom suffix."
    _, request, _ = run({}, prompt=selected)
    expected = selected.replace(clause, "Do NOT invent exact electrical function, part numbers, markings or values.")
    assert request["system_prompt"] == expected + grounding.MECHANICAL_EVIDENCE_PROMPT
    custom = "Keep my exact custom sentence about electrical type."
    _, request, _ = run({}, prompt=custom)
    assert request["system_prompt"] == custom + grounding.MECHANICAL_EVIDENCE_PROMPT


def test_valid_family_survives_cache_without_changing_caller(tmp_path):
    original = dict(_id=7, kind="generic", _detected_kind="diode", name="prior",
                    _box=(.2, .2, .4, .4), length_mm=2.5, width_mm=1.2)
    before = deepcopy(original)
    first, _, _ = run(dict(kind="capacitor"), original=original, cache=tmp_path)
    second, _, _ = run(dict(kind="resistor"), original=original, cache=tmp_path)
    # Same request is served from cache, never the differently scripted response.
    assert second == first and second["kind"] == "capacitor"
    assert original == before


def test_observed_marking_is_evidence_not_rendered_text_or_forced_identity():
    evidence = "Observed 'J?Y'; three leads; transistor possible, exact identity unresolved."
    result, request, logs = run(dict(kind="generic", package_evidence=evidence))
    assert result["package_evidence"] == evidence
    assert result["marking"] == "" and result["kind"] == "generic"
    assert any(evidence in line for line in logs)
    prompt = request["system_prompt"]
    assert "PCB silkscreen" in prompt and "three-terminal" in prompt
    assert "1.0x0.5 mm" in prompt and "pads or margins" in prompt
    assert "do not invent text" in prompt


def test_previous_no_marking_prompt_is_migrated_in_memory():
    old = "Do NOT infer exact electrical function, part number, markings or values."
    _, request, _ = run({}, prompt="Keep custom. " + old)
    assert old not in request["system_prompt"]
    assert request["system_prompt"].startswith("Keep custom. Do NOT invent")
