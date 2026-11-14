"""@brief «Verifica componente» against the document pinout, the verdict rules and the automatic fixes.

Plan docs/pinout-coherence-plan.md, items D and E (2026-11-13): checks carry
category, importance, remedy and an optional action id; the dialog's verdict
is derived from them, never from a translated title.
"""
import time

import pytest

from kicadgen.services import component_check as check
from kicadgen.services.component_check import Check, document_checks
from test_kicad_library import _artifacts


def _pinout(*names):
    """A document pinout numbered 1..n with the given names."""
    return [{'pin': str(index), 'name': name} for index, name in enumerate(names, 1)]


def _rows(*names):
    """Applied symbol rows numbered 1..n with the given names."""
    return [{'pin': str(index), 'name': name} for index, name in enumerate(names, 1)]


# --- document checks (service) ---------------------------------------------------------------

def test_a_coherent_document_gives_no_finding():
    """@brief Negative: pinout, pads, names and side labels agree -> one OK, nothing to fix."""
    document = {'pinout': _pinout('GND', '3V3', 'GPIO4'),
                'side_labels': [('1', 'G'), ('2', '3.3V'), ('3', '4')]}
    results = document_checks(document, _rows('GND', '3V3', 'GPIO4'), {'1', '2', '3'})
    assert [r.status for r in results] == ['OK']
    assert results[0].category == 'document'


def test_pinout_entries_without_a_pad_are_an_error_that_blocks_use():
    """@brief Positive: entries 4-6 name no pad of the footprint -> ERROR importance 1 with their pins."""
    document = {'pinout': _pinout('GND', '3V3', 'GPIO4', 'GPIO5', 'GPIO6', 'GPIO7')}
    results = document_checks(document, _rows('GND', '3V3', 'GPIO4'), {'1', '2', '3'})
    dropped = [r for r in results if r.code == 'document_dropped']
    assert len(dropped) == 1
    assert dropped[0].status == 'ERROR' and dropped[0].importance == 1
    assert dropped[0].category == 'document' and dropped[0].remedy
    assert dropped[0].pins == ('4', '5', '6')
    assert '4–6' in dropped[0].detail


def test_a_side_label_different_from_the_pinout_name_is_a_warning_to_verify():
    """@brief Positive: «GND» printed where the pinout says «3V3» -> WARN importance 2 on that pad.

    @note CONTRACT CHANGED 2026-11-13 (generality audit H3): the label relation is a
        heuristic on common abbreviations, so it asks to verify and never blocks use.
    """
    document = {'pinout': _pinout('3V3', 'GPIO4'), 'side_labels': [('1', 'GND'), ('2', '4')]}
    results = document_checks(document, _rows('3V3', 'GPIO4'), {'1', '2'})
    labels = [r for r in results if r.code == 'document_side_label']
    assert len(labels) == 1 and labels[0].status == 'WARN' and labels[0].importance == 2
    assert labels[0].pins == ('1',) and 'approssimata' in labels[0].remedy
    assert not any(r.status == 'ERROR' for r in results)


def test_only_structural_findings_block_use():
    """@brief Positive: a dropped entry blocks (ERROR 1); negative: a label difference alone never does."""
    dropped = document_checks({'pinout': _pinout('GND', '3V3', 'IO3'),
                               'side_labels': [('1', '5V')]}, _rows('GND', '3V3'), {'1', '2'})
    assert [r.code for r in dropped if r.status == 'ERROR' and r.importance == 1] == ['document_dropped']


def test_another_layout_gets_no_side_based_document_check():
    """@brief Negative (audit M1): a layout without sides gets no finding speaking of the sides.

    @note Positive twin: the same document on «sides» yields the dropped-entry error.
    """
    document = {'pinout': _pinout('GND', '3V3', 'IO3'), 'layout': 'package'}
    assert document_checks(document, _rows('GND', '3V3'), {'1', '2'}) == []
    document['layout'] = 'sides'
    assert any(r.code == 'document_dropped' for r in document_checks(document, _rows('GND', '3V3'),
                                                                     {'1', '2'}))


