"""@brief Models that cannot read images are not choosable for the steps that send images.

User request (2026-11-14): in the model lists (DeepSeek direct and
OpenRouter), a model declared text only is greyed out and cannot be chosen
in the list of a step that sends images; a model whose capability is not
declared stays choosable with a note; a step that sends only text keeps every
model choosable. One rule, `openrouter.image_capability`, serves the picker
and the send-time checks. Offline: the OpenRouter catalogue is a cache file
written by the test, the list download is replaced.
"""
import itertools
import json
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from kicadgen.services import openrouter
from kicadgen.services.openrouter import (DATASHEET_CAPABILITIES, IMAGES_NOT_READ, IMAGES_READ,
                                          IMAGES_UNDECLARED, PHOTO_CAPABILITIES, ModelInfo)

#: Capabilities of a step that sends only text (settings_dialog._TEXT_ONLY_CAPABILITIES).
TEXT_STEP = ("tools",)

#: OpenRouter catalogue rows: the OpenRouter models' own declarations and
#: the ones the DeepSeek direct models borrow.
_ROWS = [{"id": "deepseek/deepseek-v4-pro", "architecture": {"input_modalities": ["text"]}},
         {"id": "deepseek/deepseek-flash", "architecture": {"input_modalities": ["text", "image"]}},
         {"id": "deepseek/deepseek-quiet", "architecture": {}},
         {"id": "vendor/text-only", "architecture": {"input_modalities": ["text", "file"]},
          "supported_parameters": ["tools"]},
         {"id": "vendor/other-text", "architecture": {"input_modalities": ["text"]},
          "supported_parameters": ["tools"]},
         {"id": "vendor/vision", "architecture": {"input_modalities": ["text", "image", "file"]},
          "supported_parameters": ["tools"]},
         {"id": "vendor/quiet", "architecture": {}, "supported_parameters": ["tools"]}]

#: A new file name per catalogue: the parsed catalogue is memoised per file version.
_SEQUENCE = itertools.count()


def _catalog(monkeypatch, tmp_path, rows=_ROWS, age_s=0.0):
    """Write a cached OpenRouter catalogue `age_s` seconds old (never a download)."""
    path = tmp_path / f"models-{next(_SEQUENCE)}.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    if age_s:
        stamp = path.stat().st_mtime - age_s
        os.utime(path, (stamp, stamp))
    monkeypatch.setattr(openrouter, "MODELS_CACHE_PATH", path)


def _openrouter(identity, modalities):
    """An OpenRouter row that declares its own modalities, with tool calling."""
    return ModelInfo(id=identity, name=identity, prompt_usd_per_mtok=.3,
                     completion_usd_per_mtok=1.2, context_length=128000,
                     input_modalities=modalities, supports_tools=True)


def _deepseek(name):
    """A DeepSeek direct row: the provider declares neither prices nor capabilities."""
    return ModelInfo(id="deepseek-api/" + name, name=name, prompt_usd_per_mtok=math.nan,
                     completion_usd_per_mtok=math.nan, context_length=0, input_modalities=(),
                     supports_tools=False, provider="deepseek", capabilities_declared=False)


TEXT_ONLY = _openrouter("vendor/text-only", ("text", "file"))
VISION = _openrouter("vendor/vision", ("text", "image", "file"))
QUIET = _openrouter("vendor/quiet", ())


# ── The rule ────────────────────────────────────────────────────────────────

def test_the_capability_of_an_openrouter_model_is_its_fresh_catalogue_entry(monkeypatch, tmp_path):
    """@brief Positive «no» and «yes» from the fresh catalogue; an empty declaration is undeclared.

    @note CONTRACT CHANGED 2026-11-14 (review M1): a ModelInfo row no longer
        answers from its own modalities, which may come from an expired cache
        (offline fallback of list_models): only the fresh catalogue judges,
        as at the call. Negative: the same rows with a stale catalogue.
    """
    _catalog(monkeypatch, tmp_path)
    assert openrouter.image_capability(TEXT_ONLY) == IMAGES_NOT_READ
    assert openrouter.image_capability(VISION.id) == IMAGES_READ
    assert openrouter.image_capability(QUIET) == IMAGES_UNDECLARED
    _catalog(monkeypatch, tmp_path, age_s=openrouter.MODELS_CACHE_TTL_S + 60)
    assert openrouter.image_capability(TEXT_ONLY) == IMAGES_UNDECLARED
    assert openrouter.image_capability(VISION) == IMAGES_UNDECLARED


