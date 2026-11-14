"""@brief Background component verification: a verdict and one row per thing to do."""
import threading
from dataclasses import dataclass, field
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt, QSize
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
                             QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QVBoxLayout,
                             QToolButton, QHBoxLayout, QWidget)
from PyQt6 import sip
from .theme import GREEN
from ..services.component_check import (verify_component, Check, pin_ranges,
                                        IMPORTANCE_BLOCKS_USE, IMPORTANCE_BEFORE_PRODUCTION,
                                        ACTION_REEXPORT_SYMBOL, ACTION_ANSWER_CONTACT_NC,
                                        ACTION_EDIT_PINOUT, ACTION_SHOW_IN_KICAD,
                                        ACTION_OPEN_KICAD_TAB, ACTION_OPEN_SYMBOL_TAB)

#: Hand-off of the electrical types: the dialog closes and the window asks
#: the AI for the types (`MainWindow._suggest_pin_types`).
ACTION_SUGGEST_PIN_TYPES = 'suggest_pin_types'

#: Italian source of the button label of each action (`Check.action` and
#: `CODE_ACTIONS`). An action id this table does not name gets «Correggi».
ACTION_LABELS = {
    ACTION_REEXPORT_SYMBOL: 'Riesporta il simbolo',
    ACTION_ANSWER_CONTACT_NC: 'Rispondi alla domanda',
    ACTION_EDIT_PINOUT: 'Apri la tabella Pinout',
    ACTION_SHOW_IN_KICAD: 'Mostra in KiCad',
    ACTION_OPEN_KICAD_TAB: 'Vai alla scheda KiCad',
    ACTION_OPEN_SYMBOL_TAB: 'Rivedi nella scheda Simbolo',
    ACTION_SUGGEST_PIN_TYPES: 'Suggerisci con AI',
}
#: Actions that change the component: a success runs the check again by itself.
FIX_ACTIONS = frozenset({ACTION_REEXPORT_SYMBOL, ACTION_ANSWER_CONTACT_NC, ACTION_EDIT_PINOUT})
#: Actions that only show something: no re-check.
VIEW_ACTIONS = frozenset({ACTION_SHOW_IN_KICAD})
#: Actions done by the window after the dialog closes (`handoff`).
HANDOFF_ACTIONS = frozenset({ACTION_SUGGEST_PIN_TYPES, ACTION_OPEN_SYMBOL_TAB, ACTION_OPEN_KICAD_TAB})
#: Buttons of a row by check code, for the checks that name no action:
#: the electrical types are suggested by the AI or reviewed in the Symbol tab
#: (the Pinout table does not edit them).
CODE_ACTIONS = {
    'electrical_type': (ACTION_SUGGEST_PIN_TYPES, ACTION_OPEN_SYMBOL_TAB),
    'missing_name': (ACTION_OPEN_SYMBOL_TAB,),
    'text_overlap': (ACTION_OPEN_SYMBOL_TAB,),
    'contact_functions': (ACTION_OPEN_SYMBOL_TAB,),
    'pin_role_contradiction': (ACTION_OPEN_SYMBOL_TAB,),
}

#: Per-pin checks merged into one line with their pins as ranges: code ->
#: (Italian line template, Italian remedy template or None). Both take
#: `{count}` and `{pins}`.
PIN_GROUPS = {
    'electrical_type': ('Tipi elettrici da revisionare: {count} pin ({pins}).',
                        'Conferma i tipi elettrici dei pin {pins} consultando il datasheet.'),
    'missing_name': ('Nomi mancanti: {count} pin ({pins}).', None),
    'text_overlap': ('Testi sovrapposti nel simbolo: {count} pin ({pins}).',
                     'Sposta i testi dei pin {pins} nella scheda Simbolo.'),
}

#: Longest wait, at application exit, for the verification to leave after a
#: stop request, in milliseconds. `verify_component` has no cancellation
#: channel: the stop is seen only between two KiCad commands, and one command
#: may run for its whole 60 s limit. Same bound and reasoning as
#: photo_review_dialog.MATCH_QUIT_WAIT_MS: a quit must never look like a hang.
CHECK_QUIT_WAIT_MS = 5000


class _CheckAborted(BaseException):
    """Raised from the emit hook to leave `verify_component` after an «Interrompi».

    A BaseException on purpose: the verification catches `Exception` around
    single checks to report them, and a stop request must not be reported as
    a failed check and then ignored.
    """


