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


def _flattened_pad():
    """The broken shape deepseek-flash returned once out of three identical
    requests (2026-10-31): one pad's fields at the top level, pads a string."""
    return dict(drawing_unit="mm", contacts_view="bottom", confidence="high", pads="1",
                pads_origin="custom", x_mm=0.0, y_mm=0.0, drill_mm=1.2, pad_type="thru_hole",
                pad_length_mm=2.2, pad_width_mm=2.2, shape="circle")


def test_a_broken_answer_shape_is_logged_and_paid_once_more(offline, monkeypatch, tmp_path):
    """Positive: the same request comes back well formed and is used."""
    call = Mock(side_effect=[_flattened_pad(), _single()])
    monkeypatch.setattr(ext, "call_tool", call)
    lines = []
    params = ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                           document_roles={"quote.png": ["hires"]},
                                           progress=lines.append)
    assert call.call_count == 2
    first, second = (c.kwargs for c in call.call_args_list)
    assert {k: v for k, v in first.items() if k != "content_blocks"} == {
        k: v for k, v in second.items() if k != "content_blocks"}
    # Same request plus one block naming the broken schema rule, no value.
    assert second["content_blocks"][:-1] == first["content_blocks"]
    assert second["content_blocks"][-1] == {"type": "text", "text": ext._SHAPE_CORRECTION}
    assert any(line.startswith("AI_RESPONSE_UNREADABLE attempt=1 {") and '"pads": "1"' in line
               for line in lines)
    assert [p["number"] for p in params.package_geometry["pins"]] == ["1", "2", "5"]


def test_two_broken_answer_shapes_stop_without_a_third_call(offline, monkeypatch, tmp_path):
    """Negative: one retry only, both answers in the log."""
    call = Mock(side_effect=[_flattened_pad(), _single(pads=[]), _single()])
    monkeypatch.setattr(ext, "call_tool", call)
    lines = []
    with pytest.raises(ExtractionError, match="Anche il nuovo tentativo.*pads OPPURE"):
        ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                      document_roles={"quote.png": ["hires"]},
                                      progress=lines.append)
    assert call.call_count == 2
    assert [l.split(" ", 2)[1] for l in lines if l.startswith("AI_RESPONSE_UNREADABLE")] == [
        "attempt=1", "attempt=2"]


def test_a_well_formed_answer_is_not_paid_twice(offline, monkeypatch, tmp_path):
    """Negative: a valid shape, even with numbers later found wrong, is one call."""
    call = Mock(side_effect=[_single(), _single()])
    monkeypatch.setattr(ext, "call_tool", call)
    ext.extract_pin_header_params([_drawing(tmp_path)], component_family="single_tht",
                                  document_roles={"quote.png": ["hires"]})
    assert call.call_count == 1


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
                                            numbering_prompt="NUMBERING",
                                            pad_model="vendor/reader"))
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
    assert window._extract_worker.pad_model == "vendor/reader"
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
    # Since 2026-10-30 (user) a partial confirmation blocks: two pads unchecked.
    assert "solo 3 pad su 5" in data["_pin_map_conflict"]
    data = _main_with_pads(RELAY, view="top")
    apply_dedicated_pads(data, _pad_reading(RELAY, view="bottom"))
    assert "viste diverse" in data["_pin_map_conflict"]
    data = _main_with_pads(RELAY)
    apply_dedicated_pads(data, _pad_reading({"1": RELAY["1"]}))
    assert "meno di due" in data["_pin_map_conflict"]
    data = _main_with_pads(RELAY)
    apply_dedicated_pads(data, {"view": "unknown", "pads": {}, "notes": "", "failed": "boom"})
    assert "chiamata non riuscita: boom" in data["_pin_map_conflict"]


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
    # No drawing role: no second reading is possible, and since 2026-10-31
    # (adversarial review G1) positions nothing confirms block anyway.
    assert call.call_count == 1, "no drawing role: no dedicated call"
    assert params.pin_map_conflict and "nessun disegno in immagine" in params.pin_map_conflict


_CHAINS_ANSWER = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
    {"axis": "x", "stops": ["1", "5", "2"], "values": [5.08, 12.7]},
    {"axis": "y", "stops": ["1,2", "5"], "values": [2.54]}]}


