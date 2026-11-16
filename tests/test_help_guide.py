"""@brief The Italian user guide ships as a self-contained, well-formed HTML page."""
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "kicadgen" / "gui" / "assets" / "help" / "guida.html"
REPOSITORY = "https://github.com/MirkoUgoliniDev/KiCad-ComponentGenerator"

#: Elements that never have a closing tag in HTML.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}


class _Checker(HTMLParser):
    """Collect nesting errors, ids, anchors and every URL-bearing attribute."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.ids, self.anchors, self.urls = [], [], set(), [], []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attributes = dict(attrs)
        if "id" in attributes:
            if attributes["id"] in self.ids:
                self.errors.append(f"duplicate id {attributes['id']}")
            self.ids.add(attributes["id"])
        for name in ("href", "src", "action", "data", "poster"):
            if attributes.get(name):
                self.urls.append((tag, name, attributes[name]))
        if tag == "a" and attributes.get("href", "").startswith("#"):
            self.anchors.append(attributes["href"][1:])
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> (open: {self.stack[-3:]})")
            return
        self.stack.pop()


def _parse(text):
    checker = _Checker()
    checker.feed(text)
    checker.close()
    return checker


@pytest.fixture(scope="module")
def guide_text():
    assert GUIDE.is_file(), "guida.html is missing"
    return GUIDE.read_text(encoding="utf-8")


def test_guide_is_well_formed_html(guide_text):
    """@brief Balanced tags, unique ids, every internal anchor resolves."""
    checker = _parse(guide_text)
    assert not checker.errors, checker.errors
    assert not checker.stack, f"unclosed tags: {checker.stack}"
    assert guide_text.lstrip().lower().startswith("<!doctype html>")
    assert '<html lang="it">' in guide_text and '<meta charset="utf-8">' in guide_text
    missing = [anchor for anchor in checker.anchors if anchor not in checker.ids]
    assert not missing, f"broken anchors: {missing}"


def test_checker_detects_broken_markup():
    """@brief Negative: the checker is not blind to a misnested page."""
    assert _parse("<html><body><p><b>x</p></b></body></html>").errors
    assert _parse("<div><p>x</p>").stack == ["div"]


def test_guide_has_no_external_resources(guide_text):
    """@brief Offline and private: the only external link is the project repository."""
    checker = _parse(guide_text)
    for tag, attribute, url in checker.urls:
        if url.startswith("#"):
            continue
        assert tag == "a" and attribute == "href", f"external resource: <{tag} {attribute}={url}>"
        assert url.startswith(REPOSITORY), f"unexpected link: {url}"
    assert not {"script", "img", "link", "iframe", "object", "embed"} & set(checker.tags)
    # CSS may fetch too: no url(...) and no @import in the inline style.
    assert not re.search(r"url\s*\(|@import", guide_text, re.IGNORECASE)
    assert not re.findall(r"https?://(?!github\.com/MirkoUgoliniDev/KiCad-ComponentGenerator)"
                          r"[^\s\"'<>)]+", guide_text)


def test_guide_follows_light_and_dark_theme(guide_text):
    """@brief Readable in both schemes: explicit colours for light and a dark override."""
    assert "prefers-color-scheme: dark" in guide_text and "--bg" in guide_text


@pytest.mark.parametrize("label", [
    # Dal 26/09 i documenti hanno piu' ruoli per file: le caselle usano le
    # etichette brevi dei ruoli (document_roles.ROLE_SHORT).
    "Nuovo progetto...", "Pinout", "Quote", "Alta risoluzione", "Piazzole", "Vista laterale",
    "Nuovo documento", "Aggiungi…", "Scheda con componenti · connettori passanti",
    "Scheda con componenti · connettori castellated (mezzo foro)",
    "Estrai parametri con AI", "Credito AI", "Footprint 2D (KiCadGen)",
    "Modelli dalla libreria KiCad", "Solido 3D con FreeCAD", "Chiudi",
    "Modifica componenti…", "Scelta del modello STEP", "Colori",
    "Mostra gli header nel modello STEP", "Montaggio e analisi", "Dati del progetto",
    "Torna ai progetti", "Strumenti AI", "Footprint sulla scheda ospitante",
    "Aggiorna solo il footprint", "Pad Ø:", "Foro Ø:",
    "Saldatura diretta / piazzole del modulo", "Strip header / pad circolari con foro",
    # CONTRACT CHANGED 2026-11-15 (guided KiCad tab): the step commands say
    # their name as text, «Apri in KiCad» and the two installations.
    "Apri in KiCad", "Installa nel progetto KiCad (consigliato)",
    "Installa come libreria globale", "Controlla di nuovo", "analisi-ai.log", "freecad.log",
    # 2026-10-30: the guide still said «modello 3D» for these two commands.
    "Rigenera modello STEP con queste annotazioni", "Elimina dal modello STEP (falso positivo)",
])
def test_guide_names_the_real_commands(guide_text, label):
    """@brief The guide uses the Italian labels the program actually shows.

    @note Whitespace is collapsed first, as a browser does: a label may wrap
        across source lines.
    """
    assert label in re.sub(r"\s+", " ", guide_text)


@pytest.mark.parametrize("label,source", [
    ("Nuovo progetto...", "kicadgen/gui/main_window.py"),
    ("Footprint 2D (KiCadGen)", "kicadgen/gui/main_window.py"),
    ("Modifica componenti…", "kicadgen/gui/main_window.py"),
    ("Mostra gli header nel modello STEP",
     "kicadgen/gui/main_window.py"),
    ("Aggiorna solo il footprint", "kicadgen/gui/main_window.py"),
    ("Montaggio e analisi", "kicadgen/gui/project_wizard.py"),
    ("Torna ai progetti", "kicadgen/gui/project_wizard.py"),
    # CONTRACT CHANGED 2026-11-15: the guide names the preview «Apri in KiCad»
    # (step 2 of the KiCad tab), no longer «Prova in KiCad...».
    ("Apri in KiCad", "kicadgen/gui/main_window.py"),
    ("Scelta del modello STEP", "kicadgen/gui/model_candidates_dialog.py"),
    ("Colori", "kicadgen/gui/model_candidates_dialog.py"),
    ("'top': 'Quote'", "kicadgen/services/document_roles.py"),
    ("'side_view': 'Vista laterale'", "kicadgen/services/document_roles.py"),
    ("Nuovo documento", "kicadgen/gui/guided_documents.py"),
])
def test_quoted_labels_still_exist_in_the_code(label, source):
    """@brief When a command is renamed in the code, this fails and the guide is updated."""
    assert label in (ROOT / source).read_text(encoding="utf-8")


def test_guide_ships_in_the_package():
    """@brief The PyInstaller spec copies the whole assets folder, help/ included."""
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    assert '(str(ROOT / "kicadgen" / "gui" / "assets"), "kicadgen/gui/assets")' in spec


# ── Translated guides (2026-10-30: the guide opened in Italian in every language) ──

TRANSLATIONS = {"en": "guida.en.html", "fr": "guida.fr.html", "de": "guida.de.html"}


def _structure(text):
    """Tag sequence, ids and link targets: what a translation must keep."""
    checker = _parse(text)
    return checker.tags, sorted(checker.ids), [url for _tag, _name, url in checker.urls]


@pytest.mark.parametrize("language", sorted(TRANSLATIONS))
def test_each_translated_guide_keeps_the_italian_structure(guide_text, language):
    """@brief Same elements, ids and links as guida.html, its own lang, well-formed.

    @note A chapter added to one guide only breaks this test, so the four
        guides cannot drift apart unnoticed.
    """
    path = GUIDE.with_name(TRANSLATIONS[language])
    assert path.is_file(), f"{path.name} is missing"
    text = path.read_text(encoding="utf-8")
    checker = _parse(text)
    assert not checker.errors and not checker.stack
    assert f'<html lang="{language}">' in text and '<meta charset="utf-8">' in text
    assert _structure(text) == _structure(guide_text)


def test_the_structure_check_sees_a_missing_chapter(guide_text):
    """@brief Negative: removing one section changes the structure."""
    trimmed = re.sub(r"<h2[^>]*>.*?</h2>", "", guide_text, count=1, flags=re.S)
    assert _structure(trimmed) != _structure(guide_text)


@pytest.mark.parametrize("language", sorted(TRANSLATIONS))
@pytest.mark.parametrize("label", [
    "Nuovo progetto...", "Estrai parametri con AI", "Apri in KiCad",
    "Installa nel progetto KiCad (consigliato)", "Installa come libreria globale",
    "Controlla di nuovo",
    "Aggiorna solo il footprint", "Scelta del modello STEP",
    "Rigenera modello STEP con queste annotazioni", "Elimina dal modello STEP (falso positivo)",
])
def test_translated_guides_name_the_labels_of_their_interface(language, label):
    """@brief A quoted command appears as that language's interface shows it.

    @note The label comes from the bundled catalog, so a renamed translation
        fails here instead of leaving the guide wrong.
    """
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    expected = DEFAULT_TRANSLATIONS["ui:" + label][language]
    text = GUIDE.with_name(TRANSLATIONS[language]).read_text(encoding="utf-8")
    assert expected in re.sub(r"\s+", " ", text)


def _section(text, anchor):
    """The text of the `<h3 id=anchor>` section up to the next heading, whitespace collapsed."""
    match = re.search(rf'<h3 id="{anchor}">(.*?)<h[23] ', text, re.S)
    assert match, f"section {anchor} is missing"
    return re.sub(r"\s+", " ", match.group(1))


def test_the_check_section_says_a_stopped_check_must_be_repeated(guide_text):
    """@brief Review 2026-11-15: «Interrompi» and a check that could not start
    leave the installation blocked until «Controlla di nuovo»; the guide said
    only that warnings and checks not run allow it.

    @note Tied to the code: the two states must still be wait reasons of the
        installation in the window, or this fails and the guide is updated.
    """
    section = _section(guide_text, "controllo")
    assert "controllo interrotto" in section and "non è riuscito a partire" in section
    assert "va ripetuto prima di installare" in section
    window = (ROOT / "kicadgen" / "gui" / "main_window.py").read_text(encoding="utf-8")
    reasons = window[window.index("KICAD_CHECK_WAIT_REASONS = {"):]
    reasons = reasons[:reasons.index("}")]
    assert '"aborted":' in reasons and '"failed":' in reasons


def test_the_check_section_lists_every_reason_it_waits_for(guide_text):
    """@brief The wait reasons are those of `_kicad_block_now`, not «only» two.

    @note Negative included: the old sentence («Aspetta solo se...») that
        named only work in progress and mismatching files is gone.
    """
    section = _section(guide_text, "controllo")
    assert "Aspetta solo se" not in section
    for reason in ("sta scrivendo i file", "lasciate bloccate", "domanda sul footprint",
                   "non corrispondono ai parametri", "modello STEP è obsoleto"):
        assert reason in section, reason
    window = (ROOT / "kicadgen" / "gui" / "main_window.py").read_text(encoding="utf-8")
    for code in ('"busy"', '"pads_kept_blocked"', '"footprint_question"'):
        assert code in window, code


def test_the_kicad_chapter_installs_without_a_step_model(guide_text):
    """@brief Review 2026-11-15: the chapter's last paragraph said the installation
    waits for «the required files», contradicting installation without STEP."""
    text = re.sub(r"\s+", " ", guide_text)
    assert "finché i file richiesti non sono presenti" not in text
    assert "l'installazione resta disabilitata finché non c'è il footprint" in text


def test_guide_never_calls_the_step_file_a_3d_model(guide_text):
    """@brief The visible name is «modello STEP» (project rule), never «modello 3D»."""
    assert not re.search(r"modell[oi]\s+3D", guide_text, re.IGNORECASE)


# ── Check states of step 3 in the docs (review 2026-11-15, round 2, R13) ──
#
# CONTRACT CHANGED 2026-11-15 (round 2): the four guides and both READMEs
# said that a kicad-cli not answering within 60 s or not starting «does not
# block installation». The window now has states for it: «unread» blocks
# (a KiCad read of an installed file is missing), «cli_unusable» allows (the
# same failure in two runs in a row: a lasting cause, like no kicad-cli),
# «crashed» allows (internal error after every required check),
# «crash_incomplete» blocks (internal error before them). Each sentence below
# is tied to the state code it describes, so a state renamed or removed in the
# window fails here and the docs are realigned.

#: Per document: (state code, phrase that describes it). The phrases are the
#: bold heads of the step-3 list in the guides and the README bullets.
#: CONTRACT CHANGED 2026-11-15 (round 4, item 2): two more blocking states,
#: «file_busy» (a component file held by another program kept a required
#: check from running) and «incomplete» (a run that ended without concluding
#: a required check); both wait for «Controlla di nuovo».
CHECK_STATE_PHRASES = {
    "guida.html": [("unread", "KiCad non ha letto tutti i file"),
                   ("cli_unusable", "stesso errore di KiCad CLI in due controlli di fila"),
                   ("crashed", "controllo fermato da un errore interno"),
                   ("crash_incomplete", "altrimenti l'installazione resta bloccata finché"),
                   ("file_busy", "un file del componente è in uso da un altro programma"),
                   ("incomplete", "controllo terminato senza concludere tutte le verifiche che servono")],
    "guida.en.html": [("unread", "KiCad did not read every file"),
                      ("cli_unusable", "same KiCad CLI failure in two checks in a row"),
                      ("crashed", "check stopped by an internal error"),
                      ("crash_incomplete", "otherwise installation stays blocked until"),
                      ("file_busy", "a file of the component is in use by another program"),
                      ("incomplete", "check ended without concluding every check the installation needs")],
    "guida.fr.html": [("unread", "KiCad n’a pas lu tous les fichiers"),
                      ("cli_unusable", "même échec de KiCad CLI dans deux contrôles de suite"),
                      ("crashed", "contrôle arrêté par une erreur interne"),
                      ("crash_incomplete", "sinon l’installation reste bloquée tant que"),
                      ("file_busy", "un fichier du composant est utilisé par un autre programme"),
                      ("incomplete", "contrôle terminé sans conclure toutes les vérifications nécessaires")],
    "guida.de.html": [("unread", "KiCad hat nicht alle Dateien gelesen"),
                      ("cli_unusable", "derselbe Fehler von KiCad CLI in zwei Prüfungen hintereinander"),
                      ("crashed", "durch einen internen Fehler angehaltene Prüfung"),
                      ("crash_incomplete", "andernfalls bleibt die Installation gesperrt"),
                      ("file_busy", "eine Datei des Bauteils wird von einem anderen Programm verwendet"),
                      ("incomplete", "Prüfung beendet, ohne alle Prüfungen abzuschließen, die die "
                                     "Installation braucht")],
}

#: The former sentences that called every tool problem harmless, per guide.
FORMER_TOOL_SENTENCES = {
    "guida.html": "risulta «non eseguito» e non blocca l'installazione",
    "guida.en.html": "it is reported as «not run» and does not block installation",
    "guida.fr.html": "il apparaît comme « non exécuté » et ne bloque pas l’installation",
    "guida.de.html": "Es gilt als „nicht ausgeführt“ und sperrt die Installation nicht",
}


def _window_source():
    """The text of main_window.py, where the check states are decided."""
    return (ROOT / "kicadgen" / "gui" / "main_window.py").read_text(encoding="utf-8")


def _state_codes_returned():
    """The state codes `_kicad_check_state` returns and the ones that wait (block)."""
    window = _window_source()
    body = window[window.index("def _kicad_check_state(self)"):]
    body = body[:body.index("\n    def ", 10)]
    returned = set(re.findall(r'return "(\w+)"', body))
    returned |= {code for pair in re.findall(r'return "(\w+)" if \w+ else "(\w+)"', body) for code in pair}
    reasons = window[window.index("KICAD_CHECK_WAIT_REASONS = {"):]
    reasons = reasons[:reasons.index("\n    }")]
    waiting = set(re.findall(r'^\s+"(\w+)":', reasons, re.M))
    return returned, waiting


def test_the_documented_check_states_exist_and_block_as_described():
    """@brief «unread», «crash_incomplete», «file_busy» and «incomplete» block (wait reasons of
    step 3); «cli_unusable» and «crashed» are states that do NOT wait, i.e. allow the
    installation, as the docs say."""
    returned, waiting = _state_codes_returned()
    for code in ("unread", "crash_incomplete", "cli_unusable", "crashed", "file_busy", "incomplete"):
        assert code in returned, code
    assert {"unread", "crash_incomplete", "file_busy", "incomplete"} <= waiting
    assert not {"cli_unusable", "crashed"} & waiting


@pytest.mark.parametrize("name", sorted(CHECK_STATE_PHRASES))
def test_each_guide_describes_every_check_state_of_step_3(name):
    """@brief Positive: every state has its phrase in the check section of each guide.
    Negative: the former «does not block» sentence is gone."""
    text = GUIDE.with_name(name).read_text(encoding="utf-8")
    section = _section(text, "controllo")
    returned, _waiting = _state_codes_returned()
    for code, phrase in CHECK_STATE_PHRASES[name]:
        assert code in returned, code
        assert phrase in section, (name, code, phrase)
    assert FORMER_TOOL_SENTENCES[name] not in section
    # CONTRACT CHANGED 2026-11-15 (round 3): a timeout CAN now be a lasting
    # cause (the same timeout in two checks in a row); a single timeout keeps
    # blocking as «unread», and the guide names the read budget of the service.
    from kicadgen.services.component_check import CLI_READ_TIMEOUT_S
    assert str(CLI_READ_TIMEOUT_S) in section


@pytest.mark.parametrize("readme,phrases,former", [
    ("README.md", ["KiCad did not read every file", "same KiCad CLI failure in two checks in a row",
                   "stopped on an internal error", "otherwise installation stays blocked",
                   "a file of the component is in use by another program",
                   "check ended without concluding every check the installation needs"],
     "KiCad CLI missing or not\n  answering within 60 seconds): you can install"),
    ("README.it.md", ["KiCad non ha letto tutti i file", "stesso errore di KiCad CLI in due controlli di fila",
                      "fermato da un errore interno", "altrimenti l'installazione resta",
                      "un file del componente è in uso da un altro programma",
                      "controllo terminato senza concludere tutte le verifiche che servono"],
     "KiCad CLI\n  assente o che non risponde entro 60 secondi): puoi installare"),
])
def test_the_readmes_describe_the_check_states(readme, phrases, former):
    """@brief Same states in both READMEs; the former sentence that let a 60 s timeout install
    is gone (compared with whitespace collapsed)."""
    text = (ROOT / readme).read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", text)
    for phrase in phrases:
        assert phrase in flat, (readme, phrase)
    assert re.sub(r"\s+", " ", former) not in flat


# ── Checks a stopped run must have concluded (review 2026-11-15, round 3) ──
#
# CONTRACT CHANGED 2026-11-15 (round 3): the docs listed five hand-picked
# required checks (footprint file, pin-pad match, STEP integrity, KiCad's reads
# of the footprint and of the symbol file) and the test compared
# `REQUIRED_FOR_INSTALL` with that literal set. The service now declares EVERY
# check that can block (`component_check.BLOCKING_CHECKS`), each with the
# conditions under which it applies; «crashed» allows the installation only
# when every applicable one concluded. The test is now tied to that table: each
# key must be mapped to the words the Italian guide uses for its family, and
# each of its conditions to the words that say when it applies, in the SAME
# item of the guide's «required-checks» list. A blocking check or a condition
# added to the service fails here until it is documented; one removed fails
# because its mapping is stale.

#: Words of the Italian guide that name the family of each blocking check
#: (a key of `component_check.BLOCKING_CHECKS`).
BLOCKING_CHECK_WORDS = {
    "kicad_version": "la versione di KiCad",
    "footprint_file": "il file del footprint",
    "footprint_pads_overlap": "pad sovrapposti",
    "lead_hole": "il loro passaggio nei fori",
    "model_link": "collegamenti al modello STEP",
    "kicad_footprint_read": "le letture di KiCad del footprint",
    "pin_pad": "la corrispondenza pin-pad",
    "kicad_symbol_read": "del simbolo applicato",
    "kicad_symbol_file_read": "del file del simbolo installato",
    "symbol_error": "la costruzione del simbolo",
    "document_dropped": "la piedinatura del documento",
    "document_unnamed": "la piedinatura del documento",
    # Added 2026-11-16 (review R8-4/S3): a declared contact total above the
    # pad limit is a blocking document check of the same family; the guide's
    # «la piedinatura del documento» item already covers it.
    "document_count_unusable": "la piedinatura del documento",
    "pin_role_contradiction": "le funzioni dei contatti",
    "step_integrity": "l'integrità del modello STEP",
    "kicad_3d_import": "la sua importazione in KiCad",
    "drc": "il DRC dell'anteprima",
}

#: Words of the Italian guide that say when a check applies, per declared
#: condition (`component_check.CONDITIONS`).
CONDITION_WORDS = {
    "cli": "con KiCad CLI",
    "step": "quando c'è uno STEP",
    "symbol_file": "file del simbolo installato, quando esiste",
    "document": "quando esiste una lettura del documento",
    "contacts": "quando il simbolo dichiara dei contatti",
    "pad_leads": "quando ci sono terminali dichiarati",
}


def _required_check_items(text):
    """The plain-text items of the «required-checks» list of the check section, whitespace collapsed."""
    section = re.search(r'<h3 id="controllo">(.*?)<h[23] ', text, re.S).group(1)
    match = re.search(r'<ul class="required-checks">(.*?)</ul>', section, re.S)
    assert match, "the check section has no required-checks list"
    items = re.split(r"<li>", match.group(1))[1:]
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", item)).strip() for item in items]


def _undocumented_checks(items):
    """Problems of `items` against `BLOCKING_CHECKS`: a family with no item, a condition missing from its item."""
    from kicadgen.services.component_check import BLOCKING_CHECKS
    problems = []
    for code, entry in BLOCKING_CHECKS.items():
        holders = [item for item in items if BLOCKING_CHECK_WORDS[code] in item]
        if not holders:
            problems.append((code, "no item"))
            continue
        for condition in sorted(entry.applies):
            if not any(CONDITION_WORDS[condition] in item for item in holders):
                problems.append((code, condition))
    return problems


def test_the_required_checks_the_docs_list_are_those_of_the_service(guide_text):
    """@brief Every blocking check of the service, with the conditions under which it applies,
    is named in the guide's list of what a stopped check must have concluded.

    @note Tied to `BLOCKING_CHECKS` and `CONDITIONS`: the mappings must cover
        exactly their keys, so a check or a condition added or removed in the
        service fails here until the docs follow.
    """
    from kicadgen.services.component_check import BLOCKING_CHECKS, CONDITIONS, REQUIRED_FOR_INSTALL
    assert set(BLOCKING_CHECK_WORDS) == set(BLOCKING_CHECKS)
    assert REQUIRED_FOR_INSTALL == frozenset(BLOCKING_CHECKS)
    assert set(CONDITION_WORDS) == set(CONDITIONS)
    items = _required_check_items(guide_text)
    assert _undocumented_checks(items) == []
    # The former five-item sentence is gone.
    section = _section(guide_text, "controllo")
    assert "integrità dello STEP quando c'è" not in section
    assert "letture di KiCad del footprint e del file del simbolo" not in section


def test_the_required_checks_test_sees_a_missing_family_or_condition(guide_text):
    """@brief Negative: an item without a check's condition, or without its family, is reported."""
    items = _required_check_items(guide_text)
    no_condition = [item.replace(CONDITION_WORDS["pad_leads"], "") for item in items]
    assert ("lead_hole", "pad_leads") in _undocumented_checks(no_condition)
    no_family = [item.replace(BLOCKING_CHECK_WORDS["drc"], "") for item in items]
    assert ("drc", "no item") in _undocumented_checks(no_family)