def test_a_deepseek_direct_model_borrows_the_fresh_openrouter_declaration(monkeypatch, tmp_path):
    """@brief DeepSeek direct: «deepseek-api/<name>» reads «deepseek/<name>» of the catalogue."""
    _catalog(monkeypatch, tmp_path)
    assert openrouter.image_capability("deepseek-api/deepseek-v4-pro") == IMAGES_NOT_READ
    assert openrouter.image_capability(_deepseek("deepseek-v4-pro")) == IMAGES_NOT_READ
    assert openrouter.image_capability("deepseek-api/deepseek-flash") == IMAGES_READ
    # Negative: listed without modalities, or not listed at all, is undeclared.
    assert openrouter.image_capability("deepseek-api/deepseek-quiet") == IMAGES_UNDECLARED
    assert openrouter.image_capability("deepseek-api/deepseek-unlisted") == IMAGES_UNDECLARED
    # The send-time check is the same rule.
    assert openrouter.deepseek_declared_text_only("deepseek-api/deepseek-v4-pro") is True
    assert openrouter.deepseek_declared_text_only("deepseek-api/deepseek-flash") is False
    # The OpenRouter route of the same name is not a DeepSeek direct model.
    assert openrouter.deepseek_declared_text_only("deepseek/deepseek-v4-pro") is False


def test_a_stale_or_missing_catalogue_is_undeclared_never_text_only(monkeypatch, tmp_path):
    """@brief Stale data never refuses: the model may have gained image input since."""
    _catalog(monkeypatch, tmp_path, age_s=openrouter.MODELS_CACHE_TTL_S + 60)
    assert openrouter.image_capability("deepseek-api/deepseek-v4-pro") == IMAGES_UNDECLARED
    assert openrouter.deepseek_declared_text_only("deepseek-api/deepseek-v4-pro") is False
    monkeypatch.setattr(openrouter, "MODELS_CACHE_PATH", tmp_path / "absent.json")
    assert openrouter.image_capability("deepseek-api/deepseek-v4-pro") == IMAGES_UNDECLARED


def test_the_catalogue_is_parsed_once_per_file_version(monkeypatch, tmp_path):
    """@brief Repeated questions share one parse; a rewritten file is parsed again (review L1)."""
    _catalog(monkeypatch, tmp_path)
    reads = []
    original = openrouter._load_models_cache
    monkeypatch.setattr(openrouter, "_load_models_cache",
                        lambda ignore_ttl=False: reads.append(1) or original(ignore_ttl))
    caps = openrouter.image_capabilities([TEXT_ONLY, VISION, _deepseek("deepseek-v4-pro")])
    assert caps == {TEXT_ONLY.id: IMAGES_NOT_READ, VISION.id: IMAGES_READ,
                    "deepseek-api/deepseek-v4-pro": IMAGES_NOT_READ}
    openrouter.image_capability("deepseek-api/deepseek-flash")
    openrouter.deepseek_declared_text_only("deepseek-api/deepseek-v4-pro")
    assert reads == [1], "one parse for every question on the same file"
    # Negative: a new version of the file (another modification time) is parsed again.
    stamp = openrouter.MODELS_CACHE_PATH.stat().st_mtime - 30
    os.utime(openrouter.MODELS_CACHE_PATH, (stamp, stamp))
    openrouter.image_capability(VISION)
    assert reads == [1, 1]


def test_offline_with_an_expired_cache_the_list_and_the_call_agree(picker, monkeypatch, tmp_path):
    """@brief Review M1: list_models falls back to a 3-day-old cache; its rows are undeclared, never greyed.

    The send-time check sees the same «undeclared» and keeps the model.
    """
    import urllib.error
    import urllib.request
    from kicadgen.services import prompt_models
    _catalog(monkeypatch, tmp_path, age_s=3 * 24 * 3600)

    def offline(*args, **kwargs):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    rows = openrouter.list_models()
    assert {m.id for m in rows} >= {TEXT_ONLY.id, VISION.id}, "the expired cache answers"
    caps = openrouter.image_capabilities(rows)
    assert caps[TEXT_ONLY.id] == IMAGES_UNDECLARED
    dialog = picker(None, rows, PHOTO_CAPABILITIES)
    row = _row(dialog, TEXT_ONLY.id)
    assert row is not None and _enabled(dialog, row)
    assert "non dichiarata" in dialog.table.item(row, 0).toolTip()
    assert prompt_models.usable_model("crop_prompt", {"crop_prompt": TEXT_ONLY.id},
                                      VISION.id) == TEXT_ONLY.id
    # Positive counterpart: the same rows with a fresh catalogue are greyed.
    _catalog(monkeypatch, tmp_path)
    fresh = picker(None, rows, PHOTO_CAPABILITIES)
    fresh.check_compatible.setChecked(False)
    assert not _enabled(fresh, _row(fresh, TEXT_ONLY.id))


