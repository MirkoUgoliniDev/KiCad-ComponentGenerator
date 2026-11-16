"""@brief Round-2 fixes of the progress card: program text recognised by its origin, re-composed on a switch.

T1 (real trial 2026-11-15): in French the finished card's outcome never
changed. «✓ Terminé : empreinte et modèle STEP générés.» was taken for a
«key : value» trace field (`_SPACED_KEY_VALUE`) because French typography puts
a space before the colon, and `set_status` dropped it. The class: a translated
sentence of the program must never be classified by a shape that one
language's punctuation produces. The outcome and the first status are passed
as program text (`composed=True`); a line that crossed a worker signal as a
plain string is recognised by its origin, the catalogue template whose literal
text carries the colon. External tool output is still kept off the card.

T2: the texts the card composes (header «tool · detail», step labels, step
results, the «98% · 45/46 componenti» count, the running status) stayed in the
language of the moment they were written; they now follow a language switch,
the texts the workers composed through `messages.tx` included.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from kicadgen.gui import analysis_overlay as ao  # noqa: E402
from kicadgen.gui.analysis_overlay import AnalysisOverlay  # noqa: E402
from kicadgen.services.analysis_profiles import ProfileStore  # noqa: E402
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, I18n  # noqa: E402
from kicadgen.services.messages import Message  # noqa: E402

LANGUAGES = ("it", "en", "fr", "de")
OUTCOMES = ("Completato: footprint e modello STEP generati.", "Completato: footprint generato.",
            "Completato: modello STEP rigenerato.")
#: Lines external tools print on their own stdout: they must stay in the log.
EXTERNAL_RECORDS = ("WorkSession : Sending all data", "Step File Name : C:/out/Test.step")


def _rendering(source, language):
    """The bundled catalogue text of an Italian source in one language."""
    return DEFAULT_TRANSLATIONS["ui:" + source][language]


@pytest.fixture
def card(qapp, tmp_path):
    """@brief A card with a real I18n (Italian first), released after the test."""
    i18n = I18n(ProfileStore(tmp_path / "profiles.sqlite3"), "it")
    host = QWidget()
    host.resize(1100, 900)
    overlay = AnalysisOverlay(host, i18n=i18n)
    host.show()
    QApplication.processEvents()
    yield i18n, overlay
    overlay.stop()
    host.close()
    host.deleteLater()
    qapp.processEvents()


# ── T1: origin, not typography ──────────────────────────────────────────────

@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("source", OUTCOMES)
def test_every_language_rendering_of_an_outcome_is_a_human_line(source, language):
    """@brief Positive: the outcome in IT, EN, FR (« : ») and DE is never a machine line."""
    assert ao.is_machine_line(_rendering(source, language)) is False


@pytest.mark.parametrize("record", EXTERNAL_RECORDS)
def test_an_external_key_value_record_is_still_a_machine_line(record):
    """@brief Negative: the same spaced-colon shape from an external tool, in no catalogue, goes to the log."""
    assert ao.is_machine_line(record) is True


def test_only_the_colon_the_catalogue_wrote_is_excused():
    """@brief A catalogue template quoting an external record in its VALUE stays a machine line.

    «Modèle introuvable : {reference}»: with a part name as the value the
    line is the program's sentence; with a trace field as the value the
    second colon is not the template's literal text, so the line is not
    excused -- the decision is on the colon's position, not on the language.
    """
    template = _rendering("Modello non trovato: {reference}", "fr")
    assert ao.is_machine_line(template.format(reference="SOT-23-6")) is False
    assert ao.is_machine_line(template.format(reference="WorkSession : Sending all data")) is True
    # Two lines glued together: the catalogue explains the first colon only.
    glued = _rendering(OUTCOMES[0], "fr") + " WorkSession : Sending all data"
    assert ao.is_machine_line(glued) is True


def test_a_template_without_a_literal_word_never_excuses_a_line():
    """@brief «{count} : {names}» would match anything: it is not an origin, the line stays machine."""
    assert _rendering("{count}: {names}", "fr") == "{count} : {names}"
    assert ao.is_machine_line("12 : alpha, beta") is True


def test_the_other_rules_still_apply_to_catalogue_lines():
    """@brief Negative: the colon excuse is for the spaced colon only; a decoration run keeps the line off."""
    line = _rendering(OUTCOMES[0], "fr") + " ======"
    assert ao.is_machine_line(line) is True


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_finished_card_shows_the_outcome_in_every_language(card, language):
    """@brief Positive: `show_completed` writes the outcome whatever the language's typography."""
    i18n, overlay = card
    i18n.set_language(language)
    overlay.start("Generazione footprint e modello STEP")
    overlay.set_status("Avvio di FreeCAD…")
    source = OUTCOMES[0]
    overlay.show_completed(i18n.text(source), source=source)
    assert overlay.status_plain_text() == f"✓ {_rendering(source, language)}"