def test_an_entry_on_no_side_is_never_a_dropped_pin():
    """@brief Positive (L6): an exposed pad numbered in the pinout, declared off the sides and
    MISSING from the footprint -> WARN importance 2 (a missing electrical pad); present as a
    pad -> only a final-review note (importance 3).

    @note Negative: it is never `document_dropped` and never an ERROR.
    @note CONTRACT CHANGED 2026-11-13 (L6): the missing declared pad was importance 3.
    """
    document = {'pinout': _pinout('GND', '3V3', 'EP'), 'off_side_pins': ['3']}
    results = document_checks(document, _rows('GND', '3V3'), {'1', '2'})
    assert not any(r.code == 'document_dropped' or r.status == 'ERROR' for r in results)
    off = [r for r in results if r.code == 'document_off_side']
    assert len(off) == 1 and off[0].importance == 2 and off[0].pins == ('3',)
    present = document_checks(document, _rows('GND', '3V3', 'EP'), {'1', '2', '3'})
    note = [r for r in present if r.code == 'document_off_side_present']
    assert len(note) == 1 and note[0].importance == 3 and note[0].pins == ('3',)
    assert not any(r.code == 'document_off_side' for r in present)


def test_an_unknown_label_relation_never_yields_a_finding():
    """@brief Negative: «TX» against «GPIO43» cannot be told apart -> no contradiction."""
    document = {'pinout': _pinout('GPIO43'), 'side_labels': [('1', 'TX')]}
    results = document_checks(document, _rows('GPIO43'), {'1'})
    assert not any(r.status == 'ERROR' for r in results)


def test_an_unnamed_pin_the_document_names_is_an_error():
    """@brief Positive: pin 2 has no name, the document calls it «3V3» -> ERROR importance 1."""
    document = {'pinout': _pinout('GND', '3V3')}
    results = document_checks(document, _rows('GND', ''), {'1', '2'})
    unnamed = [r for r in results if r.code == 'document_unnamed']
    assert len(unnamed) == 1 and unnamed[0].status == 'ERROR' and unnamed[0].pins == ('2',)


def test_an_unnamed_pin_the_document_does_not_name_is_not_a_document_error():
    """@brief Negative: the document has no name for pin 2 either -> no document finding."""
    document = {'pinout': [{'pin': '1', 'name': 'GND'}, {'pin': '2', 'name': ''}]}
    results = document_checks(document, _rows('GND', ''), {'1', '2'})
    assert not any(r.code == 'document_unnamed' for r in results)


def test_keep_sides_turns_the_contradictions_into_a_user_choice_warning():
    """@brief Positive: after «keep_sides» the same contradictions are WARN importance 2 that name the choice."""
    document = {'pinout': _pinout('GND', '3V3', 'GPIO4', 'GPIO5'), 'decision': 'keep_sides',
                'side_labels': [('1', '5V')]}
    results = document_checks(document, _rows('GND', '3V3', 'GPIO4'), {'1', '2', '3'})
    flagged = [r for r in results if r.code in ('document_dropped', 'document_side_label')]
    assert len(flagged) == 2
    assert all(r.status == 'WARN' and r.importance == 2 for r in flagged)
    # The label finding is a WARN 2 anyway (H3); the choice is still named.
    assert all('Scelta dell’utente' in r.detail for r in flagged)
    assert not any(r.status == 'ERROR' for r in results)


def test_renumber_decision_keeps_the_error():
    """@brief Negative: a «renumber» decision does not excuse a remaining contradiction."""
    document = {'pinout': _pinout('GND', '3V3', 'GPIO4', 'GPIO5'), 'decision': 'renumber'}
    results = document_checks(document, _rows('GND', '3V3', 'GPIO4'), {'1', '2', '3'})
    assert any(r.status == 'ERROR' and r.code == 'document_dropped' for r in results)


def test_a_user_typed_name_that_differs_is_a_warning_before_production():
    """@brief Positive: the user typed «5V» on pin 2, which the document calls «3V3» -> WARN 2."""
    document = {'pinout': _pinout('GND', '3V3'), 'user_names': {'2'}}
    results = document_checks(document, _rows('GND', '5V'), {'1', '2'})
    typed = [r for r in results if r.code == 'document_user_name']
    assert len(typed) == 1 and typed[0].status == 'WARN' and typed[0].importance == 2
    assert typed[0].pins == ('2',)


def test_a_differing_name_not_typed_by_the_user_is_not_a_user_warning():
    """@brief Negative: same names, but pin 2 is not in `user_names` -> no user-name warning."""
    document = {'pinout': _pinout('GND', '3V3'), 'user_names': set()}
    results = document_checks(document, _rows('GND', '5V'), {'1', '2'})
    assert not any(r.code == 'document_user_name' for r in results)