@pytest.mark.parametrize("language", sorted(TRANSLATIONS))
def test_each_guide_lists_as_many_required_check_families(guide_text, language):
    """@brief The translated guides keep the same number of families as the Italian list."""
    text = GUIDE.with_name(TRANSLATIONS[language]).read_text(encoding="utf-8")
    assert len(_required_check_items(text)) == len(_required_check_items(guide_text))


# ── Lasting kicad-cli causes (review 2026-11-15, round 3) ──
#
# CONTRACT CHANGED 2026-11-15 (round 3): the docs said that a check stopping
# every time on the same internal error was a lasting cause and that a
# timeout never was. The window now treats every DECLARED tool-side failure
# as possibly lasting (`CLI_TOOL_FAILURES`: start, timeout, not_run,
# environment), with its own sentence per cause (`KICAD_PERSISTENT_CAUSE_TEXTS`),
# and an internal crash of the program never is (`_kicad_failure_signature`
# returns None on «check_crashed»).

#: Words of the Italian guide's «cli_unusable» item, per declared cause (a key
#: of `KICAD_PERSISTENT_CAUSE_TEXTS`); «tool» is a mix of causes.
PERSISTENT_CAUSE_WORDS = {
    "start": "KiCad CLI non si avvia",
    "exit_code": "con lo stesso codice di uscita",
    "no_output": "senza produrre alcun risultato",
    "timeout": "non risponde mai entro il tempo",
    "environment": "rifiuta anche un file di prova valido",
    "tool": "una combinazione fissa di queste cause",
    # CONTRACT CHANGED 2026-11-15 (round 4, item 4): the check's own
    # temporary folder unusable is a declared tool-side cause with its text.
    "scratch": "la cartella temporanea del controllo non è utilizzabile",
}

