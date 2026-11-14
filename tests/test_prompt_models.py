"""@brief The model of every analysis step (user request 2026-11-12, «Modello AI» table)."""
import pytest

from kicadgen.services import prompt_catalog as pc
from kicadgen.services import prompt_models as pm
from kicadgen.services.analysis_profiles import ProfileError, ProfileStore


def test_every_step_is_a_registered_prompt_and_every_prompt_with_a_call_has_a_step():
    """@brief The steps are profile prompts; the prompts riding inside another request map to it."""
    fields = set(pc.fields())
    assert {item.field for item in pm.STEPS} <= fields
    assert set(pm.STEP_OF_FIELD) <= fields and set(pm.STEP_OF_FIELD.values()) <= {s.field for s in pm.STEPS}
    # Role prompts and image instructions are part of another request: no step, no model.
    assert all(not f.startswith("role_prompt_") for f in (s.field for s in pm.STEPS))


def test_a_step_uses_its_model_else_the_fallback():
    """@brief Positive: an own model wins; negative: blank, absent or a non-step field keep the fallback."""
    models = pm.step_models({"numbering_prompt": "vendor/cheap", "system_prompt": ""}, "vendor/pad")
    assert pm.model_for("numbering_prompt", models, "main") == "vendor/cheap"
    assert pm.model_for("system_prompt", models, "main") == "main", "blank = the single model"
    assert pm.model_for("crop_prompt", models, "main") == "main", "no choice = the single model"
    assert pm.model_for("role_prompt_top", models, "main") == "main", "not a step"
    assert pm.model_for("numbering_prompt", None, "main") == "main", "no table: as before"


def test_the_pad_steps_keep_the_old_pad_model_until_a_choice_is_saved():
    """@brief Migration: the old «Posizioni pad» model reads pad positions and body quotes as before."""
    models = pm.step_models({}, "openai/gpt-5.6-sol")
    assert models["pad_positions_prompt"] == models["body_dimensions_prompt"] == "openai/gpt-5.6-sol"
    models = pm.step_models({"body_dimensions_prompt": ""}, "openai/gpt-5.6-sol")
    assert models["body_dimensions_prompt"] == "", "a saved «single model» wins over the old field"
    assert models["pad_positions_prompt"] == "openai/gpt-5.6-sol"


def test_prompts_riding_inside_a_request_use_its_step():
    models = {"inventory_prompt": "vendor/vision", "verification_prompt": "vendor/check"}
    assert pm.model_for("inventory_enumerate_prompt", models, "main") == "vendor/vision"
    assert pm.model_for("inventory_enumerate_empty_prompt", models, "main") == "vendor/vision"
    assert pm.model_for("mechanical_evidence_prompt", models, "main") == "vendor/check"


def test_the_store_saves_step_models_globally_and_refuses_unknown_fields(tmp_path):
    store = ProfileStore(tmp_path / "p.sqlite3")
    before = store.defaults()
    assert store.step_models_stored() == {}
    store.save_step_models({"numbering_prompt": " vendor/cheap ", "crop_prompt": ""})
    assert store.step_models_stored() == {"numbering_prompt": "vendor/cheap", "crop_prompt": ""}
    assert store.step_models()["numbering_prompt"] == "vendor/cheap"
    assert store.defaults() == before, "the AI defaults are untouched"
    with pytest.raises(ProfileError):
        store.save_step_models({"role_prompt_top": "x"})
    with pytest.raises(ProfileError):
        store.save_step_models({"numbering_prompt": None})


def _catalog(monkeypatch, tmp_path, rows):
    """A fresh cached OpenRouter catalogue with `rows` (no network)."""
    import json
    from kicadgen.services import openrouter
    path = tmp_path / "models.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(openrouter, "MODELS_CACHE_PATH", path)


_ROWS = [{"id": "deepseek/text-only", "architecture": {"input_modalities": ["text"]}},
         {"id": "deepseek/vision", "architecture": {"input_modalities": ["text", "image"]}},
         {"id": "vendor/text-only", "architecture": {"input_modalities": ["text"]}},
         {"id": "vendor/vision", "architecture": {"input_modalities": ["text", "image"]}}]


