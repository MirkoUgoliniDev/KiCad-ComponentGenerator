"""@brief Check panel of the KiCad tab: language switch, guard line, internal failures (review 2026-11-15, fix C6).

@details
- M3: every check the panel shows or copies is recomposed at DISPLAY time
  through `component_check.localized`, so rows, tooltips and the report follow
  a language switch; a raw log carried by a check is shown as written.
- L5: the line under the rows (the window's wait, an action's outcome) is
  composed again on a language switch, not captured once.
- M2/I3: an internal failure of the verification is `check_failed` only when
  it emitted nothing; after some checks it is `check_crashed`, the checks
  already emitted stay.
- `highlighted_button` ignores the rows only scheduled for deletion.

The REAL `component_check.localized` is used, with a faked catalog behind
`messages.tx` (the translator the application registers) and the same table
as the panel's `tx`; a spy counts the calls. Positive and negative cases each.
"""
import time

import pytest
from PyQt6.QtWidgets import QLabel, QPushButton

from kicadgen.services import component_check as check
from kicadgen.services import messages
from kicadgen.services.component_check import Check
from tests.test_component_check_document import _CREATED, _dialog, _release_dialogs  # noqa: F401

#: Translation tables of the fake: Italian source -> text, per language.
_TABLES = {
    'en': {
        'Solidi 3D': '3D solids', 'Presenza di solidi non confermata.': 'Solids not confirmed.',
        'Ricontrolla il modello STEP.': 'Check the STEP model again.',
        'Rimedio: {remedy}': 'Remedy: {remedy}', 'Avviso': 'Warning',
        'KiCad CLI': 'KiCad CLI',
        'Attendi la fine del lavoro in corso.': 'Wait for the current work to end.',
        'Anteprima aperta.': 'Preview opened.',
        'Controllo del componente': 'Component check', 'Usa «Controlla di nuovo».': 'Use «Check again».',
        'Non eseguito: {item} ({reason}).': 'Not run: {item} ({reason}).',
        'Il controllo si è fermato per un errore interno «{error}»; controlli già eseguiti: {count}.':
            'The check stopped on an internal error «{error}»; checks already run: {count}.',
    },
    'fr': {
        'Solidi 3D': 'Solides 3D', 'Presenza di solidi non confermata.': 'Solides non confirmés.',
        'Ricontrolla il modello STEP.': 'Vérifiez à nouveau le modèle STEP.',
        'Rimedio: {remedy}': 'Remède : {remedy}', 'Avviso': 'Avertissement',
        'KiCad CLI': 'KiCad CLI',
        'Attendi la fine del lavoro in corso.': 'Attendez la fin du travail en cours.',
        'Anteprima aperta.': 'Aperçu ouvert.',
        'Controllo del componente': 'Contrôle du composant',
        'Usa «Controlla di nuovo».': 'Utilisez « Contrôler à nouveau ».',
        'Non eseguito: {item} ({reason}).': 'Non exécuté : {item} ({reason}).',
        'Il controllo si è fermato per un errore interno «{error}»; controlli già eseguiti: {count}.':
            'Le contrôle s’est arrêté sur une erreur interne « {error} » ; contrôles déjà exécutés : {count}.',
    },
}
#: The raw kicad-cli log line a check carries: never translated, by anyone.
_RAW_LOG = 'kicad-cli: Error loading footprint (line 12)'


class _Language:
    """The fake interface language: the panel's `tx` and the catalog of `messages.tx` read it."""

    def __init__(self, code):
        self.code = code
        #: Every check `localized` was asked for, in order.
        self.localized_calls = []

    def tx(self, source):
        """Panel translation: Italian source -> text in the current language."""
        return _TABLES[self.code].get(source, source)

    def raw(self, key, default=None):
        """Stand-in of `I18n.raw`, the translator `messages.tx` uses («ui:» + Italian source)."""
        return _TABLES[self.code].get(key[len('ui:'):], default)


@pytest.fixture
def language(monkeypatch):
    """The fake language, starting in English: catalog registered, the real `localized` spied on."""
    current = _Language('en')
    monkeypatch.setattr(messages, '_translator', current.raw)
    real = check.localized

    def spy(result):
        current.localized_calls.append(result)
        return real(result)
    monkeypatch.setattr(check, 'localized', spy)
    return current


def _en_checks(language):
    """Checks as a run in ENGLISH makes them: texts translated through `component_check.tx` then."""
    assert language.code == 'en'
    return [Check('WARN', check.tx('Solidi 3D'), check.tx('Presenza di solidi non confermata.'),
                  'technical', 3, check.tx('Ricontrolla il modello STEP.'), None, 'solids_unconfirmed'),
            Check('WARN', 'KiCad CLI', _RAW_LOG, 'technical', 3, '', None, 'kicad_cli_log')]


