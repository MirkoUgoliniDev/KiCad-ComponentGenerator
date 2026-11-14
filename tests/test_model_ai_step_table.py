"""@brief «Modello AI» step table (user request 2026-11-12): one model per analysis step, chosen globally.

Covers the window (rows, groups, two-line step cells, «Cambia…», «Usa l'unico»,
restore of the steps only, round trip, no prompt editor, keyboard access and
scrolling, size fitted to every row within the screen, key warnings, session
note), the main window's save path, the key check of every paid command, the
log lines naming the resolved model, the workers forwarding `step_models` and
the translation of the new texts.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtWidgets import QDialog, QPlainTextEdit, QTextEdit

from kicadgen.gui import settings_dialog, workers
from kicadgen.gui.settings_dialog import COL_MODEL, COL_STEP, HINT_ROLE, LlmSettingsDialog
from kicadgen.services.datasheet_extractor import DEFAULT_PAD_POSITIONS_MODEL
from kicadgen.services.openrouter import (DATASHEET_CAPABILITIES, DEFAULT_MODEL,
                                          PHOTO_CAPABILITIES)
from kicadgen.services.prompt_models import LEGACY_PAD_STEPS, STEPS

GROUPS = list(dict.fromkeys(item.group for item in STEPS))


def _rows(dialog):
    """(step text, model text, field) of every table row; field None on a group row."""
    rows = []
    for row in range(dialog.table.rowCount()):
        step = dialog.table.item(row, COL_STEP)
        model = dialog.table.item(row, COL_MODEL)
        rows.append((step.text(), model.text() if model is not None else None,
                     step.data(Qt.ItemDataRole.UserRole)))
    return rows


def _keys(monkeypatch, *, openrouter_key, deepseek_key):
    """Fake the key sources of both providers (never the real Credential Manager)."""
    from kicadgen.services import openrouter, providers
    present = openrouter.ApiKeySource(openrouter.KEY_SOURCE_DOTENV, ".env")
    absent = openrouter.ApiKeySource(openrouter.KEY_SOURCE_NONE)
    monkeypatch.setattr(openrouter, "api_key_source",
                        lambda: present if openrouter_key else absent)
    monkeypatch.setattr(providers, "deepseek_key_source",
                        lambda: present if deepseek_key else absent)


@pytest.fixture
def dialog(qapp):
    """A «Modello AI» window on a DeepSeek single model, released after the test."""
    created = LlmSettingsDialog(current_model="deepseek-api/deepseek-flash",
                                current_step_models={"crop_prompt": "openai/gpt-4o"})
    yield created
    created.close()
    created.deleteLater()


# -- the window ---------------------------------------------------------------------------

def test_the_table_has_one_row_per_step_under_its_group_caption(dialog):
    """@brief Positive: groups in STEPS order, each followed by its steps; the hint is drawn under the title.

    @note Negative: a group row carries no step field, no hint and no buttons.
    """
    rows = _rows(dialog)
    assert dialog.table.rowCount() == len(STEPS) + len(GROUPS)
    expected = []
    for group in GROUPS:
        expected.append((group, None))
        expected.extend((item.title, item.field) for item in STEPS if item.group == group)
    assert [(text, field) for text, _model, field in rows] == expected
    for row, (_text, _model, field) in enumerate(rows):
        cell = dialog.table.item(row, COL_STEP)
        if field is None:
            assert dialog.table.cellWidget(row, 2) is None
            assert dialog.table.columnSpan(row, 0) == 3
            assert not cell.data(HINT_ROLE)
        else:
            hint = next(item.hint for item in STEPS if item.field == field)
            assert cell.data(HINT_ROLE) == hint and cell.toolTip() == hint
    assert not dialog.findChildren(QTextEdit) and not dialog.findChildren(QPlainTextEdit)


def test_the_step_column_header_is_not_the_pin_pitch(qapp, tmp_path):
    """@brief Finding 1: the header has its own source, «Passo dell'analisi», never the pin-pitch «Passo».

    @note Positive: English «Analysis step». Negative: not «Pitch», the pitch entry's English.
    """
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n

    class Owner(QDialog):
        """Parent carrying the catalogue, as the main window does."""
    owner = Owner()
    owner.i18n = I18n(ProfileStore(tmp_path / "p.sqlite3"), "en")
    d = LlmSettingsDialog(current_model="vendor/m", parent=owner)
    try:
        assert d.table.horizontalHeaderItem(COL_STEP).text() == "Passo dell'analisi"
        owner.i18n.retranslate_widgets(d)
        assert d.table.horizontalHeaderItem(COL_STEP).text() == "Analysis step"
        assert d.table.horizontalHeaderItem(COL_STEP).text() != owner.i18n.text("Passo")
    finally:
        d.deleteLater()
        owner.deleteLater()


def test_a_step_shows_the_model_it_really_uses(dialog):
    """@brief Positive: an own model shows its id in bold; a following step shows «= modello predefinito», muted.

    @note CONTRACT CHANGED 2026-11-13: «modello unico» renamed «modello predefinito» (user decision; EN default model, FR modèle par défaut, DE Standardmodell).

    @note Negative: changing the single model changes the following rows' tooltip, never an own one.
    """
    models = {field: model for _text, model, field in _rows(dialog) if field}
    assert models["crop_prompt"] == "openai/gpt-4o"
    assert models["inventory_prompt"] == "= modello predefinito"
    own_row = dialog._rows["crop_prompt"][0]
    single_row = dialog._rows["inventory_prompt"][0]
    own, follows = dialog.table.item(own_row, COL_MODEL), dialog.table.item(single_row, COL_MODEL)
    assert own.font().bold() and not follows.font().bold()
    assert not own.font().italic() and not follows.font().italic()
    assert follows.foreground().color() == settings_dialog.MUTED_COLOR
    assert own.foreground().color() != settings_dialog.MUTED_COLOR
    assert "deepseek-api/deepseek-flash" in follows.toolTip()
    dialog._set_single_model("deepseek-api/deepseek-pro")
    assert "deepseek-api/deepseek-pro" in dialog.table.item(single_row, COL_MODEL).toolTip()
    assert dialog.table.item(own_row, COL_MODEL).text() == "openai/gpt-4o"


def test_change_sets_one_step_and_use_single_clears_it(dialog, monkeypatch):
    """@brief «Cambia…» sets the step through the picker of every source, with the step's capabilities.

    @note Negative: «Usa il predefinito» is hidden (its room kept) on a step that already
        follows the default model, and clearing one step leaves the others unchanged.
    @note CONTRACT CHANGED 2026-11-13: «modello unico» renamed «modello predefinito» (user decision; EN default model, FR modèle par défaut, DE Standardmodell).
    """
    calls = []

    def pick(current, parent, **kwargs):
        calls.append((current, kwargs))
        return "vendor/reader"
    monkeypatch.setattr(settings_dialog, "pick_model", pick)
    _row, change, single = dialog._rows["pin_type_prompt"]
    assert single.isHidden() and single.sizePolicy().retainSizeWhenHidden()
    assert single.text() == "Usa il predefinito"
    change.click()
    assert dialog.step_models()["pin_type_prompt"] == "vendor/reader" and not single.isHidden()
    assert calls[-1][0] == "deepseek-api/deepseek-flash", "the picker opens on the model in use"
    assert "provider" not in calls[-1][1], "every source is listed"
    assert tuple(calls[-1][1]["capabilities"]) == tuple(DATASHEET_CAPABILITIES)
    dialog._rows["inventory_prompt"][1].click()
    assert tuple(calls[-1][1]["capabilities"]) == tuple(PHOTO_CAPABILITIES)
    single.click()
    assert dialog.step_models()["pin_type_prompt"] == "" and single.isHidden()
    assert dialog.step_models()["crop_prompt"] == "openai/gpt-4o"
    assert dialog.step_models()["inventory_prompt"] == "vendor/reader"
    # Cancelling the picker changes nothing.
    monkeypatch.setattr(settings_dialog, "pick_model", lambda *a, **k: None)
    dialog._rows["crop_prompt"][1].click()
    assert dialog.step_models()["crop_prompt"] == "openai/gpt-4o"


def test_changed_step_models_are_only_the_steps_the_user_changed(dialog, monkeypatch):
    """@brief Positive: a changed step is reported; negative: untouched or changed-back steps are not."""
    assert dialog.changed_step_models() == {}
    monkeypatch.setattr(settings_dialog, "pick_model", lambda *a, **k: "vendor/x")
    dialog._pick_step_model("marking_prompt")
    assert dialog.changed_step_models() == {"marking_prompt": "vendor/x"}
    dialog._step_follows_single("marking_prompt")
    assert dialog.changed_step_models() == {}


def test_restore_puts_only_the_steps_back_and_keeps_the_single_model(dialog):
    """@brief Finding 6. Positive: pad and body steps on their reader, the rest on the single model.

    @note Negative: the single model and its source do not change, and the crop step chosen
        before does not survive the restore.
    """
    source = dialog.source()
    dialog._restore_step_defaults()
    assert dialog.model() == "deepseek-api/deepseek-flash" and dialog.source() == source
    expected = {item.field: (DEFAULT_PAD_POSITIONS_MODEL if item.field in LEGACY_PAD_STEPS else "")
                for item in STEPS}
    assert dialog.step_models() == expected
    assert dialog.btn_restore_steps.text() == "Ripristina passi predefiniti"
    assert "non cambiano" in dialog.btn_restore_steps.toolTip()
    assert DEFAULT_PAD_POSITIONS_MODEL in dialog.btn_restore_steps.toolTip()


def test_step_models_round_trip_and_the_old_pad_argument(qapp):
    """@brief Positive: what the window receives is what it returns, one entry per step.

    @note Negative: without a table the old `current_pad_model` still seeds the two pad steps,
        and "" there means the single model.
    """
    given = {"system_prompt": "vendor/a", "marking_prompt": " vendor/b "}
    d = LlmSettingsDialog(current_model="vendor/m", current_step_models=given)
    try:
        result = d.step_models()
        assert set(result) == {item.field for item in STEPS}
        assert result["system_prompt"] == "vendor/a" and result["marking_prompt"] == "vendor/b"
        assert all(value == "" for field, value in result.items()
                   if field not in ("system_prompt", "marking_prompt"))
        assert d.vision_model() == d.verification_model() == d.model() == "vendor/m"
    finally:
        d.deleteLater()
    d = LlmSettingsDialog(current_model="vendor/m", current_pad_model="")
    try:
        assert all(d.step_models()[field] == "" for field in LEGACY_PAD_STEPS)
    finally:
        d.deleteLater()


def test_the_buttons_are_reachable_by_keyboard_and_named_after_their_step(dialog):
    """@brief Every row button takes Tab focus, names its step and describes it with the hint."""
    for item in STEPS:
        _row, change, single = dialog._rows[item.field]
        for button in (change, single):
            assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
            assert item.title in button.accessibleName()
            assert button.accessibleDescription() == item.hint
            assert not button.autoDefault(), "Enter must not trigger a row button"
        assert item.hint in change.toolTip()
    # «Cambia…» everywhere (finding 13): the single model's button uses the same verb.
    assert dialog.btn_pick_single.text() == "Cambia…"


def test_the_step_section_is_collapsed_by_default_and_counts_the_own_models(qapp, monkeypatch):
    """@brief User (2026-11-12, «dobbiamo semplificare la UX»): one model row, the steps in a closed section.

    @note Positive: the title counts the steps with a model of their own and follows every
        change. Negative: with none it says «nessuno»; the section never opens by itself.
    """
    _keys(monkeypatch, openrouter_key=True, deepseek_key=True)
    d = LlmSettingsDialog(current_model="vendor/m", current_step_models={})
    try:
        assert not d.steps_box.isExpanded()
        assert d.steps_box.baseTitle() == "Modelli diversi per singoli passi (nessuno)"
        d._steps["crop_prompt"] = "vendor/c"
        d._refresh_rows()
        assert d.steps_box.baseTitle().endswith("(1 passo con un modello proprio)")
        d._steps["marking_prompt"] = "vendor/s"
        d._refresh_rows()
        assert d.steps_box.baseTitle().endswith("(2 passi con un modello proprio)")
        d._restore_step_defaults()
        assert d.steps_box.baseTitle().endswith("(2 passi con un modello proprio)")
        assert not d.steps_box.isExpanded()
    finally:
        d.deleteLater()


def test_the_table_needs_no_horizontal_scroll_when_open(dialog, qapp, monkeypatch):
    """@brief The open section shows every column (long ids are elided, not scrolled) on a 1920 x 1040 screen."""
    _keys(monkeypatch, openrouter_key=True, deepseek_key=True)
    monkeypatch.setattr(LlmSettingsDialog, "_available_geometry",
                        lambda self: QRect(0, 0, 1920, 1040))
    dialog._refresh_key_state()
    dialog.show()
    dialog.steps_box.setExpanded(True)
    qapp.processEvents()
    assert dialog.table.horizontalScrollBar().maximum() == 0
    assert dialog.table.columnWidth(COL_MODEL) >= 200


def _shown_on_screen(qapp, monkeypatch, height, expanded=True):
    """A window shown on a fake screen `height` px tall (available area), section open or closed."""
    _keys(monkeypatch, openrouter_key=True, deepseek_key=True)
    monkeypatch.setattr(LlmSettingsDialog, "_available_geometry",
                        lambda self: QRect(0, 0, 1920, height))
    d = LlmSettingsDialog(current_model="vendor/m")
    d.show()
    qapp.processEvents()
    if expanded:
        d.steps_box.setExpanded(True)
        qapp.processEvents()
    return d


def test_the_window_is_compact_when_closed_and_grows_when_the_section_opens(qapp, monkeypatch):
    """@brief Positive: opening the section grows the window; negative: closing it again shrinks it back."""
    d = _shown_on_screen(qapp, monkeypatch, 1040, expanded=False)
    try:
        closed = d.height()
        assert d.table.isHidden() or not d.table.isVisible()
        d.steps_box.setExpanded(True)
        qapp.processEvents()
        assert d.table.isVisible() and d.height() > closed
        d.steps_box.setExpanded(False)
        qapp.processEvents()
        assert d.height() == closed
    finally:
        d.close()
        d.deleteLater()


def test_on_a_1080_px_screen_every_row_is_visible_without_scrolling(qapp, monkeypatch):
    """@brief User (2026-11-12): «aprila di default più alta». Positive: with the section open
    every row fits the viewport.

    @note The height comes from the real row heights after polish, not from a constant.
    """
    d = _shown_on_screen(qapp, monkeypatch, 1040)
    try:
        rows = sum(d.table.rowHeight(row) for row in range(d.table.rowCount()))
        assert d.table.verticalScrollBar().maximum() == 0
        assert d.table.viewport().height() >= rows
        assert d.height() <= 1040
    finally:
        d.close()
        d.deleteLater()


def test_on_a_small_screen_the_window_is_capped_and_tab_scrolls_to_the_focused_row(qapp, monkeypatch):
    """@brief Negative: a screen too short for every row caps the window and the table scrolls.

    @note A row button taking the focus (Tab) brings its row into view (finding 3).
    """
    d = _shown_on_screen(qapp, monkeypatch, 420)
    try:
        assert d.height() <= 420
        assert d.table.verticalScrollBar().maximum() > 0
        d.table.verticalScrollBar().setValue(0)
        row, change, _single = d._rows["pin_type_prompt"]
        change.setFocus(Qt.FocusReason.TabFocusReason)
        qapp.processEvents()
        rect = d.table.visualRect(d.table.model().index(row, COL_STEP))
        assert d.table.verticalScrollBar().value() > 0
        assert rect.top() >= 0 and rect.bottom() <= d.table.viewport().height()
    finally:
        d.close()
        d.deleteLater()


def test_the_model_picker_offers_only_the_allowed_sources(qapp, monkeypatch):
    """@brief model_dialog `providers`. Positive: only the allowed sources are listed, the
    list starting from the first one when the current model's source is not allowed.

    @note Negative: without `providers` every source is listed.
    """
    from kicadgen.gui import model_dialog
    from kicadgen.services.providers import PROVIDER_DEEPSEEK, PROVIDERS
    monkeypatch.setattr(model_dialog.ModelPickerDialog, "_load_models", lambda self, *a, **k: None,
                        raising=False)
    for allowed, expected in (((PROVIDER_DEEPSEEK,), [PROVIDER_DEEPSEEK]), (None, list(PROVIDERS))):
        kwargs = {"providers": allowed} if allowed is not None else {}
        dialog = model_dialog.ModelPickerDialog("vendor/data", None, **kwargs)
        try:
            listed = [dialog.combo_provider.itemData(i) for i in range(dialog.combo_provider.count())]
            assert listed == expected
            assert dialog.provider() == expected[0] if allowed else True
        finally:
            dialog._closed = True
            dialog.close()


def test_no_key_row_when_every_source_in_use_has_its_key(qapp, monkeypatch):
    """@brief User (2026-11-12, «togliamo questo»): with every key present nothing is shown below the table."""
    _keys(monkeypatch, openrouter_key=True, deepseek_key=True)
    d = LlmSettingsDialog(current_model="deepseek-api/deepseek-flash",
                          current_step_models={"crop_prompt": "openai/gpt-4o"})
    try:
        assert d.keys_row.isHidden()
        assert d.label_keys_used.text() == ""
    finally:
        d.deleteLater()


def test_a_missing_key_names_the_steps_and_what_happens_to_them(qapp, monkeypatch):
    """@brief Finding 7. Positive: a saved or changed OpenRouter step without a key is blocked, an
    unsaved factory reader falls back; both are named, in the warning colour, with «Account AI...».

    @note Negative: returning the steps to the single model removes the row.
    """
    _keys(monkeypatch, openrouter_key=False, deepseek_key=True)
    opened = []
    d = LlmSettingsDialog(current_model="deepseek-api/deepseek-flash",
                          current_step_models={"crop_prompt": "openai/gpt-4o",
                                               "pad_positions_prompt": "openai/gpt-5.6-sol"},
                          stored_step_fields={"crop_prompt"},
                          account_opener=lambda: opened.append(True))
    try:
        assert not d.keys_row.isHidden()
        text = d.label_keys_used.text()
        blocked, fallback = text.split("\n")
        assert "Lettura dei ritagli" in blocked and "non parte" in blocked
        # CONTRACT CHANGED 2026-11-13: «modello unico» renamed «modello predefinito» (user decision; EN default model, FR modèle par défaut, DE Standardmodell) in the fallback line.
        assert "Posizioni dei pad" in fallback and "modello predefinito" in fallback
        assert settings_dialog.YELLOW in d.label_keys_used.styleSheet()
        d.btn_account.click()
        assert opened == [True]
        d._step_follows_single("crop_prompt")
        d._step_follows_single("pad_positions_prompt")
        assert d.keys_row.isHidden()
    finally:
        d.deleteLater()


def test_the_window_has_no_session_model_note(qapp):
    """@brief CONTRACT CHANGED 2026-11-13: no per-session model exists any more (user decision).

    Positive: the window opens with the default model and has no session note.
    Negative: a caller that still passes `session_model` is refused (TypeError),
    instead of a value silently ignored.
    """
    d = LlmSettingsDialog(current_model="vendor/m")
    try:
        assert not hasattr(d, "label_session")
        assert d.model() == "vendor/m"
    finally:
        d.deleteLater()
    with pytest.raises(TypeError):
        LlmSettingsDialog(current_model="vendor/m", session_model="vendor/session")


def test_the_new_texts_follow_the_language(qapp, tmp_path):
    """@brief Positive: in English the captions, titles, hints and buttons are English.

    @note Negative: in Italian they stay the Italian sources.
    @note CONTRACT CHANGED 2026-11-13: «modello unico» renamed «modello predefinito» (user decision; EN default model, FR modèle par défaut, DE Standardmodell).
    """
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n
    from kicadgen.services.messages import set_translator
    store = ProfileStore(tmp_path / "p.sqlite3")
    expected = {
        "en": ("Document reading", "Reading — single component", "= default model", "Change…",
               "Use default", "Re-reads every body enlarged."),
        "fr": ("Lecture des documents", "Lecture — composant seul", "= modèle par défaut", "Changer…",
               "Utiliser le défaut", "Relit chaque corps agrandi."),
        # CONTRACT CHANGED 2026-11-14: the German step title reads «Lesen —
        # Einzelbauteil» (practical review: «Lesung» read oddly); the old text
        # is registered as superseded so existing databases update.
        "de": ("Dokumentlesung", "Lesen — Einzelbauteil", "= Standardmodell", "Ändern…",
               "Standard verwenden", "Liest jedes Gehäuse vergrößert erneut."),
        "it": ("Lettura documenti", "Lettura — componente singolo", "= modello predefinito", "Cambia…",
               "Usa il predefinito", "Rilegge ogni corpo ingrandito."),
    }
    for language, (group, title, follows, change, single, hint) in expected.items():
        set_translator(I18n(store, language).raw)
        try:
            d = LlmSettingsDialog(current_model="vendor/m")
            texts = [text for text, _model, _field in _rows(d)]
            models = [model for _text, model, field in _rows(d) if field]
            assert group in texts and title in texts, language
            assert models[0] == follows
            _row, change_button, single_button = d._rows["crop_prompt"]
            assert change_button.text() == change and single_button.text() == single
            crop_row = d._rows["crop_prompt"][0]
            assert d.table.item(crop_row, COL_STEP).data(HINT_ROLE) == hint
            d.deleteLater()
        finally:
            set_translator(None)


# -- the profile manager ------------------------------------------------------------------

def test_the_profile_editor_names_each_step_prompt_like_the_table_and_its_model(qapp, tmp_path):
    """@brief Finding 5. Positive: a step's prompt tab carries the step title and, under the editor,
    the model that sends it (its own, or the single one); a prompt riding inside a step shows
    that step's model.

    @note Negative: a text sent by no model of its own (photo instructions) has no model line.
    """
    from PyQt6.QtWidgets import QLabel, QTabWidget
    from kicadgen.gui import profile_dialog
    from kicadgen.services.analysis_profiles import ProfileStore
    store = ProfileStore(tmp_path / "p.sqlite3")
    store.save_step_models({"crop_prompt": "vendor/crops"})
    editor = profile_dialog.ProfileEditor(store)
    try:
        captions = [tab.tabText(i) for tab in editor.findChildren(QTabWidget)
                    for i in range(tab.count())]
        for title in ("Lettura — schede e moduli", "Lettura — componente singolo",
                      "Numerazione dei pin", "Piedini", "Lettura dei ritagli"):
            assert title in captions
        assert "Reofori" not in captions and "Lettura ritagli" not in captions
        lines = {label.objectName(): label.text() for label in editor.findChildren(QLabel)
                 if label.objectName().endswith("_model")}
        assert "vendor/crops" in lines["crop_prompt_model"]
        single = store.defaults()["model"]
        assert "modello predefinito" in lines["marking_prompt_model"] and single in lines["marking_prompt_model"]
        assert "Modello AI..." in lines["crop_prompt_model"]
        if "inventory_enumerate_prompt" in editor.fields:
            assert "inventory_enumerate_prompt_model" in lines
        assert "image_instructions_model" not in lines
    finally:
        editor.close()
        editor.deleteLater()


# -- the main window ----------------------------------------------------------------------

class _Stub:
    """A «Modello AI» window that answers OK with the given step table."""

    def __init__(self, steps):
        self._steps = steps

    def __call__(self, *, current_model, parent, current_step_models, **kwargs):
        self.shown = dict(current_step_models)
        self.model_shown = current_model
        self.kwargs = kwargs
        return self

    def exec(self):
        return QDialog.DialogCode.Accepted

    def deleteLater(self):
        pass

    def model(self):
        return self.model_shown

    vision_model = verification_model = model

    def pad_model(self):
        return self._steps(self.shown).get("pad_positions_prompt", "")

    def step_models(self):
        return self._steps(self.shown)


def test_ok_without_changes_saves_no_step(window, monkeypatch):
    """@brief Negative: an untouched table stores nothing, so the pad steps keep their old default."""
    from kicadgen.gui import main_window as gui
    stub = _Stub(lambda shown: dict(shown))
    monkeypatch.setattr(gui, "LlmSettingsDialog", stub)
    window._edit_llm_settings()
    assert window.profile_store.step_models_stored() == {}
    assert window.profile_store.step_models()["pad_positions_prompt"] == DEFAULT_PAD_POSITIONS_MODEL


def test_a_changed_pad_step_saves_only_itself_and_never_the_old_pad_field(window, monkeypatch):
    """@brief Finding B. Positive: the changed pad step is stored.

    @note Negative: the untouched body step is not turned into a saved choice and the old
        'pad_model' default is not rewritten, so the body step keeps following it.
    """
    from kicadgen.gui import main_window as gui
    before = window.profile_store.defaults()["pad_model"]
    stub = _Stub(lambda shown: dict(shown, pad_positions_prompt="vendor/pads"))
    monkeypatch.setattr(gui, "LlmSettingsDialog", stub)
    window._edit_llm_settings()
    assert window.profile_store.step_models_stored() == {"pad_positions_prompt": "vendor/pads"}
    assert window.profile_store.defaults()["pad_model"] == before
    assert window.profile_store.step_models()["body_dimensions_prompt"] == (before or "")


def test_a_changed_step_is_saved_and_logged(window, monkeypatch):
    """@brief Positive: the changed step is stored; the log says «Modelli AI salvati» and names the step.

    @note CONTRACT CHANGED 2026-11-13: «Modello unico» renamed «Modello predefinito», and
        the window no longer receives a session model (the per-session model was removed).
    """
    from kicadgen.gui import main_window as gui
    stub = _Stub(lambda shown: dict(shown, marking_prompt="vendor/markings"))
    monkeypatch.setattr(gui, "LlmSettingsDialog", stub)
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
    window._edit_llm_settings()
    assert window.profile_store.step_models_stored() == {"marking_prompt": "vendor/markings"}
    assert any(line.startswith("Modelli AI salvati. Modello predefinito:") for line in logged)
    assert not any("SQLite" in line or "LLM" in line for line in logged)
    assert any("Lettura delle sigle → vendor/markings" in line for line in logged)
    # The window receives what it needs for its notes and its account button.
    assert "session_model" not in stub.kwargs
    assert stub.kwargs["stored_step_fields"] == set()
    assert stub.kwargs["account_opener"] == window._open_ai_account


def test_the_menu_entries_say_what_each_window_decides(window):
    """@brief Finding 11: prompts -> model -> account, each entry with its own tooltip."""
    actions = [action for action in window.menu_config.actions() if not action.isSeparator()]
    assert actions.index(window.action_profiles) < actions.index(window.action_defaults) \
        < actions.index(window.action_ai_account)
    assert "prompt" in window.action_profiles.toolTip()
    assert "passo" in window.action_defaults.toolTip()
    assert window.action_profiles.toolTip() != window.action_defaults.toolTip()


def _start_extraction(window, monkeypatch, family, detector="ai"):
    """Start an extraction with a stopped worker and return it."""
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("quote.png")], None))
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData(detector))
    window.combo_component_type.setCurrentIndex(window.combo_component_type.findData(family))
    window._extract_with_ai()
    return window._extract_worker


def test_the_extraction_worker_receives_the_steps_and_the_default_model(window, monkeypatch):
    """@brief Positive: the worker gets the step table; the saved default model is its `model`.

    @note Negative: the step chosen in the table is not replaced by the default model.
    @note CONTRACT CHANGED 2026-11-13: there is no session model to replace the
        default one any more (user decision); the window has no such attribute.
    """
    window.profile_store.save_step_models({"crop_prompt": "vendor/crops"})
    assert not hasattr(window, "_session_model")
    worker = _start_extraction(window, monkeypatch, "aggregate_tht")
    assert worker.model == window._default_model()
    assert worker.step_models == window.profile_store.step_models()
    assert worker.step_models["crop_prompt"] == "vendor/crops"


def test_the_photo_call_line_names_the_inventory_step_model(window, monkeypatch):
    """@brief Finding C. Positive: a chosen inventory step is the model the photo line names.

    @note Negative: without a step choice the line names the configured model.
    """
    lines = []
    monkeypatch.setattr(window, "_on_ai_progress", lambda text, *a, **k: lines.append(str(text)))
    _start_extraction(window, monkeypatch, "aggregate_tht", detector="vlm")
    photo = [line for line in lines if "fotografia affidata" in line]
    assert photo and "modello del passo" not in photo[-1]
    window.profile_store.save_step_models({"inventory_prompt": "vendor/inventory"})
    lines.clear()
    _start_extraction(window, monkeypatch, "aggregate_tht", detector="vlm")
    photo = [line for line in lines if "fotografia affidata" in line]
    assert "vendor/inventory" in photo[-1] and "Inventario dei componenti" in photo[-1]


def test_the_key_check_covers_the_chosen_steps_of_the_run(window, monkeypatch):
    """@brief Positive: a chosen photo step joins the run's models for a board using that step.

    @note Negative: for a single part the photo step is left out, and the pad steps that
        still follow the old default are an optional reader, not a chosen model.
    """
    window.profile_store.save_step_models({"crop_prompt": "vendor/crops"})
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("aggregate_tht"))
    detector = window.combo_photo_detector
    detector.setCurrentIndex(detector.findData("yolo"))
    assert "vendor/crops" in window._run_models()
    combo.setCurrentIndex(combo.findData("single_tht"))
    assert "vendor/crops" not in window._run_models()
    assert DEFAULT_PAD_POSITIONS_MODEL not in window._run_models()
    window.profile_store.save_step_models({"pad_positions_prompt": "vendor/pads"})
    assert "vendor/pads" in window._run_models()


@pytest.mark.parametrize("detector, used, unused", [
    ("ai", ("grounding_prompt", "verification_prompt", "terminal_prompt"),
     ("inventory_prompt", "crop_prompt", "marking_prompt")),
    ("vlm", ("inventory_prompt", "crop_prompt", "marking_prompt"),
     ("grounding_prompt", "verification_prompt", "terminal_prompt")),
    ("yolo_smd", ("crop_prompt",), ("inventory_prompt", "grounding_prompt", "terminal_prompt")),
])
def test_the_photo_steps_follow_the_detector_actually_used(window, detector, used, unused):
    """@brief Finding A: on a board the photo steps are those of the detector, never the others.

    @note «AI (attuale)» pays the terminals inside the extraction, so its key check includes them.
    """
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("aggregate_tht"))
    steps = window._run_steps(detector=detector)
    assert set(used) <= set(steps)
    assert not set(unused) & set(steps)
    assert "system_prompt" in steps and "system_prompt_single" not in steps


def test_side_commands_check_only_their_own_step(window):
    """@brief Finding A. Positive: each side command checks its own step's chosen model.

    @note Negative: a chosen crop model on another source is not asked for by a pin-type
        suggestion, a terminal pass or a re-read.
    """
    window.profile_store.save_step_models({"crop_prompt": "vendor/crops",
                                           "pin_type_prompt": "vendor/pins",
                                           "body_dimensions_prompt": "vendor/body"})
    assert window._run_steps("pin_types") == ("pin_type_prompt",)
    assert window._run_steps("terminals") == ("terminal_prompt",)
    assert window._run_steps("leads") == ("lead_prompt",)
    assert window._run_steps("reread", field="body_height_mm") == ("body_dimensions_prompt",)
    assert window._run_steps("reread", field="pad_layout") == ("pad_positions_prompt",)
    assert window._run_steps("relaunch", detector="yolo_smd") == ("crop_prompt",)
    pins = window._run_models("pin_types")
    assert "vendor/pins" in pins and "vendor/crops" not in pins
    assert "vendor/crops" not in window._run_models("terminals")
    reread = window._run_models("reread", field="body_height_mm")
    assert "vendor/body" in reread and "vendor/crops" not in reread
    assert "vendor/crops" in window._run_models("relaunch", detector="yolo")


# -- the workers ----------------------------------------------------------------------------

def test_the_terminal_worker_forwards_the_steps(qapp, monkeypatch):
    """@brief Positive: observe_terminals receives `step_models`; negative: None forwards nothing."""
    from kicadgen.services import photo_terminals
    seen = []

    def fake(photo, components, model, call, log, cache_dir=None, system_prompt=None, step_models=None):
        seen.append(step_models)
        return []
    monkeypatch.setattr(photo_terminals, "observe_terminals", fake)
    for given in ({"terminal_prompt": "vendor/t"}, None):
        worker = workers.TerminalDetailWorker(b"png", [], "vendor/m", step_models=given)
        worker.run()
    assert seen == [{"terminal_prompt": "vendor/t"}, None]


def _run_reread(monkeypatch, table, ready=lambda models, progress=None, **kwargs: None):
    """Run a pad re-read with a fake extractor; return (what it received, the log lines)."""
    from kicadgen.services import datasheet_extractor, openrouter, providers
    seen = {}

    def fake_reread(stored, documents, *, model, call, progress=None, same_model=None,
                    step_models=None, **kwargs):
        seen.update(model=model, same_model=same_model, step_models=step_models)
        return {"updated": True}
    monkeypatch.setattr(datasheet_extractor, "reread_pad_positions", fake_reread, raising=False)
    monkeypatch.setattr(openrouter, "call_tool", lambda **kwargs: {})
    monkeypatch.setattr(providers, "ensure_providers_ready", ready)
    worker = workers.RereadWorker({"k": 1}, ["a.pdf"], None, field="pad_layout",
                                  model="vendor/main", pad_model="vendor/old",
                                  component_family="single_tht", step_models=table)
    logs = []
    worker.progress.connect(logs.append)
    worker.run()
    return seen, [str(getattr(line, "text", line)) for line in logs]


def test_the_reread_worker_hands_the_table_and_the_main_model(qapp, monkeypatch):
    """@brief With a table the re-read gets the MAIN model and resolves its reader itself.

    @note Negative: `same_model` is not sent then (the re-read decides it).
    """
    table = {"pad_positions_prompt": "vendor/pads"}
    seen, logs = _run_reread(monkeypatch, table)
    assert seen == {"model": "vendor/main", "same_model": None, "step_models": table}
    assert any("vendor/pads" in line for line in logs)


def test_the_reread_log_names_the_model_that_will_really_answer(qapp, monkeypatch):
    """@brief Finding C. Negative: a step reader on another source that cannot run is not named;
    the log names the main model that will answer instead."""
    from kicadgen.services.providers import ProviderError

    def not_ready(models, progress=None, **kwargs):
        raise ProviderError("no key")
    _seen, logs = _run_reread(monkeypatch, {"pad_positions_prompt": "deepseek-api/deepseek-pro"},
                              ready=not_ready)
    reading = [line for line in logs if line.startswith("Rilettura di pad_layout")]
    assert reading and "vendor/main" in reading[-1] and "deepseek-pro" not in reading[-1]
