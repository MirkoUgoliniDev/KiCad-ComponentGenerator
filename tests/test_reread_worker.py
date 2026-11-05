"""@brief RereadWorker: the partial re-run of one datum of a single-part extraction.

Review 2026-11-04, M16 («nessuna ri-esecuzione parziale» = every clarification
repaid the whole extraction) and M13 (the log must say which model re-reads).
The extractor functions are injected on the module: no network (conftest
blocks it) and no dependence on the extractor's own implementation.
"""

import threading

from kicadgen.gui import workers
from kicadgen.services import datasheet_extractor, openrouter, providers
from kicadgen.services.ai_diagnostics import CallCancelled


def _run(worker):
    """Run the worker synchronously and return what reached `error`, `done` and `progress`."""
    errors, done, progress = [], [], []
    worker.error.connect(errors.append)
    worker.done.connect(done.append)
    worker.progress.connect(progress.append)
    worker.run()
    return errors, done, progress


def _text(progress):
    """Join the progress rows (Message or str) as plain text."""
    return "\n".join(str(getattr(row, "text", row)) for row in progress)


def test_body_height_reread_emits_the_result_with_the_dedicated_reader(qapp, monkeypatch):
    """@brief Positive: the injected function receives stored, documents, model and a logged call.

    CONTRACT CHANGED (generality audit 2026-11-05, A2): the height is re-read
    by the reader `_pad_reading_model` picks, as the full extraction does,
    because the stored main list came from the main model and two lists of
    one model are one witness. Here the pad model's provider is not checked
    (the rule is replaced), so the dedicated model is chosen as given.
    """
    seen = {}
    monkeypatch.setattr(datasheet_extractor, "_pad_reading_model",
                        lambda pad_model, main, family, progress, check: pad_model)

    def fake_reread(stored, documents, *, model, call, progress, document_roles=None,
                    same_model=None, **kwargs):
        seen["same_model"] = same_model
        seen.update(stored=stored, documents=documents, model=model, roles=document_roles,
                    kwargs=kwargs)
        # The call is openrouter.call_tool wrapped by make_logged_call: it
        # writes the lifecycle lines to `progress`.
        seen["answer"] = call(model=model, tool={"name": "report_height"})
        return {"updated": True}

    monkeypatch.setattr(datasheet_extractor, "reread_body_height", fake_reread, raising=False)
    monkeypatch.setattr(openrouter, "call_tool", lambda **kwargs: {"height_mm": 15.5})
    # No provider check against the network: the readiness helper is replaced.
    monkeypatch.setattr(providers, "ensure_providers_ready",
                        lambda models, progress=None: seen.update(ready=list(models)))
    stored = {"body": {"height_mm": 15.5}, "_candidates": {"k": {}}}
    worker = workers.RereadWorker(stored, ["a.pdf"], {"a.pdf": ["datasheet"]},
                                  field="body_height_mm", model="deepseek-api/deepseek-flash",
                                  pad_model="openai/gpt-5.6-sol", component_family="single_tht")
    errors, done, progress = _run(worker)
    assert not errors and done == [{"updated": True}]
    assert seen["stored"] == stored and seen["stored"] is not stored
    assert seen["documents"] == ["a.pdf"] and seen["roles"] == {"a.pdf": ["datasheet"]}
    assert seen["answer"] == {"height_mm": 15.5}
    # The height is re-read by the DEDICATED reader (not the main model),
    # and the extractor is told the two readers differ.
    assert seen["model"] == "openai/gpt-5.6-sol" and seen["same_model"] is False
    assert "ready" not in seen, "no direct provider to check for an OpenRouter model"
    text = _text(progress)
    assert "openai/gpt-5.6-sol" in text and "body_height_mm" in text
    # The wrapped call logged its lifecycle through the worker's progress,
    # the answer's arguments included (log_prompt=True as in the extraction).
    assert "AI_CALL_START" in text and "AI_CALL_END" in text
    assert "AI_CALL_RESULT" in text and '"height_mm":15.5' in text


def test_pad_reread_uses_the_extractors_pad_model_rule(qapp, monkeypatch):
    """@brief Positive: the pad model is chosen by `_pad_reading_model`, as the full extraction does."""
    seen = {}

    def fake_reread(stored, documents, *, model, call, progress, **kwargs):
        seen["model"] = model
        return "params"

    def fake_choice(pad_model, main, family, progress, check):
        seen["asked"] = (pad_model, main, family)
        return "chosen/pad-model"

    monkeypatch.setattr(datasheet_extractor, "reread_pad_positions", fake_reread, raising=False)
    monkeypatch.setattr(datasheet_extractor, "_pad_reading_model", fake_choice)
    worker = workers.RereadWorker({}, [], None, field="pad_layout", model="main/model",
                                  pad_model="dedicated/model", component_family="single_smd")
    errors, done, progress = _run(worker)
    assert not errors and done == ["params"]
    assert seen["asked"] == ("dedicated/model", "main/model", "single_smd")
    assert seen["model"] == "chosen/pad-model"
    assert "chosen/pad-model" in _text(progress)