def _row_labels(panel):
    """The sentence labels of the rows on screen now (not those pending deletion)."""
    labels = []
    for index in range(panel.items_layout.count()):
        row = panel.items_layout.itemAt(index).widget()
        labels.extend(label for label in row.findChildren(QLabel) if label.property('i18n_skip'))
    return labels


def test_rows_tooltips_and_report_follow_a_language_switch(qapp, monkeypatch, language):
    """@brief Positive: run in EN, switch to FR -> rows, tooltips and report in FR, no EN left.
    Negative: the raw log detail is the same before and after, although the panel's `tx` would
    map it (the panel never translates a check's detail itself)."""
    _TABLES['fr'][_RAW_LOG] = 'TRADUCTION INTERDITE'
    try:
        panel = _dialog(qapp, monkeypatch, _en_checks(language), tx=language.tx)
        texts = [item.text for item in panel.items]
        assert 'Check the STEP model again.' in texts
        assert '3D solids' in panel.report.toPlainText() and _RAW_LOG in panel.report.toPlainText()
        language.code = 'fr'
        calls = len(language.localized_calls)
        panel.retranslate()
        # Recomposed at display time, from the service's localized checks.
        assert len(language.localized_calls) > calls
        texts = [item.text for item in panel.items]
        assert 'Vérifiez à nouveau le modèle STEP.' in texts
        assert 'Check the STEP model again.' not in texts
        report = panel.report.toPlainText()
        assert 'Solides 3D' in report and 'Solides non confirmés.' in report
        assert 'Remède : Vérifiez à nouveau le modèle STEP.' in report
        assert '3D solids' not in report and 'Remedy:' not in report
        tips = '\n'.join(label.toolTip() for label in _row_labels(panel))
        assert 'Solides 3D: Solides non confirmés.' in tips and '3D solids' not in tips
        # Negative: the raw log is carried as written, never translated.
        assert _RAW_LOG in report and _RAW_LOG in tips and 'TRADUCTION INTERDITE' not in report
        # «Copia rapporto» copies what is on screen, in French.
        copied = panel._copy_text()
        assert 'Vérifiez à nouveau le modèle STEP.' in copied and 'Check the STEP' not in copied
    finally:
        del _TABLES['fr'][_RAW_LOG]


def test_the_wait_line_follows_the_language_and_goes_away_when_the_work_ends(qapp, monkeypatch, language):
    """@brief Positive: a row clicked while the window is busy shows the wait in EN, then in FR after
    the switch. Negative: once the work is over, a switch hides the line instead of repeating a
    wait that no longer applies."""
    busy = [True]
    shown = []
    drc = Check('WARN', 'DRC', 'Anello sottile: pad 3.', 'technical', 2, 'Guarda il punto.',
                check.ACTION_SHOW_IN_KICAD, 'drc_annular_width', ('3',))
    panel = _dialog(qapp, monkeypatch, [drc], tx=language.tx,
                    actions={check.ACTION_SHOW_IN_KICAD: lambda: shown.append(1) or ''})
    panel.action_guard = lambda: language.tx('Attendi la fine del lavoro in corso.') if busy[0] else ''
    panel.action_buttons[check.ACTION_SHOW_IN_KICAD].click()
    assert shown == [] and panel.action_status.text() == 'Wait for the current work to end.'
    language.code = 'fr'
    panel.retranslate()
    assert panel.action_status.text() == 'Attendez la fin du travail en cours.'
    assert not panel.action_status.isHidden()
    busy[0] = False
    language.code = 'en'
    panel.retranslate()
    assert panel.action_status.isHidden()


def test_an_outcome_given_as_a_callable_follows_the_language(qapp, monkeypatch, language):
    """@brief Positive: an action returning a callable outcome -> the line is composed again in FR.
    Negative: an outcome returned as plain text is shown as written (the window translated it)."""
    drc = Check('WARN', 'DRC', 'Anello sottile: pad 3.', 'technical', 2, 'Guarda il punto.',
                check.ACTION_SHOW_IN_KICAD, 'drc_annular_width', ('3',))
    panel = _dialog(qapp, monkeypatch, [drc], tx=language.tx,
                    actions={check.ACTION_SHOW_IN_KICAD: lambda: lambda: language.tx('Anteprima aperta.')})
    panel.action_buttons[check.ACTION_SHOW_IN_KICAD].click()
    assert panel.action_status.text() == 'Preview opened.'
    language.code = 'fr'
    panel.retranslate()
    assert panel.action_status.text() == 'Aperçu ouvert.'
    language.code = 'en'
    panel = _dialog(qapp, monkeypatch, [drc], tx=language.tx,
                    actions={check.ACTION_SHOW_IN_KICAD: lambda: language.tx('Anteprima aperta.')})
    panel.action_buttons[check.ACTION_SHOW_IN_KICAD].click()
    language.code = 'fr'
    panel.retranslate()
    assert panel.action_status.text() == 'Preview opened.'