#: Per document: (sentence that says an internal error is never lasting,
#: former sentences that must be gone).
LASTING_CAUSE_SENTENCES = {
    "guida.html": ("Un errore interno del programma non conta mai come causa che dura",
                   ("Un tempo scaduto non conta mai come causa che dura", "sullo stesso errore interno")),
    "guida.en.html": ("An internal error of the program never counts as a lasting cause",
                      ("A timeout never counts as a lasting cause", "on the same internal error")),
    "guida.fr.html": ("Une erreur interne du programme ne compte jamais comme une cause durable",
                      ("Un délai dépassé ne compte jamais comme une cause durable",
                       "sur la même erreur interne")),
    "guida.de.html": ("Ein interner Fehler des Programms gilt nie als dauerhafte Ursache",
                      ("Eine Zeitüberschreitung gilt nie als dauerhafte Ursache", "mit demselben internen Fehler")),
    "README.md": ("An internal error of the program never counts as a lasting cause",
                  ("A timeout never counts as a lasting cause", "on the same internal error")),
    "README.it.md": ("Un errore interno del programma non conta mai come causa che dura",
                     ("Un tempo scaduto non conta mai come causa che dura", "sullo stesso errore interno")),
}


def _persistent_cause_keys():
    """The keys of `MainWindow.KICAD_PERSISTENT_CAUSE_TEXTS`, read from the source."""
    window = _window_source()
    table = window[window.index("KICAD_PERSISTENT_CAUSE_TEXTS = {"):]
    table = table[:table.index("\n    }")]
    return set(re.findall(r'^\s+"(\w+)":', table, re.M))


