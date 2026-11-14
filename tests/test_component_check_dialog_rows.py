"""@brief The redesigned «Verifica componente» dialog: one row per thing to do (2026-11-14).

@details User-approved redesign: under the verdict, «Per l'uso finale mancano
N cose · M controlli superati» counts ROWS; each row has an icon, a short
sentence and only its own buttons; rows are ordered errors, importance 2,
importance 3, not run, manual; the checks that need kicad-cli fold into one
«KiCad non trovato» row when it is missing; the first button of the first
error (else importance-2) row is highlighted; a successful FIX re-runs the
check by itself, a VIEW and a failure do not, a HAND-OFF closes the dialog;
«Interrompi» is shown only while the checks run; the technical details list
each check once. The verification is faked. Positive and negative cases.
"""

from kicadgen.gui import component_check_dialog as ccd
from kicadgen.services import component_check as check
from kicadgen.services.component_check import Check
from tests.test_component_check_document import _dialog, _release_dialogs, _wait_finished  # noqa: F401

_ERROR = Check('ERROR', 'Geometria del footprint', 'Pad 1 e pad 2: il rame si sovrappone.', 'technical', 1,
               'Sposta uno dei due pad.', None, 'footprint_pads_overlap', ('1', '2'))
_DRC = Check('WARN', 'DRC del footprint in KiCad', 'Anello troppo sottile: pad 3.\nKiCad: x', 'technical', 2,
             'Guarda il punto.', check.ACTION_SHOW_IN_KICAD, 'drc_annular_width', ('3',))
_FINAL = Check('WARN', 'Solidi 3D', 'Presenza di solidi non confermata.')
_SKIP = Check('SKIP', 'Corrispondenza pin-pad', 'Footprint non leggibile.')
_MANUAL = Check('WARN', 'Verifiche manuali', 'ERC del tuo schema e DRC della tua scheda in KiCad.', 'manual',
                3, '', None, 'manual_checks')
_OK = Check('OK', 'KiCad CLI', 'x')
_LIBRARY = Check('WARN', 'Libreria KiCad', 'Versione diversa.', 'technical', 2,
                 'Reinstallala dalla scheda KiCad.', check.ACTION_OPEN_KICAD_TAB, 'kicad_library')


def test_rows_are_ordered_and_counted_as_rows(qapp, monkeypatch):
    """@brief Positive: manual first in the input, still last; N counts rows, M passed checks."""
    dialog = _dialog(qapp, monkeypatch, [_MANUAL, _SKIP, _FINAL, _DRC, _ERROR, _OK, _OK])
    assert [item.rank for item in dialog.items] == [ccd.RANK_ERROR, ccd.RANK_BEFORE_PRODUCTION,
                                                    ccd.RANK_FINAL_REVIEW, ccd.RANK_NOT_RUN,
                                                    ccd.RANK_MANUAL]
    assert [item.icon for item in dialog.items][:2] == ['✖', '⚠']
    assert dialog.headline.text() == 'Per l’uso finale mancano 4 cose · 2 controlli superati'
    # The DRC row keeps its own sentence (with its pads), not the remedy.
    assert dialog.items[1].text == 'Anello troppo sottile: pad 3.'
    dialog.close()


def test_kicad_cli_skips_fold_into_one_row_only_when_kicad_is_missing(qapp, monkeypatch):
    """@brief Positive: kicad-cli missing -> its three checks are one row «KiCad non trovato: 3 …»;
    negative: a SKIP of another check stays its own row, and without the missing-CLI row nothing folds."""
    cli = [Check('SKIP', 'KiCad', 'KiCad CLI non disponibile.', code='kicad_cli', needs_cli=True),
           Check('SKIP', 'Lettura footprint in KiCad', 'Richiede KiCad CLI.', code='kicad_footprint_read',
                 needs_cli=True),
           Check('SKIP', 'DRC del footprint in KiCad', 'Richiede KiCad CLI.', code='drc', needs_cli=True),
           Check('SKIP', 'ERC del simbolo in KiCad', 'Richiede KiCad CLI.', code='erc', needs_cli=True)]
    dialog = _dialog(qapp, monkeypatch, cli + [_SKIP])
    assert [item.text for item in dialog.items] == [
        'Non eseguito: Corrispondenza pin-pad (Footprint non leggibile).',
        'KiCad non trovato: 3 controlli non eseguiti.']
    # The folded row says which checks it stands for.
    assert 'DRC del footprint in KiCad' in dialog.items[1].tooltip
    dialog.close()
    # CONTRACT CHANGED 2026-11-14 (review P6): only SKIPs structurally marked
    # «needs kicad-cli» fold; a DRC skipped for another reason (no preview)
    # stays its own row even beside the «KiCad» row.
    other = Check('SKIP', 'DRC del footprint in KiCad', 'DRC non eseguito: anteprima non creata.', code='drc')
    dialog = _dialog(qapp, monkeypatch, [cli[0], other])
    assert [item.text for item in dialog.items] == [
        'Non eseguito: DRC del footprint in KiCad (DRC non eseguito: anteprima non creata).',
        'KiCad non trovato: 0 controlli non eseguiti.']
    dialog.close()