def test_an_open_side_numbering_question_is_a_warning_before_production():
    """@brief Positive: the question is open -> WARN importance 2; negative: closed -> nothing."""
    document = {'pinout': _pinout('GND'), 'open_question': True}
    assert any(r.code == 'document_open_question' and r.importance == 2
               for r in document_checks(document, _rows('GND'), {'1'}))
    document['open_question'] = False
    assert not any(r.code == 'document_open_question'
                   for r in document_checks(document, _rows('GND'), {'1'}))


def test_verify_component_passes_the_document_and_ranks_its_checks(tmp_path, monkeypatch):
    """@brief Positive: `document=` adds the document checks; negative: without it none appears."""
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    rows = [{'pin': '1', 'name': 'GPIO1'}]
    document = {'pinout': [{'pin': '1', 'name': 'GPIO1'}, {'pin': '2', 'name': 'GPIO2'}]}
    results = check.verify_component(art.component_dir, 'Demo', rows, document=document)
    assert any(r.category == 'document' and r.status == 'ERROR' and r.pins == ('2',) for r in results)
    plain = check.verify_component(art.component_dir, 'Demo', rows)
    assert not any(r.category == 'document' for r in plain)
    manual = [r for r in plain if r.code == 'manual_checks']
    assert len(manual) == 1 and manual[0].category == 'manual' and manual[0].importance == 3


def test_electrical_types_are_final_reviews_with_their_pin():
    """@brief The per-pin electrical type warning is category final, importance 3, with its pin."""
    results = check.verify_component('.', 'Demo', [{'pin': '7', 'name': 'A'}])
    types = [r for r in results if r.code == 'electrical_type']
    assert len(types) == 1 and types[0].category == 'final' and types[0].importance == 3
    assert types[0].pins == ('7',)


def test_file_alignment_warnings_carry_their_action(tmp_path, monkeypatch):
    """@brief Positive: no exported symbol -> reexport_symbol (importance 2). Negative: the
    missing model reference is an informational note WITHOUT a fix, and passed checks never
    carry an action.

    @note CONTRACT CHANGED 2026-11-13 (KiCad workflow review): the «link the STEP model» fix
        is removed -- the component-folder .kicad_mod has no model node by design, a written
        ${KIPRJMOD} link only made the import check inconclusive, and a regeneration drops it.
    """
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    actions = {r.action for r in results if r.action}
    assert actions == {check.ACTION_REEXPORT_SYMBOL}
    assert not any(r.action for r in results if r.status == 'OK')
    by_action = {r.action: r for r in results if r.action}
    assert by_action[check.ACTION_REEXPORT_SYMBOL].importance == 2
    assert by_action[check.ACTION_REEXPORT_SYMBOL].category == 'technical'
    link = [r for r in results if r.code == 'model_link_missing']
    assert len(link) == 1 and link[0].importance == 3 and link[0].action is None
    assert all(r.importance == 3 for r in results
               if r.status == 'WARN' and r.category == 'technical'
               and r.action != check.ACTION_REEXPORT_SYMBOL)


def test_a_stale_exported_symbol_offers_the_reexport(tmp_path, monkeypatch):
    """@brief Positive: a .kicad_sym that differs from the applied symbol -> reexport_symbol."""
    art = _artifacts(tmp_path)
    (art.component_dir / 'Demo.kicad_sym').write_text('(kicad_symbol_lib)', encoding='utf-8')
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    stale = [r for r in results if r.code == 'symbol_export_stale']
    assert len(stale) == 1 and stale[0].action == check.ACTION_REEXPORT_SYMBOL


def test_a_linked_model_offers_no_link_action(tmp_path, monkeypatch):
    """@brief Negative: a footprint that references its model gets no «model link» note."""
    from kicadgen.services.kicad_library import attach_3d_model
    art = _artifacts(tmp_path)
    art.footprint_path.write_text(attach_3d_model(art.footprint_path.read_text(),
                                                  art.step_path.as_posix()))
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    results = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    assert not any(r.code == 'model_link_missing' for r in results)


# --- dialog verdict ------------------------------------------------------------------------------