# ── The picker ──────────────────────────────────────────────────────────────

@pytest.fixture
def picker(qapp, monkeypatch, tmp_path):
    """Factory of pickers that never download: the list is fed by the test (None: not loaded).

    A fresh catalogue with `_ROWS` is in place; a test may write another one.
    """
    from kicadgen.gui import model_dialog
    _catalog(monkeypatch, tmp_path)
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "_load_models", lambda self, *a, **k: None)
    monkeypatch.setattr(model_dialog, "list_models", lambda *a, **k: pytest.fail("no download"))
    opened = []

    def make(current, models, capabilities, provider=None):
        dialog = model_dialog.ModelPickerDialog(current, None, capabilities=capabilities,
                                                provider=provider)
        opened.append(dialog)
        if models is not None:
            dialog._on_models_loaded(models)
        return dialog

    yield make
    for dialog in opened:
        dialog.close()
        dialog.deleteLater()


def _row(dialog, model_id):
    """The table row showing `model_id`, or None."""
    from kicadgen.gui.model_dialog import _COL_ID
    for row in range(dialog.table.rowCount()):
        if dialog.table.item(row, _COL_ID).text() == model_id:
            return row
    return None


def _enabled(dialog, row):
    """Whether the row can be selected (every cell enabled and selectable)."""
    from PyQt6.QtCore import Qt
    wanted = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    return all(dialog.table.item(row, c).flags() & wanted == wanted
               for c in range(dialog.table.columnCount()))


def _ok(dialog):
    from PyQt6.QtWidgets import QDialogButtonBox
    return dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)


@pytest.mark.parametrize("capabilities", [PHOTO_CAPABILITIES, DATASHEET_CAPABILITIES])
def test_a_text_only_model_is_greyed_and_unselectable_in_an_image_step(picker, capabilities):
    """@brief Positive: the text-only row is disabled, says why, and cannot become the choice."""
    dialog = picker(VISION.id, [TEXT_ONLY, VISION], capabilities)
    dialog.check_compatible.setChecked(False)
    row = _row(dialog, TEXT_ONLY.id)
    assert row is not None and not _enabled(dialog, row)
    assert "OpenRouter" in dialog.table.item(row, 0).toolTip()
    dialog.table.selectRow(row)
    assert dialog.selected_model() == VISION.id, "a disabled row never becomes the choice"
    assert not dialog.is_choosable(TEXT_ONLY.id)
    # The image-capable model next to it is a normal row, without a note.
    vision = _row(dialog, VISION.id)
    assert _enabled(dialog, vision) and dialog.table.item(vision, 0).toolTip() == ""
    assert _ok(dialog).isEnabled()


def test_the_same_model_is_choosable_in_a_text_only_step(picker):
    """@brief Negative of the above: a step that sends no image keeps every model choosable."""
    dialog = picker(None, [TEXT_ONLY, VISION], TEXT_STEP)
    row = _row(dialog, TEXT_ONLY.id)
    assert row is not None and _enabled(dialog, row)
    assert dialog.table.item(row, 0).toolTip() == ""
    dialog.table.selectRow(row)
    assert dialog.selected_model() == TEXT_ONLY.id and _ok(dialog).isEnabled()


def test_an_undeclared_capability_stays_choosable_with_a_note(picker):
    """@brief An OpenRouter row declaring no modality is neither hidden nor greyed: it carries a note."""
    dialog = picker(None, [QUIET, VISION], PHOTO_CAPABILITIES)
    assert dialog.check_compatible.isChecked()
    row = _row(dialog, QUIET.id)
    assert row is not None and _enabled(dialog, row), "undeclared is not «incompatible»"
    assert "non dichiarata" in dialog.table.item(row, 0).toolTip()
    dialog.table.selectRow(row)
    assert dialog.selected_model() == QUIET.id and _ok(dialog).isEnabled()
    assert "non dichiarata" in dialog.label_selected.text()


def test_deepseek_direct_rows_follow_the_borrowed_declaration(picker, monkeypatch, tmp_path):
    """@brief DeepSeek list: v4-pro (declared text only) greyed, flash normal, unlisted with a note."""
    _catalog(monkeypatch, tmp_path)
    models = [_deepseek("deepseek-v4-pro"), _deepseek("deepseek-flash"),
              _deepseek("deepseek-unlisted")]
    dialog = picker("deepseek-api/deepseek-flash", models, DATASHEET_CAPABILITIES,
                    provider="deepseek")
    assert dialog.check_compatible.isChecked()
    assert not _enabled(dialog, _row(dialog, "deepseek-api/deepseek-v4-pro"))
    flash = _row(dialog, "deepseek-api/deepseek-flash")
    assert _enabled(dialog, flash) and dialog.table.item(flash, 0).toolTip() == ""
    unlisted = _row(dialog, "deepseek-api/deepseek-unlisted")
    assert _enabled(dialog, unlisted)
    assert "non dichiarata" in dialog.table.item(unlisted, 0).toolTip()
    # Negative: the same list for a text-only step greys nothing.
    text_dialog = picker(None, models, TEXT_STEP, provider="deepseek")
    assert all(_enabled(text_dialog, row) for row in range(text_dialog.table.rowCount()))