def test_the_window_never_makes_an_internal_crash_lasting_and_counts_timeouts():
    """@brief The facts the docs state: a crash has no failure signature, a timeout is a tool failure."""
    window = _window_source()
    body = window[window.index("def _kicad_failure_signature(self"):]
    body = body[:body.index("\n    def ", 10)]
    assert re.search(r'if any\(result\.code == "check_crashed" for result in results\):\s+return None', body)
    failures = re.search(r"CLI_TOOL_FAILURES = frozenset\(\{(.*?)\}\)", window, re.S).group(1)
    # CONTRACT CHANGED 2026-11-15 (round 4, item 4): «scratch» (the check's
    # temporary folder unusable) is a declared tool-side failure too.
    assert set(re.findall(r'"(\w+)"', failures)) == {"start", "timeout", "not_run", "environment", "scratch"}


def test_the_guide_names_every_lasting_cause_of_the_window(guide_text):
    """@brief Every declared cause of «cli_unusable» is named in the guide; tied to the window's table."""
    assert set(PERSISTENT_CAUSE_WORDS) == _persistent_cause_keys()
    section = _section(guide_text, "controllo")
    for cause, words in PERSISTENT_CAUSE_WORDS.items():
        assert words in section, (cause, words)


@pytest.mark.parametrize("name", sorted(LASTING_CAUSE_SENTENCES))
def test_the_docs_say_an_internal_error_is_never_lasting(name):
    """@brief Positive: the «never lasting» sentence is there. Negative: the former sentences
    (a timeout never lasting, the same internal error as a lasting cause) are gone."""
    path = GUIDE.with_name(name) if name.startswith("guida") else ROOT / name
    flat = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
    present, former = LASTING_CAUSE_SENTENCES[name]
    assert present in flat, (name, present)
    for sentence in former:
        assert sentence not in flat, (name, sentence)


