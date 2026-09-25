"""@brief Offline checks of photo cache and enum validation (audit D5, D6, D9 for the top inventory).

All model calls are injected fakes; nothing reaches OpenRouter.
"""
import json

import numpy as np
import pytest

pytest.importorskip("cv2")

from kicadgen.services import photo_grounding as grounding
from kicadgen.services import photo_top_inventory as top
from kicadgen.services.ai_diagnostics import CallCancelled

KINDS = ("generic", "resistor", "capacitor")


def _locate_kwargs():
    return dict(model="offline", system_prompt="p", content_blocks=[],
                tool=grounding.grounding_tool(KINDS), max_tokens=10, has_pdf=False)


def _classify_kwargs():
    return dict(model="offline", system_prompt="p", content_blocks=[],
                tool=grounding.classification_tool(KINDS), max_tokens=10, has_pdf=False)


# ── D5: empty answers in the response cache ────────────────────────────────

def test_empty_localization_is_cached_with_an_explicit_marker_and_log(tmp_path):
    log = []
    calls = []
    call = lambda **kw: calls.append(kw) or {"components": []}
    assert grounding.cached_call(call, _locate_kwargs(), tmp_path, "zona", log.append) == {"components": []}
    [record] = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*.json")]
    assert record["empty_response"] is True
    assert any("vuota" in line for line in log)
    log.clear()
    again = grounding.cached_call(lambda **kw: pytest.fail("must replay"), _locate_kwargs(),
                                  tmp_path, "zona", log.append)
    assert again == {"components": []} and len(calls) == 1
    assert any("VUOTA" in line for line in log)


def test_a_non_empty_localization_carries_no_empty_marker(tmp_path):
    entry = {"box_2d": [100, 100, 200, 200], "label": "Resistor", "kind": "resistor"}
    log = []
    grounding.cached_call(lambda **kw: {"components": [entry]}, _locate_kwargs(), tmp_path,
                          "zona", log.append)
    [record] = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*.json")]
    assert "empty_response" not in record
    assert not any("vuota" in line.lower() for line in log)


def test_an_empty_classification_is_never_cached(tmp_path):
    """Negative, structurally different tool: an empty verdict list is retried."""
    calls = []
    call = lambda **kw: calls.append(1) or {"components": []}
    for _ in range(2):
        grounding.cached_call(call, _classify_kwargs(), tmp_path, "verifica", lambda m: None)
    assert len(calls) == 2 and not list(tmp_path.glob("*.json"))


# ── D6: enums and numeric types before copying ─────────────────────────────

def _component():
    return dict(name="R", kind="resistor", _id=1, _box=(.2, .2, .4, .4),
                x_mm=0, y_mm=0, length_mm=2, width_mm=1, height_mm=.5)


def test_height_as_a_numeric_string_is_rejected():
    vote = dict(id=1, is_component=True, is_complete_package=True, name="R", kind="resistor",
                height_mm="1.5", rotation_deg=0, color_hex="#112233", confidence="high",
                observation="two terminals", package="")
    [result] = grounding.apply_classifications([_component()], {"components": [vote]},
                                               KINDS, 40, 20, lambda m: None)
    assert result["_uncertain"] and result["_classification_validated"] is False
    [ok] = grounding.apply_classifications([_component()], {"components": [dict(vote, height_mm=1.5)]},
                                           KINDS, 40, 20, lambda m: None)
    assert ok["_classification_validated"] is True and ok["height_mm"] == 1.5


@pytest.mark.parametrize("confidence,expected,uncertain", [
    ("certain", "low", True), (None, "low", True), (["high"], "low", True), ("medium", "medium", False)])
def test_mechanical_verdict_confidence_is_enum_checked(confidence, expected, uncertain):
    photo = np.full((60, 120, 3), 90, np.uint8)
    verdict = {"id": 1, "present": True, "package": ""}
    if confidence is not None:
        verdict["confidence"] = confidence
    call = lambda **kw: {"components": [verdict]}
    log = []
    [result] = grounding.mechanical_envelopes(photo, [_component()], "offline", call,
                                              log.append, None)
    assert result["confidence"] == expected and result["_uncertain"] is uncertain


@pytest.mark.parametrize("confidence,expected", [("sure", "low"), (7, "low"), ("high", "high")])
def test_top_inventory_decode_checks_the_confidence_enum(confidence, expected):
    response = {"components": [dict(box=[10, 10, 20, 20], complete=True, confidence=confidence,
                                    evidence="body")]}
    [row] = top._decode(response, (0, 0, 100, 50), (100, 50), (100, 50), "t", lambda m: None)
    assert row["confidence"] == expected


# ── D9: the top inventory never swallows a cancellation ────────────────────

def test_top_inventory_propagates_a_cancellation():
    class Stop(CallCancelled):
        pass

    def call(**kw):
        raise Stop("user")
    with pytest.raises(Stop):
        top.detect_top_bodies(np.zeros((50, 100, 3), np.uint8), "offline", call, lambda m: None)


def test_top_inventory_still_downgrades_an_ordinary_failure():
    """Negative: an ordinary error keeps the region unaudited, as before."""
    log = []

    def call(**kw):
        raise ValueError("offline failure")
    result = top.detect_top_bodies(np.zeros((50, 100, 3), np.uint8), "offline", call,
                                   log.append, residual=False)
    assert result["failed_stages"] >= 1 and result["components"] == []
    assert any("ValueError" in line for line in log)