def test_a_stale_catalogue_leaves_deepseek_models_choosable(picker, monkeypatch, tmp_path):
    """@brief Stale data: the text-only declaration is not trusted, the model stays choosable."""
    _catalog(monkeypatch, tmp_path, age_s=openrouter.MODELS_CACHE_TTL_S + 60)
    dialog = picker(None, [_deepseek("deepseek-v4-pro")], PHOTO_CAPABILITIES, provider="deepseek")
    row = _row(dialog, "deepseek-api/deepseek-v4-pro")
    assert _enabled(dialog, row)
    assert "non dichiarata" in dialog.table.item(row, 0).toolTip()


def test_a_saved_choice_now_text_only_is_shown_marked_and_never_changed_silently(picker):
    """@brief The saved model stays visible (even through the filter), marked; OK and accept refuse it.

    Cancel keeps the saved choice: nothing replaces it behind the user's back.
    """
    from PyQt6.QtWidgets import QDialog
    dialog = picker(TEXT_ONLY.id, [TEXT_ONLY, VISION], PHOTO_CAPABILITIES)
    assert dialog.check_compatible.isChecked()
    row = _row(dialog, TEXT_ONLY.id)
    assert row is not None, "the saved choice is not hidden by the compatibility filter"
    assert not _enabled(dialog, row)
    assert dialog.selected_model() == TEXT_ONLY.id, "not replaced by another model"
    assert TEXT_ONLY.id in dialog.label_selected.text()
    assert "OpenRouter" in dialog.label_selected.text()
    assert not _ok(dialog).isEnabled()
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and not dialog._closed
    # Choosing an image-capable model unlocks OK.
    dialog.table.selectRow(_row(dialog, VISION.id))
    assert dialog.selected_model() == VISION.id and _ok(dialog).isEnabled()
    # Negative: other text-only models are still hidden by the filter.
    other = _openrouter("vendor/other-text", ("text",))
    dialog._on_models_loaded([TEXT_ONLY, other, VISION])
    assert _row(dialog, other.id) is None


def test_the_worker_hands_the_capabilities_with_the_list(qapp, monkeypatch, tmp_path):
    """@brief The DeepSeek list's capabilities are computed in the worker (the cache read off the GUI thread)."""
    from kicadgen.gui import model_dialog
    _catalog(monkeypatch, tmp_path)
    monkeypatch.setattr(model_dialog, "deepseek_models", lambda: [_deepseek("deepseek-v4-pro")])
    worker = model_dialog._ModelsWorker(provider="deepseek")
    received = []
    worker.done.connect(lambda models, caps: received.append((models, caps)))
    worker.run()  # synchronous: no thread started
    assert received and received[0][1] == {"deepseek-api/deepseek-v4-pro": IMAGES_NOT_READ}
    worker._release()  # drops the exit-time wait, then deletes


# ── The «Modello AI» step table ─────────────────────────────────────────────

_TABLE_ROWS = [{"id": "vendor/text-only", "architecture": {"input_modalities": ["text", "file"]}},
               {"id": "vendor/vision", "architecture": {"input_modalities": ["text", "image"]}}]


@pytest.fixture
def step_table(qapp, monkeypatch, tmp_path):
    """Factory of «Modello AI» windows over a fresh offline catalogue, released after the test."""
    from kicadgen.gui.settings_dialog import LlmSettingsDialog
    _catalog(monkeypatch, tmp_path, _TABLE_ROWS)
    opened = []

    def make(steps):
        dialog = LlmSettingsDialog(current_model="vendor/vision", current_step_models=steps)
        opened.append(dialog)
        return dialog

    yield make
    for dialog in opened:
        dialog.close()
        dialog.deleteLater()


def _model_cell(dialog, field):
    from kicadgen.gui.settings_dialog import COL_MODEL
    return dialog.table.item(dialog._rows[field][0], COL_MODEL)


