"""@brief Dedicated prompt and tool of the single-component analysis (user, 2026-10-10).

One part reads its drawing pad by pad with its own system prompt; boards keep
the main prompt and tool. Three prompts live behind one selector in the LLM
settings dialog. Every paid call is a Mock.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import math
from pathlib import Path
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services.datasheet_extractor import (
    DEFAULT_PIN_NUMBERING_PROMPT, DEFAULT_SINGLE_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT,
    ExtractionError, SINGLE_COMPONENT_FAMILIES, single_answer_to_data)

PADS = [{"number": "1", "x_mm": 0.0, "y_mm": 0.0, "pad_type": "thru_hole", "drill_mm": 1.0,
         "pad_length_mm": 1.8, "pad_width_mm": 1.8},
        {"number": "2", "x_mm": 17.78, "y_mm": 0.0, "pad_type": "thru_hole", "drill_mm": 1.0,
         "pad_length_mm": 1.8, "pad_width_mm": 1.8},
        {"number": "5", "x_mm": 5.08, "y_mm": 2.54, "pad_type": "thru_hole", "drill_mm": 1.2,
         "pad_length_mm": 2.0, "pad_width_mm": 2.0}]


def _single(**extra):
    answer = dict(drawing_unit="mm", contacts_view="bottom", confidence="high",
                  body_length_mm=20.5, body_width_mm=10.5, body_height_mm=20.5,
                  lead_exit="bottom", lead_diameter_mm=0.5, pads_origin="pin1", pads=list(PADS),
                  value_sources={"body_length_mm": "read"})
    answer.update(extra)
    return answer


# -- answer mapping ------------------------------------------------------------------

def test_pads_become_explicit_package_geometry_in_the_declared_view():
    data = single_answer_to_data(_single())
    geometry = data["package_geometry"]
    assert data["layout"] == "matrix" and "rows" not in data
    assert geometry["source_view"] == "bottom" and geometry["origin"] == "pin1"
    assert geometry["family"] == "unknown" and geometry["needs_review"] is True
    assert [p["number"] for p in geometry["pins"]] == ["1", "2", "5"]
    assert geometry["pins"][0]["shape"] == "circle" and geometry["pins"][0]["drill_mm"] == 1.0
    assert geometry["body_length_mm"] == 20.5 and data["lead_exit"] == "bottom"


def test_a_declared_grid_is_flattened_into_the_matrix_fields():
    data = single_answer_to_data(_single(pads=[], grid={
        "rows": 2, "cols": 8, "pitch_mm": 2.54, "row_spacing_mm": 10.16, "pad_diameter_mm": 1.5,
        "drill_mm": 0.8, "populated_pins": [1, 7, 8, 9, 10, 16]}))
    assert "package_geometry" not in data
    assert (data["rows"], data["cols"], data["pitch_mm"], data["row_spacing_mm"]) == (2, 8, 2.54, 10.16)
    assert data["populated_pins"] == [1, 7, 8, 9, 10, 16]


@pytest.mark.parametrize("extra", [dict(pads=[]), dict(grid={"rows": 2, "cols": 2, "pad_diameter_mm": 1.5, "drill_mm": 0.8})])
def test_neither_or_both_forms_are_refused(extra):
    """Negative: nothing to build a footprint from, or two contradicting descriptions."""
    with pytest.raises(ExtractionError, match="pads OPPURE"):
        single_answer_to_data(_single(**extra))
    with pytest.raises(ExtractionError):
        single_answer_to_data("not a dict")


def test_a_main_shaped_answer_passes_through_untouched():
    """The model may still answer with the main shape: it is not refused."""
    legacy = dict(layout="matrix", rows=2, cols=4, pitch_mm=2.54, confidence="high")
    assert single_answer_to_data(legacy) == legacy


# -- which tool and prompt the analysis uses ---------------------------------------------

@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)


def _drawing(tmp_path):
    from PIL import Image
    path = tmp_path / "quote.png"
    Image.new("RGB", (300, 200), "white").save(path)
    return path


def test_a_single_part_calls_the_dedicated_tool_with_its_prompt(offline, monkeypatch, tmp_path):
    call = Mock(return_value=_single(drawing_unit="inch", pads=[
        {"number": "1", "x_mm": 0.0, "y_mm": 0.0, "pad_type": "thru_hole", "drill_mm": 0.04,
         "pad_length_mm": 0.07, "pad_width_mm": 0.07},
        {"number": "2", "x_mm": 0.1, "y_mm": 0.0, "pad_type": "thru_hole", "drill_mm": 0.04,
         "pad_length_mm": 0.07, "pad_width_mm": 0.07}],
        body_length_mm=0.8, body_width_mm=0.4, body_height_mm=0.4, lead_diameter_mm=0.02))
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["hires"]})
    kwargs = call.call_args.kwargs
    assert kwargs["tool"]["function"]["name"] == "report_single_component"
    assert kwargs["system_prompt"] == DEFAULT_SINGLE_SYSTEM_PROMPT
    pins = params.package_geometry["pins"]
    assert pins[1]["x_mm"] == pytest.approx(2.54), "inches converted once"
    assert params.body_length_mm == pytest.approx(20.32)
    assert params.lead_diameter_mm == pytest.approx(0.508)


def test_a_board_keeps_the_main_tool_and_prompt(offline, monkeypatch, tmp_path):
    """Negative: the family decides; a module answer is not passed through the mapping."""
    call = Mock(return_value=dict(layout="castellated", body_length_mm=40, body_width_mm=20,
                                  castellated_sides=[dict(edge="left", count=2, pitch_mm=2.54)],
                                  confidence="high"))
    monkeypatch.setattr(ext, "call_tool", call)
    ext.extract_pin_header_params([_drawing(tmp_path)], component_family="aggregate_castellated",
                                  document_roles={"quote.png": ["hires"]})
    kwargs = call.call_args_list[0].kwargs      # the first call reads the documents
    assert kwargs["tool"]["function"]["name"] == "report_pin_header_params"
    assert kwargs["system_prompt"] == DEFAULT_SYSTEM_PROMPT


def test_a_custom_single_prompt_replaces_the_default(offline, monkeypatch, tmp_path):
    call = Mock(return_value=_single())
    monkeypatch.setattr(ext, "call_tool", call)
    ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_smd",
                                  system_prompt="my single prompt")
    assert call.call_args.kwargs["system_prompt"] == "my single prompt"


def test_the_single_prompt_and_tool_carry_no_sample_data():
    import json
    from tests.test_extraction_prompts import _sample_tokens_in
    assert _sample_tokens_in(DEFAULT_SINGLE_SYSTEM_PROMPT) == []
    assert _sample_tokens_in(json.dumps(ext._single_component_tool(), ensure_ascii=False)) == []


# -- profile store -----------------------------------------------------------------------

def test_the_store_carries_the_two_new_prompts_as_defaults(tmp_path):
    from kicadgen.services.analysis_profiles import ProfileStore, factory_defaults
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    defaults = store.defaults()
    assert defaults["system_prompt_single"] == DEFAULT_SINGLE_SYSTEM_PROMPT
    assert defaults["numbering_prompt"] == DEFAULT_PIN_NUMBERING_PROMPT
    assert factory_defaults()["numbering_prompt"] == DEFAULT_PIN_NUMBERING_PROMPT
    store.save_defaults(dict(system_prompt_single="mine", numbering_prompt="num"))
    assert store.snapshot("single_tht")["system_prompt_single"] == "mine"
    assert store.snapshot("single_tht")["numbering_prompt"] == "num"


def test_a_version_5_database_gains_the_prompts_without_touching_the_rest(tmp_path):
    import sqlite3
    from kicadgen.services.analysis_profiles import ProfileStore
    path = tmp_path / "profiles.sqlite3"
    ProfileStore(path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM ai_defaults WHERE name IN ('system_prompt_single', 'numbering_prompt')")
        db.execute("UPDATE ai_defaults SET value='kept' WHERE name='system_prompt'")
        db.execute("PRAGMA user_version=5")
    store = ProfileStore(path)
    defaults = store.defaults()
    assert defaults["system_prompt_single"] == DEFAULT_SINGLE_SYSTEM_PROMPT
    assert defaults["numbering_prompt"] == DEFAULT_PIN_NUMBERING_PROMPT
    assert defaults["system_prompt"] == "kept"


# -- settings dialog ----------------------------------------------------------------------

def test_the_selector_holds_three_independent_prompts(qapp):
    from kicadgen.gui.settings_dialog import LlmSettingsDialog
    d = LlmSettingsDialog("board text", "vendor/m", current_single_prompt="single text",
                          current_numbering_prompt="numbering text", current_family="single_tht")
    try:
        assert [d.combo_prompt_kind.itemText(i) for i in range(d.combo_prompt_kind.count())] == [
            "Schede e moduli", "Componente singolo", "Numerazione dei pin (chiamata dedicata)"]
        assert d.editor.toPlainText() == "board text"
        assert "componente singolo" in d.prompt_usage.text()
        d.combo_prompt_kind.setCurrentIndex(1)
        assert d.editor.toPlainText() == "single text"
        d.editor.setPlainText("single edited")
        d.combo_prompt_kind.setCurrentIndex(2)
        assert d.editor.toPlainText() == "numbering text"
        d.tabs.setCurrentIndex(1)
        d._restore_defaults()
        assert d.numbering_prompt() == DEFAULT_PIN_NUMBERING_PROMPT
        assert d.single_prompt() == "single edited", "restoring one entry never touches the others"
        assert d.prompt() == "board text"
        d.combo_prompt_kind.setCurrentIndex(0)
        assert d.editor.toPlainText() == "board text"
        d.editor.setPlainText("   ")
        assert d.prompt() == "" and d.single_prompt() == "single edited"
    finally:
        d.deleteLater()


def test_the_usage_line_follows_the_family(qapp):
    from kicadgen.gui.settings_dialog import LlmSettingsDialog
    for family, fragment in (("aggregate_tht", "scheda o modulo"), (None, "dipende dalla tipologia")):
        d = LlmSettingsDialog("b", "m", current_family=family)
        try:
            assert fragment in d.prompt_usage.text()
        finally:
            d.deleteLater()


# -- the window picks the prompt by family ----------------------------------------------

def test_the_window_sends_the_single_prompt_for_a_single_part(window, monkeypatch):
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("quote.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.profile_store.save_defaults(dict(system_prompt="BOARD", system_prompt_single="SINGLE",
                                            numbering_prompt="NUMBERING"))
    # This test is about which prompt the window sends, not about the photo
    # detector. The default detector (yolo_smd) validates its weights before
    # the extraction starts and, where the git-ignored model files are absent
    # (the GitHub runner), stops on a QMessageBox.warning that the suite's
    # modal guard turns into a failure (CI review 2026-10-27, I3). The AI
    # detector needs no local file, as in test_automatic_generation.py.
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("ai"))
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))
    window._extract_with_ai()
    assert window._extract_worker.system_prompt == "SINGLE"
    assert window._extract_worker.numbering_prompt == "NUMBERING"
    window._extract_worker = None
    combo.setCurrentIndex(combo.findData("aggregate_tht"))
    window._extract_with_ai()
    assert window._extract_worker.system_prompt == "BOARD"


# -- dedicated reading of the pad positions, compared by distances -----------------------

from kicadgen.services.datasheet_extractor import apply_dedicated_pads, read_pad_positions

RELAY = {"1": (2.54, 0.0), "5": (5.08, 5.08), "4": (0.0, 7.62), "2": (17.78, 0.0), "3": (17.78, 7.62)}


def _main_with_pads(pads, unit="mm", view="bottom"):
    return {"drawing_unit": unit, "contacts_view": view,
            "package_geometry": {"pins": [{"number": n, "x_mm": x, "y_mm": y}
                                          for n, (x, y) in pads.items()]}}


def _pad_reading(pads, view="bottom"):
    return {"view": view, "pads": dict(pads), "notes": ""}


def test_same_distances_from_another_origin_are_a_confirmation():
    shifted = {n: (x + 10.0, y - 3.0) for n, (x, y) in RELAY.items()}
    data = _main_with_pads(RELAY)
    apply_dedicated_pads(data, _pad_reading(shifted))
    assert "_pin_map_conflict" not in data and "confermate" in data["_numbering_note"]


def test_a_different_distance_blocks_without_choosing_a_reading():
    """VE12H-K: the main answer put 1, 5 and 4 in a row; the drawing has them diagonal."""
    wrong = dict(RELAY, **{"1": (5.08, 0.0), "5": (2.54, 0.0), "4": (0.0, 0.0)})
    data = _main_with_pads(wrong)
    apply_dedicated_pads(data, _pad_reading(RELAY))
    assert "non coincidono" in data["_pin_map_conflict"] and "1-4" in data["_pin_map_conflict"]
    assert data["package_geometry"]["pins"][0]["x_mm"] == 5.08, "the main pads stay"


def test_units_are_reconciled_before_comparing():
    inches = {n: (x / 25.4, y / 25.4) for n, (x, y) in RELAY.items()}
    data = _main_with_pads(inches, unit="inch")
    apply_dedicated_pads(data, _pad_reading(RELAY))
    assert "_pin_map_conflict" not in data and "confermate" in data["_numbering_note"]


def test_a_subset_or_a_different_view_is_said():
    data = _main_with_pads(RELAY)
    apply_dedicated_pads(data, _pad_reading({"1": RELAY["1"], "4": RELAY["4"], "2": RELAY["2"]}))
    assert "solo per 3 pad su 5" in data["_numbering_note"] and "_pin_map_conflict" not in data
    data = _main_with_pads(RELAY, view="top")
    apply_dedicated_pads(data, _pad_reading(RELAY, view="bottom"))
    assert "viste diverse" in data["_pin_map_conflict"]
    data = _main_with_pads(RELAY)
    apply_dedicated_pads(data, _pad_reading({"1": RELAY["1"]}))
    assert "meno di due" in data["_numbering_note"]
    data = _main_with_pads(RELAY)
    apply_dedicated_pads(data, {"view": "unknown", "pads": {}, "notes": "", "failed": "boom"})
    assert "chiamata non riuscita: boom" in data["_numbering_note"]


def test_the_reader_converts_the_declared_unit_and_drops_unusable_chains(tmp_path):
    """The program adds the transcribed chains up; a malformed chain is reported, not used."""
    answer = {"drawing_unit": "inch", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["1", "2"], "values": [0.1]},
        {"axis": "y", "stops": ["1,2", "3"], "values": [0.2]},
        {"axis": "x", "stops": ["2", "3"], "values": ["x"]}]}
    reading = read_pad_positions([_drawing(tmp_path)], model="m", call=Mock(return_value=answer))
    assert reading["view"] == "bottom"
    distance = math.dist(reading["pads"]["1"], reading["pads"]["2"])
    assert distance == pytest.approx(2.54) and set(reading["pads"]) == {"1", "2"}
    assert reading["unplaced"] == ["3"] and len(reading["malformed"]) == 1 and not reading["problems"]
    assert read_pad_positions([], model="m", call=Mock()) is None


def test_end_to_end_a_single_part_with_pads_gets_the_second_reading(offline, monkeypatch, tmp_path):
    """Two calls: the dedicated tool, then the pad positions; a disagreement blocks."""
    wrong = [dict(p, x_mm=x, y_mm=y) for p, (x, y) in zip(PADS, [(0.0, 0.0), (2.54, 0.0), (5.08, 0.0)])]
    # Chains with no outline stop: no closure to check, so no reason to prefer either reading.
    second = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
        {"axis": "x", "stops": ["1", "5", "2"], "values": [2.54, 12.7]},
        {"axis": "y", "stops": ["1,2", "5"], "values": [5.08]}]}
    call = Mock(side_effect=[_single(pads=wrong), second])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["top"]})
    assert call.call_count == 2
    assert call.call_args_list[1].kwargs["tool"]["function"]["name"] == "report_pad_positions"
    assert params.pin_map_conflict and "non coincidono" in params.pin_map_conflict
    call = Mock(side_effect=[_single(pads=wrong), second])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["hires"]})
    assert call.call_count == 1 and params.pin_map_conflict is None, "no drawing role: no check"


def test_the_pad_prompt_and_tool_carry_no_sample_data():
    from tests.test_extraction_prompts import _sample_tokens_in
    assert _sample_tokens_in(ext.DEFAULT_PAD_POSITIONS_PROMPT) == []
    assert _sample_tokens_in(json.dumps(ext._PAD_POSITIONS_TOOL, ensure_ascii=False)) == []
