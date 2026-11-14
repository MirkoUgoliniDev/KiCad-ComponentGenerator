"""@brief Every document step calls the model chosen for it (user request, 2026-11-12).

The step-model table (prompt_models, «Modello AI») is resolved at each call
site of the document extraction, the partial re-reads and the pin-type
suggestion. Each wired step gets a positive case (its own model reaches the
call) and a negative one (None, "" or another step's choice leave the old
model), plus the fallback of a step model that cannot run. Every paid call
is a Mock recording `model`.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import providers
from kicadgen.services.prompt_models import STEPS

from minimal_pdf import MINIMAL_PDF
from test_single_component_prompt import _CHAINS_ANSWER, _BODY_ANSWER, _drawing, _single, offline  # noqa: F401
from test_pin_numbering_call import _MAIN as _MATRIX_MAIN, U_NUMBERING, _answer as _numbering_answer

#: Every step on the single model: the table a profile with no choice gives.
_BLANK = {step.field: "" for step in STEPS}


def _steps(**chosen):
    """The step table with the given choices, every other step on the single model."""
    return dict(_BLANK, **chosen)


def _models(call):
    """The model of every faked paid call, in order."""
    return [c.kwargs["model"] for c in call.call_args_list]


def _not_ready(monkeypatch, refused):
    """Make the provider check refuse exactly the model list [refused]."""
    real = providers.ensure_providers_ready

    def ready(models, progress=None, **kwargs):
        if list(models) == [refused]:
            raise providers.ProviderError("no key")
        return real(models, progress, **kwargs)

    monkeypatch.setattr(providers, "ensure_providers_ready", ready)


def _single_run(monkeypatch, tmp_path, logs, **kwargs):
    """A single part: main answer, pad chains, body quotes, three faked calls."""
    call = Mock(side_effect=[_single(), _CHAINS_ANSWER, _BODY_ANSWER])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], model="vendor/main",
                                           component_family="single_tht",
                                           document_roles={"quote.png": ["top"]},
                                           progress=logs.append, **kwargs)
    return call, params


# -- main reading of a single part, pad and body readings ----------------------------

def test_the_single_part_main_reading_uses_its_step_and_sizes_the_images_for_it(
        offline, monkeypatch, tmp_path):
    sized = []
    real = ext._file_to_content_block
    monkeypatch.setattr(ext, "_file_to_content_block",
                        lambda path, model=None: sized.append(model) or real(path, model))
    logs = []
    call, _ = _single_run(monkeypatch, tmp_path, logs,
                          step_models=_steps(system_prompt_single="vendor/single"))
    # The dedicated readings fall back to the main reading's model.
    assert _models(call) == ["vendor/single"] * 3
    assert sized[0] == "vendor/single", "the main request is sized for its step model"


def test_the_board_prompt_choice_does_not_move_a_single_part(offline, monkeypatch, tmp_path):
    """Negative: the step is the prompt sent; a single part sends 'system_prompt_single'."""
    call, _ = _single_run(monkeypatch, tmp_path, [],
                          step_models=_steps(system_prompt="vendor/board"))
    assert _models(call) == ["vendor/main"] * 3


def test_pad_and_body_readings_each_use_their_own_step(offline, monkeypatch, tmp_path):
    logs = []
    call, _ = _single_run(monkeypatch, tmp_path, logs, pad_model="vendor/legacy",
                          step_models=_steps(pad_positions_prompt="vendor/pads",
                                             body_dimensions_prompt="vendor/body"))
    assert _models(call) == ["vendor/main", "vendor/pads", "vendor/body"]
    assert call.call_args_list[1].kwargs["tool"]["function"]["name"] == "report_pad_positions"
    assert call.call_args_list[2].kwargs["tool"]["function"]["name"] == "report_body_dimensions"
    assert any("posizioni dei pad" in line and "con vendor/pads" in line for line in logs)
    assert any("quote del corpo" in line and "con vendor/body" in line for line in logs)


@pytest.mark.parametrize("step_models, expected", [
    # None: the behaviour before 2026-11-12, pad_model reads pads AND body.
    (None, ["vendor/main", "vendor/legacy", "vendor/legacy"]),
    # A table given: pad_model is ignored (the table already carries it by
    # migration, LEGACY_PAD_STEPS); blank steps follow the main reading.
    (_BLANK, ["vendor/main", "vendor/main", "vendor/main"]),
])
def test_without_a_step_choice_the_old_models_read(offline, monkeypatch, tmp_path, step_models,
                                                   expected):
    call, _ = _single_run(monkeypatch, tmp_path, [], pad_model="vendor/legacy",
                          step_models=step_models)
    assert _models(call) == expected


def test_an_unusable_pad_or_body_step_model_falls_back_and_says_so(offline, monkeypatch, tmp_path):
    _not_ready(monkeypatch, "deepseek-api/pads")
    logs = []
    call, params = _single_run(monkeypatch, tmp_path, logs,
                               step_models=_steps(pad_positions_prompt="deepseek-api/pads",
                                                  body_dimensions_prompt="vendor/body"))
    assert _models(call) == ["vendor/main", "vendor/main", "vendor/body"]
    assert any("deepseek-api/pads" in line and "no key" in line and "vendor/main" in line
               for line in logs)
    assert "Posizioni dei pad lette dal modello principale vendor/main" in params.notes
    assert "Quote del corpo lette dal modello principale" not in params.notes


def test_an_unusable_body_step_model_is_named_in_the_result(offline, monkeypatch, tmp_path):
    _not_ready(monkeypatch, "deepseek-api/body")
    call, params = _single_run(monkeypatch, tmp_path, [],
                               step_models=_steps(body_dimensions_prompt="deepseek-api/body"))
    assert _models(call) == ["vendor/main"] * 3
    assert ("Quote del corpo lette dal modello principale vendor/main al posto di "
            "deepseek-api/body") in params.notes
    assert "Posizioni dei pad lette dal modello principale" not in params.notes


# -- dedicated numbering ---------------------------------------------------------------

def _numbering_run(monkeypatch, tmp_path, **kwargs):
    """A single matrix part: main answer and the dedicated numbering, two faked calls."""
    call = Mock(side_effect=[dict(_MATRIX_MAIN), _numbering_answer(U_NUMBERING)])
    monkeypatch.setattr(ext, "call_tool", call)
    ext.extract_pin_header_params([_drawing(tmp_path)], model="vendor/main",
                                  document_roles={"quote.png": ["top"]},
                                  component_family="single_tht", **kwargs)
    assert call.call_args_list[1].kwargs["tool"]["function"]["name"] == "report_pin_numbering"
    return call


def test_the_numbering_reading_uses_its_step(offline, monkeypatch, tmp_path):
    call = _numbering_run(monkeypatch, tmp_path, step_models=_steps(numbering_prompt="vendor/num"))
    assert _models(call) == ["vendor/main", "vendor/num"]


@pytest.mark.parametrize("step_models", [None, _BLANK, _steps(pad_positions_prompt="vendor/pads")])
def test_the_numbering_reading_keeps_the_main_model_without_its_own_choice(
        offline, monkeypatch, tmp_path, step_models):
    call = _numbering_run(monkeypatch, tmp_path, step_models=step_models)
    assert _models(call) == ["vendor/main", "vendor/main"]


# -- castellated board: main reading and contact-style recheck ------------------------

def _castellated_run(monkeypatch, tmp_path, sides, logs, sized=None, **kwargs):
    """A castellated board read from a PDF: main answer, then the optional style recheck."""
    source = tmp_path / "drawing.pdf"
    source.write_bytes(MINIMAL_PDF)
    call = Mock(return_value=dict(layout="castellated", confidence="high", castellated_sides=sides))
    monkeypatch.setattr(ext, "call_tool", call)
    if sized is not None:
        real = ext._file_to_content_block
        monkeypatch.setattr(ext, "_file_to_content_block",
                            lambda path, model=None: sized.append(model) or real(path, model))
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kw: None)
    ext.extract_pin_header_params([source], model="vendor/main", progress=logs.append, **kwargs)
    return call


def test_the_contact_style_recheck_uses_its_step_with_documents_sized_for_it(monkeypatch, tmp_path):
    sized = []
    call = _castellated_run(monkeypatch, tmp_path, [dict(edge="top", count=2, pitch_mm=2.54)], [],
                            sized, step_models=_steps(system_prompt="vendor/board",
                                                      contact_style_prompt="vendor/style"))
    assert _models(call) == ["vendor/board", "vendor/style"]
    assert sized == ["vendor/board", "vendor/style"]


@pytest.mark.parametrize("step_models", [None, _BLANK])
def test_the_contact_style_recheck_keeps_the_main_model_without_its_own_choice(
        monkeypatch, tmp_path, step_models):
    sized = []
    call = _castellated_run(monkeypatch, tmp_path, [dict(edge="top", count=2, pitch_mm=2.54)], [],
                            sized, step_models=step_models)
    assert _models(call) == ["vendor/main", "vendor/main"]
    assert sized == ["vendor/main"], "the main request is reused, not rebuilt"


def test_a_step_that_is_not_reached_is_never_resolved(monkeypatch, tmp_path):
    """Negative: observed styles make no recheck, so its unusable model writes no fallback line."""
    _not_ready(monkeypatch, "deepseek-api/style")
    logs = []
    call = _castellated_run(monkeypatch, tmp_path,
                            [dict(edge="top", count=2, pitch_mm=2.54, contact_style="half_hole")],
                            logs, step_models=_steps(contact_style_prompt="deepseek-api/style"))
    assert call.call_count == 1
    assert not any("deepseek-api/style" in line for line in logs)


def test_an_unusable_main_step_model_falls_back_to_the_model(monkeypatch, tmp_path):
    _not_ready(monkeypatch, "deepseek-api/board")
    logs = []
    call = _castellated_run(monkeypatch, tmp_path,
                            [dict(edge="top", count=2, pitch_mm=2.54, contact_style="half_hole")],
                            logs, step_models=_steps(system_prompt="deepseek-api/board"))
    assert _models(call) == ["vendor/main"]
    assert any("deepseek-api/board" in line and "no key" in line for line in logs)


# -- photo stage: the table is forwarded, only when given ------------------------------

@pytest.mark.parametrize("step_models", [None, _steps(inventory_prompt="vendor/photo")])
def test_the_photo_stage_forwards_the_step_table_only_when_given(monkeypatch, step_models):
    from kicadgen.services import photo_inventory
    seen = []

    def inventory(*args, **kwargs):
        seen.append(kwargs)
        return None

    monkeypatch.setattr(photo_inventory, "extract_inventory", inventory)
    data = dict(layout="castellated", confidence="high", body_length_mm=20.0, body_width_mm=10.0,
                castellated_sides=[dict(edge="top", count=2, pitch_mm=2.54,
                                        contact_style="half_hole")])
    ext._extract_photo_stage(
        data, [], progress=None, announce=lambda *a: None, check_cancel=lambda: None,
        document_roles=None, component_family=None, photo_detector="yolo", vision_model=None,
        model="vendor/main", verification_model=None, diagnostic_call=Mock(),
        extra_instructions=None, photo_cache_dir=None, grounding_prompt=None,
        verification_prompt=None, terminal_prompt=None, yolo_model_path=None,
        review_proposals=None, yolo_confidence=None, yolo_settings=None,
        step_models=step_models)
    assert len(seen) == 1
    if step_models is None:
        assert "step_models" not in seen[0], "an inventory predating the keyword keeps working"
    else:
        assert seen[0]["step_models"] is step_models


def test_the_extraction_hands_the_table_to_the_photo_stage(monkeypatch, tmp_path):
    table = _steps(crop_prompt="vendor/crops")
    seen = {}
    monkeypatch.setattr(ext, "_extract_photo_stage",
                        lambda data, paths, **kwargs: seen.update(kwargs) or "done")
    _castellated_run(monkeypatch, tmp_path,
                     [dict(edge="top", count=2, pitch_mm=2.54, contact_style="half_hole")], [],
                     step_models=table)
    assert seen["step_models"] is table


# -- partial re-reads --------------------------------------------------------------------

def _stored_pads():
    from test_single_component_prompt import PADS
    wrong = [dict(p, x_mm=x, y_mm=y) for p, (x, y) in zip(PADS, [(0.0, 0.0), (2.54, 0.0), (5.08, 0.0)])]
    return ext.single_answer_to_data(_single(pads=wrong))


_AGREEING = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
    {"axis": "x", "stops": ["1", "2", "5"], "values": [2.54, 2.54]},
    {"axis": "y", "stops": ["1,2,5", "edge"], "values": [3.0]}]}


def test_the_pad_reread_uses_its_step_and_counts_as_another_model(offline, tmp_path):
    call = Mock(return_value=_AGREEING)
    reread = ext.reread_pad_positions(_stored_pads(), [_drawing(tmp_path)], model="vendor/main",
                                      call=call, same_model=True,
                                      step_models=_steps(pad_positions_prompt="vendor/reader"))
    assert _models(call) == ["vendor/reader"]
    assert reread.pin_map_conflict is None and "confermate" in reread.notes


def test_a_blank_pad_step_rereads_with_the_main_model_as_one_witness(offline, tmp_path):
    """Negative twin: the main model re-reading is one reading, whatever same_model the caller said."""
    call = Mock(return_value=_AGREEING)
    reread = ext.reread_pad_positions(_stored_pads(), [_drawing(tmp_path)], model="vendor/main",
                                      call=call, same_model=False, step_models=_BLANK)
    assert _models(call) == ["vendor/main"]
    _same_model_question(reread)


def test_without_a_table_the_reread_keeps_the_callers_model(offline, tmp_path):
    call = Mock(return_value=_AGREEING)
    ext.reread_pad_positions(_stored_pads(), [_drawing(tmp_path)], model="vendor/reader", call=call)
    assert _models(call) == ["vendor/reader"]


@pytest.mark.parametrize("step_models, expected", [
    (_steps(body_dimensions_prompt="vendor/body"), "vendor/body"),
    # Another step's choice does not move the body reading.
    (_steps(pad_positions_prompt="vendor/pads"), "vendor/main"),
    (None, "vendor/main"),
])
def test_the_height_reread_uses_the_body_step(offline, tmp_path, step_models, expected):
    stored = ext.single_answer_to_data(_single(body_height_mm=None))
    call = Mock(return_value={"drawing_unit": "mm", "body_height": 16.9,
                              "printed_vertical_quotes": [16.9]})
    ext.reread_body_height(stored, [_drawing(tmp_path)], model="vendor/main", call=call,
                           step_models=step_models)
    assert _models(call) == [expected]


# -- the model that gave the stored main answer (review finding 1) -----------------------

def test_the_stored_main_answer_records_the_model_that_gave_it(offline, monkeypatch, tmp_path):
    """The main step's model, not the caller's single model, is recorded in raw_main."""
    _, params = _single_run(monkeypatch, tmp_path, [],
                            step_models=_steps(system_prompt_single="vendor/single"))
    assert params.raw_main[ext.RAW_MAIN_MODEL_KEY] == "vendor/single"
    _, params = _single_run(monkeypatch, tmp_path, [], step_models=None)
    assert params.raw_main[ext.RAW_MAIN_MODEL_KEY] == "vendor/main"


def test_a_model_answer_cannot_set_the_recorded_model(offline, monkeypatch, tmp_path):
    """Negative: the key is program state, written after the «_» keys of the answer are dropped."""
    answer = dict(_single(), **{ext.RAW_MAIN_MODEL_KEY: "vendor/forged"})
    call = Mock(side_effect=[answer, _CHAINS_ANSWER, _BODY_ANSWER])
    monkeypatch.setattr(ext, "call_tool", call)
    params = ext.extract_pin_header_params([_drawing(tmp_path)], model="vendor/main",
                                           component_family="single_tht",
                                           document_roles={"quote.png": ["top"]})
    assert params.raw_main[ext.RAW_MAIN_MODEL_KEY] == "vendor/main"


def _recorded(model):
    """A stored main answer recording `model` as the model that gave it."""
    return dict(_stored_pads(), **{ext.RAW_MAIN_MODEL_KEY: model})


def test_the_reread_compares_with_the_recorded_model_not_the_callers(offline, tmp_path):
    """Positive: main step X, pad step X, the caller says Y: one model reading twice (H6)."""
    call = Mock(return_value=_AGREEING)
    reread = ext.reread_pad_positions(_recorded("vendor/x"), [_drawing(tmp_path)],
                                      model="vendor/y", call=call, same_model=False,
                                      step_models=_steps(pad_positions_prompt="vendor/x"))
    assert _models(call) == ["vendor/x"]
    _same_model_question(reread)
    # The re-read result still names the producer, for a re-read of it.
    assert reread.raw_main[ext.RAW_MAIN_MODEL_KEY] == "vendor/x"


def test_a_blank_pad_step_rereads_with_the_recorded_model(offline, tmp_path):
    """A blank step means the main reading's model: the recorded X, not the caller's Y."""
    call = Mock(return_value=_AGREEING)
    reread = ext.reread_pad_positions(_recorded("vendor/x"), [_drawing(tmp_path)],
                                      model="vendor/y", call=call, step_models=_BLANK)
    assert _models(call) == ["vendor/x"]
    _same_model_question(reread)


def test_a_pad_step_other_than_the_recorded_model_is_independent(offline, tmp_path):
    """Negative: pad step Z, recorded X; the caller's model is Z itself, still independent of X."""
    call = Mock(return_value=_AGREEING)
    reread = ext.reread_pad_positions(_recorded("vendor/x"), [_drawing(tmp_path)],
                                      model="vendor/z", call=call, same_model=True,
                                      step_models=_steps(pad_positions_prompt="vendor/z"))
    assert _models(call) == ["vendor/z"]
    assert reread.pin_map_conflict is None and "confermate" in reread.notes


