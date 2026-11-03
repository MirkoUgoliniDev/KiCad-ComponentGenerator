"""@brief The dedicated body-quote reading inside the single-part analysis (user, 2026-11-03).

Three offline calls faked in order: the main answer, the pad chains, the
printed body quotes. Positive: a main answer without a height gets the
side view's 16.9 mm. Negative twin: a main answer with an invented 18.9 mm
against the printed 16.9 mm ends with NO height, so no STEP is built on a
value nothing confirms.
"""
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext

from test_single_component_prompt import _CHAINS_ANSWER, _drawing, _single, offline  # noqa: F401

#: The printed quotes agree with the fake main answer's plan sizes (20.5 x 10.5).
_BODY = {"drawing_unit": "mm", "body_length": 20.5, "body_width": 10.5, "body_height": 16.9,
         "lead_length": 3.5}


def _extract(monkeypatch, main, body, tmp_path, logs):
    call = Mock(side_effect=[main, _CHAINS_ANSWER, body])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params(
        [_drawing(tmp_path)], model="vendor/main", component_family="single_tht",
        pad_model="vendor/reader", document_roles={"quote.png": ["top"]}, progress=logs.append)
    return call, params


def test_a_missing_height_is_taken_from_the_printed_side_view(offline, monkeypatch, tmp_path):
    logs = []
    call, params = _extract(monkeypatch, _single(body_height_mm=None), _BODY, tmp_path, logs)
    assert call.call_args_list[2].kwargs["tool"]["function"]["name"] == "report_body_dimensions"
    assert call.call_args_list[2].kwargs["model"] == "vendor/reader"
    assert params.body_height_mm == pytest.approx(16.9)
    assert params.pin_length_3d_mm == pytest.approx(3.5)
    assert params.body_length_mm == pytest.approx(20.5)
    assert any("Quote del corpo confermate" in line and "lunghezza" in line for line in logs)
    assert any("assenti nella risposta principale" in line and "altezza" in line for line in logs)


def test_an_invented_height_contradicted_by_the_printed_quote_builds_no_body(offline, monkeypatch,
                                                                              tmp_path):
    logs = []
    main = _single(body_height_mm=18.9, value_sources={"body_length_mm": "read", "body_height_mm": "read"})
    _call, params = _extract(monkeypatch, main, _BODY, tmp_path, logs)
    assert params.body_height_mm is None
    assert params.body_length_mm == pytest.approx(20.5)
    assert any("18.90 mm contro 16.90 mm" in line for line in logs)


def test_a_failed_body_reading_keeps_the_main_answer(offline, monkeypatch, tmp_path):
    """Best effort: the third call failing never loses the paid main answer or its height."""
    logs = []
    call = Mock(side_effect=[_single(body_height_mm=20.5), _CHAINS_ANSWER,
                             ext.OpenRouterError("down")])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params(
        [_drawing(tmp_path)], model="vendor/main", component_family="single_tht",
        document_roles={"quote.png": ["top"]}, progress=logs.append)
    assert params.body_height_mm == pytest.approx(20.5)
    assert any("quote del corpo non riuscita" in line for line in logs)


def test_an_estimated_height_is_replaced_by_the_printed_quote_end_to_end(offline, monkeypatch, tmp_path):
    """Positive twin: the same 18.9 declared «estimated» yields to the printed 16.9."""
    logs = []
    main = _single(body_height_mm=18.9, value_sources={"body_length_mm": "read", "body_height_mm": "estimated"})
    _call, params = _extract(monkeypatch, main, _BODY, tmp_path, logs)
    assert params.body_height_mm == pytest.approx(16.9)
    assert any("sostituisce la stima" in line for line in logs)