# ── Failures that are the component's, and kicad-cli that worked (round 4) ──
#
# CONTRACT CHANGED 2026-11-15 (round 4, items 1 and 4): the docs said that a
# crash, an exit without output or no answer in time was «never an error of
# the component». The service now runs the differential control for those
# failures too: KiCad reading the program's sound control file of the same
# kind makes the failure the COMPONENT's (an ERROR, `cli_failure`
# «component»), e.g. a name with «%» (now refused in names,
# `output_names.KICAD_UNSAFE_NAME_CHARACTERS`). The window assumes no lasting
# cause when kicad-cli worked in the same run (`_kicad_cli_worked`), and the
# check's temporary folder unusable is a lasting cause of its own («scratch»).

#: Per document: (component-failure sentence, unsafe-name template filled with
#: each unsafe character, «kicad-cli worked» sentence, «scratch» cause, former
#: sentence that must be gone).
ROUND4_SENTENCES = {
    "guida.html": ("non finisce mai di leggerlo", "un nome che contiene «{char}»",
                   "Nessuna causa che dura viene mai presunta quando KiCad CLI ha funzionato nello "
                   "stesso controllo", "la cartella temporanea del controllo non è utilizzabile",
                   "non è mai un errore del componente"),
    "guida.en.html": ("never finishes reading it", "a name containing «{char}»",
                      "No lasting cause is ever assumed when KiCad CLI worked in the same check",
                      "the check's temporary folder is unusable", "is never an error of the component"),
    "guida.fr.html": ("ne finit jamais de le lire", "un nom qui contient « {char} »",
                      "Aucune cause durable n’est jamais supposée quand KiCad CLI a fonctionné dans "
                      "le même contrôle", "le dossier temporaire du contrôle est inutilisable",
                      "n’est jamais une erreur du composant"),
    "guida.de.html": ("liest sie nie zu Ende", "ein Name mit „{char}“",
                      "Eine dauerhafte Ursache wird nie angenommen, wenn KiCad CLI in derselben "
                      "Prüfung funktioniert hat", "der temporäre Ordner der Prüfung ist nicht nutzbar",
                      "ist nie ein Fehler des Bauteils"),
    "README.md": ("never finishes reading it", "a name containing `{char}`",
                  "No lasting cause is ever assumed when KiCad CLI worked in the same check",
                  "the check's temporary folder is unusable", "is never an error of the component"),
    "README.it.md": ("non finisce mai di leggerlo", "un nome che contiene `{char}`",
                     "Nessuna causa che dura viene mai presunta quando KiCad CLI ha funzionato nello "
                     "stesso controllo", "la cartella temporanea del controllo non è utilizzabile",
                     "non è mai un errore del componente"),
}