@pytest.mark.parametrize("caller_model, independent", [("vendor/x", False), ("vendor/y", True)])
def test_without_a_table_the_record_decides_same_model(offline, tmp_path, caller_model,
                                                       independent):
    """No table: the caller's model reads; whether it is the producer comes from the record."""
    call = Mock(return_value=_AGREEING)
    reread = ext.reread_pad_positions(_recorded("vendor/x"), [_drawing(tmp_path)],
                                      model=caller_model, call=call,
                                      same_model=independent)  # the caller claims the opposite
    assert _models(call) == [caller_model]
    # CONTRACT CHANGED 2026-11-13 (round 8): the producer re-reading is one
    # reading -- no confirmation, asked as one recommended layout instead of
    # a block; an independent reader confirms.
    if independent:
        assert reread.pin_map_conflict is None and "confermate" in reread.notes
    else:
        _same_model_question(reread)


def test_an_unusable_recorded_model_leaves_the_reread_to_the_callers_model(
        offline, monkeypatch, tmp_path):
    """The recorded producer cannot run now: the caller's model reads, independent of it."""
    _not_ready(monkeypatch, "deepseek-api/x")
    logs = []
    call = Mock(return_value=_AGREEING)
    reread = ext.reread_pad_positions(_recorded("deepseek-api/x"), [_drawing(tmp_path)],
                                      model="vendor/y", call=call, progress=logs.append,
                                      step_models=_BLANK)
    assert _models(call) == ["vendor/y"]
    assert any("deepseek-api/x" in line and "no key" in line for line in logs)
    assert reread.pin_map_conflict is None


