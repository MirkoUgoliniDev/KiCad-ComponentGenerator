"""@brief Round-3 fixes of the progress card: the catalogue origin is a property of the WHOLE template.

Round 2 excused a French spaced colon (« : ») only when the literal piece of
text around the colon carried a word, so 112 French templates such as
«{name} : {outcome}» («Composant 3 sur 12 — U1 : lectures concordantes…»)
still left the card in French only, and a catalogue LABEL inside an external
record («[OCC] Fichier : C:/…», the label «File:») excused that record.
The class: the classification of a program sentence must not depend on the
language it is shown in, and a catalogue word alone is not an origin. The
tests here are catalogue-wide, not on the sentence of the report.

Also: matching is bounded in time on the GUI thread (a 5 000-character line
took 22 s), and a line that a generic template («Pin {number}») also renders
is recomposed from its most specific template.
"""
import re
import time

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from kicadgen.gui import analysis_overlay as ao  # noqa: E402
from kicadgen.gui.analysis_overlay import AnalysisOverlay  # noqa: E402
from kicadgen.services.analysis_profiles import ProfileStore  # noqa: E402
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, I18n  # noqa: E402

LANGUAGES = ("it", "en", "fr", "de")

#: Placeholder names that hold a count or an ordinal in this catalogue: they
#: get a number, every other placeholder gets a word (neutral sample values).
_COUNT_NAME = re.compile(r"count|total|current|index|number|^n$|^num|done|size")

#: Lines external programs (FreeCAD, OpenCASCADE, kicad-cli) print on their
#: own stdout, from the overlay tests of earlier rounds, plus the record that
#: round 2 excused through the catalogue label «File:» («Fichier :»).
EXTERNAL_RECORDS = (
    "FreeCAD: Step File Name : C:/Lavori/Componenti/Test.step(134238 ents) Write Done",
    "FreeCAD: ** WorkSession : Sending all data",
    "WorkSession : Sending all data",
    "Step File Name : C:/out/Test.step",
    "Step File Name : /home/mirko/lavori/Test.step",
    "[OCC] Fichier : C:/tmp/x.step",
    "Fichier : C:/tmp/x.step",
    "FreeCAD: ******        Transferring Shape, ShapeType = 0    ******",
    "Shapes written (98211 ents)",
    "FreeCAD exit=0, timeout=False",
    "FreeCAD 1.1.1, Libs: 1.1.1R41202 (Git)",
    "(C) 2001-2026 FreeCAD contributors",
    "[2026-09-21T00:08:36+02:00] FreeCAD start",
    "Recompute......",
    "FreeCAD: (47 %)",
)


def _rendering(source, language):
    """The bundled catalogue text of an Italian source in one language."""
    return DEFAULT_TRANSLATIONS["ui:" + source][language]


def _sample(parts):
    """A template rendered with neutral values: a number for counts, a word for the rest."""
    return " ".join("".join(
        ("3" if _COUNT_NAME.search(value) else "Alpha") if kind == "ph" else value
        for kind, value in parts).split())


def _catalogue():
    """Every (source, language, template) of the bundled catalogue."""
    for key, entry in DEFAULT_TRANSLATIONS.items():
        if not key.startswith("ui:") or len(key) == 3 or not isinstance(entry, dict):
            continue
        for language in LANGUAGES:
            text = entry.get(language)
            if isinstance(text, str) and text.strip():
                yield key[3:], language, text


def _unanchored(parts):
    """True when a template's literal text carries no word of three letters («{a} : {b}»)."""
    return ao._anchor_word(parts) is None


# ── 1. Catalogue-wide: a program sentence is human in every language ────────