def test_the_first_button_of_the_first_error_row_is_highlighted(qapp, monkeypatch):
    """@brief Positive: an error row with a button wins; without errors, the first importance-2 row;
    negative: only final reviews -> nothing highlighted."""
    fixed = Check('ERROR', 'Simbolo esportato', 'x', 'technical', 1, 'Riesporta.', check.ACTION_REEXPORT_SYMBOL,
                  'symbol_export_stale')
    actions = {check.ACTION_REEXPORT_SYMBOL: lambda: None, check.ACTION_SHOW_IN_KICAD: lambda: None}
    dialog = _dialog(qapp, monkeypatch, [_DRC, fixed], actions=actions)
    assert dialog.highlighted_button() is dialog.action_buttons[check.ACTION_REEXPORT_SYMBOL]
    assert not any(button.isDefault() or button.autoDefault()
                   for button in dialog.items_box.findChildren(ccd.QPushButton))
    dialog.close()
    dialog = _dialog(qapp, monkeypatch, [_FINAL, _DRC], actions=actions)
    assert dialog.highlighted_button() is dialog.action_buttons[check.ACTION_SHOW_IN_KICAD]
    dialog.close()
    # An error row without a button highlights nothing below it (review P4).
    dialog = _dialog(qapp, monkeypatch, [_ERROR, _DRC], actions=actions)
    assert dialog.highlighted_button() is None
    dialog.close()
    dialog = _dialog(qapp, monkeypatch, [_FINAL, _MANUAL], actions=actions)
    assert dialog.highlighted_button() is None
    dialog.close()


def test_a_view_runs_without_a_recheck(qapp, monkeypatch):
    """@brief Negative of the automatic re-run: «Mostra in KiCad» shows its outcome, the check is not repeated."""
    runs, shown = [], []
    dialog = _dialog(qapp, monkeypatch, [_DRC], calls=runs,
                     actions={check.ACTION_SHOW_IN_KICAD: lambda: shown.append(1) or 'Anteprima aperta.'})
    dialog.action_buttons[check.ACTION_SHOW_IN_KICAD].click()
    assert shown == [1] and len(runs) == 1 and not dialog._running
    assert dialog.action_status.text() == 'Anteprima aperta.'
    dialog.close()


def test_a_handoff_closes_the_dialog_and_names_what_to_do(qapp, monkeypatch):
    """@brief Positive: «Vai alla scheda KiCad» and «Suggerisci con AI» close with their hand-off;
    no callable is needed for a hand-off."""
    dialog = _dialog(qapp, monkeypatch, [_LIBRARY])
    dialog.action_buttons[check.ACTION_OPEN_KICAD_TAB].click()
    assert dialog.handoff == check.ACTION_OPEN_KICAD_TAB and not dialog.ai_review_requested
    assert dialog.result() == dialog.DialogCode.Accepted
    types = [Check('WARN', 'Revisione simbolo', 'Tipo elettrico da verificare: 1', 'final', 3, '', None,
                   'electrical_type', ('1',))]
    dialog = _dialog(qapp, monkeypatch, types)
    dialog.action_buttons[ccd.ACTION_SUGGEST_PIN_TYPES].click()
    assert dialog.handoff == ccd.ACTION_SUGGEST_PIN_TYPES and dialog.ai_review_requested


def test_stop_is_shown_only_while_running_and_details_list_each_check_once(qapp, monkeypatch):
    """@brief Positive: finished -> «Interrompi» hidden, «Ripeti la verifica» enabled; the details
    hold each check once and no verdict line."""
    dialog = _dialog(qapp, monkeypatch, [_ERROR, _DRC, _OK])
    assert not dialog.stop_button.isVisibleTo(dialog) and dialog.rerun_button.isEnabled()
    report = dialog.report.toPlainText()
    assert report.count('Geometria del footprint') == 1 and report.count('DRC del footprint') == 1
    assert 'NON USARE' not in report
    dialog.rerun_button.click()
    assert dialog.stop_button.isVisibleTo(dialog) and not dialog.rerun_button.isEnabled()
    _wait_finished(qapp, dialog)
    assert not dialog.stop_button.isVisibleTo(dialog)
    dialog.close()


