"""@brief «Account AI...» in the main window and the key check before paid runs (PIANO-FIX 2, 2026-09-26)."""
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui import ai_account_dialog
from kicadgen.services.photo_inventory import ReviewBundle
from tests.test_photo_inventory_gui import gui


@pytest.fixture
def no_key(window, monkeypatch):  # noqa: F811
    """The window of a user who never configured a key (no source has one)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return window


@pytest.fixture
def questions(monkeypatch):
    """Questions asked, as (title, text); the answer is set in `questions.answer`."""
    class Asked(list):
        answer = QMessageBox.StandardButton.No
    asked = Asked()
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args, **kwargs: asked.append((args[1], args[2])) or asked.answer)
    return asked


def _ready_for_extraction(window, monkeypatch):  # noqa: F811
    """Everything an AI extraction needs except the key; records whether it started."""
    started = []
    monkeypatch.setattr(window, "_photo_detector_options", lambda: ("vision", None))
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("doc.pdf")], None))
    monkeypatch.setattr(window, "_begin_ai_run", lambda title: started.append(title))
    monkeypatch.setattr(window, "_start_extract_worker", lambda *args, **kwargs: None)
    return started


def _saved_review_bundle(tmp_path):
    """A nonempty reviewed extraction bundle eligible for paid reconciliation."""
    return ReviewBundle(
        json_path=tmp_path / "review.json", body_length_mm=40, body_width_mm=20,
        proposals=[{"_id": 1, "_box": [.2, .2, .4, .4], "name": "R1",
                    "kind": "resistor", "x_mm": -8, "y_mm": -4,
                    "length_mm": 8, "width_mm": 4, "height_mm": 1}])


def test_the_configuration_menu_opens_the_ai_account_window(window, monkeypatch):  # noqa: F811
    config_menu = window.menuBar().actions()[1].menu()
    assert window.action_ai_account in config_menu.actions()
    refreshed = []
    monkeypatch.setattr(window.ai_account, "refresh_balance", lambda: refreshed.append(True))
    monkeypatch.setattr(ai_account_dialog.AiAccountDialog, "exec", lambda self: 0)
    # La finestra vera legge i saldi in background: nel test nessuna rete
    # (prima chiamava davvero openrouter.ai con la chiave finta).
    read = []
    monkeypatch.setattr(ai_account_dialog.AiAccountDialog, "refresh_balances",
                        lambda self: read.append(True))
    window.action_ai_account.trigger()
    dialog = window._ai_account_dialog
    assert isinstance(dialog, ai_account_dialog.AiAccountDialog)
    dialog.key_changed.emit()
    assert refreshed == [True]
    assert read == [True]           # the application asks for the balances


def test_with_a_key_the_extraction_starts_without_questions(window, monkeypatch,
                                                            questions):  # noqa: F811
    started = _ready_for_extraction(window, monkeypatch)
    window._extract_with_ai()
    assert started == ["Analisi AI"] and questions == []


def test_without_a_key_nothing_paid_starts_and_the_account_window_is_offered(
        no_key, monkeypatch, questions):  # noqa: F811
    window = no_key
    started = _ready_for_extraction(window, monkeypatch)
    opened = []
    # CONTRACT CHANGED 2026-11-12 (fresh-install review): the window opens on
    # the provider that lacks the key, through `_open_ai_account_for`.
    monkeypatch.setattr(window, "_run_models", lambda *args, **kwargs: ("openai/gpt-x",))
    monkeypatch.setattr(window, "_open_ai_account_for", lambda provider: opened.append(provider))
    window._extract_with_ai()
    assert started == [] and opened == [] and len(questions) == 1
    assert "Account AI..." in questions[0][1] and "OpenRouter" in questions[0][1]
    questions.answer = QMessageBox.StandardButton.Yes
    window._extract_with_ai()
    assert started == [] and opened == ["openrouter"]


def test_the_yolo_only_pretest_needs_no_key(no_key, monkeypatch, questions):  # noqa: F811
    """@brief Negativo: il pretest solo YOLO e' locale e gratuito, niente domanda sulla chiave."""
    window = no_key
    pretests = []
    monkeypatch.setattr(window, "_photo_detector_options", lambda: ("yolo_only", "model.pt"))
    monkeypatch.setattr("kicadgen.services.photo_yolo.validate_yolo_setup", lambda path: None)
    monkeypatch.setattr(window, "_selected_documents", lambda: ([Path("doc.pdf")], None))
    monkeypatch.setattr(window, "_start_yolo_pretest", lambda *args: pretests.append(args))
    window._extract_with_ai()
    assert len(pretests) == 1 and questions == []