def test_the_outcome_follows_a_switch_to_french(card):
    """@brief T1 as the user met it: started in Italian, switched to French, the outcome is French."""
    i18n, overlay = card
    overlay.start("Generazione footprint e modello STEP")
    source = OUTCOMES[1]
    overlay.show_completed(i18n.text(source), source=source)
    i18n.set_language("fr")
    overlay.refresh_command_texts()
    assert overlay.status_plain_text() == "✓ Terminé : empreinte générée."


def test_a_relayed_french_sentence_reaches_the_card_and_a_trace_field_does_not(card):
    """@brief A worker line (plain str, already French) is shown; an external record keeps the last status."""
    i18n, overlay = card
    i18n.set_language("fr")
    overlay.start("Generazione modello STEP")
    stopping = _rendering("Interruzione in corso: chiusura del processo FreeCAD…", "fr")
    overlay.set_status(str(stopping))
    assert overlay.status_plain_text() == stopping
    overlay.set_status(EXTERNAL_RECORDS[0])
    assert overlay.status_plain_text() == stopping


def test_composed_text_bypasses_the_filter_by_origin(card):
    """@brief `composed=True` and a `messages.Message` are program text; a plain str is classified."""
    _i18n, overlay = card
    overlay.start("Analisi AI in corso")
    overlay.set_status("Primo stato leggibile.")
    overlay.set_status(EXTERNAL_RECORDS[0])
    assert overlay.status_plain_text() == "Primo stato leggibile."
    overlay.set_status(EXTERNAL_RECORDS[0], composed=True)
    assert overlay.status_plain_text() == EXTERNAL_RECORDS[0]
    overlay.set_status(Message(EXTERNAL_RECORDS[1]))
    # Shown (its path shortened for display, as every path on the card).
    assert overlay.status_plain_text() == "Step File Name : …/Test.step"


# ── T2: composed texts follow a language switch ─────────────────────────────

def test_recompose_carries_a_worker_line_into_another_language():
    """@brief A `tx` line written in French is composed again in English with its values."""
    class Lookup:
        """The two calls `recompose_in_language` makes, on the bundled catalogue."""

        language = "en"

        def text(self, source):
            return DEFAULT_TRANSLATIONS.get("ui:" + source, {}).get(self.language, source)

        def raw(self, key, default=None):
            return DEFAULT_TRANSLATIONS.get(key, {}).get(self.language, default)

    lookup = Lookup()
    french = _rendering("{matched} trovati · {not_found} non trovati · {review} da verificare", "fr")
    line = french.format(matched=3, not_found=31, review=12)
    assert ao.recompose_in_language(line, lookup) == "3 found · 31 not found · 12 to review"
    # Negative: a text no anchored template renders (a model name) comes back unchanged.
    assert ao.recompose_in_language("anthropic/claude-opus-5", lookup) == "anthropic/claude-opus-5"
    # Negative: an unanchored template («{count}: {names}») is never used to recompose.
    assert ao.recompose_in_language("12: alpha, beta", lookup) == "12: alpha, beta"


def test_header_steps_results_and_count_follow_a_language_switch(card):
    """@brief Positive: every composed text of a running generation is English after the switch."""
    i18n, overlay = card
    overlay.start("Generazione footprint e modello STEP", abortable=True)
    overlay.set_steps(["Footprint 2D (KiCadGen)", "Confronto con i consolidati"])
    overlay.set_tool("footprint", "scrittura del file .kicad_mod")
    overlay.set_current_step(1)
    overlay.set_step_result(1, "3 trovati · 31 non trovati · 12 da verificare")
    overlay.set_progress(45, 46, "componenti")
    assert overlay.tool.text() == "KiCadGen (footprint 2D, calcolo locale) · scrittura del file .kicad_mod"
    i18n.set_language("en")
    overlay.refresh_command_texts()
    assert overlay.tool.text() == "KiCadGen (2D footprint, computed locally) · writing the .kicad_mod file"
    plain = overlay.steps_plain_text()
    assert "Compare with accepted matches" in plain and "Confronto" not in plain
    assert "3 found · 31 not found · 12 to review" in plain and "trovati" not in plain
    assert overlay.progress_label.text() == "98% · 45/46 components"
    # The step list main_window logs from is the translated one.
    assert overlay._step_labels[1] == "Compare with accepted matches"


def test_a_detail_outside_the_catalogue_is_kept_as_written(card):
    """@brief Negative: a model name in the header is not a catalogue text and is not touched."""
    i18n, overlay = card
    overlay.start("Analisi AI in corso")
    overlay.set_tool("llm", "anthropic/claude-opus-5")
    i18n.set_language("fr")
    overlay.refresh_command_texts()
    assert overlay.tool.text().endswith(" · anthropic/claude-opus-5")
    assert overlay.tool.text().startswith(_rendering("Modello AI", "fr"))