def test_a_saved_text_only_step_model_is_marked_without_opening_the_picker(step_table):
    """@brief Positive: warning colour, icon and a tooltip saying the step falls back to the default."""
    from kicadgen.gui import settings_dialog
    dialog = step_table({"crop_prompt": "vendor/text-only", "marking_prompt": "vendor/vision"})
    cell = _model_cell(dialog, "crop_prompt")
    assert cell.text() == "vendor/text-only"
    assert cell.foreground().color() == settings_dialog.WARNING_COLOR
    assert not cell.icon().isNull()
    assert "OpenRouter" in cell.toolTip() and "predefinito" in cell.toolTip()
    # Negative: an image-capable own model is a normal row, without icon or note.
    vision = _model_cell(dialog, "marking_prompt")
    assert vision.foreground().color() != settings_dialog.WARNING_COLOR
    assert vision.icon().isNull() and "non dichiarata" not in vision.toolTip()
    # Its text takes the style sheet's table colour: no colour of its own (the table palette's
    # Text drew it black on the dark table, practical test 2026-11-14).
    from PyQt6.QtCore import Qt
    assert vision.data(Qt.ItemDataRole.ForegroundRole) is None
    # Negative: a step following the default model is not judged here.
    follows = _model_cell(dialog, "inventory_prompt")
    assert follows.icon().isNull() and follows.foreground().color() == settings_dialog.MUTED_COLOR


def test_an_undeclared_step_model_gets_only_a_note(step_table):
    """@brief Undeclared is never a warning: normal colour, no icon, one line in the tooltip."""
    from kicadgen.gui import settings_dialog
    dialog = step_table({"crop_prompt": "vendor/unlisted"})
    cell = _model_cell(dialog, "crop_prompt")
    assert cell.foreground().color() != settings_dialog.WARNING_COLOR and cell.icon().isNull()
    assert "non dichiarata" in cell.toolTip()


def test_a_text_only_model_in_a_step_without_images_is_not_marked(qapp, monkeypatch, tmp_path):
    """@brief Negative: the same model on a step that sends no image is a normal own model."""
    import dataclasses
    from kicadgen.gui import settings_dialog
    monkeypatch.setattr(settings_dialog, "STEPS", tuple(
        dataclasses.replace(item, images=False) if item.field == "crop_prompt" else item
        for item in settings_dialog.STEPS))
    _catalog(monkeypatch, tmp_path, _TABLE_ROWS)
    dialog = settings_dialog.LlmSettingsDialog(current_model="vendor/vision",
                                               current_step_models={"crop_prompt": "vendor/text-only"})
    try:
        cell = _model_cell(dialog, "crop_prompt")
        assert cell.icon().isNull() and cell.foreground().color() != settings_dialog.WARNING_COLOR
        assert "OpenRouter" not in cell.toolTip() and "non dichiarata" not in cell.toolTip()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_the_mark_follows_a_new_pick_and_a_stale_catalogue_never_warns(step_table, monkeypatch, tmp_path):
    """@brief A pick re-reads the catalogue; a stale catalogue turns «text only» into a note."""
    from kicadgen.gui import settings_dialog
    dialog = step_table({"crop_prompt": "vendor/vision"})
    assert _model_cell(dialog, "crop_prompt").icon().isNull()
    monkeypatch.setattr(settings_dialog, "pick_model", lambda *a, **k: "vendor/text-only")
    dialog._pick_step_model("crop_prompt")
    assert not _model_cell(dialog, "crop_prompt").icon().isNull()
    # The catalogue ages past its refresh period: no warning, only the note.
    _catalog(monkeypatch, tmp_path, _TABLE_ROWS, age_s=openrouter.MODELS_CACHE_TTL_S + 60)
    monkeypatch.setattr(settings_dialog, "pick_model", lambda *a, **k: None)
    dialog._pick_step_model("crop_prompt")
    dialog._refresh_rows()
    cell = _model_cell(dialog, "crop_prompt")
    assert cell.icon().isNull() and "non dichiarata" in cell.toolTip()


def test_a_text_only_default_model_is_marked_in_its_row(qapp, monkeypatch, tmp_path):
    """@brief Positive: warning icon, colour and a tooltip saying image steps would refuse it.

    Negative: an image-capable default and an undeclared one are not marked.
    """
    from kicadgen.gui import settings_dialog
    _catalog(monkeypatch, tmp_path, _TABLE_ROWS)
    marks = {}
    for model in ("vendor/text-only", "vendor/vision", "vendor/unlisted"):
        dialog = settings_dialog.LlmSettingsDialog(current_model=model, current_step_models={})
        try:
            marks[model] = (not dialog.label_default_warning.isHidden(),
                            dialog.field_model.toolTip(), dialog.field_model.styleSheet())
        finally:
            dialog.close()
            dialog.deleteLater()
    shown, tooltip, style = marks["vendor/text-only"]
    assert shown and "OpenRouter" in tooltip and "vendor/text-only" in tooltip
    assert settings_dialog.WARNING_COLOR.name() in style
    for model in ("vendor/vision", "vendor/unlisted"):
        assert marks[model] == (False, "", ""), model