def test_the_inventory_relaunch_checks_the_key_before_paying(no_key, monkeypatch,
                                                              questions, tmp_path):  # noqa: F811
    window = no_key
    started = []
    bundle = _saved_review_bundle(tmp_path)
    monkeypatch.setattr(window, "_ensure_review_bundle", lambda: bundle)
    monkeypatch.setattr(window, "_review_photo", lambda bundle: b"png")
    monkeypatch.setattr(window, "_begin_ai_run", lambda title: started.append(title))
    workers = []
    monkeypatch.setattr(window, "_start_reconcile_worker", lambda *args: workers.append(args))
    window._relaunch_inventory()
    assert started == [] and workers == [] and len(questions) == 1
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    window._relaunch_inventory()
    assert started == ["Rilancio inventario"]
    assert len(workers) == 1 and workers[0][0] is bundle
    assert workers[0][2].body_length_mm == 40
    assert len(questions) == 1


def test_the_terminal_detail_checks_the_key_before_paying(no_key, monkeypatch,
                                                           questions):  # noqa: F811
    window = no_key
    window._photo_png = b"png"
    monkeypatch.setattr(window, "_photo_components_from_table", lambda: [{"id": "U1"}])
    workers = []
    monkeypatch.setattr(gui, "TerminalDetailWorker", lambda *args, **kwargs: workers.append(args))
    window._start_terminal_refinement()
    assert workers == [] and len(questions) == 1
    assert "Account AI..." in questions[0][1]


# --- Fresh install (user, 2026-11-12: «cosa succede se dopo il setup non trovo
# kicad, freecad o almeno una key LLM?») --------------------------------------

DEEPSEEK_MODEL = "deepseek-api/deepseek-chat"


def test_a_missing_deepseek_key_offers_the_account_window_like_openrouter(
        window, monkeypatch, questions):  # noqa: F811
    """@brief Positive: no DeepSeek key -> the same question as OpenRouter, window on DeepSeek."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    opened = []
    monkeypatch.setattr(window, "_open_ai_account_for", lambda provider: opened.append(provider))
    assert window._ensure_keys_for((DEEPSEEK_MODEL,)) is False
    assert len(questions) == 1 and "DeepSeek" in questions[0][1]
    assert "Account AI..." in questions[0][1]
    questions.answer = QMessageBox.StandardButton.Yes
    assert window._ensure_keys_for((DEEPSEEK_MODEL,)) is False
    assert opened == ["deepseek"]


def test_a_present_deepseek_key_asks_nothing(window, monkeypatch, questions):  # noqa: F811
    """@brief Negative: with the DeepSeek key set the paid command may start, no question."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test-fixture")
    assert window._ensure_keys_for((DEEPSEEK_MODEL,)) is True
    assert questions == []


def test_only_the_providers_of_the_models_are_checked(no_key, monkeypatch):  # noqa: F811
    """@brief Negative: a missing OpenRouter key does not matter to a DeepSeek-only run."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test-fixture")
    assert no_key._providers_without_key((DEEPSEEK_MODEL,)) == []
    assert no_key._providers_without_key((DEEPSEEK_MODEL, "openai/gpt-x")) == ["openrouter"]


def _startup_box(window, monkeypatch, *, kicad, freecad, click_account=False):
    """Run the start-up warning with the given applications; return what it showed."""
    from kicadgen.services.system_dependencies import ExternalApplications
    monkeypatch.setattr(gui, "detect_external_applications",
                        lambda: ExternalApplications(kicad, freecad))
    shown = []

    def run_modal(message, *args, **kwargs):
        buttons = {button.text(): button for button in message.buttons()}
        shown.append((message.text(), message.informativeText(), tuple(buttons)))
        if click_account:
            # What a click does: QMessageBox records the clicked button.
            buttons[window.i18n.text("Account AI...")].click()
        return 0

    monkeypatch.setattr(window, "_run_modal", run_modal)
    window._startup_dependencies_checked = False
    return window._warn_missing_external_applications(), shown


def test_startup_names_a_missing_ai_key_even_with_kicad_and_freecad(
        window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positive: both applications found but no key -> warning with the key line and «Account AI...»."""
    monkeypatch.setattr(window, "_run_models", lambda *args, **kwargs: (DEEPSEEK_MODEL,))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    opened = []
    monkeypatch.setattr(window, "_open_ai_account_for", lambda provider: opened.append(provider))
    found, shown = _startup_box(window, monkeypatch, kicad=str(tmp_path / "k.exe"),
                                freecad=str(tmp_path / "f.exe"), click_account=True)
    assert found and len(shown) == 1
    heading, explanation, buttons = shown[0]
    assert heading == "Non rilevate: chiave AI DeepSeek"
    assert "✗ Nessuna chiave AI per DeepSeek" in explanation
    assert "KiCadGen-Tool" not in explanation and "KiCadGen" in explanation
    assert set(buttons) == {"Account AI...", "Continua"}
    assert opened == ["deepseek"]