def test_the_height_reread_reads_with_the_recorded_model_on_a_blank_step(offline, tmp_path):
    stored = dict(ext.single_answer_to_data(_single(body_height_mm=None)),
                  **{ext.RAW_MAIN_MODEL_KEY: "vendor/x"})
    call = Mock(return_value={"drawing_unit": "mm", "body_height": 16.9,
                              "printed_vertical_quotes": [16.9]})
    reread = ext.reread_body_height(stored, [_drawing(tmp_path)], model="vendor/y", call=call,
                                    step_models=_BLANK)
    assert _models(call) == ["vendor/x"]
    assert reread.raw_main[ext.RAW_MAIN_MODEL_KEY] == "vendor/x"


@pytest.mark.parametrize("value", ["", "   ", 7, None])
def test_a_blank_or_malformed_record_is_ignored(value):
    """A hand-edited file is not trusted with a model: no record, the caller's base as before."""
    assert ext.stored_main_model({ext.RAW_MAIN_MODEL_KEY: value}) is None
    assert ext.stored_main_model({ext.RAW_MAIN_MODEL_KEY: " vendor/x "}) == "vendor/x"


# -- lazy, memoised resolution of the pad and body readers (review finding 5) -------------

def test_one_unusable_model_asked_by_both_steps_is_checked_and_noted_once(
        offline, monkeypatch, tmp_path):
    """The legacy pad model on both steps: one check, one fallback line, the pad note only."""
    checks = []
    real = providers.ensure_providers_ready

    def ready(models, progress=None, **kwargs):
        if list(models) == ["deepseek-api/legacy"]:
            checks.append(models)
            raise providers.ProviderError("no key")
        return real(models, progress, **kwargs)

    monkeypatch.setattr(providers, "ensure_providers_ready", ready)
    logs = []
    call, params = _single_run(monkeypatch, tmp_path, logs,
                               step_models=_steps(pad_positions_prompt="deepseek-api/legacy",
                                                  body_dimensions_prompt="deepseek-api/legacy"))
    assert _models(call) == ["vendor/main"] * 3
    assert len(checks) == 1
    assert sum("deepseek-api/legacy" in line and "no key" in line for line in logs) == 1
    assert "Posizioni dei pad lette dal modello principale vendor/main" in params.notes
    assert "Quote del corpo lette dal modello principale" not in params.notes