def test_the_default_model_picker_greys_text_only_models(qapp, monkeypatch, tmp_path):
    """@brief «Cambia…» of the default model opens a picker for image steps: text-only is greyed.

    The default model serves every step that sends images, so its picker uses
    the default capabilities (images included); the change is then re-marked.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QDialog
    from kicadgen.gui import model_dialog, settings_dialog
    _catalog(monkeypatch, tmp_path, _TABLE_ROWS)
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "_load_models", lambda self, *a, **k: None)
    seen = {}

    def fake_exec(picker):
        picker._on_models_loaded([TEXT_ONLY, VISION])
        picker.check_compatible.setChecked(False)
        flags = picker.table.item(_row(picker, TEXT_ONLY.id), 0).flags()
        seen["text_only_enabled"] = bool(flags & Qt.ItemFlag.ItemIsEnabled)
        seen["vision_enabled"] = _enabled(picker, _row(picker, VISION.id))
        picker.table.selectRow(_row(picker, VISION.id))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "exec", fake_exec)
    dialog = settings_dialog.LlmSettingsDialog(current_model="vendor/text-only",
                                               current_step_models={})
    try:
        assert not dialog.label_default_warning.isHidden()
        dialog._pick_model()
        assert seen == {"text_only_enabled": False, "vision_enabled": True}
        assert dialog.model() == VISION.id and dialog.label_default_warning.isHidden()
    finally:
        dialog.close()
        dialog.deleteLater()


# ── Review of 2026-11-14 ────────────────────────────────────────────────────

def test_a_saved_text_only_choice_stays_refused_before_the_list_and_after_a_provider_switch(picker):
    """@brief Review M2: the saved choice's capability is known at open, whatever list is shown.

    Negative: an image-capable saved choice stays acceptable in the same states.
    """
    dialog = picker(TEXT_ONLY.id, None, PHOTO_CAPABILITIES)
    assert not dialog.is_choosable(TEXT_ONLY.id) and not _ok(dialog).isEnabled()
    dialog.accept()
    assert not dialog._closed
    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData("deepseek"))
    dialog._on_models_loaded([_deepseek("deepseek-flash")])
    assert dialog.selected_model() == TEXT_ONLY.id and not _ok(dialog).isEnabled()
    fine = picker(VISION.id, None, PHOTO_CAPABILITIES)
    assert fine.is_choosable(VISION.id) and _ok(fine).isEnabled()
    # A text-only step never refuses it.
    text_step = picker(TEXT_ONLY.id, None, TEXT_STEP)
    assert text_step.is_choosable(TEXT_ONLY.id) and _ok(text_step).isEnabled()


def test_the_saved_choice_is_exempt_only_from_the_image_requirement(picker):
    """@brief Review L4: a saved model without tool calling stays hidden by the filter; a text-only one shows."""
    no_tools = ModelInfo(id="vendor/vision", name="x", prompt_usd_per_mtok=.1,
                         completion_usd_per_mtok=.1, context_length=1,
                         input_modalities=("text", "image", "file"), supports_tools=False)
    dialog = picker(no_tools.id, [no_tools, TEXT_ONLY], PHOTO_CAPABILITIES)
    assert _row(dialog, no_tools.id) is None, "lacking tools: hidden as before"
    shown = picker(TEXT_ONLY.id, [TEXT_ONLY, VISION], PHOTO_CAPABILITIES)
    assert _row(shown, TEXT_ONLY.id) is not None and not _enabled(shown, _row(shown, TEXT_ONLY.id))


def test_a_borrowed_declaration_names_its_source_in_the_tooltip(picker):
    """@brief Review L3: a DeepSeek direct row names the OpenRouter entry its answer comes from."""
    dialog = picker(None, [_deepseek("deepseek-v4-pro")], PHOTO_CAPABILITIES, provider="deepseek")
    tooltip = dialog.table.item(_row(dialog, "deepseek-api/deepseek-v4-pro"), 0).toolTip()
    assert "deepseek/deepseek-v4-pro" in tooltip
    assert openrouter.catalog_id("deepseek-api/deepseek-v4-pro") == "deepseek/deepseek-v4-pro"
    assert openrouter.catalog_id(VISION.id) == VISION.id, "an OpenRouter id is its own entry"


def test_ok_on_the_unchanged_default_keeps_the_step_following_it(step_table, monkeypatch):
    """@brief Review M2: OK on the default the picker opened with is not a choice of the step.

    Negative: another model picked is stored as the step's own model.
    """
    from kicadgen.gui import settings_dialog
    dialog = step_table({})
    monkeypatch.setattr(settings_dialog, "pick_model", lambda current, *a, **k: current)
    dialog._pick_step_model("crop_prompt")
    assert dialog.step_models()["crop_prompt"] == ""
    monkeypatch.setattr(settings_dialog, "pick_model", lambda *a, **k: "vendor/other")
    dialog._pick_step_model("crop_prompt")
    assert dialog.step_models()["crop_prompt"] == "vendor/other"


def test_the_pre_run_check_names_the_image_steps_on_a_text_only_default(monkeypatch, tmp_path):
    """@brief Review L2: positive message for a text-only default used by image steps; negatives None."""
    from kicadgen.services import prompt_models
    _catalog(monkeypatch, tmp_path)
    message = prompt_models.default_model_image_problem(TEXT_ONLY.id, {}, ["crop_prompt"])
    assert message and TEXT_ONLY.id in message and "Lettura dei ritagli" in message
    assert "Inventario" not in message, "only the steps of the run are named"
    # Negatives: an image-capable or undeclared default, or every image step on its own model.
    assert prompt_models.default_model_image_problem(VISION.id, {}, None) is None
    assert prompt_models.default_model_image_problem("vendor/unlisted", {}, None) is None
    assert prompt_models.default_model_image_problem(
        TEXT_ONLY.id, {"crop_prompt": VISION.id}, ["crop_prompt"]) is None
    _catalog(monkeypatch, tmp_path, age_s=openrouter.MODELS_CACHE_TTL_S + 60)
    assert prompt_models.default_model_image_problem(TEXT_ONLY.id, {}, None) is None


# ── Practical test of 2026-11-14 ────────────────────────────────────────────

ALPHA = _openrouter("vendor/alpha", ("text", "image", "file"))
BETA = _openrouter("vendor/beta", ("text", "image", "file"))


def _selected_rows(dialog):
    """Model ids of the rows that look selected."""
    return [dialog.table.item(index.row(), 0).text()
            for index in dialog.table.selectionModel().selectedRows()]


def test_typing_a_search_never_changes_the_choice(picker):
    """@brief HIGH: the search hides rows but never picks another model, nor returns a hidden one.

    Positive: the saved model hidden by the search stays the choice (OK returns
    it unchanged); a model the user picked and the search then hides is kept
    but OK is locked until it is visible again. Negative: a search that keeps
    the chosen row visible keeps it selected, with OK enabled.
    """
    dialog = picker(VISION.id, [ALPHA, BETA, VISION], PHOTO_CAPABILITIES)
    assert _selected_rows(dialog) == [VISION.id]
    for typed in ("v", "vendor/al", "vendor/alpha", "zzz-nothing"):
        dialog.field_search.setText(typed)
        assert dialog.selected_model() == VISION.id, typed
        assert VISION.id not in _selected_rows(dialog) or typed == "v"
    assert dialog.table.rowCount() == 0
    assert "Nessuna selezione visibile" in dialog.label_selected.text()
    assert _ok(dialog).isEnabled(), "OK leaves the saved model unchanged"
    dialog.field_search.setText("")
    assert _selected_rows(dialog) == [VISION.id]
    # The user picks BETA; a search hiding it neither selects ALPHA nor allows OK.
    dialog.table.selectRow(_row(dialog, BETA.id))
    assert dialog.selected_model() == BETA.id
    dialog.field_search.setText("alpha")
    assert dialog.selected_model() == BETA.id and _selected_rows(dialog) == []
    assert not _ok(dialog).isEnabled() and BETA.id in dialog.label_selected.text()
    dialog.accept()
    assert not dialog._closed, "a hidden pick is never returned"
    # Negative: the pick visible again is selected and accepted.
    dialog.field_search.setText("beta")
    assert _selected_rows(dialog) == [BETA.id] and _ok(dialog).isEnabled()
    assert "Nessuna selezione visibile" not in dialog.label_selected.text()


def test_greyed_rows_carry_their_own_grey_under_the_style_sheet(picker):
    """@brief The app style sheet gives every table item one colour: the disabled grey is on the item.

    Negative: an enabled row keeps the default foreground.
    """
    from kicadgen.gui import model_dialog
    dialog = picker(None, [TEXT_ONLY, VISION], PHOTO_CAPABILITIES)
    dialog.check_compatible.setChecked(False)
    grey = dialog.table.item(_row(dialog, TEXT_ONLY.id), 0).foreground().color()
    assert grey == model_dialog._DISABLED_TEXT
    normal = dialog.table.item(_row(dialog, VISION.id), 0).foreground().color()
    assert normal != model_dialog._DISABLED_TEXT


def test_the_provider_note_takes_no_room_when_empty(picker):
    """@brief OpenRouter has no note: the label is hidden; DeepSeek's note is shown and names no model."""
    dialog = picker(None, [], PHOTO_CAPABILITIES, provider="openrouter")
    assert dialog.label_provider_note.isHidden()
    deepseek = picker(None, [], PHOTO_CAPABILITIES, provider="deepseek")
    assert not deepseek.label_provider_note.isHidden()
    assert "deepseek-flash" not in deepseek.label_provider_note.text()


