"""Targeted style recovery must never guess missing visual evidence."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as de
# A real one-page PDF: fake bytes are refused since review E1 (2026-10-27).
from minimal_pdf import MINIMAL_PDF


def observation(**changes):
    return dict(edge="left", half_hole_visible=True, through_hole_visible=True,
                confidence="high", evidence="Photo: open plated edge and enclosed hole.", **changes)


def run(rows):
    data = {"layout": "castellated", "castellated_sides": [
        {"edge": "left", "count": 3, "pitch_mm": 2.54, "first_pin": 1,
         "contact_style": None},
        {"edge": "right", "count": 2, "pitch_mm": 5.08, "first_pin": 4,
         "contact_style": "through_hole"}]}
    before = deepcopy(data)
    call = Mock(return_value={"sides": rows})
    de._recheck_missing_contact_styles(data, [{"type": "image_url"}], call, "test")
    return data, before, call


@pytest.mark.parametrize("half,through,style", [
    (True, True, "half_and_through"), (True, False, "half_hole"),
    (False, True, "through_hole")])
def test_clear_features_recover_only_missing_style(half, through, style):
    row = observation()
    row.update(half_hole_visible=half, through_hole_visible=through)
    data, before, call = run([row])
    side = data["castellated_sides"][0]
    assert side["contact_style"] == style
    for key in ("count", "pitch_mm", "first_pin"):
        assert side[key] == before["castellated_sides"][0][key]
    assert data["castellated_sides"][1] == before["castellated_sides"][1]
    assert call.call_count == 1
    assert call.call_args.kwargs["tool"]["function"]["name"] == "observe_contact_styles"


@pytest.mark.parametrize("changes", [
    {"confidence": "medium"}, {"evidence": " "},
    {"half_hole_visible": None}, {"through_hole_visible": None},
    {"half_hole_visible": 1}, {"half_hole_visible": False, "through_hole_visible": False},
    {"edge": "right"}, {"edge": "unknown"}])
def test_unclear_or_unrequested_reply_keeps_manual_fallback(changes):
    row = observation()
    row.update(changes)
    data, before, _ = run([row])
    assert data == before


def test_duplicate_edge_cannot_be_adopted():
    data, before, _ = run([observation(), observation()])
    assert data == before


def test_no_missing_style_or_no_image_does_not_call_provider():
    call = Mock()
    for sides, blocks in [([], [{"type": "image_url"}]),
                          ([{"edge": "left", "contact_style": None}], [])]:
        de._recheck_missing_contact_styles(
            {"layout": "castellated", "castellated_sides": sides}, blocks, call, "test")
    call.assert_not_called()


def test_provider_error_preserves_manual_fallback():
    data = {"layout": "castellated", "castellated_sides": [
        {"edge": "left", "contact_style": None}]}
    before = deepcopy(data)
    call = Mock(side_effect=de.OpenRouterError("unavailable"))
    de._recheck_missing_contact_styles(data, [{"type": "image_url"}], call, "test")
    assert data == before


def test_extraction_adopts_style_and_skips_manual_dialog_contract(tmp_path, monkeypatch):
    source = tmp_path / "board.pdf"
    source.write_bytes(MINIMAL_PDF)
    call = Mock(side_effect=[
        {"layout": "castellated", "castellated_sides": [
            {"edge": "left", "count": 2, "pitch_mm": 2.54}]},
        {"sides": [observation()]}])
    monkeypatch.setattr(de, "api_key", lambda: "offline")
    monkeypatch.setattr(de, "check_credit", lambda **kw: None)
    monkeypatch.setattr(de, "call_tool", call)
    result = de.extract_pin_header_params([source])
    assert call.call_count == 2
    assert result.castellated_sides[0]["contact_style"] == "half_and_through"
    assert result.unobserved_contact_style_edges() == []
    assert "Tipo di contatto ricontrollato" in result.notes
