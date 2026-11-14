"""@brief The dedicated pad reader is refused only when its provider DECLARES it text-only.

`datasheet_extractor._pad_reading_model` asks `openrouter.image_capability`,
the one rule of the model picker and of the send-time checks, for both
providers: a declared text-only reader falls back to the main model (the
reading is made on drawing images), an undeclared one is kept. No network:
the OpenRouter catalogue is a cached file written by the test.
"""
import json

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import openrouter

_ROWS = [{"id": "vendor/text-only", "architecture": {"input_modalities": ["text"]}},
         {"id": "vendor/vision", "architecture": {"input_modalities": ["text", "image"]}},
         {"id": "vendor/silent", "architecture": {}}]


def _catalog(monkeypatch, tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_ROWS), encoding="utf-8")
    monkeypatch.setattr(openrouter, "MODELS_CACHE_PATH", path)


def _pick(model, logs):
    # Same provider as the main model: no key or credit check is made.
    return ext._pad_reading_model(model, "vendor/main", "single_tht", logs.append, None)


def test_an_openrouter_reader_declared_text_only_falls_back_to_the_main_model(monkeypatch,
                                                                                tmp_path):
    _catalog(monkeypatch, tmp_path)
    logs = []
    assert _pick("vendor/text-only", logs) == "vendor/main"
    assert any("solo testo" in line for line in logs)


def test_an_undeclared_or_image_reader_is_kept(monkeypatch, tmp_path):
    """Negative: no declaration, or image declared, never refuses the reader."""
    _catalog(monkeypatch, tmp_path)
    logs = []
    assert _pick("vendor/silent", logs) == "vendor/silent"
    assert _pick("vendor/vision", logs) == "vendor/vision"
    assert _pick("vendor/not-in-catalogue", logs) == "vendor/not-in-catalogue"
    assert not logs