def _dialog(qapp, monkeypatch, checks, *, tx=lambda text: text, actions=None, calls=None):
    """Run ComponentCheckDialog on a faked verification emitting `checks`; wait until finished."""
    from kicadgen.gui import component_check_dialog as ccd

    def verify(*, emit, **_):
        if calls is not None:
            calls.append(1)
        for item in checks:
            emit(item)
    monkeypatch.setattr(ccd, 'verify_component', verify)
    dialog = ccd.ComponentCheckDialog({}, tx=tx, actions=actions)
    _CREATED.append(dialog)
    deadline = time.monotonic() + 5
    while dialog._running and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not dialog._running
    return dialog


#: Dialogs made by `_dialog`, released after each test (`_release_dialogs`).
_CREATED = []


@pytest.fixture(autouse=True)
def _release_dialogs(qapp):
    """Delete every dialog a test made through `_dialog`, then let Qt process the deletions.

    A dialog kept alive until the garbage collector runs can crash a later
    test (rules/tests.md: dialogs are released after use).
    """
    yield
    while _CREATED:
        dialog = _CREATED.pop()
        try:
            dialog.deleteLater()
        except RuntimeError:
            pass  # already deleted by Qt
    qapp.processEvents()


def ccd_module():
    """The check dialog module (imported late: it needs a QApplication)."""
    from kicadgen.gui import component_check_dialog
    return component_check_dialog


def _wait_finished(qapp, dialog):
    """Wait for a re-run of the dialog to finish."""
    deadline = time.monotonic() + 5
    while dialog._running and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not dialog._running


_DOC_ERROR = Check('ERROR', 'Pinout del documento', 'Voci del pinout senza pad nel footprint: 23–43.',
                   'document', 1, 'Rispondi alla domanda sulla numerazione dei lati.', None,
                   'document_dropped', ('23',))
_MANUAL = Check('WARN', 'Verifiche manuali', 'ERC e DRC.', 'manual', 3,
                'In KiCad esegui ERC sullo schema e DRC sulla scheda prima della produzione.',
                None, 'manual_checks')
_USER_CHOICE = Check('WARN', 'Nomi dei pin', 'Nomi scritti a mano diversi.', 'document', 2,
                     'Confronta i nomi.', None, 'document_user_name', ('2',))
_TECH_WARN = Check('WARN', 'Solidi 3D', 'Presenza di solidi non confermata.')
_SKIP = Check('SKIP', 'KiCad', 'KiCad CLI non disponibile.')


def test_a_document_error_says_do_not_use_with_cause_and_remedy_first(qapp, monkeypatch):
    """@brief Positive: a document ERROR -> «NON USARE», the first line names cause and remedy."""
    dialog = _dialog(qapp, monkeypatch, [Check('OK', 'KiCad CLI', 'x'), _DOC_ERROR, _MANUAL])
    # CONTRACT CHANGED 2026-11-14 (user-approved redesign of «Verifica componente»):
    # no «Causa:»/«Rimedio:» block and no grouped text label any more; one row
    # per thing to do (`dialog.items`: rank, sentence, buttons) under the verdict.
    assert dialog.verdict.text() == 'NON USARE'
    first = dialog.items[0]
    assert first.rank == ccd_module().RANK_ERROR and first.icon == '✖'
    assert first.text == 'Rispondi alla domanda sulla numerazione dei lati (pin 23)'
    assert dialog.items[-1].rank == ccd_module().RANK_MANUAL
    assert dialog.headline.text() == 'Per l’uso finale manca 1 cosa · 1 controllo superato'
    dialog.close()


def test_a_coherent_run_is_not_do_not_use(qapp, monkeypatch):
    """@brief Negative: the same run without the document error -> «PRONTO PER LE PROVE»."""
    dialog = _dialog(qapp, monkeypatch, [Check('OK', 'KiCad CLI', 'x'), _MANUAL])
    assert dialog.verdict.text() == 'PRONTO PER LE PROVE'
    assert [item.rank for item in dialog.items] == [ccd_module().RANK_MANUAL]
    assert dialog.headline.text() == 'Per l’uso finale non manca nulla · 1 controllo superato'
    dialog.close()