def test_every_rendering_of_a_human_italian_template_is_human_in_every_language():
    """@brief Property over the WHOLE catalogue (18 000 renderings): the language never decides.

    When the Italian rendering of a template is a line for a person, its
    IT/EN/FR/DE renderings with the same neutral values are too. The only
    exceptions allowed are the templates with no literal word at all
    («{label} : {value}»): rendered, they ARE the «Field : value» shape of a
    trace record, and excusing them would let «WorkSession : Sending all data»
    through (see the negative test below). They are listed, not hidden.
    """
    failures, unclassifiable = [], set()
    for source, language, text in _catalogue():
        italian = _sample(ao._template_parts(DEFAULT_TRANSLATIONS["ui:" + source].get("it", source)))
        if ao.is_machine_line(italian):
            continue
        parts = ao._template_parts(text)
        if not ao.is_machine_line(_sample(parts)):
            continue
        if _unanchored(parts):
            unclassifiable.add(text)
        else:
            failures.append((language, source, _sample(parts)))
    assert failures == []
    # The listed exceptions: French renderings of templates made of placeholders only.
    assert unclassifiable and all(" : " in text for text in unclassifiable)
    assert "{label} : {value}" in unclassifiable and "{count} : {names}" in unclassifiable


@pytest.mark.parametrize("record", EXTERNAL_RECORDS)
def test_external_tool_records_stay_machine_lines(record):
    """@brief Negative of the property: the same « : » from an external tool never borrows an origin."""
    assert ao.is_machine_line(record) is True


def test_an_unanchored_template_cannot_be_excused_without_excusing_a_trace_field():
    """@brief Why «{label} : {value}» stays unclassified: its rendering and a trace field are one shape."""
    template = _rendering("{label}: {value}", "fr")
    assert ao._template_parts(template) == (("ph", "label"), ("lit", " : "), ("ph", "value"))
    assert ao.is_machine_line(template.format(label="WorkSession", value="Sending all data")) is True


def test_the_word_may_be_anywhere_in_the_template_not_next_to_the_colon():
    """@brief Positive: «{name} : {outcome}» inside «Composant {current} sur {total} — …» is excused.

    The literal piece « : » has no word; the template's «Composant» and «sur»
    are the evidence. Negative twin: the label «Fichier :» inside a longer
    external record renders no WHOLE line, so it excuses nothing.
    """
    outcome = _rendering("letture concordi, categoria sostenuta dai ritagli", "fr")
    line = _rendering("Componente {current} di {total} — {name}: {outcome}.", "fr").format(
        current=3, total=12, name="U1", outcome=outcome)
    assert " : " in line and ao.is_machine_line(line) is False
    assert _rendering("File:", "fr") == "Fichier :"
    assert ao.is_machine_line("[OCC] Fichier : C:/tmp/x.step") is True


def test_a_colon_in_a_value_is_still_not_the_templates():
    """@brief Negative: one literal colon cannot explain two; the whole-template rule keeps round 2's."""
    line = _rendering("Componente {current} di {total} — {name}: {outcome}.", "fr").format(
        current=3, total=12, name="U1", outcome="WorkSession : Sending all data")
    assert ao.is_machine_line(line) is True


# ── 1b. Bare templates («Word : {value}»): the values decide ────────────────

@pytest.mark.parametrize("line", [
    "Échec : délai dépassé",                                 # a reason in prose
    "Échec : [Errno 2] No such file: 'C:/x/y.step'",         # a path INSIDE prose
    "Broche 3 : RX_TX",                                      # a designator, not a record header
])
def test_a_bare_template_with_a_prose_value_is_human(line):
    """@brief Positive: «Échec : {error}», «Broche {number} : {name}» with a value written for a person."""
    assert ao.is_machine_line(line) is False


@pytest.mark.parametrize("line", [
    "Échec : C:/tmp/x.step",                                 # the value IS a path: a field record
    "Cause : FreeCAD 1.1.1, Libs: 1.1.1R41202 (Git)",        # the value is a start-up banner
])
def test_a_bare_template_with_a_record_shaped_value_is_not_excused(line):
    """@brief Negative: one word and a colon are thin evidence; a record-shaped value outweighs it."""
    assert ao.is_machine_line(line) is True