class _CheckWorker(QThread):
    """Run `verify_component` off the GUI thread and stream each check back.

    @note Ownership (review 2026-10-27, C7): the thread belongs to
        QApplication, not to the dialog, exactly like
        `model_dialog._ModelsWorker`. A QThread destroyed while running
        aborts the whole process, and a dialog can be destroyed without
        passing through `closeEvent` (a parent window deleted, an
        application quit): with QApplication as the owner the worker
        outlives the dialog, deletes itself when finished, and the
        application exit waits for it instead of destroying it.
    """

    result = pyqtSignal(object)

    def __init__(self, args, parent=None, error=None):
        """@brief Prepare one verification run owned by QApplication.

        @param error An exception raised while building `args`: the run emits
            it as one ERROR row and ends normally (the dialog never stays stuck).

        @param args Keyword arguments of `verify_component`.
        @param parent Ignored for ownership: QApplication.instance() is the
            owner when there is one, so that no dialog can destroy the
            running thread (see the class note). Kept for signature
            compatibility.
        """
        application = QApplication.instance()
        super().__init__(application if application is not None else parent)
        self.args = args
        self.error = error
        #: Set by «Interrompi» (and at application exit): no further KiCad
        #: command is started once the running one has returned.
        self.cancel = threading.Event()
        #: True when the run ended because of `cancel`, not by completion.
        self.aborted = False
        if application is not None:
            # Quitting while a KiCad command is in flight would destroy the
            # running thread: stop it after the current command and wait at
            # most CHECK_QUIT_WAIT_MS (see `_stop_for_quit`).
            application.aboutToQuit.connect(self._stop_for_quit)
        self.finished.connect(self._release)

    def stop(self):
        """@brief Ask the verification to end after the KiCad command in flight.

        @note `verify_component` offers no cancellation channel, so the stop
            is checked each time a check result is emitted, i.e. between two
            commands; the command already running is not killed (it still
            ends within its 60 s limit).
        """
        self.cancel.set()

    def _stop_for_quit(self):
        """Stop after the current command, waiting a BOUNDED time for the thread.

        A thread still inside a KiCad command when the wait expires is taken
        away from QApplication and handed to C++ with no owner: destroyed with
        the application while running, it would abort the process (Qt's
        «Destroyed while thread is still running»). The process exit ends it.
        """
        self.stop()
        if self.wait(CHECK_QUIT_WAIT_MS):
            return
        self.setParent(None)
        sip.transferto(self, None)

    def _emit(self, result):
        """Forward one check, then leave the verification if a stop was asked."""
        self.result.emit(result)
        if self.cancel.is_set():
            raise _CheckAborted()

    def run(self):
        """@brief QThread override: run every check, emitting each result as it completes.

        @note Any failure of the verification itself is reported as one ERROR
            check so that the dialog always reaches its verdict. A stop asked
            with `stop()` ends the run at the next emitted check and sets
            `aborted`; the checks already emitted stay.
        """
        try:
            if self.cancel.is_set():
                raise _CheckAborted()
            if self.error is not None:
                self.result.emit(Check('ERROR', 'Verifica interrotta', str(self.error)))
                return
            verify_component(**self.args, emit=self._emit)
        except _CheckAborted:
            self.aborted = True
        except Exception as exc:
            self.result.emit(Check('ERROR', 'Verifica interrotta', str(exc)))

    def _release(self):
        """Drop the exit-time wait and delete the thread once finished.

        Left connected, at exit Qt would call `wait` on an object already
        removed by `deleteLater` (same sequence as `model_dialog._ModelsWorker`).
        """
        application = QApplication.instance()
        if application is not None:
            try:
                application.aboutToQuit.disconnect(self._stop_for_quit)
            except TypeError:
                # Never connected or already disconnected: nothing to undo.
                pass
        self.deleteLater()


#: Rank of an item in the dialog, in display order.
RANK_ERROR, RANK_BEFORE_PRODUCTION, RANK_FINAL_REVIEW, RANK_NOT_RUN, RANK_MANUAL, RANK_PASSED = range(6)
#: Icon of each rank: a problem ✖/⚠, the checks to do by hand in KiCad a
#: checklist box, the passed checks a tick (practical test 2026-11-14: the
#: manual row looked like one more problem).
RANK_ICONS = {RANK_ERROR: '✖', RANK_BEFORE_PRODUCTION: '⚠', RANK_FINAL_REVIEW: '⚠',
              RANK_NOT_RUN: '⚠', RANK_MANUAL: '☐', RANK_PASSED: '✓'}