def test_two_different_unusable_models_are_each_noted(offline, monkeypatch, tmp_path):
    """Negative twin: different requested models fall back separately, each named."""
    real = providers.ensure_providers_ready

    def ready(models, progress=None, **kwargs):
        if list(models) in (["deepseek-api/pads"], ["deepseek-api/body"]):
            raise providers.ProviderError("no key")
        return real(models, progress, **kwargs)

    monkeypatch.setattr(providers, "ensure_providers_ready", ready)
    _, params = _single_run(monkeypatch, tmp_path, [],
                            step_models=_steps(pad_positions_prompt="deepseek-api/pads",
                                               body_dimensions_prompt="deepseek-api/body"))
    assert "al posto di deepseek-api/pads" in params.notes
    assert "al posto di deepseek-api/body" in params.notes


def test_pad_and_body_readers_are_not_resolved_when_their_reading_does_not_run(
        offline, monkeypatch, tmp_path):
    """A matrix part never reaches the pad reading: its unusable model writes no line."""
    _not_ready(monkeypatch, "deepseek-api/pads")
    logs = []
    _numbering_run(monkeypatch, tmp_path, progress=logs.append,
                   step_models=_steps(pad_positions_prompt="deepseek-api/pads"))
    assert not any("deepseek-api/pads" in line for line in logs)


def _same_model_question(params):
    """The pad-layout question of two agreeing readings of ONE model (round 8, real TEL run).

    CONTRACT CHANGED 2026-11-13: such readings are one reading, still never
    adopted, but they are ASKED as one recommended confirmation instead of a
    hard block without a question.
    """
    assert not params.pin_map_conflict
    (question,) = [q for q in params.questions or () if q["field"] == "pad_layout"]
    assert "stesso modello" in question["text"]
    assert len(question["options"]) == 1 and question["options"][0].get("recommended") is True
    return question