def test_a_template_with_more_words_keeps_a_lone_path_value():
    """@brief Structural twin: «Impossible de supprimer {path} : {error}» has two words of its own.

    The literal text alone pins the origin, so a lone path in its value
    (the file the program could not delete) keeps the line on the card, as in
    Italian. The lone-path refusal is for bare templates only.
    """
    line = _rendering("Impossibile eliminare {path}: {error}", "fr").format(
        path="C:/tmp/x.step", error="accès refusé")
    assert ao.is_machine_line(line) is False


def test_the_banner_value_is_machine_in_italian_too():
    """@brief The value rules are the ones an Italian «Campo: valore» line meets: same verdict."""
    assert ao.is_machine_line("Causa: FreeCAD 1.1.1, Libs: 1.1.1R41202 (Git)") is True


@pytest.fixture
def card(qapp, tmp_path):
    """@brief A card with a real I18n (French), released after the test."""
    i18n = I18n(ProfileStore(tmp_path / "profiles.sqlite3"), "fr")
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


def test_the_crop_reading_progress_reaches_the_card_in_french(card):
    """@brief As the reviewer met it: the worker's plain-str line is shown, an external record is not."""
    _i18n, overlay = card
    overlay.start("Analisi AI in corso")
    outcome = _rendering("letture concordi, categoria sostenuta dai ritagli", "fr")
    line = _rendering("Componente {current} di {total} — {name}: {outcome}.", "fr").format(
        current=3, total=12, name="U1", outcome=outcome)
    overlay.set_status(str(line))
    assert overlay.status_plain_text() == line
    overlay.set_status("[OCC] Fichier : C:/tmp/x.step")
    assert overlay.status_plain_text() == line


# ── 2. Time on the GUI thread ───────────────────────────────────────────────

#: Generous bound, per cold run: measured on the development PC about 0.5 ms
#: for a 5 000-character line (beyond the cap) and at most 7 ms to classify,
#: search and recompose the worst line under the cap with every cache and
#: compiled pattern cleared (before round 3: 849 ms in `is_machine_line`,
#: 22 s for a line of catalogue sentences). The margin, 7x to 100x, keeps a
#: slower CI runner green while a regression to super-linear matching --
#: seconds -- still fails. The best of three cold runs is taken, so a
#: garbage collection or a scheduler pause during one run is not a failure.
_BUDGET_SECONDS = 0.050


def _best_cold_seconds(action, runs=3):
    """The shortest of `runs` timings of `action`, each started with cleared caches."""
    best = float("inf")
    for _run in range(runs):
        _clear_caches()
        start = time.perf_counter()
        action()
        best = min(best, time.perf_counter() - start)
    return best


def _clear_caches():
    """Start a measurement cold: no memoised line, no compiled pattern."""
    ao._machine_line.cache_clear()
    ao._cached_readings.cache_clear()
    ao._TEMPLATE_PATTERNS.clear()


def _french_mix(length, seed):
    """French catalogue sentences with a spaced colon, joined up to `length` characters."""
    import random
    sentences = sorted(" ".join(text.split()) for _source, language, text in _catalogue()
                       if language == "fr" and " : " in text)
    pick = random.Random(seed)
    line = ""
    while len(line) < length:
        line += pick.choice(sentences) + " "
    return line[:length]


def test_the_cap_is_derived_from_the_catalogue():
    """@brief Contract: twice the longest literal text of an indexed template, not a tuned number."""
    ao._catalog_index()
    longest = max(len(ao._literal_text(parts)) for candidates in ao._catalog_index().values()
                  for _source, _language, parts in candidates)
    assert ao._template_match_limit() == 2 * longest


def test_a_line_beyond_the_cap_is_not_matched_and_one_within_is():
    """@brief Positive and negative of the cap on the same French sentence."""
    line = _rendering("Componente {current} di {total} — {name}: {outcome}.", "fr").format(
        current=3, total=12, name="U1", outcome="lectures concordantes")
    assert ao.catalog_readings(line)
    padded = line[:-1] + " " + "x" * ao._template_match_limit() + "."
    assert ao.catalog_readings(padded) == []