def _open_table(qapp, monkeypatch, step_table, steps, screen_width):
    """A «Modello AI» window with its step section open on a screen `screen_width` px wide."""
    from PyQt6.QtCore import QRect
    from kicadgen.gui import settings_dialog
    monkeypatch.setattr(settings_dialog.LlmSettingsDialog, "_available_geometry",
                        lambda self: QRect(0, 0, screen_width, 1040))
    monkeypatch.setattr(settings_dialog.LlmSettingsDialog, "_keyed_providers",
                        lambda self: ("openrouter", "deepseek"))
    dialog = step_table(steps)
    dialog._refresh_key_state()
    dialog.show()
    dialog.steps_box.setExpanded(True)
    qapp.processEvents()
    return dialog


def test_the_model_column_fits_its_texts_and_the_hints_wrap_on_a_small_screen(qapp, monkeypatch,
                                                                              step_table):
    """@brief Positive: on an 800 px screen the model column still holds «= modello predefinito» and the
    longest id, without horizontal scroll, and the step hints wrap (taller rows). Negative: on a wide
    screen the hints stay on one line."""
    from PyQt6.QtGui import QFont, QFontMetrics
    from kicadgen.gui.settings_dialog import COL_MODEL, COL_STEP
    steps = {"crop_prompt": "vendor/model-5.6-sol"}
    wide = _open_table(qapp, monkeypatch, step_table, steps, 1920)
    wide_height = wide.table.rowHeight(wide._rows["inventory_prompt"][0])
    small = _open_table(qapp, monkeypatch, step_table, steps, 800)
    table = small.table
    assert table.horizontalScrollBar().maximum() == 0
    bold = QFont(table.font())
    bold.setBold(True)
    needed = max(QFontMetrics(table.font()).horizontalAdvance("= modello predefinito"),
                 QFontMetrics(bold).horizontalAdvance(steps["crop_prompt"]))
    assert table.columnWidth(COL_MODEL) >= needed
    assert table.columnWidth(COL_MODEL) >= table.horizontalHeader().sectionSizeHint(COL_MODEL)
    small_height = table.rowHeight(small._rows["inventory_prompt"][0])
    assert small_height > wide_height, "the hint wraps instead of being cut"
    assert table.columnWidth(COL_STEP) < wide.table.columnWidth(COL_STEP)