#: Ranks that are something MISSING for the final use (counted in the headline).
MISSING_RANKS = (RANK_ERROR, RANK_BEFORE_PRODUCTION, RANK_FINAL_REVIEW, RANK_NOT_RUN)
#: Passed checks named in the ✔ row, by code, in this order (Italian sources):
#: what the user most wants to know was verified.
PASSED_LABELS = {
    'pin_pad': 'pin-pad', 'footprint_geometry': 'geometria del footprint', 'lead_hole': 'terminali nei fori',
    'drc': 'DRC', 'erc': 'ERC', 'kicad_footprint_read': 'lettura in KiCad',
    'kicad_3d_import': 'modello STEP', 'contact_functions': 'funzioni NC/NO', 'kicad_library': 'libreria KiCad',
}
#: The codes of the rows of each ✓ label's family: a label is not named as
#: passed when any of them did not pass (review 2026-11-14 L6: «geometria del
#: footprint» beside a courtyard warning, «modello STEP» beside «Solidi 3D»).
PASSED_FAMILIES = {
    'footprint_geometry': ('footprint_geometry', 'footprint_pads_overlap', 'footprint_courtyard'),
    'lead_hole': ('lead_hole', 'lead_hole_contradicted'),
    'kicad_3d_import': ('kicad_3d_import', 'step_integrity', 'step_model', 'solids_unconfirmed'),
    'drc': ('drc', 'drc_*'), 'erc': ('erc', 'erc_*'),
}
#: Italian source of the level of each rank: the icon's accessible name and
#: tooltip, and the prefix of the row in «Copia rapporto» (an icon alone is
#: not read by a screen reader nor kept by every clipboard).
RANK_LEVELS = {RANK_ERROR: 'Errore', RANK_BEFORE_PRODUCTION: 'Da rivedere',
               RANK_FINAL_REVIEW: 'Da completare per l’uso finale', RANK_NOT_RUN: 'Non eseguito',
               RANK_MANUAL: 'Da fare in KiCad', RANK_PASSED: 'Superato'}
#: One line under the verdict saying what it means (Italian sources).
VERDICT_EXPLANATIONS = {
    'NON USARE': 'Ci sono errori che impediscono l’uso: correggili e ripeti la verifica.',
    'DA RIVEDERE PRIMA DELL’USO': 'Prima della produzione va rivisto almeno un punto qui sotto.',
    'ESITO NON CONCLUSIVO': 'Alcuni controlli non sono stati eseguiti: il risultato non è completo.',
    'UTILIZZABILE CON RISERVE': 'Va bene per prove e prototipi; completa i punti qui sotto prima '
                                'dell’uso finale.',
    'PRONTO PER LE PROVE': 'I controlli automatici sono superati; restano le verifiche da fare in KiCad.',
    'PRONTO PER L’USO': 'I controlli automatici sono superati.',
}


@dataclass
class CheckItem:
    """@brief One row of the dialog: one thing to do, with its own buttons.

    @param rank `RANK_*`: errors first, then before production, final
        reviews, checks not run, the manual checks last.
    @param text The short sentence shown (remedy or title, with pin ranges).
    @param actions Action ids of its buttons, in order (only the ones the
        dialog can run are shown).
    @param results The checks the row stands for.
    @param pins Pin numbers of the row, in arrival order.
    @param tooltip Extra text for the row's tooltip (the folded checks, a
        group remedy), '' for none.
    """
    rank: int
    text: str
    actions: list = field(default_factory=list)
    results: list = field(default_factory=list)
    pins: list = field(default_factory=list)

    tooltip: str = ''

    @property
    def level(self) -> str:
        """@brief The row's level.

        @return The Italian source of `RANK_LEVELS` for its rank.
        """
        return RANK_LEVELS[self.rank]

    @property
    def icon(self) -> str:
        """@brief The row's icon.

        @return «✖» for an error, «⚠» otherwise.
        """
        return RANK_ICONS[self.rank]