def test_the_pad_positions_are_read_by_the_pad_model(offline, monkeypatch, tmp_path):
    """Profile field 'pad_model' (user, 2026-10-30): only the dedicated reading changes model."""
    call = Mock(side_effect=[_single(), _CHAINS_ANSWER])
    monkeypatch.setattr(ext, "call_tool", call)
    logs = []
    ext.extract_pin_header_params([_drawing(tmp_path)], model="vendor/main",
                                  component_family="single_tht", pad_model="vendor/reader",
                                  document_roles={"quote.png": ["top"]}, progress=logs.append)
    assert [c.kwargs["model"] for c in call.call_args_list] == ["vendor/main", "vendor/reader"]
    assert call.call_args_list[1].kwargs["max_tokens"] == ext.PAD_POSITIONS_MAX_TOKENS
    assert any("con vendor/reader" in line for line in logs)


@pytest.mark.parametrize("pad_model", [None, "", "   "])
def test_a_blank_pad_model_follows_the_main_model(offline, monkeypatch, tmp_path, pad_model):
    call = Mock(side_effect=[_single(), _CHAINS_ANSWER])
    monkeypatch.setattr(ext, "call_tool", call)
    ext.extract_pin_header_params([_drawing(tmp_path)], model="vendor/main",
                                  component_family="single_tht", pad_model=pad_model,
                                  document_roles={"quote.png": ["top"]})
    assert [c.kwargs["model"] for c in call.call_args_list] == ["vendor/main", "vendor/main"]


def test_a_pad_model_whose_source_is_not_ready_falls_back_to_the_main_model(
        offline, monkeypatch, tmp_path):
    """Another provider without a key never refuses the run: the main model reads, the log says why."""
    from kicadgen.services import providers
    real = providers.ensure_providers_ready

    def ready(models, progress=None, **kwargs):
        if list(models) == ["deepseek-api/reader"]:
            raise providers.ProviderError("no key")
        return real(models, progress, **kwargs)

    monkeypatch.setattr(providers, "ensure_providers_ready", ready)
    call = Mock(side_effect=[_single(), _CHAINS_ANSWER])
    monkeypatch.setattr(ext, "call_tool", call)
    logs = []
    ext.extract_pin_header_params([_drawing(tmp_path)], model="vendor/main",
                                  component_family="single_tht", pad_model="deepseek-api/reader",
                                  document_roles={"quote.png": ["top"]}, progress=logs.append)
    assert [c.kwargs["model"] for c in call.call_args_list] == ["vendor/main", "vendor/main"]
    assert any("deepseek-api/reader" in line and "no key" in line for line in logs)


def test_a_board_never_checks_the_pad_model(offline, monkeypatch):
    """Negative: no dedicated pad reading outside single parts, so no provider check either."""
    from kicadgen.services import providers
    checked = []
    monkeypatch.setattr(providers, "ensure_providers_ready",
                        lambda models, progress=None, **kwargs: checked.append(list(models)))
    assert ext._pad_reading_model("deepseek-api/reader", "vendor/main", "aggregate_tht",
                                  None, None) == "vendor/main"
    assert checked == []
    assert ext._pad_reading_model("deepseek-api/reader", "vendor/main", "single_smd",
                                  None, None) == "deepseek-api/reader"
    assert checked == [["deepseek-api/reader"]]


def test_the_settings_dialog_edits_the_pad_model(qapp, monkeypatch):
    from kicadgen.gui import settings_dialog
    from kicadgen.gui.settings_dialog import LlmSettingsDialog
    d = LlmSettingsDialog("b", "deepseek-api/deepseek-flash")
    try:
        assert d.pad_model() == ext.DEFAULT_PAD_POSITIONS_MODEL, "factory default when unset"
        monkeypatch.setattr(settings_dialog, "pick_model", lambda *a, **k: "vendor/reader")
        d._pick_pad_model()
        assert d.pad_model() == "vendor/reader" and d.model() == "deepseek-api/deepseek-flash"
        d._pad_model_follows_main()
        assert d.pad_model() == ""
        d.tabs.setCurrentIndex(0)
        d._restore_defaults()
        assert d.pad_model() == ext.DEFAULT_PAD_POSITIONS_MODEL
    finally:
        d.deleteLater()
    d = LlmSettingsDialog("b", "vendor/m", current_pad_model="")
    try:
        assert d.pad_model() == "", "a blank saved value stays blank"
    finally:
        d.deleteLater()


def test_the_pad_prompt_and_tool_carry_no_sample_data():
    from tests.test_extraction_prompts import _sample_tokens_in
    assert _sample_tokens_in(ext.DEFAULT_PAD_POSITIONS_PROMPT) == []
    assert _sample_tokens_in(json.dumps(ext._PAD_POSITIONS_TOOL, ensure_ascii=False)) == []