def test_the_headline_singular_and_zero_forms(qapp, monkeypatch):
    """@brief «manca 1 cosa · 1 controllo superato» and «non manca nulla · nessun controllo superato»."""
    dialog = _dialog(qapp, monkeypatch, [_FINAL, _OK])
    assert dialog.headline.text() == 'Per l’uso finale manca 1 cosa · 1 controllo superato'
    dialog.close()
    dialog = _dialog(qapp, monkeypatch, [_MANUAL])
    assert dialog.headline.text() == 'Per l’uso finale non manca nulla · nessun controllo superato'
    dialog.close()

# ── Review of the dialog (2026-11-14) ────────────────────────────────────

def test_many_rows_scroll_and_text_overlaps_fold_into_one_row(qapp, monkeypatch):
    """@brief P1: the rows live in a scroll area; three overlaps are ONE row with their pins and
    the group remedy in its tooltip (P8)."""
    overlaps = [Check('WARN', 'Revisione simbolo', f'Testi sovrapposti: {a}, {b}', 'final', 3, '', None,
                      'text_overlap', (a, b)) for a, b in (('1', '2'), ('2', '3'), ('7', '9'))]
    many = [Check('WARN', f'Avviso {n}', 'x', 'technical', 3, f'Rimedio {n}') for n in range(60)]
    dialog = _dialog(qapp, monkeypatch, overlaps + many)
    assert dialog.items_scroll.widget() is dialog.items_box
    folded = [item for item in dialog.items if item.text.startswith('Testi sovrapposti nel simbolo')]
    assert len(folded) == 1 and folded[0].text == 'Testi sovrapposti nel simbolo: 5 pin (1–3, 7, 9).'
    assert 'Sposta i testi dei pin 1–3, 7, 9' in folded[0].tooltip
    assert folded[0].actions == [check.ACTION_OPEN_SYMBOL_TAB]
    dialog.close()


def test_two_stale_libraries_stay_two_rows(qapp, monkeypatch):
    """@brief P5: the project and the global library rows keep their own sentence (scope inside)."""
    rows = [Check('WARN', 'Libreria KiCad', f'Libreria {scope} non aggiornata (footprint): x', 'technical', 2,
                  'Reinstallala dalla scheda KiCad.', check.ACTION_OPEN_KICAD_TAB, 'kicad_library')
            for scope in ('del progetto', 'globale')]
    dialog = _dialog(qapp, monkeypatch, rows)
    assert [item.text for item in dialog.items] == [
        'Libreria del progetto non aggiornata (footprint): x', 'Libreria globale non aggiornata (footprint): x']
    dialog.close()


def test_the_level_is_named_for_screen_readers_and_in_the_copy(qapp, monkeypatch):
    """@brief P9: the icon has its level as accessible name and tooltip; the copy prefixes it."""
    dialog = _dialog(qapp, monkeypatch, [_ERROR, _MANUAL])
    # CONTRACT CHANGED 2026-11-14 (practical test 7): the manual row has its own
    # checklist icon and the level «Da fare in KiCad».
    icons = [label for label in dialog.items_box.findChildren(ccd.QLabel) if label.text() in ('✖', '⚠', '☐')]
    assert [icon.accessibleName() for icon in icons] == ['Errore', 'Da fare in KiCad']
    assert icons[0].toolTip() == 'Errore'
    copied = dialog._copy_text()
    assert '✖ [Errore] Sposta uno dei due pad (pin 1–2)' in copied
    assert '☐ [Da fare in KiCad]' in copied
    dialog.close()



# ── Practical test (2026-11-14) ───────────────────────────────────────────