def test_the_code_attributes_failures_and_lasting_causes_as_the_docs_say():
    """@brief The facts behind `ROUND4_SENTENCES`: the service declares a component-side read
    failure, the window keeps it out of lasting causes, kicad-cli that worked lifts none,
    «scratch» is a lasting cause with its sentence, and «%» is refused in names."""
    from kicadgen.core.output_names import KICAD_UNSAFE_NAME_CHARACTERS, kicad_unsafe_characters
    service = (ROOT / "kicadgen" / "services" / "component_check.py").read_text(encoding="utf-8")
    assert "cli_failure='component'" in service
    window = _window_source()
    assert 'CLI_COMPONENT_FAILURE = "component"' in window
    body = window[window.index("def _kicad_failure_signature(self"):]
    body = body[:body.index("\n    def ", 10)]
    assert re.search(r"if self\._kicad_cli_worked\(results\):\s+return None", body)
    assert "scratch" in _persistent_cause_keys()
    assert "%" in KICAD_UNSAFE_NAME_CHARACTERS
    # Positive and negative: a name with «%» is refused, one without is not.
    assert kicad_unsafe_characters("x%NOPE%y") == "%"
    assert kicad_unsafe_characters("x_NOPE_y") == ""


@pytest.mark.parametrize("name", sorted(ROUND4_SENTENCES))
def test_the_docs_say_which_failures_are_the_components(name):
    """@brief Positive: each document says that a crash, no output or an endless read on the
    component's file is the component's error when the control file of the same kind is read,
    names every unsafe name character, says no lasting cause is assumed when kicad-cli worked
    and names the «scratch» cause. Negative: the former «never an error of the component» is gone."""
    from kicadgen.core.output_names import KICAD_UNSAFE_NAME_CHARACTERS
    path = GUIDE.with_name(name) if name.startswith("guida") else ROOT / name
    flat = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
    component, unsafe, worked, scratch, former = ROUND4_SENTENCES[name]
    for sentence in (component, worked, scratch):
        assert sentence in flat, (name, sentence)
    for char in KICAD_UNSAFE_NAME_CHARACTERS:
        assert unsafe.format(char=char) in flat, (name, char)
    assert former not in flat, (name, former)


def test_the_installation_guide_names_the_current_commands():
    """@brief docs/INSTALLATION-WINDOWS.md names «1 · Component check» and «Open in KiCad» with
    both editors; the former «Check component for KiCad» is gone."""
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    text = re.sub(r"\s+", " ", (ROOT / "docs" / "INSTALLATION-WINDOWS.md").read_text(encoding="utf-8"))
    assert DEFAULT_TRANSLATIONS["ui:1 · Controllo del componente"]["en"] in text
    assert DEFAULT_TRANSLATIONS["ui:Apri in KiCad"]["en"] in text
    assert "Schematic Editor and PCB Editor" in text
    assert "Check component for KiCad" not in text and "(its PCB Editor)" not in text
