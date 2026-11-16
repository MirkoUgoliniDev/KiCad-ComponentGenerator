"""@brief Review round 2 (2026-11-15, R14): translations corrected in place reach existing databases.

The start-up seeding of the SQLite catalogue only inserts missing keys
(`INSERT OR IGNORE`), so a text corrected in `DEFAULT_TRANSLATIONS` without
changing its key never reached a database created before: the stored row wins.
The former texts are listed in `i18n.SUPERSEDED_DEFAULTS`
(`_CORRECTED_IN_PLACE_2026_11_15`), which the start-up replaces only where the
stored text is still exactly the old default. These tests run on a COPY of the
committed `data/analysis_profiles.sqlite3` (never the file itself) and on a
fresh database, with a positive case (old default replaced) and a negative one
(a user-edited text kept).
"""
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS, SUPERSEDED_DEFAULTS
from kicadgen.services import i18n

#: The committed profile database: copied, never opened in place.
COMMITTED_DB = Path(__file__).resolve().parents[1] / 'data' / 'analysis_profiles.sqlite3'

#: (key, language) of every text corrected in place on 2026-11-15 (rounds 1 and 2).
CORRECTED = [(key, language)
             for key, languages in i18n._CORRECTED_IN_PLACE_2026_11_15.items()
             for language in languages]


def _stored(path, key, language):
    """The text the database holds for (key, language), or None."""
    db = sqlite3.connect(path)
    try:
        row = db.execute('SELECT text FROM translations WHERE key=? AND language=?',
                         (key, language)).fetchone()
    finally:
        db.close()
    return None if row is None else row[0]


def _write(path, rows):
    """Store `rows` (key, language, text) as if an older build had seeded them."""
    db = sqlite3.connect(path)
    try:
        with db:
            db.executemany('INSERT OR REPLACE INTO translations VALUES (?,?,?)', rows)
    finally:
        db.close()


def test_the_review_corrections_are_listed_as_superseded():
    """Each correction named by the review is in the table, with its former text."""
    expected = {
        ('ui:Premi «Applica» nella scheda Simbolo elettrico per salvarle, poi ricontrolla.', 'de'),
        ('kicad.plan_shadowing_global_symbol', 'fr'),
    }
    nothing_found = [key for key in DEFAULT_TRANSLATIONS if key.startswith('ui:«Nessun problema trovato» vuol')]
    assert len(nothing_found) == 1
    expected |= {(nothing_found[0], 'en'), (nothing_found[0], 'de')}
    german = [key for key, values in DEFAULT_TRANSLATIONS.items()
              if key in i18n._CORRECTED_IN_PLACE_2026_11_15
              and 'Elektrisches Symbol' in ' '.join(i18n._CORRECTED_IN_PLACE_2026_11_15[key].get('de', ()))]
    # The three German messages that named the Symbol tab «Elektrisches Symbol», plus
    # the «Premi Applica» one.
    assert len(german) == 4
    assert expected <= set(CORRECTED)


@pytest.mark.parametrize('key,language', CORRECTED)
def test_a_superseded_text_is_never_the_current_one(key, language):
    """An old text equal to the current default would make the update a no-op and hide a typo."""
    current = DEFAULT_TRANSLATIONS[key][language]
    assert current not in SUPERSEDED_DEFAULTS[key][language]
    # Same placeholders: replacing the stored text must not change what the caller fills in.
    names = lambda text: sorted(re.findall(r'\{(\w+)', text))
    for old in SUPERSEDED_DEFAULTS[key][language]:
        assert names(old) == names(current)


def test_start_up_replaces_the_old_defaults_on_a_copy_of_the_committed_database(tmp_path):
    """Positive: a copy of the committed database holding the former texts gets the new ones."""
    copy = tmp_path / 'profiles.sqlite3'
    shutil.copyfile(COMMITTED_DB, copy)
    ProfileStore(copy)  # bring the copy to the current schema first
    _write(copy, [(key, language, SUPERSEDED_DEFAULTS[key][language][0]) for key, language in CORRECTED])
    ProfileStore(copy)
    for key, language in CORRECTED:
        assert _stored(copy, key, language) == DEFAULT_TRANSLATIONS[key][language], (key, language)


def test_start_up_keeps_a_text_the_user_edited(tmp_path):
    """Negative twin: a stored text that is not exactly a former default is the user's and stays."""
    copy = tmp_path / 'profiles.sqlite3'
    shutil.copyfile(COMMITTED_DB, copy)
    ProfileStore(copy)
    edited = {(key, language): SUPERSEDED_DEFAULTS[key][language][0] + ' (mio)'
              for key, language in CORRECTED}
    _write(copy, [(key, language, text) for (key, language), text in edited.items()])
    ProfileStore(copy)
    for (key, language), text in edited.items():
        assert _stored(copy, key, language) == text, (key, language)


def test_the_other_languages_of_a_corrected_key_are_left_alone(tmp_path):
    """Only the (key, language) pairs listed move: an Italian text edited by the user stays."""
    path = tmp_path / 'fresh.sqlite3'
    ProfileStore(path)
    key = 'ui:Premi «Applica» nella scheda Simbolo elettrico per salvarle, poi ricontrolla.'
    _write(path, [(key, 'it', 'Testo mio'), (key, 'de', SUPERSEDED_DEFAULTS[key]['de'][0])])
    ProfileStore(path)
    assert _stored(path, key, 'it') == 'Testo mio'
    assert _stored(path, key, 'de') == DEFAULT_TRANSLATIONS[key]['de']


@pytest.mark.parametrize('language', ['en', 'fr', 'de'])
def test_a_message_naming_the_symbol_tab_uses_that_tab_name(language):
    """Every message whose Italian source names the «Simbolo elettrico» tab names it, in each
    language, as the tab itself is labelled (`tab.symbol.name`); the Italian name or another
    wording («Elektrisches Symbol») is refused.
    """
    tab = DEFAULT_TRANSLATIONS['tab.symbol.name'][language]
    offending = [key for key, values in DEFAULT_TRANSLATIONS.items()
                 if re.search(r'scheda «?Simbolo elettrico', values.get('it', ''))
                 and language in values and tab.casefold() not in values[language].casefold()]
    assert offending == []
    leftover = [key for key, values in DEFAULT_TRANSLATIONS.items()
                if re.search(r'«\s?Simbolo elettrico\s?»', values.get(language, ''))]
    assert leftover == []


def test_no_german_message_calls_the_symbol_tab_elektrisches_symbol():
    """German names the tab «Schaltplansymbol» (tab.symbol.name['de'])."""
    assert DEFAULT_TRANSLATIONS['tab.symbol.name']['de'] == 'Schaltplansymbol'
    wrong = [key for key, values in DEFAULT_TRANSLATIONS.items()
             if re.search(r'(Tab|Registerkarte|Reiter)\s+„?Elektrisches Symbol', values.get('de', ''))]
    assert wrong == []