def test_cancel_before_the_paid_call_reaches_error_not_done(qapp, monkeypatch):
    """@brief Positive: «Interrompi» already pressed: no request starts, the stop is reported."""
    called = []

    def fake_reread(stored, documents, *, model, call, progress, **kwargs):
        called.append(True)
        return call(lambda **_kw: {}, model=model, tool={"name": "t"})

    monkeypatch.setattr(datasheet_extractor, "reread_body_height", fake_reread, raising=False)
    worker = workers.RereadWorker({}, [], None, field="body_height_mm", model="main/model")
    worker.cancel_event.set()
    errors, done, _ = _run(worker)
    assert not done and not called and len(errors) == 1


def test_cancel_during_the_function_is_honoured_by_the_logged_call(qapp, monkeypatch):
    """@brief Positive: the cancel checker given to make_logged_call stops a later paid call."""
    paid = []

    def fake_reread(stored, documents, *, model, call, progress, cancel=None, **kwargs):
        assert isinstance(cancel, threading.Event)
        cancel.set()
        return call(lambda **_kw: paid.append(True), model=model, tool={"name": "t"})

    monkeypatch.setattr(datasheet_extractor, "reread_body_height", fake_reread, raising=False)
    worker = workers.RereadWorker({}, [], None, field="body_height_mm", model="main/model")
    errors, done, _ = _run(worker)
    assert not done and not paid and len(errors) == 1


def test_missing_extractor_function_is_reported_not_raised(qapp, monkeypatch):
    """@brief Negative: an extractor without the API gives one error signal and no crash."""
    monkeypatch.delattr(datasheet_extractor, "reread_pad_positions", raising=False)
    worker = workers.RereadWorker({}, [], None, field="pad_layout", model="main/model")
    errors, done, _ = _run(worker)
    assert not done and len(errors) == 1 and "reread_pad_positions" in errors[0]


def test_unknown_field_is_reported(qapp):
    """@brief Negative: a field with no re-read function is refused through `error`."""
    worker = workers.RereadWorker({}, [], None, field="contacts_view")
    errors, done, _ = _run(worker)
    assert not done and len(errors) == 1 and "contacts_view" in errors[0]


def test_extraction_error_and_other_failures_reach_error(qapp, monkeypatch):
    """@brief Negative: ExtractionError and unexpected failures are reported, never raised."""
    for exc in (datasheet_extractor.ExtractionError("quota mancante"), ValueError("boom")):
        def failing(*args, _exc=exc, **kwargs):
            raise _exc

        monkeypatch.setattr(datasheet_extractor, "reread_body_height", failing, raising=False)
        worker = workers.RereadWorker({}, [], None, field="body_height_mm", model="main/model")
        errors, done, _ = _run(worker)
        assert not done and len(errors) == 1 and str(exc) in errors[0]


def test_cancelled_call_inside_the_function_is_reported_as_cancel(qapp, monkeypatch):
    """@brief Positive: a CallCancelled raised by the service is reported with its own text."""
    def fake_reread(*args, **kwargs):
        raise CallCancelled("fermato")

    monkeypatch.setattr(datasheet_extractor, "reread_body_height", fake_reread, raising=False)
    worker = workers.RereadWorker({}, [], None, field="body_height_mm", model="main/model")
    errors, done, _ = _run(worker)
    assert errors == ["fermato"] and not done


def test_a_reread_that_falls_back_on_the_main_model_says_so(qapp, monkeypatch):
    """@brief Negative (A2): when the reader rule falls back on the main model, `same_model=True` is
    passed, so the extractor counts the new list and the stored one as ONE witness."""
    seen = {}

    def fake_reread(stored, documents, *, model, call, progress, same_model=None, **kwargs):
        seen["model"], seen["same_model"] = model, same_model
        return "params"

    monkeypatch.setattr(datasheet_extractor, "reread_body_height", fake_reread, raising=False)
    monkeypatch.setattr(datasheet_extractor, "_pad_reading_model",
                        lambda pad_model, main, family, progress, check: main)
    monkeypatch.setattr(providers, "ensure_providers_ready", lambda models, progress=None: None)
    worker = workers.RereadWorker({}, [], None, field="body_height_mm", model="main/model",
                                  pad_model="dedicated/model", component_family="single_tht")
    errors, done, _ = _run(worker)
    assert not errors and done == ["params"]
    assert seen == {"model": "main/model", "same_model": True}