def test_startup_is_silent_with_applications_and_keys(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Negative: applications found and the key present -> no warning at all."""
    monkeypatch.setattr(window, "_run_models", lambda *args, **kwargs: (DEEPSEEK_MODEL,))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test-fixture")
    found, shown = _startup_box(window, monkeypatch, kicad=str(tmp_path / "k.exe"),
                                freecad=str(tmp_path / "f.exe"))
    assert not found and shown == []


def test_startup_without_applications_but_with_the_key_has_no_account_button(
        window, monkeypatch):  # noqa: F811
    """@brief Negative: only applications missing -> no key line, no «Account AI...» button."""
    monkeypatch.setattr(window, "_run_models", lambda *args, **kwargs: (DEEPSEEK_MODEL,))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test-fixture")
    found, shown = _startup_box(window, monkeypatch, kicad=None, freecad=None)
    assert found
    heading, explanation, buttons = shown[0]
    assert heading == "Non rilevate: KiCad, FreeCAD"
    assert "chiave AI" not in heading and "Nessuna chiave AI" not in explanation
    assert buttons == ("Continua",)


def test_both_missing_providers_are_named_in_one_question(no_key, monkeypatch, questions):  # noqa: F811
    """@brief Positive: two providers without a key -> one question naming both, window on the first."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    opened = []
    monkeypatch.setattr(no_key, "_open_ai_account_for", lambda provider: opened.append(provider))
    questions.answer = QMessageBox.StandardButton.Yes
    assert no_key._ensure_keys_for((DEEPSEEK_MODEL, "openai/gpt-x")) is False
    assert len(questions) == 1 and "OpenRouter, DeepSeek" in questions[0][1]
    assert opened == ["openrouter"]


def test_a_malformed_key_is_not_called_missing(window, monkeypatch, questions):  # noqa: F811
    """@brief Positive: a configured but unusable key keeps its own reason; negative: no «Nessuna chiave»."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-\u00e9bad")
    assert window._ensure_keys_for((DEEPSEEK_MODEL,)) is False
    text = questions[0][1]
    assert "DEEPSEEK_API_KEY" in text and "sostituirla" in text
    assert "Nessuna chiave" not in text
    monkeypatch.setattr(window, "_run_models", lambda *args, **kwargs: (DEEPSEEK_MODEL,))
    found, shown = _startup_box(window, monkeypatch, kicad="k.exe", freecad="f.exe")
    assert found and "Chiave AI per DeepSeek non utilizzabile" in shown[0][1]
    assert "Nessuna chiave AI" not in shown[0][1]


def test_escape_closes_the_startup_box_as_continue(window, monkeypatch):  # noqa: F811
    """@brief Positive: Escape on the two-button box means «Continua» and opens nothing."""
    monkeypatch.setattr(window, "_run_models", lambda *args, **kwargs: (DEEPSEEK_MODEL,))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from kicadgen.services.system_dependencies import ExternalApplications
    monkeypatch.setattr(gui, "detect_external_applications", lambda: ExternalApplications("k", "f"))
    opened, escape = [], []
    monkeypatch.setattr(window, "_open_ai_account_for", lambda provider: opened.append(provider))

    def run_modal(message, *args, **kwargs):
        escape.append(message.escapeButton().text())
        message.escapeButton().click()
        return 0

    monkeypatch.setattr(window, "_run_modal", run_modal)
    window._startup_dependencies_checked = False
    assert window._warn_missing_external_applications()
    assert escape == ["Continua"] and opened == []


def test_a_failing_model_lookup_still_shows_the_application_warning(window, monkeypatch):  # noqa: F811
    """@brief Negative: `_run_models` raising never stops the start; the key line is left out and logged."""
    def broken(*args, **kwargs):
        raise RuntimeError("profile edge case")
    monkeypatch.setattr(window, "_run_models", broken)
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda text, **kwargs: logged.append(text))
    found, shown = _startup_box(window, monkeypatch, kicad=None, freecad=None)
    assert found and shown[0][0] == "Non rilevate: KiCad, FreeCAD"
    assert logged and "profile edge case" in logged[0]


def _key_only_startup(window, monkeypatch, tick):
    """Start-up with KiCad and FreeCAD found and no DeepSeek key; optionally tick the dismiss box."""
    from kicadgen.services.system_dependencies import ExternalApplications
    monkeypatch.setattr(window, "_run_models", lambda *args, **kwargs: (DEEPSEEK_MODEL,))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(gui, "detect_external_applications", lambda: ExternalApplications("k", "f"))
    saved, boxes = [], []
    monkeypatch.setattr(gui, "save_config", lambda config: saved.append(dict(config)))

    def run_modal(message, *args, **kwargs):
        boxes.append(message.checkBox())
        if tick and message.checkBox() is not None:
            message.checkBox().setChecked(True)
        return 0

    monkeypatch.setattr(window, "_run_modal", run_modal)
    window._startup_dependencies_checked = False
    return window._warn_missing_external_applications(), boxes, saved


def test_the_key_notice_can_be_dismissed_for_good(window, monkeypatch, questions):  # noqa: F811
    """@brief Positive: ticked once -> saved, and the next start with only the key missing is silent."""
    window._config.pop(gui.STARTUP_KEY_NOTICE_DISMISSED_KEY, None)
    shown, boxes, saved = _key_only_startup(window, monkeypatch, tick=True)
    assert shown and boxes[0] is not None
    assert boxes[0].text() == "Non avvisare più per la chiave AI"
    assert saved and saved[-1][gui.STARTUP_KEY_NOTICE_DISMISSED_KEY] is True
    shown, boxes, _saved = _key_only_startup(window, monkeypatch, tick=False)
    assert not shown and boxes == []
    # The question before a paid command is not silenced by the start-up choice.
    assert window._ensure_keys_for((DEEPSEEK_MODEL,)) is False and len(questions) == 1


def test_not_ticking_keeps_the_key_notice(window, monkeypatch):  # noqa: F811
    """@brief Negative: left unticked -> nothing saved and the next start warns again."""
    window._config.pop(gui.STARTUP_KEY_NOTICE_DISMISSED_KEY, None)
    shown, _boxes, saved = _key_only_startup(window, monkeypatch, tick=False)
    assert shown and saved == []
    shown, boxes, _saved = _key_only_startup(window, monkeypatch, tick=False)
    assert shown and boxes[0] is not None


def test_a_dismissed_key_notice_still_reports_missing_applications(window, monkeypatch):  # noqa: F811
    """@brief Negative: dismissed key notice, FreeCAD missing -> warning without key line nor box."""
    from kicadgen.services.system_dependencies import ExternalApplications
    window._config[gui.STARTUP_KEY_NOTICE_DISMISSED_KEY] = True
    monkeypatch.setattr(window, "_run_models", lambda *args, **kwargs: (DEEPSEEK_MODEL,))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(gui, "detect_external_applications", lambda: ExternalApplications("k", None))
    shown = []

    def run_modal(message, *args, **kwargs):
        shown.append((message.text(), message.informativeText(), message.checkBox(),
                      tuple(button.text() for button in message.buttons())))
        return 0

    monkeypatch.setattr(window, "_run_modal", run_modal)
    window._startup_dependencies_checked = False
    assert window._warn_missing_external_applications()
    heading, explanation, box, buttons = shown[0]
    assert heading == "Non rilevate: FreeCAD"
    assert "chiave AI per" not in explanation.lower() and box is None
    assert buttons == ("Continua",)


def test_the_account_window_switches_the_key_notice_back_on(window, monkeypatch):  # noqa: F811
    """@brief Positive: dismissed at start-up, then ticked in «Account AI» -> warned again and saved."""
    saved = []
    monkeypatch.setattr(gui, "save_config", lambda config: saved.append(dict(config)))
    window._config[gui.STARTUP_KEY_NOTICE_DISMISSED_KEY] = True
    assert window._startup_key_notice_on() is False
    window._set_startup_key_notice(True)
    assert window._startup_key_notice_on() is True
    assert saved[-1][gui.STARTUP_KEY_NOTICE_DISMISSED_KEY] is False
    window._set_startup_key_notice(False)
    assert window._startup_key_notice_on() is False