def test_an_importance_two_warning_gives_the_new_verdict(qapp, monkeypatch):
    """@brief Positive: a user-choice warning -> «DA RIVEDERE PRIMA DELL’USO», even beside a SKIP."""
    dialog = _dialog(qapp, monkeypatch, [_USER_CHOICE, _SKIP, _MANUAL])
    assert dialog.verdict.text() == 'DA RIVEDERE PRIMA DELL’USO'
    module = ccd_module()
    assert [item.rank for item in dialog.items] == [module.RANK_BEFORE_PRODUCTION,
                                                    module.RANK_NOT_RUN, module.RANK_MANUAL]
    assert dialog.items[0].text == 'Confronta i nomi (pin 2)'
    dialog.close()


def test_an_importance_three_warning_keeps_usable_with_reservations(qapp, monkeypatch):
    """@brief Negative: a technical warning of importance 3 -> «UTILIZZABILE CON RISERVE», not the new verdict."""
    dialog = _dialog(qapp, monkeypatch, [_TECH_WARN, _MANUAL])
    assert dialog.verdict.text() == 'UTILIZZABILE CON RISERVE'
    assert dialog.items[0].rank == ccd_module().RANK_FINAL_REVIEW
    assert dialog.items[0].text == 'Solidi 3D: Presenza di solidi non confermata.'
    dialog.close()


def test_a_skip_still_gives_a_non_conclusive_verdict(qapp, monkeypatch):
    """@brief A skipped check without importance-2 warnings -> «ESITO NON CONCLUSIVO»."""
    dialog = _dialog(qapp, monkeypatch, [_SKIP, _TECH_WARN, _MANUAL])
    assert dialog.verdict.text() == 'ESITO NON CONCLUSIVO'
    dialog.close()


@pytest.mark.parametrize('checks, expected', [
    ([_MANUAL], 'PRONTO PER LE PROVE'),
    ([_TECH_WARN, _MANUAL], 'UTILIZZABILE CON RISERVE'),
    ([_USER_CHOICE, _MANUAL], 'DA RIVEDERE PRIMA DELL’USO'),
    ([_DOC_ERROR, _MANUAL], 'NON USARE'),
])
def test_the_verdict_does_not_depend_on_the_interface_language(qapp, monkeypatch, checks, expected):
    """@brief Every text translated (here with a prefix): the verdict is still the structural one."""
    dialog = _dialog(qapp, monkeypatch, checks, tx=lambda text: '«EN» ' + text)
    assert dialog.verdict.text() == '«EN» ' + expected
    dialog.close()


def test_per_pin_warnings_are_compressed_into_ranges(qapp, monkeypatch):
    """@brief Electrical type warnings of pins 1, 2, 3, 5 read as one line «1–3, 5»."""
    checks = [Check('WARN', 'Revisione simbolo', f'Tipo elettrico da verificare: {pin}', 'final', 3,
                    '', None, 'electrical_type', (pin,)) for pin in ('1', '2', '3', '5')]
    dialog = _dialog(qapp, monkeypatch, checks)
    # CONTRACT CHANGED 2026-11-14: one ROW for the four pins, with its own two
    # hand-off buttons (AI suggestion, Symbol tab) instead of one dialog-wide button.
    assert [item.text for item in dialog.items] == ['Tipi elettrici da revisionare: 4 pin (1–3, 5).']
    module = ccd_module()
    assert dialog.items[0].actions == [module.ACTION_SUGGEST_PIN_TYPES, check.ACTION_OPEN_SYMBOL_TAB]
    assert dialog.action_buttons[module.ACTION_SUGGEST_PIN_TYPES].text() == 'Suggerisci con AI'
    assert dialog.headline.text() == 'Per l’uso finale manca 1 cosa · nessun controllo superato'
    dialog.close()


def test_a_shared_remedy_is_shown_once(qapp, monkeypatch):
    """@brief Two document errors with the same remedy -> the remedy appears once."""
    other = Check('ERROR', 'Etichette dei lati', 'Etichetta diversa.', 'document', 1,
                  _DOC_ERROR.remedy, None, 'document_side_label', ('1',))
    dialog = _dialog(qapp, monkeypatch, [_DOC_ERROR, other])
    # CONTRACT CHANGED 2026-11-14: the two checks are ONE row (same sentence),
    # with the pins of both; the titles stay in its tooltip and the details.
    assert len(dialog.items) == 1 and dialog.items[0].pins == ['23', '1']
    assert dialog.items[0].text.count('Rispondi alla domanda') == 1
    assert 'Etichette dei lati' in dialog.report.toPlainText()
    dialog.close()