class ComponentCheckDialog(QDialog):
    """@brief Run artifact checks in a worker and present them as a list of things to do.

    @note Layout (user-approved redesign, 2026-11-14): a verdict, one line
        «Per l'uso finale mancano N cose · M controlli superati» counting
        ROWS (not pins), then one row per thing to do -- icon, short
        sentence, its own buttons -- errors first, then importance 2,
        importance 3, checks not run, the manual checks last. The technical
        details are collapsed and list each check once.
    """

    def __init__(self, args, parent=None, tx=lambda s: s, actions=None):
        """@brief Build the report window and start the verification worker.

        @param args Keyword arguments of `verify_component` (directory, name,
            rows...), or a callable returning them: called for EVERY run (the
            first, «Ripeti la verifica», the re-run after a fix), so each run
            reads the window as it is then.
        @param parent Owner window.
        @param tx Translation function (Italian source -> shown text).
        @param actions Mapping {action id: callable} of the fixes and views
            the creator can run (`Check.action`). A FIX (`FIX_ACTIONS`) that
            succeeds runs the verification again by itself; a VIEW
            (`VIEW_ACTIONS`) does not. A hand-off (`HANDOFF_ACTIONS`) needs no
            callable: the dialog closes with `handoff` set and the creator
            routes it.
        @note The worker starts on the next event-loop turn, so the window is
            laid out before the first result arrives.
        """
        super().__init__(parent)
        self.tx = tx
        self.args = args
        self.actions = dict(actions or {})
        #: Action id -> the first button showing it, rebuilt at each finished run.
        self.action_buttons = {}
        #: The rows of the last finished run (`CheckItem`), in display order.
        self.items = []
        #: What the closed dialog hands to its creator (`HANDOFF_ACTIONS`), or None.
        self.handoff = None
        self.ai_review_requested = False
        self.results = []
        self.setWindowTitle(tx('Verifica componente per KiCad'))
        self.resize(820, 470)
        layout = QVBoxLayout(self)
        self.status = QLabel(tx('Verifica in corso: ogni comando KiCad ha un limite di 60 secondi.'))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        verdict_row = QHBoxLayout()
        self.verdict_icon = QLabel()
        self.verdict_icon.setFixedSize(48, 48)
        verdict_row.addWidget(self.verdict_icon)
        self.verdict = QLabel(tx('Sto verificando il componente…'))
        self.verdict.setStyleSheet('font-size: 18px; font-weight: bold;')
        verdict_row.addWidget(self.verdict)
        verdict_row.addStretch()
        layout.addLayout(verdict_row)
        #: One line saying what the verdict means (`VERDICT_EXPLANATIONS`).
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        #: «Per l'uso finale mancano N cose · M controlli superati».
        self.headline = QLabel()
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)
        self.items_box = QWidget()
        self.items_layout = QVBoxLayout(self.items_box)
        self.items_layout.setContentsMargins(0, 0, 0, 0)
        self.items_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        # The rows scroll: sixty of them must not push the buttons off screen.
        self.items_scroll = QScrollArea()
        self.items_scroll.setWidgetResizable(True)
        self.items_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.items_scroll.setWidget(self.items_box)
        # The free height goes to the rows' area (rows at its top), never
        # between the verdict, the headline and the rows.
        layout.addWidget(self.items_scroll, 1)
        self.action_status = QLabel()
        self.action_status.setWordWrap(True)
        self.action_status.hide()
        layout.addWidget(self.action_status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.details_toggle = QToolButton()
        self.details_toggle.setText(tx('Mostra dettagli tecnici'))
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.setCheckable(True)
        self.details_toggle.toggled.connect(self._toggle_details)
        layout.addWidget(self.details_toggle)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMaximumHeight(0)
        self.report.hide()
        layout.addWidget(self.report)
        buttons = QDialogButtonBox()
        self.rerun_button = QPushButton(tx('Ripeti la verifica'))
        self.rerun_button.clicked.connect(self._rerun)
        buttons.addButton(self.rerun_button, QDialogButtonBox.ButtonRole.ActionRole)
        copy = QPushButton(tx('Copia rapporto'))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._copy_text()))
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        # «Interrompi» (Stop): shown only while the checks run.
        self.stop_button = QPushButton(tx('Interrompi'))
        self.stop_button.clicked.connect(self._stop_requested)
        buttons.addButton(self.stop_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.close_button = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText(tx('Chiudi'))
        for button in (self.rerun_button, copy, self.stop_button, self.close_button):
            button.setAutoDefault(False)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._start_worker()

    def _start_worker(self):
        """Create the verification worker and start it on the next event-loop turn."""
        # Owned by QApplication, not by this window: see _CheckWorker.
        try:
            args, error = (self.args() if callable(self.args) else dict(self.args)), None
        except Exception as exc:  # noqa: BLE001 - shown as one ERROR row, the dialog still ends
            args, error = {}, exc
        self.worker = _CheckWorker(args, error=error)
        self.worker.result.connect(self._result)
        self.worker.finished.connect(self._finished)
        self._running = True
        self.rerun_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.stop_button.show()
        QTimer.singleShot(0, self.worker.start)

    def _rerun(self, *, keep_status=False):
        """Run every check again: «Ripeti la verifica», or after a successful fix.

        @param keep_status True keeps the line that says why it runs again.
        """
        if self._running:
            return
        self.results = []
        self.items = []
        self.report.setPlainText('')
        self.verdict.setText(self.tx('Sto verificando il componente…'))
        self.verdict_icon.clear()
        self.headline.setText('')
        self.explanation.setText('')
        if not keep_status:
            self.action_status.hide()
        self._clear_items()
        self.status.setText(self.tx('Verifica in corso: ogni comando KiCad ha un limite di 60 secondi.'))
        self.status.show()
        self.progress.show()
        self._start_worker()

    def _result(self, result):
        self.results.append(result)
        self.report.setPlainText(self._format_report())

    def _toggle_details(self, expanded):
        self.report.setVisible(expanded)
        self.report.setMaximumHeight(260 if expanded else 0)
        self.details_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details_toggle.setText(self.tx(
            'Nascondi dettagli tecnici' if expanded else 'Mostra dettagli tecnici'))
        self.resize(self.width(), 650 if expanded else 470)

    @staticmethod
    def _pin_ranges(values):
        """Pin numbers as compact ranges («1–4, 7, A1»): `component_check.pin_ranges`.

        Only a canonical ASCII decimal joins a range: «01» is another pin than
        «1» (KiCad compares pad numbers as text) and must not collapse into
        it, and a Unicode digit such as «²» passes `str.isdigit` but not
        `int`. Every other value is listed as written.
        """
        return pin_ranges(values)

    def _pin_groups(self):
        """Pins of the per-pin checks that are merged into one row, by code.

        @return {code: [pins in arrival order]} for the codes of `PIN_GROUPS`
            whose checks are warnings, read from `Check.code` and
            `Check.pins`, never from the translated detail.
        """
        groups = {}
        for result in self.results:
            if result.status == 'WARN' and result.code in PIN_GROUPS:
                groups.setdefault(result.code, []).extend(str(pin) for pin in result.pins)
        return groups

    def _pin_group_line(self, code, pins):
        """The merged line of one per-pin group, pins compressed into ranges."""
        template = PIN_GROUPS[code][0]
        return self.tx(template).format(count=len(set(pins)), pins=self._pin_ranges(pins))

    def _format_report(self):
        """The technical details: each check once, problems first then the passed ones, per-pin warnings merged."""
        labels = {'OK': 'Superato', 'ERROR': 'Errore', 'WARN': 'Avviso', 'SKIP': 'Non verificato'}
        lines = []
        merged = {}
        ordered = ([r for r in self.results if r.status != 'OK']
                   + [r for r in self.results if r.status == 'OK'])
        for result in ordered:
            if result.status == 'WARN' and result.code in PIN_GROUPS:
                # One line per group (below), not one per pin.
                merged.setdefault(result.code, result.title)
                continue
            line = (f'[{self.tx(labels.get(result.status, result.status))}] {self.tx(result.title)}\n'
                    f'{self.tx(result.detail)}\n')
            if result.remedy and result.status != 'OK':
                line += self.tx('Rimedio: {remedy}').format(remedy=result.remedy) + '\n'
            lines.append(line)
        groups = self._pin_groups()
        problems = sum(r.status != 'OK' for r in self.results if not (
            r.status == 'WARN' and r.code in PIN_GROUPS))
        merged_lines = [f'[{self.tx(labels["WARN"])}] {self.tx(title)}\n'
                        f'{self._pin_group_line(code, groups[code])}\n' for code, title in merged.items()]
        # The merged per-pin groups are problems: listed with the problems.
        lines[problems:problems] = merged_lines
        return '\n'.join(lines)

    def _copy_text(self):
        """«Copia rapporto»: the verdict, the rows to do, then the technical details."""
        rows = '\n'.join(f'{item.icon} [{self.tx(item.level)}] {item.text}' for item in self.items)
        return '\n'.join(part for part in (self.verdict.text(), self.headline.text(), rows,
                                           '', self.report.toPlainText()) if part is not None)

    @staticmethod
    def _blocks_use(result):
        """True for a check that forbids the use: any ERROR, or importance 1."""
        return result.status == 'ERROR' or (
            result.status == 'WARN' and result.importance == IMPORTANCE_BLOCKS_USE)

    @staticmethod
    def _before_production(result):
        """True for a warning to fix before production (importance 2)."""
        return result.status == 'WARN' and result.importance == IMPORTANCE_BEFORE_PRODUCTION

    def _rank(self, result):
        """The `RANK_*` of one non-passed check."""
        if result.category == 'manual':
            return RANK_MANUAL
        if self._blocks_use(result):
            return RANK_ERROR
        if self._before_production(result):
            return RANK_BEFORE_PRODUCTION
        if result.status == 'SKIP':
            return RANK_NOT_RUN
        return RANK_FINAL_REVIEW

    def _item_text(self, result):
        """The short sentence of one check's row (pin ranges added by `_build_items`)."""
        detail = str(self.tx(result.detail)).strip().splitlines()
        first = detail[0] if detail else ''
        if result.category == 'manual':
            return str(self.tx(result.detail)).strip()
        if result.status == 'SKIP':
            return self.tx('Non eseguito: {item} ({reason}).').format(
                item=self.tx(result.title), reason=first.rstrip('.'))
        if result.code.startswith(('drc_', 'erc_')) or result.code == 'kicad_library':
            # The detail says which pads/pins or which library (its scope):
            # rows of two different libraries must not merge into one.
            return first
        if result.remedy:
            return str(self.tx(result.remedy))
        return f'{self.tx(result.title)}: {first}' if first else str(self.tx(result.title))

    def _build_items(self):
        """@brief The rows of the finished run, in display order.

        @return [`CheckItem`]: one per thing to do. Per-pin warnings of a
            code are one row with their pins as ranges; rows with the same
            sentence are merged (a remedy shared by two checks is shown
            once); when kicad-cli is missing, every check that needed it and
            the row that says so are ONE row «KiCad non trovato: N controlli
            non eseguiti». Passed checks are no rows.
        """
        folded = []
        items, by_key = [], {}
        for result in self.results:
            if result.status == 'OK':
                continue
            if result.status == 'SKIP' and getattr(result, 'needs_cli', False):
                # Structurally marked by the check: skipped ONLY because
                # kicad-cli is missing (never recognised by its text).
                folded.append(result)
                continue
            rank = self._rank(result)
            grouped = result.status == 'WARN' and result.code in PIN_GROUPS
            text = '' if grouped else self._item_text(result)
            key = (rank, 'group:' + result.code) if grouped else (rank, text)
            item = by_key.get(key)
            if item is None:
                item = by_key[key] = CheckItem(rank, text)
                items.append(item)
            item.results.append(result)
            item.pins.extend(str(pin) for pin in result.pins if str(pin) not in item.pins)
            for action in ([result.action] if result.action else []) + list(CODE_ACTIONS.get(result.code, ())):
                if action not in item.actions:
                    item.actions.append(action)
        for (rank, key), item in by_key.items():
            if key.startswith('group:'):
                code = key[len('group:'):]
                item.text = self._pin_group_line(code, item.pins)
                remedy = PIN_GROUPS[code][1]
                if remedy:
                    item.tooltip = self.tx(remedy).format(count=len(set(item.pins)),
                                                          pins=self._pin_ranges(item.pins))
            elif item.pins and not any(r.code.startswith(('drc_', 'erc_')) for r in item.results):
                item.text = self.tx('{text} (pin {pins})').format(
                    text=item.text.rstrip('.'), pins=self._pin_ranges(item.pins))
        checks = [r for r in folded if r.code != 'kicad_cli']
        if folded:
            items.append(CheckItem(RANK_NOT_RUN, self.tx(
                'KiCad non trovato: {count} controlli non eseguiti.').format(count=len(checks)),
                results=list(folded), tooltip='\n'.join(self.tx(r.title) for r in checks)))
        # What WAS verified, in one line: a code counts only when every check
        # of it passed (one stale library makes «libreria KiCad» not passed).
        failed = {r.code for r in self.results if r.status != 'OK'}

        def family_failed(result):
            for code in PASSED_FAMILIES.get(result.code, (result.code,)):
                if code.endswith('*') and any(f.startswith(code[:-1]) for f in failed):
                    return True
                if code in failed:
                    return True
            # Families by CODE, never by title: «Geometria del footprint» titles
            # the overlap, the courtyard and the terminals alike.
            return False
        passed = [r for r in self.results if r.status == 'OK' and r.code in PASSED_LABELS
                  and not family_failed(r)]
        names = [self.tx(label) for code, label in PASSED_LABELS.items()
                 if any(r.code == code for r in passed)]
        if names:
            items.append(CheckItem(RANK_PASSED, self.tx('{checks}: nessun problema.').format(
                checks=', '.join(names)), results=passed))
        return sorted(items, key=lambda item: item.rank)

    def _headline_text(self):
        """«Per l'uso finale mancano N cose · M controlli superati», with the singular and zero forms."""
        missing = sum(item.rank in MISSING_RANKS for item in self.items)
        passed = sum(result.status == 'OK' for result in self.results)
        first = (self.tx('Per l’uso finale non manca nulla') if missing == 0 else
                 self.tx('Per l’uso finale manca 1 cosa') if missing == 1 else
                 self.tx('Per l’uso finale mancano {count} cose').format(count=missing))
        second = (self.tx('nessun controllo superato') if passed == 0 else
                  self.tx('1 controllo superato') if passed == 1 else
                  self.tx('{count} controlli superati').format(count=passed))
        return f'{first} · {second}'

    def _set_verdict(self):
        """Verdict, headline and rows of a finished run.

        @note The verdict reads only `Check.status`, `Check.category` and
            `Check.importance`, never a translated title: any ERROR (or an
            importance-1 warning) -> «NON USARE»; an importance-2 warning ->
            «DA RIVEDERE PRIMA DELL’USO»; a skipped check -> «ESITO NON
            CONCLUSIVO»; other warnings -> «UTILIZZABILE CON RISERVE», or
            «PRONTO PER LE PROVE» when the manual checks are the only ones.
        """
        counts = {status: sum(result.status == status for result in self.results)
                  for status in ('OK', 'ERROR', 'WARN', 'SKIP')}
        blocking = any(self._blocks_use(r) for r in self.results)
        review = any(self._before_production(r) for r in self.results)
        manual_only_warning = all(
            result.status != 'WARN' or result.category == 'manual' for result in self.results)
        if blocking:
            title, color = 'NON USARE', '#f38ba8'
        elif review:
            title, color = 'DA RIVEDERE PRIMA DELL’USO', '#fab387'
        elif counts['SKIP']:
            title, color = 'ESITO NON CONCLUSIVO', '#f9e2af'
        elif counts['WARN'] and not manual_only_warning:
            title, color = 'UTILIZZABILE CON RISERVE', '#f9e2af'
        elif counts['WARN']:
            title, color = 'PRONTO PER LE PROVE', '#f9e2af'
        else:
            title, color = 'PRONTO PER L’USO', '#a6e3a1'
        self.verdict.setText(self.tx(title))
        self.verdict.setStyleSheet(f'font-size: 16px; font-weight: bold; color: {color};')
        self.explanation.setText(self.tx(VERDICT_EXPLANATIONS[title]))
        icon_name = ('check-status-red.svg' if blocking else
                     'check-status-yellow.svg' if counts['WARN'] or counts['SKIP'] else
                     'check-status-green.svg')
        self.verdict_icon.setPixmap(QIcon(str(Path(__file__).parent / 'assets' / icon_name))
                                    .pixmap(QSize(44, 44)))
        self.items = self._build_items()
        self.headline.setText(self._headline_text())
        self._show_items()

    def _clear_items(self):
        """Remove the rows and buttons of the previous run."""
        while self.items_layout.count():
            entry = self.items_layout.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()
        self.action_buttons = {}

    def _action_available(self, action):
        """True for an action the dialog can run: a hand-off, or one the creator provides."""
        return action in HANDOFF_ACTIONS or callable(self.actions.get(action))

    def _show_items(self):
        """One row per item: icon, sentence (full detail as tooltip), its own buttons.

        @note The highlighted button (object name «primary») is the first
            button of the FIRST row when that row is an error, or -- without
            any error row -- an importance-2 row; a first row without a
            button highlights nothing below it. It is never the default
            button and no row button is auto-default: Enter must not
            re-export, launch KiCad or close the dialog.
        """
        self._clear_items()
        target = next((item for item in self.items if item.rank == RANK_ERROR), None) or next(
            (item for item in self.items if item.rank == RANK_BEFORE_PRODUCTION), None)
        for item in self.items:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 2, 0, 2)
            level = self.tx(item.level)
            icon = QLabel(item.icon)
            icon.setFixedWidth(18)
            icon.setAccessibleName(level)
            icon.setToolTip(level)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            line.addWidget(icon, alignment=Qt.AlignmentFlag.AlignVCenter)
            # The manual row names itself («Da fare in KiCad: …»): it is a
            # to-do in KiCad, not a problem found in the component.
            shown = f'{level}: {item.text}' if item.rank == RANK_MANUAL else item.text
            text = QLabel(shown)
            text.setWordWrap(True)
            if item.rank == RANK_PASSED:
                # The theme's OK green, explicitly: the application stylesheet
                # gives every QLabel its text colour, and the tick glyph comes from
                # a fallback font that ignores it -- the green status icon is drawn.
                text.setStyleSheet(f'QLabel {{ color: {GREEN}; }}')
                icon.setPixmap(QIcon(str(Path(__file__).parent / 'assets' / 'check-status-green.svg'))
                               .pixmap(QSize(14, 14)))
            tips = [f'{self.tx(r.title)}: {self.tx(r.detail)}'
                    + (('\n' + self.tx('Rimedio: {remedy}').format(remedy=r.remedy)) if r.remedy else '')
                    for r in item.results if not getattr(r, 'needs_cli', False)]
            if item.tooltip:
                tips.append(item.tooltip)
            text.setToolTip('\n'.join(tips))
            line.addWidget(text, 1, Qt.AlignmentFlag.AlignVCenter)
            first = True
            for action in item.actions:
                if not self._action_available(action):
                    continue
                button = QPushButton(self.tx(ACTION_LABELS.get(action, 'Correggi')))
                button.setAutoDefault(False)
                button.setDefault(False)
                button.clicked.connect(lambda _checked=False, a=action: self._run_action(a))
                if item is target and first:
                    button.setObjectName('primary')
                first = False
                line.addWidget(button, alignment=Qt.AlignmentFlag.AlignVCenter)
                self.action_buttons.setdefault(action, button)
            self.items_layout.addWidget(row)

    def highlighted_button(self):
        """@brief The highlighted button of the rows.

        @return The button named «primary» (the next thing to do), or None.
        """
        return next((button for button in self.items_box.findChildren(QPushButton)
                     if button.objectName() == 'primary'), None)

    def _run_action(self, action):
        """Run one action of a row.

        @param action Action id.
        @note A hand-off closes the dialog with `handoff` set. A view runs and
            says how it went, nothing more. A fix that succeeds runs the whole
            verification again by itself («Verifica ripetuta
            automaticamente»); a failing one is reported and nothing re-runs.
        """
        if self._running:
            return
        if action in HANDOFF_ACTIONS:
            self.handoff = action
            self.ai_review_requested = action == ACTION_SUGGEST_PIN_TYPES
            self.accept()
            return
        try:
            outcome = self.actions[action]()
        except Exception as exc:  # noqa: BLE001 - shown to the user, the dialog stays usable
            template = ('Non riuscito: {error}' if action in VIEW_ACTIONS
                        else 'Correzione non riuscita: {error}')
            self.action_status.setText(self.tx(template).format(error=exc))
            self.action_status.show()
            return
        message = outcome.strip() if isinstance(outcome, str) and outcome.strip() else ''
        if action in FIX_ACTIONS:
            message = (message + '\n' if message else '') + self.tx('Verifica ripetuta automaticamente.')
            self.action_status.setText(message)
            self.action_status.show()
            self._rerun(keep_status=True)
            return
        if message:
            self.action_status.setText(message)
            self.action_status.show()

    def _set_aborted_verdict(self):
        """Verdict of a run stopped by «Interrompi»: never a readiness claim.

        The checks emitted before the stop stay in the rows and the details;
        what was not run is said, and nothing is called ready.
        """
        self.verdict.setText(self.tx('VERIFICA INTERROTTA'))
        self.verdict.setStyleSheet('font-size: 16px; font-weight: bold; color: #f9e2af;')
        self.verdict_icon.setPixmap(
            QIcon(str(Path(__file__).parent / 'assets' / 'check-status-yellow.svg'))
            .pixmap(QSize(44, 44)))
        self.explanation.setText(self.tx(
            'Verifica interrotta su richiesta: i controlli restanti non sono stati eseguiti. '
            'Ripeti la verifica prima di usare il componente.'))
        self.headline.setText(self.tx('Verifica interrotta: {count} controlli eseguiti.').format(
            count=len(self.results)))
        # No ✓ row: a partial run cannot say what is free of problems.
        self.items = [item for item in self.items if item.rank != RANK_PASSED]
        self._show_items()

    def _stop_requested(self):
        """Ask the worker to stop after the KiCad command in flight and say so."""
        if not self._running:
            return
        self.worker.stop()
        self.stop_button.setEnabled(False)
        self.status.setText(self.tx(
            'Interruzione richiesta: termina il comando KiCad in corso (al massimo 60 '
            'secondi), poi la verifica si ferma.'))

    def _finished(self):
        self._running = False
        self.stop_button.setEnabled(False)
        self.stop_button.hide()
        self.progress.hide()
        self._set_verdict()
        if self.worker.aborted:
            self._set_aborted_verdict()
        self.report.setPlainText(self._format_report())
        self.status.hide()
        if not self.details_toggle.isChecked():
            self.resize(self.width(), 470)
        self.close_button.setEnabled(True)
        self.rerun_button.setEnabled(True)

    def reject(self):
        """@brief Allow dismissal only after the verification worker finishes."""
        if not self._running:
            super().reject()

    def closeEvent(self, event):
        """@brief Keep the dialog alive while its verification thread runs.

        @param event Qt close event; ignored while the worker is running.
        """
        if self._running:
            event.ignore()
        else:
            super().closeEvent(event)