@pytest.mark.parametrize("seed", range(5))
def test_a_5000_character_line_is_classified_within_budget(seed):
    """@brief Benchmark: a 5 000-character line of French catalogue sentences, cold caches."""
    line = _french_mix(5000, seed)
    assert _best_cold_seconds(lambda: ao.is_machine_line(line)) < _BUDGET_SECONDS


@pytest.mark.parametrize("seed", range(5))
def test_the_worst_line_under_the_cap_is_classified_and_recomposed_within_budget(seed):
    """@brief Benchmark below the cap, where matching really runs: classify, search and recompose."""
    line = _french_mix(ao._template_match_limit(), seed)
    lookup = _Lookup("en")

    def work():
        """Everything the card does with one received line."""
        ao.is_machine_line(line)
        ao.catalog_readings(line, whole=False)
        ao.recompose_in_language(line, lookup)
    assert _best_cold_seconds(work) < _BUDGET_SECONDS


def test_a_line_is_classified_once():
    """@brief Memoised per line text: the card asks again at every redraw."""
    line = _rendering("Componente {current} di {total} — {name}: {outcome}.", "fr").format(
        current=1, total=2, name="J9", outcome="ok")
    ao.is_machine_line(line)
    hits = ao._machine_line.cache_info().hits
    ao.is_machine_line(line)
    assert ao._machine_line.cache_info().hits == hits + 1


def test_the_earliest_alignment_still_ends_on_the_last_literal():
    """@brief The atomic groups never anchor the template's LAST literal early.

    «{remarks}.» must end at the line's last full stop, not at the first one
    inside the value; and a value made of the optional full stop alone is
    not cut empty.
    """
    template = _rendering("Nota sulla lettura per catene: {remarks}.", "fr")
    line = template.format(remarks="pas 2.54 mm. Rangée 7.62 mm")
    readings = [match for source, _l, _p, match in ao.catalog_readings(line)
                if source == "Nota sulla lettura per catene: {remarks}."]
    assert readings and readings[0].group("g0") == "pas 2.54 mm. Rangée 7.62 mm"
    assert ao.is_machine_line(line) is False


# ── 3. The most specific template recomposes the line ───────────────────────

class _Lookup:
    """The two calls `recompose_in_language` makes, on the bundled catalogue."""

    def __init__(self, language):
        self.language = language

    def text(self, source):
        return DEFAULT_TRANSLATIONS.get("ui:" + source, {}).get(self.language, source)

    def raw(self, key, default=None):
        return DEFAULT_TRANSLATIONS.get(key, {}).get(self.language, default)


@pytest.mark.parametrize("line,french", [
    ("Pin 3: GND", "Broche 3 : GND"),                       # «Pin {number}» renders it too
    ("Pin 3", "Broche 3"),                                  # only the generic one: it is the origin
    ("Pad 3: drill incompleto.", "Pastille 3 : drill incomplet."),  # «Pad {number}» renders it too
])
def test_the_most_specific_template_recomposes_the_line(line, french):
    """@brief Positive: the template with the most literal characters is the origin."""
    assert ao.recompose_in_language(line, _Lookup("fr")) == french


def test_a_tie_between_equally_specific_templates_leaves_the_line_unchanged():
    """@brief Negative: «Pad 7» is «Pad {number}» (IT/EN) or «pad {number}» (DE «Pad {number}»).

    Same literal length, different French renderings («Pastille» or
    «pastille»): the origin is ambiguous and is not guessed.
    """
    assert _rendering("pad {number}", "de") == "Pad {number}"
    assert ao.recompose_in_language("Pad 7", _Lookup("fr")) == "Pad 7"


def test_an_unanchored_template_still_never_recomposes():
    """@brief Negative kept from round 2: «{count}: {names}» is no origin."""
    assert ao.recompose_in_language("12: alpha, beta", _Lookup("fr")) == "12: alpha, beta"