def test_an_unusable_step_model_falls_back_with_a_log_line(monkeypatch, tmp_path):
    """@brief A model declared text-only cannot read images: the step falls back and says why.

    Same rule as the model picker (openrouter.image_capability): DeepSeek
    direct borrows OpenRouter's declaration, an OpenRouter model has its own.
    """
    _catalog(monkeypatch, tmp_path, _ROWS)
    lines = []
    models = {"pad_positions_prompt": "deepseek-api/text-only"}
    assert pm.usable_model("pad_positions_prompt", models, "deepseek-api/main", lines.append) == "deepseek-api/main"
    assert lines and "deepseek-api/text-only" in lines[0]
    # Negative: a model that reads images, on the fallback's own provider, is used as chosen.
    lines.clear()
    assert pm.usable_model("pin_type_prompt", {"pin_type_prompt": "deepseek-api/vision"},
                           "deepseek-api/main", lines.append) == "deepseek-api/vision"
    assert lines == []
    # Every step sends documents or photos (the pin-type step sends the datasheet).
    assert all(item.images for item in pm.STEPS)


def test_an_openrouter_text_only_step_model_falls_back_and_an_unlisted_one_is_kept(monkeypatch,
                                                                                   tmp_path):
    """@brief Positive: an OpenRouter model declared text only falls back; negative: one not in
    the catalogue (capability undeclared) is never refused."""
    _catalog(monkeypatch, tmp_path, _ROWS)
    lines = []
    assert pm.usable_model("crop_prompt", {"crop_prompt": "vendor/text-only"},
                           "vendor/main", lines.append) == "vendor/main"
    assert lines and "vendor/text-only" in lines[0]
    lines.clear()
    for chosen in ("vendor/vision", "vendor/unlisted"):
        assert pm.usable_model("crop_prompt", {"crop_prompt": chosen},
                               "vendor/main", lines.append) == chosen
    assert lines == []


def test_save_models_writes_defaults_and_steps_in_one_transaction(tmp_path):
    """@brief Positive: both saved together; negative: one invalid value writes neither (review finding 7)."""
    store = ProfileStore(tmp_path / "p.sqlite3")
    store.save_models(defaults={"model": " vendor/single "},
                      steps={"numbering_prompt": " vendor/cheap "})
    assert store.defaults()["model"] == "vendor/single"
    assert store.step_models_stored() == {"numbering_prompt": "vendor/cheap"}
    before_defaults, before_steps = store.defaults(), store.step_models_stored()
    # An invalid step aborts the default too, and an invalid default the step.
    with pytest.raises(ProfileError):
        store.save_models(defaults={"model": "vendor/other"}, steps={"role_prompt_top": "x"})
    with pytest.raises(ProfileError):
        store.save_models(defaults={"no_such_field": "x"}, steps={"crop_prompt": "vendor/c"})
    with pytest.raises(ProfileError):
        store.save_models(defaults={"model": "   "}, steps={"crop_prompt": "vendor/c"})
    assert store.defaults() == before_defaults
    assert store.step_models_stored() == before_steps


def test_save_models_rolls_back_when_the_write_fails(tmp_path, monkeypatch):
    """@brief A failure inside the write leaves neither the defaults nor the steps saved."""
    import sqlite3
    store = ProfileStore(tmp_path / "p.sqlite3")
    before_defaults = store.defaults()
    real = store._writing

    from contextlib import contextmanager

    @contextmanager
    def failing():
        with real() as db:
            class Proxy:
                """Forward to the connection, failing the metadata write after the defaults."""
                def executemany(self, sql, rows):
                    if "metadata" in sql:
                        raise sqlite3.OperationalError("disk full")
                    return db.executemany(sql, rows)
            yield Proxy()

    monkeypatch.setattr(store, "_writing", failing)
    with pytest.raises(sqlite3.OperationalError):
        store.save_models(defaults={"model": "vendor/new"}, steps={"crop_prompt": "vendor/c"})
    monkeypatch.undo()
    assert store.defaults() == before_defaults
    assert store.step_models_stored() == {}