# --- automatic fixes in the dialog ------------------------------------------------------------

_LINK = Check('WARN', 'Simbolo esportato', 'Nessun .kicad_sym.', 'technical', 2,
              'Esporta il simbolo.', check.ACTION_REEXPORT_SYMBOL, 'symbol_export_missing')


def test_the_real_alignment_warnings_make_review_before_use(qapp, monkeypatch, tmp_path):
    """@brief Positive: the missing exported symbol, as `verify_component` emits it, gives
    «DA RIVEDERE PRIMA DELL’USO» and sits in «Da correggere prima della produzione».

    @note CONTRACT CHANGED 2026-11-13: the unlinked model is by design (installation and
        preview add the model node), so it sits in «Revisioni finali», never in
        «Da correggere prima della produzione».
    """
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    produced = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    aligned = [r for r in produced if r.action or r.code == 'model_link_missing']
    assert {r.action for r in aligned} == {check.ACTION_REEXPORT_SYMBOL, None}
    dialog = _dialog(qapp, monkeypatch, aligned + [_MANUAL])
    assert dialog.verdict.text() == 'DA RIVEDERE PRIMA DELL’USO'
    module = ccd_module()
    by_rank = {}
    for item in dialog.items:
        by_rank.setdefault(item.rank, []).append(item.text)
    assert any('Esporta il simbolo' in text for text in by_rank[module.RANK_BEFORE_PRODUCTION])
    assert not any('Installa la libreria' in text for text in by_rank[module.RANK_BEFORE_PRODUCTION])
    assert any('Installa la libreria' in text for text in by_rank[module.RANK_FINAL_REVIEW])
    dialog.close()


def test_a_fresh_component_with_step_and_current_symbol_is_usable_with_reservations(
        qapp, monkeypatch, tmp_path):
    """@brief Negative of the above: STEP present, symbol exported and current, model node
    absent by design -> «UTILIZZABILE CON RISERVE», never «DA RIVEDERE PRIMA DELL’USO»,
    and no fix button (CONTRACT CHANGED 2026-11-13: the link fix was removed)."""
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    rows = [{'pin': '1', 'name': 'GPIO1'}]
    first = check.verify_component(art.component_dir, 'Demo', rows)
    assert any(r.code == 'symbol_export_missing' for r in first)
    from kicadgen.generators.symbol import (export_symbol, symbol_inputs, symbol_pin_names_hidden,
                                            symbol_property_positions)
    pins, body = symbol_inputs(rows)
    export_symbol(art.component_dir / 'Demo.kicad_sym', 'Demo', pins, footprint='KiCadGen:Demo',
                  body=body, properties=symbol_property_positions(rows),
                  pin_names_hidden=symbol_pin_names_hidden(rows))
    produced = check.verify_component(art.component_dir, 'Demo', rows)
    assert not any(r.action == check.ACTION_REEXPORT_SYMBOL for r in produced)
    link = [r for r in produced if r.code == 'model_link_missing']
    assert len(link) == 1 and link[0].importance == 3 and link[0].action is None
    # Without kicad-cli (the GitHub runner) its read checks are SKIP, which alone
    # make the verdict «ESITO NON CONCLUSIVO»; with KiCad they pass. Leave them out.
    ran = [r for r in produced if r.status != 'SKIP']
    dialog = _dialog(qapp, monkeypatch, ran,
                     actions={check.ACTION_REEXPORT_SYMBOL: lambda: None})
    assert dialog.verdict.text() == 'UTILIZZABILE CON RISERVE'
    # CONTRACT CHANGED 2026-11-14: the electrical-type row has its hand-off buttons;
    # what must be absent is a FIX button (nothing to re-export).
    assert check.ACTION_REEXPORT_SYMBOL not in dialog.action_buttons
    dialog.close()


def test_another_technical_warning_keeps_usable_with_reservations(qapp, monkeypatch, tmp_path):
    """@brief Negative: a technical warning without an alignment fix («Modello STEP» missing, as
    `verify_component` emits it) keeps «UTILIZZABILE CON RISERVE»."""
    art = _artifacts(tmp_path)
    art.step_path.unlink()
    art.wrl_path.unlink()
    monkeypatch.setattr(check, 'find_kicad_cli', lambda: None)
    produced = check.verify_component(art.component_dir, 'Demo', [{'pin': '1', 'name': 'GPIO1'}])
    other = [r for r in produced if r.status == 'WARN' and r.title == 'Modello STEP']
    assert len(other) == 1 and other[0].importance == 3
    dialog = _dialog(qapp, monkeypatch, other + [_MANUAL])
    assert dialog.verdict.text() == 'UTILIZZABILE CON RISERVE'
    dialog.close()