def _crashing_panel(qapp, monkeypatch, language, emitted, *, args=None):
    """Run the panel on a verification that emits `emitted` and then raises; wait until finished."""
    from kicadgen.gui import component_check_dialog as ccd

    def verify(*, emit, **_):
        for item in emitted:
            emit(item)
        raise ValueError('boom happened\n  File "x.py", line 3, in verify')
    monkeypatch.setattr(ccd, 'verify_component', verify)
    panel = ccd.ComponentCheckPanel(tx=language.tx)
    _CREATED.append(panel)
    panel.run(args if args is not None else {})
    deadline = time.monotonic() + 5
    while panel._running and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not panel._running
    return panel


def test_a_crash_after_some_checks_keeps_them_and_is_not_a_failed_start(qapp, monkeypatch, language):
    """@brief Positive: two checks emitted, then an internal error -> the two rows stay and ONE
    «check_crashed» row says so with the count and the exception's type (no traceback); the
    verdict is inconclusive. It follows a language switch."""
    ok = Check('OK', 'Pin-pad', 'Ogni pin ha il suo pad.', code='pin_pad')
    solids = _en_checks(language)[0]
    panel = _crashing_panel(qapp, monkeypatch, language, [ok, solids])
    codes = [result.code for result in panel.results]
    assert codes == ['pin_pad', 'solids_unconfirmed', 'check_crashed']
    assert 'check_failed' not in codes
    assert panel.verdict_key == 'ESITO NON CONCLUSIVO' and not panel.blocking
    crashed = [item for item in panel.items if item.results[0].code == 'check_crashed']
    assert len(crashed) == 1
    assert crashed[0].text == ('Not run: Component check (The check stopped on an internal error '
                               '«ValueError: boom happened»; checks already run: 2).')
    report = panel.report.toPlainText()
    assert 'Use «Check again».' in report and 'line 3' not in report and 'File "x.py"' not in report
    language.code = 'fr'
    panel.retranslate()
    crashed = [item for item in panel.items if item.results[0].code == 'check_crashed']
    assert crashed[0].text.startswith('Non exécuté : Contrôle du composant (Le contrôle s’est arrêté')
    assert 'contrôles déjà exécutés : 2' in crashed[0].text


@pytest.mark.parametrize('broken_args', [False, True])
def test_a_failure_before_any_check_is_a_failed_start(qapp, monkeypatch, language, broken_args):
    """@brief Negative of the crash row: nothing emitted before the error (in the verification or
    while building its arguments) -> ONLY «check_failed», never «check_crashed»."""
    def args():
        raise OSError('cannot read the project')
    panel = _crashing_panel(qapp, monkeypatch, language, [], args=args if broken_args else None)
    assert [result.code for result in panel.results] == ['check_failed']
    assert panel.verdict_key == 'ESITO NON CONCLUSIVO'
    report = panel.report.toPlainText()
    assert 'Component check' in report and 'Use «Check again».' in report
    # The raw exception text is the detail, as written.
    assert ('cannot read the project' if broken_args else 'boom happened') in report


def test_the_highlighted_button_ignores_rows_pending_deletion(qapp, monkeypatch, language):
    """@brief Positive: an error row with a fix -> its button is highlighted. Negative: right after
    the rows are cleared (deleteLater pending, no event-loop turn) no stale button is returned,
    although the old button is still a child of the rows' box."""
    fixed = Check('ERROR', 'Simbolo esportato', 'x', 'technical', 1, 'Riesporta.',
                  check.ACTION_REEXPORT_SYMBOL, 'symbol_export_stale')
    panel = _dialog(qapp, monkeypatch, [fixed], tx=language.tx,
                    actions={check.ACTION_REEXPORT_SYMBOL: lambda: ''})
    button = panel.highlighted_button()
    assert button is panel.action_buttons[check.ACTION_REEXPORT_SYMBOL]
    panel.show_idle('idle')
    assert button in panel.items_box.findChildren(QPushButton)
    assert panel.highlighted_button() is None