def test_an_id_too_long_for_the_screen_is_capped_and_named_in_full_by_its_tooltip(qapp, monkeypatch,
                                                                                 step_table):
    """@brief Negative of the fit: an id wider than a third of an 800 px screen is capped (elided),
    the window still needs no horizontal scroll, and the tooltip names the id in full."""
    from kicadgen.gui.settings_dialog import COL_MODEL
    long_id = "vendor/" + "a-rather-long-model-identifier-" * 3 + "v2"
    dialog = _open_table(qapp, monkeypatch, step_table, {"crop_prompt": long_id}, 800)
    assert dialog.table.horizontalScrollBar().maximum() == 0
    assert dialog.table.columnWidth(COL_MODEL) <= max(200, (800 - 16) // 3)
    assert _model_cell(dialog, "crop_prompt").toolTip().startswith(long_id + "\n")


def test_a_model_cell_names_the_full_id_and_following_rows_note_an_undeclared_default(step_table):
    """@brief The own model's tooltip starts with its id; image steps on an undeclared default get the note.

    Negative: an image-capable default adds no note to the following rows.
    """
    dialog = step_table({"crop_prompt": "vendor/vision"})
    assert _model_cell(dialog, "crop_prompt").toolTip().startswith("vendor/vision\n")
    assert "non dichiarata" not in _model_cell(dialog, "inventory_prompt").toolTip()
    dialog._set_single_model("vendor/unlisted")
    following = _model_cell(dialog, "inventory_prompt")
    assert "vendor/unlisted" in following.toolTip() and "non dichiarata" in following.toolTip()
    assert following.icon().isNull(), "a note, not a warning"