def test_an_action_button_appears_and_calls_the_provided_callable(qapp, monkeypatch):
    """@brief Positive: the check names the action and the creator provides it -> button; a click
    calls it once and offers «Ripeti la verifica», which runs the checks again."""
    called, runs = [], []
    dialog = _dialog(qapp, monkeypatch, [_LINK, _MANUAL], calls=runs,
                     actions={check.ACTION_REEXPORT_SYMBOL: lambda: called.append(1)})
    button = dialog.action_buttons[check.ACTION_REEXPORT_SYMBOL]
    assert button.text() == 'Riesporta il simbolo'
    # The highlighted button is the first one of the first importance-2 row; it
    # is NOT the default button (review 2026-11-14: Enter must not re-export).
    assert dialog.highlighted_button() is button and not button.isDefault()
    assert not button.autoDefault()
    # CONTRACT CHANGED 2026-11-14: a successful FIX runs the check again by
    # itself and says so; «Ripeti la verifica» stays in the footer for a manual re-run.
    button.click()
    assert called == [1]
    _wait_finished(qapp, dialog)
    assert len(runs) == 2
    assert 'Verifica ripetuta automaticamente.' in dialog.action_status.text()
    dialog.rerun_button.click()
    _wait_finished(qapp, dialog)
    assert len(runs) == 3
    dialog.close()


def test_no_action_button_without_a_callable_or_without_the_check(qapp, monkeypatch):
    """@brief Negative: an action not provided by the creator, or provided but named by no
    check, shows no button."""
    dialog = _dialog(qapp, monkeypatch, [_LINK, _MANUAL])
    assert dialog.action_buttons == {}
    dialog.close()
    dialog = _dialog(qapp, monkeypatch, [_MANUAL],
                     actions={check.ACTION_REEXPORT_SYMBOL: lambda: None})
    assert dialog.action_buttons == {}
    dialog.close()


def test_a_failing_action_is_reported_and_offers_no_rerun(qapp, monkeypatch):
    """@brief A fix that raises is shown in the dialog; the button stays usable, no re-run offered."""
    def broken():
        raise OSError('disk full')
    dialog = _dialog(qapp, monkeypatch, [_LINK],
                     actions={check.ACTION_REEXPORT_SYMBOL: broken})
    button = dialog.action_buttons[check.ACTION_REEXPORT_SYMBOL]
    button.click()
    assert 'disk full' in dialog.action_status.text()
    assert button.isEnabled()
    # CONTRACT CHANGED 2026-11-14: a failing fix re-runs nothing (no automatic re-check).
    assert not dialog._running and 'Verifica ripetuta' not in dialog.action_status.text()
    dialog.close()


def test_a_label_placement_conflict_is_reviewed_before_use_and_listed_once():
    """@brief Positive: a side whose labels strongly place the names elsewhere -> WARN
    importance 2 naming its pins; negative: the same pins are not listed again as plain
    label differences, while another pad's difference still is."""
    document = {'pinout': _pinout('GND', '3V3', 'IO3', 'IO4'),
                'side_labels': [('1', '3V3'), ('2', 'GND'), ('4', '5V')],
                'label_conflicts': [{'edge': 'top', 'pins': ['1', '2']}]}
    results = document_checks(document, _rows('GND', '3V3', 'IO3', 'IO4'), {'1', '2', '3', '4'})
    placement = [r for r in results if r.code == 'document_label_placement']
    assert len(placement) == 1 and placement[0].pins == ('1', '2')
    assert placement[0].status == 'WARN' and placement[0].importance == 2
    plain = [r for r in results if r.code == 'document_side_label']
    assert all(not set(r.pins) & {'1', '2'} for r in plain)
    assert not any(r.status == 'ERROR' for r in results)
    without = document_checks(dict(document, label_conflicts=[]),
                              _rows('GND', '3V3', 'IO3', 'IO4'), {'1', '2', '3', '4'})
    assert not any(r.code == 'document_label_placement' for r in without)