def test_the_verdict_is_explained_and_what_passed_is_shown(qapp, monkeypatch):
    """@brief (8): one line under the verdict; one ✔ row naming the key checks that passed, not
    counted as missing; negative: a code with one failed check is not named as passed."""
    passed = [Check('OK', 'DRC del footprint in KiCad', 'ok', code='drc'),
              Check('OK', 'Corrispondenza pin-pad', 'ok', code='pin_pad'),
              Check('OK', 'Libreria KiCad', 'ok', code='kicad_library'),
              Check('WARN', 'Libreria KiCad', 'Libreria globale non aggiornata (footprint): x', 'technical', 2,
                    'Reinstallala.', check.ACTION_OPEN_KICAD_TAB, 'kicad_library')]
    dialog = _dialog(qapp, monkeypatch, passed + [_MANUAL])
    assert dialog.explanation.text() == 'Prima della produzione va rivisto almeno un punto qui sotto.'
    ok_row = dialog.items[-1]
    assert ok_row.rank == ccd.RANK_PASSED and ok_row.icon == '✓'
    assert ok_row.text == 'pin-pad, DRC: nessun problema.'
    assert dialog.headline.text() == 'Per l’uso finale manca 1 cosa · 3 controlli superati'
    dialog.close()


def test_the_details_list_problems_first(qapp, monkeypatch):
    """@brief (9): problems before the passed checks in the technical details."""
    dialog = _dialog(qapp, monkeypatch, [_OK, _FINAL, _OK, _ERROR])
    report = dialog.report.toPlainText()
    assert report.index('Geometria del footprint') < report.index('KiCad CLI')
    assert report.index('Solidi 3D') < report.index('KiCad CLI')
    dialog.close()


def test_a_failing_args_builder_ends_in_one_error_row(qapp, monkeypatch):
    """@brief Review L4: the builder raises -> one ERROR row through the normal end, never stuck."""
    import time
    from kicadgen.gui import component_check_dialog as module

    def broken():
        raise OSError('project folder gone')
    dialog = module.ComponentCheckDialog(broken, tx=lambda text: text)
    deadline = time.monotonic() + 5
    while dialog._running and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not dialog._running and dialog.verdict.text() == 'NON USARE'
    assert [r.detail for r in dialog.results] == ['project folder gone']
    dialog.close()
    dialog.deleteLater()
    qapp.processEvents()


def test_a_failing_view_says_so_in_its_own_words(qapp, monkeypatch):
    """@brief Review L3: «Mostra in KiCad» failing -> «Non riuscito: …», never «Correzione non riuscita»."""
    def broken():
        raise RuntimeError('PCB Editor non rilevato')
    dialog = _dialog(qapp, monkeypatch, [_DRC], actions={check.ACTION_SHOW_IN_KICAD: broken})
    dialog.action_buttons[check.ACTION_SHOW_IN_KICAD].click()
    assert dialog.action_status.text() == 'Non riuscito: PCB Editor non rilevato'
    dialog.close()


def test_the_passed_row_never_contradicts_a_warning_of_its_family(qapp, monkeypatch):
    """@brief L6: a courtyard warning removes «geometria del footprint»; «Solidi 3D» removes
    «modello STEP» (L7: never «modello 3D»); negative: an unrelated warning removes nothing."""
    checks = [Check('OK', 'Geometria del footprint', 'ok', code='footprint_geometry'),
              Check('OK', 'Geometria del footprint', 'ok', code='lead_hole'),
              Check('OK', 'Importazione 3D in KiCad', 'ok', code='kicad_3d_import'),
              Check('OK', 'Corrispondenza pin-pad', 'ok', code='pin_pad')]
    courtyard = Check('WARN', 'Geometria del footprint', 'Courtyard.', 'technical', 2, 'Allarga.', None,
                      'footprint_courtyard')
    solids = Check('WARN', 'Solidi 3D', 'Solidi non confermati.', code='solids_unconfirmed')
    dialog = _dialog(qapp, monkeypatch, checks + [courtyard, solids])
    assert dialog.items[-1].text == 'pin-pad, terminali nei fori: nessun problema.'
    dialog.close()
    dialog = _dialog(qapp, monkeypatch, checks + [_FINAL])
    assert dialog.items[-1].text == ('pin-pad, geometria del footprint, terminali nei fori, modello STEP: '
                                     'nessun problema.')
    dialog.close()


def test_an_interrupted_run_says_so_and_claims_nothing_passed(qapp, monkeypatch):
    """@brief L5: aborted -> the headline says the run was interrupted and there is no ✓ row."""
    dialog = _dialog(qapp, monkeypatch, [_OK, Check('OK', 'DRC', 'ok', code='drc'), _FINAL])
    dialog.worker.aborted = True
    dialog._finished()
    assert dialog.headline.text() == 'Verifica interrotta: 3 controlli eseguiti.'
    assert all(item.rank != ccd.RANK_PASSED for item in dialog.items)
    dialog.close()
