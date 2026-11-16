"""@brief Check texts recomposed in the language active when they are SHOWN (review 2026-11-15, M3).

@details The check translates its titles, details and remedies through
`messages.tx` while it runs; the panel shows them later, possibly after a
language switch. Each `Check` keeps the recipe of its translated texts
(`Check.texts`: Italian template and values) and
`component_check.localized(check)` renders them again in the current
language. Texts that did not come from `tx` (kicad-cli's own log) stay as
they are. A fake translator tags every message with its language, so the
test does not depend on the catalog. Positive and negative twins.
"""

import copy
import pickle
import subprocess
from pathlib import Path

import pytest

from kicadgen.services import component_check as check
from kicadgen.services.messages import set_translator
from test_kicad_library import _artifacts


def _language(tag):
    """A translator that prefixes every catalog text with «[tag] », placeholders kept."""
    set_translator(lambda key, default=None: f'[{tag}] {default}')


@pytest.fixture(autouse=True)
def _no_translator_after():
    """Back to the Italian sources whatever the test did."""
    yield
    set_translator(None)


def test_a_message_with_values_is_recomposed_in_the_new_language():
    """@brief Positive: produced in EN, shown in FR -> FR text with the same values; the nested
    translated value follows the language too."""
    _language('EN')
    row = check.Check('WARN', check.tx('Libreria KiCad'),
                      check.tx('Libreria {scope} aggiornata: {path}', scope=check.tx('del progetto'),
                               path='C:/lib/Demo.kicad_mod'),
                      remedy=check.tx('Installalo dalla scheda KiCad quando è pronto.'))
    assert row.detail == '[EN] Libreria [EN] del progetto aggiornata: C:/lib/Demo.kicad_mod'
    assert set(row.texts) == {'title', 'detail', 'remedy'}
    template, values = row.texts['detail'][1], dict(row.texts['detail'][2])
    assert template == 'Libreria {scope} aggiornata: {path}' and values['path'] == 'C:/lib/Demo.kicad_mod'
    _language('FR')
    shown = check.localized(row)
    assert shown.title == '[FR] Libreria KiCad'
    assert shown.detail == '[FR] Libreria [FR] del progetto aggiornata: C:/lib/Demo.kicad_mod'
    assert shown.remedy.startswith('[FR] Installalo')
    assert (shown.status, shown.code, shown.importance) == (row.status, row.code, row.importance)
    # Shown again after another switch: the recipe travels with the copy.
    _language('DE')
    assert check.localized(shown).detail.startswith('[DE] Libreria [DE] del progetto')


def test_a_raw_kicad_log_detail_stays_as_written(tmp_path, monkeypatch):
    """@brief Negative: a read whose detail is kicad-cli's own log keeps it word for word while its
    title follows the language."""
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: 'kicad-cli')
    monkeypatch.setattr(check, 'check_kicad_version',
                        lambda cli: type('V', (), {'supported': True, 'newer_cli': None})())

    def run(command, **_kwargs):
        args = [str(a) for a in command[1:]]
        output = Path(args[args.index('-o') + 1])
        if args[:2] == ['pcb', 'drc']:
            output.write_text('{"violations": []}', encoding='utf-8')
        elif args[:2] == ['sch', 'erc']:
            output.write_text('{"sheets": []}', encoding='utf-8')
        elif args[:2] == ['pcb', 'export']:
            output.write_text('MANIFOLD_SOLID_BREP(', encoding='utf-8')
        else:
            output.mkdir(parents=True, exist_ok=True)
            (output / 'x.svg').write_text('<svg/>', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0, 'Warning: KiCad log line', '')
    monkeypatch.setattr(check, '_run_cli', run)
    _language('EN')
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    (read,) = [r for r in results if r.code == 'kicad_footprint_read']
    assert read.detail == 'Warning: KiCad log line' and 'detail' not in (read.texts or {})
    _language('FR')
    shown = check.localized(read)
    assert shown.detail == 'Warning: KiCad log line'
    assert shown.title == '[FR] Lettura footprint in KiCad'


def test_concatenated_details_keep_both_parts_and_kicad_descriptions(monkeypatch):
    """@brief Positive: a DRC row (sentence + «KiCad: …» + rules note, joined with ``+``) is
    recomposed part by part; KiCad's own description inside stays as KiCad wrote it."""
    from types import SimpleNamespace
    pad = SimpleNamespace(number='1', pad_type='thru_hole', shape='circle', at=(0.0, 0.0), rotation=0.0,
                          size=(1.0, 1.0), layers=['*.Cu'], rect_delta=None, roundrect_rratio=None,
                          drill_offset=(0.0, 0.0), hole=(0.5, 0.5), index=0, uuid='u1',
                          board_xy=(100.0, 100.0))
    report = {'violations': [{'type': 'annular_width', 'severity': 'error',
                              'description': 'Annular width (KiCad words)',
                              'items': [{'uuid': 'u1', 'pos': {'x': 0, 'y': 0}}]}]}
    _language('EN')
    (row,) = check.drc_checks(report, [pad])
    assert row.detail.startswith('[EN] ')
    _language('FR')
    lines = check.localized(row).detail.split('\n')
    assert lines[0].startswith('[FR] ') and '[FR] Anello' in lines[0] and 'pad 1' in lines[0]
    assert lines[1] == '[FR] KiCad: Annular width (KiCad words)'
    assert lines[2].startswith('[FR] Regole predefinite')


def test_a_check_without_translated_texts_is_returned_unchanged():
    """@brief Negative: plain texts (no recipe) -> the same object, no texts recorded."""
    row = check.Check('OK', 'plain title', 'plain detail')
    assert row.texts is None and check.localized(row) is row


def test_the_recipe_does_not_change_equality_and_survives_copies():
    """@brief Two checks with the same texts stay equal and hash alike, whatever their recipe;
    deepcopy and pickle keep the recipe (the panel may copy rows)."""
    translated = check.Check('WARN', check.tx('Pad stimati'), 'x')
    plain = check.Check('WARN', 'Pad stimati', 'x')
    assert translated == plain and hash(translated) == hash(plain)
    for twin in (copy.deepcopy(translated), pickle.loads(pickle.dumps(translated))):
        assert twin == translated and twin.texts == translated.texts


def test_an_exception_detail_raised_with_a_tx_message_is_recomposed(tmp_path, monkeypatch):
    """@brief Positive: «Nessun pad elettrico trovato.» raised as ValueError(tx(...)) keeps its
    recipe through `add`; negative twin: the OS's own error text has none."""
    art = _artifacts(tmp_path)
    text = art.footprint_path.read_text(encoding='utf-8')
    art.footprint_path.write_text(text.replace('smd rect', 'np_thru_hole circle').replace(
        '(layers', '(drill 0.5) (layers', 1), encoding='utf-8')
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    _language('EN')
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    (error,) = [r for r in results if r.title == '[EN] Footprint su disco' and r.status == 'ERROR']
    assert error.detail == '[EN] Nessun pad elettrico trovato.'
    _language('FR')
    assert check.localized(error).detail == '[FR] Nessun pad elettrico trovato.'
