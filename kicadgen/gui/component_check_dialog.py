"""@brief Background component verification for the KiCad tab: a verdict and one row per thing to do."""
import threading
from dataclasses import dataclass, field
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt, QSize
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QLabel,
                             QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QVBoxLayout,
                             QToolButton, QHBoxLayout, QWidget)
from PyQt6 import sip
from .theme import GREEN
from ..services.component_check import (verify_component, Check, CliStopped, pin_ranges,
                                        stop_cli_commands,
                                        IMPORTANCE_BLOCKS_USE, IMPORTANCE_BEFORE_PRODUCTION,
                                        ACTION_REEXPORT_SYMBOL, ACTION_ANSWER_CONTACT_NC,
                                        ACTION_EDIT_PINOUT, ACTION_SHOW_IN_KICAD,
                                        ACTION_OPEN_KICAD_TAB, ACTION_OPEN_SYMBOL_TAB)

#: Hand-off of the electrical types: the panel hands it over and the window asks
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
#: Actions done by the window when the panel hands them over (`handoff_requested`).
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
    # Unsaved symbol edits: «Applica» is in the Symbol tab.
    'symbol_draft_pending': (ACTION_OPEN_SYMBOL_TAB,),
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
        QApplication, not to the panel, exactly like
        `model_dialog._ModelsWorker`. A QThread destroyed while running
        aborts the whole process, and a panel can be destroyed without
        its run ending (a parent window deleted, an
        application quit): with QApplication as the owner the worker
        outlives the panel, deletes itself when finished, and the
        application exit waits for it instead of destroying it.
    """

    result = pyqtSignal(object)

    def __init__(self, args, parent=None, error=None):
        """@brief Prepare one verification run owned by QApplication.

        @param error An exception raised while building `args`: the run emits
            it as one ERROR row and ends normally (the panel never stays stuck).

        @param args Keyword arguments of `verify_component`.
        @param parent Ignored for ownership: QApplication.instance() is the
            owner when there is one, so that no panel can destroy the
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
        """@brief End the verification now: the kicad-cli command in flight is killed.

        @note The stop is also checked each time a check result is emitted;
            a killed command raises `CliStopped` inside the verification,
            which ends the run without a row (review 2026-11-15, M3: a check
            left running at a window close or a project switch must not keep
            kicad-cli alive for up to 60 s).
        """
        self.cancel.set()
        stop_cli_commands()

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

        @note Any failure of the verification itself is reported as one check
            «not run» (SKIP) so that the panel always reaches its verdict: a
            failure of the checking tool says nothing about the component and
            must not forbid its installation (review 2026-11-15, H1). A stop
            asked with `stop()` ends the run and sets `aborted`; the checks
            already emitted stay.
        """
        try:
            if self.cancel.is_set():
                raise _CheckAborted()
            if self.error is not None:
                self.result.emit(Check('SKIP', 'Controllo del componente', str(self.error),
                                       remedy='Usa «Controlla di nuovo».', code='check_failed'))
                return
            verify_component(**self.args, emit=self._emit)
        except (_CheckAborted, CliStopped):
            self.aborted = True
        except Exception as exc:
            if self.cancel.is_set():
                self.aborted = True
                return
            self.result.emit(Check('SKIP', 'Controllo del componente', str(exc),
                                   remedy='Usa «Controlla di nuovo».', code='check_failed'))

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


#: Rank of an item in the panel, in display order.
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
    """@brief One row of the panel: one thing to do, with its own buttons.

    @param rank `RANK_*`: errors first, then before production, final
        reviews, checks not run, the manual checks last.
    @param text The short sentence shown (remedy or title, with pin ranges).
    @param actions Action ids of its buttons, in order (only the ones the
        panel can run are shown).
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


class ComponentCheckPanel(QWidget):
    """@brief The component check of the KiCad tab: a verdict and one row per thing to do.

    @note Layout (user-approved redesign, 2026-11-14, moved into the KiCad
        tab on 2026-11-15): a verdict with its icon and one line saying what
        it means, the line «Per l'uso finale mancano N cose · M controlli
        superati» counting ROWS (not pins), then one row per thing to do --
        icon, short sentence, its own buttons -- errors first, then
        importance 2, importance 3, checks not run, the manual checks last.
        The technical details are collapsed and list each check once.
    @note It was a modal window until 2026-11-15 (user: «prima il controllo e
        poi la proposta», first the check, then the proposal): it lives in
        step 1 of the KiCad tab now, so its outcome stays on screen and
        decides what step 3 offers. The window that hosts it reads
        `verdict_key`, `blocking` and the signals.
    """

    #: Emitted when a run starts (the first, «Controlla di nuovo», the re-run after a fix).
    run_started = pyqtSignal()
    #: Emitted when a run has ended and its verdict is shown (completed or stopped).
    run_finished = pyqtSignal()
    #: Emitted with the action id of a hand-off (`HANDOFF_ACTIONS`) the window carries out.
    handoff_requested = pyqtSignal(str)

    #: Tallest the rows' area grows before it scrolls, in pixels: sixty rows
    #: must not push steps 2 and 3 off the tab. A bound on the layout, not a
    #: measurement.
    ITEMS_MAX_HEIGHT = 300

    def __init__(self, parent=None, tx=lambda s: s):
        """@brief Build the panel, idle until `run` is called.

        @param parent Owner widget.
        @param tx Translation function (Italian source -> shown text).
        """
        super().__init__(parent)
        self.tx = tx
        #: Keyword arguments of `verify_component`, or a callable returning
        #: them, read again at EVERY run (`run`).
        self.args = {}
        self.actions = {}
        #: Hand-offs this panel does not offer where it is shown (the KiCad
        #: tab does not offer «Vai alla scheda KiCad»: it is that tab).
        self.unavailable_actions = set()
        #: Check codes whose rows are shown but do not count in the verdict
        #: and the headline: what the window resolves itself (the KiCad tab:
        #: whether the component is installed, which step 3 does).
        self.verdict_excludes = frozenset()
        #: Action id -> the first button showing it, rebuilt at each finished run.
        self.action_buttons = {}
        #: The rows of the last finished run (`CheckItem`), in display order.
        self.items = []
        #: The last hand-off asked (`HANDOFF_ACTIONS`), or None.
        self.handoff = None
        self.ai_review_requested = False
        self.results = []
        self.worker = None
        self._running = False
        #: Source of the verdict of the last finished run (a key of
        #: `VERDICT_EXPLANATIONS`, or «VERIFICA INTERROTTA»), None before one.
        self.verdict_key = None
        #: True when the last finished run found a check that forbids the use.
        self.blocking = False
        #: True when the last run was stopped before its end.
        self.aborted = False
        #: Callable returning why the rows' actions must wait now ('' when
        #: they may run): the window's work-in-progress guard. The panel is
        #: not modal any more, so a regeneration may be rewriting the files
        #: while its rows are on screen (review 2026-11-15, M2b).
        self.action_guard = None
        #: Callable that runs the check again after a successful fix, through
        #: the window's own guards; None re-runs directly.
        self.rerun_handler = None
        #: True while a run is being stopped to be thrown away (a project
        #: switch): its rows are not shown and it ends without a verdict.
        self._forgetting = False
        #: True between «Interrompi» and the end of the run.
        self._stop_pending = False
        #: Re-composes the line under the rows in the current language, or None.
        self._action_status_source = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        verdict_row = QHBoxLayout()
        verdict_row.setSpacing(10)
        self.verdict_icon = QLabel()
        self.verdict_icon.setFixedSize(36, 36)
        verdict_row.addWidget(self.verdict_icon, 0, Qt.AlignmentFlag.AlignTop)
        verdict_text = QVBoxLayout()
        verdict_text.setSpacing(2)
        self.verdict = QLabel(tx('Controllo non ancora eseguito'))
        self.verdict.setStyleSheet('font-size: 15px; font-weight: bold;')
        self.verdict.setProperty('i18n_skip', True)
        verdict_text.addWidget(self.verdict)
        #: One line saying what the verdict means (`VERDICT_EXPLANATIONS`).
        self.explanation = QLabel()
        self.explanation.setWordWrap(True)
        self.explanation.setProperty('i18n_skip', True)
        verdict_text.addWidget(self.explanation)
        verdict_row.addLayout(verdict_text, 1)
        self.rerun_button = QPushButton(tx('Controlla ora'))
        self.rerun_button.setProperty('i18n_skip', True)
        self.rerun_button.clicked.connect(lambda: self._rerun())
        verdict_row.addWidget(self.rerun_button, 0, Qt.AlignmentFlag.AlignTop)
        # «Interrompi» (Stop): shown only while the checks run.
        self.stop_button = QPushButton(tx('Interrompi'))
        self.stop_button.setProperty('i18n_skip', True)
        self.stop_button.clicked.connect(self._stop_requested)
        self.stop_button.hide()
        verdict_row.addWidget(self.stop_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(verdict_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty('i18n_skip', True)
        self.status.hide()
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        layout.addWidget(self.progress)
        #: «Per l'uso finale mancano N cose · M controlli superati».
        self.headline = QLabel()
        self.headline.setWordWrap(True)
        self.headline.setProperty('i18n_skip', True)
        layout.addWidget(self.headline)
        self.items_box = QWidget()
        self.items_layout = QVBoxLayout(self.items_box)
        self.items_layout.setContentsMargins(0, 0, 0, 0)
        self.items_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        # The rows scroll past ITEMS_MAX_HEIGHT; below it the area takes the
        # rows' own height (`_fit_items_height`), no empty band.
        self.items_scroll = QScrollArea()
        self.items_scroll.setWidgetResizable(True)
        self.items_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.items_scroll.setWidget(self.items_box)
        self.items_scroll.setFixedHeight(0)
        layout.addWidget(self.items_scroll)
        self.action_status = QLabel()
        self.action_status.setWordWrap(True)
        self.action_status.setProperty('i18n_skip', True)
        self.action_status.hide()
        layout.addWidget(self.action_status)
        details_row = QHBoxLayout()
        self.details_toggle = QToolButton()
        self.details_toggle.setText(tx('Mostra dettagli tecnici'))
        self.details_toggle.setProperty('i18n_skip', True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.setCheckable(True)
        self.details_toggle.toggled.connect(self._toggle_details)
        details_row.addWidget(self.details_toggle)
        details_row.addStretch()
        self.copy_button = QPushButton(tx('Copia rapporto'))
        self.copy_button.setProperty('i18n_skip', True)
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self._copy_text()))
        self.copy_button.setEnabled(False)
        details_row.addWidget(self.copy_button)
        layout.addLayout(details_row)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMaximumHeight(0)
        self.report.hide()
        layout.addWidget(self.report)
        for button in (self.rerun_button, self.stop_button, self.copy_button):
            button.setAutoDefault(False)

    # ── runs ──────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """@brief Tell whether a verification is running now.

        @return True from the start of a run until its verdict is shown.
        """
        return self._running

    def run(self, args, actions=None, keep_status=False) -> bool:
        """@brief Start a verification now, unless one is already running.

        @param args Keyword arguments of `verify_component` (directory, name,
            rows...), or a callable returning them: called for EVERY run (this
            one, «Controlla di nuovo», the re-run after a fix), so each run
            reads the window as it is then.
        @param actions Mapping {action id: callable} of the fixes and views the
            window can run (`Check.action`). A FIX (`FIX_ACTIONS`) that
            succeeds runs the verification again by itself; a VIEW
            (`VIEW_ACTIONS`) does not. A hand-off (`HANDOFF_ACTIONS`) needs no
            callable: the panel emits `handoff_requested` and the window
            routes it.
        @param keep_status True keeps the line under the rows (the outcome of
            the fix that asked for this run).
        @return True when a run started, False when one was already running.
        """
        if self._running:
            return False
        self.args = args
        self.actions = dict(actions or {})
        self._rerun(keep_status=keep_status)
        return True

    def show_idle(self, message):
        """@brief Show that no valid verdict exists, with the reason, between runs.

        @param message Translated reason (no check yet, files changed, ...).
        @note Never called while a run is in progress: the running state
            keeps its own lines.
        """
        if self._running:
            return
        self.results, self.items = [], []
        self.verdict_key, self.blocking, self.aborted = None, False, False
        self._clear_items()
        self.verdict.setText(message)
        self.verdict.setStyleSheet('font-size: 15px; font-weight: bold;')
        self.verdict_icon.clear()
        self.explanation.setText('')
        self.headline.setText('')
        self.action_status.hide()
        self.report.setPlainText('')
        self.copy_button.setEnabled(False)
        self._label_rerun()

    def _label_rerun(self):
        """«Controlla ora» before any outcome, «Controlla di nuovo» once one is on screen."""
        self.rerun_button.setText(self.tx('Controlla di nuovo' if self.verdict_key else 'Controlla ora'))

    def stop(self):
        """@brief Stop a running verification now (its kicad-cli command is killed)."""
        self._stop_requested()

    def forget(self):
        """@brief Stop a running verification and throw it away: no rows, no verdict (a project switch).

        @note A run stopped this way ends idle, not «interrupted»: the user
            did not stop it, and coming back to its project must start a new
            check rather than ask for one (review 2026-11-15, L2).
        """
        if not self._running:
            return
        self._forgetting = True
        self._clear_items()
        self._stop_requested()

    def _start_worker(self):
        """Create the verification worker and start it on the next event-loop turn."""
        # Owned by QApplication, not by this panel: see _CheckWorker.
        try:
            args, error = (self.args() if callable(self.args) else dict(self.args)), None
        except Exception as exc:  # noqa: BLE001 - shown as one ERROR row, the panel still ends
            args, error = {}, exc
        self.worker = _CheckWorker(args, error=error)
        self.worker.result.connect(self._result)
        self.worker.finished.connect(self._finished)
        self._running = True
        self.rerun_button.setEnabled(False)
        # Only «Interrompi» while it runs: a greyed «Controlla ora» beside it
        # read as a second command.
        self.rerun_button.hide()
        self.copy_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.stop_button.show()
        self.run_started.emit()
        QTimer.singleShot(0, self.worker.start)

    def _rerun(self, *, keep_status=False):
        """Run every check again: a first run, «Controlla di nuovo», or after a successful fix.

        @param keep_status True keeps the line that says why it runs again.
        """
        if self._running:
            return
        self.results = []
        self.items = []
        self.verdict_key, self.blocking, self.aborted = None, False, False
        self.report.setPlainText('')
        self.verdict.setText(self.tx('Sto controllando il componente…'))
        self.verdict.setStyleSheet('font-size: 15px; font-weight: bold;')
        self.verdict_icon.clear()
        self.headline.setText('')
        self.explanation.setText('')
        if not keep_status:
            self.action_status.hide()
            self._action_status_source = None
        self._clear_items()
        self._stop_pending = False
        self.status.setText(self.tx('Verifica in corso: ogni comando KiCad ha un limite di 60 secondi.'))
        self.status.show()
        self.progress.show()
        self._start_worker()

    def _result(self, result):
        if self._forgetting:
            return
        self.results.append(result)
        self.report.setPlainText(self._format_report())

    def _toggle_details(self, expanded):
        self.report.setVisible(expanded)
        self.report.setMaximumHeight(260 if expanded else 0)
        self.report.setMinimumHeight(160 if expanded else 0)
        self.details_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details_toggle.setText(self.tx(
            'Nascondi dettagli tecnici' if expanded else 'Mostra dettagli tecnici'))

    def retranslate(self):
        """@brief Recompose every visible text in the current language (language switch).

        @note The verdict, the rows and the details are built from the checks'
            Italian sources, so they are composed again; an idle panel keeps
            the reason its window gives it again.
        """
        self._label_rerun()
        self.stop_button.setText(self.tx('Interrompi'))
        self.copy_button.setText(self.tx('Copia rapporto'))
        self._toggle_details(self.details_toggle.isChecked())
        if self._action_status_source is not None:
            self.action_status.setText(self._action_status_source())
        if self._running:
            self.verdict.setText(self.tx('Sto controllando il componente…'))
            self.status.setText(self._stop_text() if self._stop_pending else self.tx(
                'Verifica in corso: ogni comando KiCad ha un limite di 60 secondi.'))
            return
        if self.verdict_key is None:
            return
        self._set_verdict()
        if self.aborted:
            self._set_aborted_verdict()
        self.report.setPlainText(self._format_report())

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
                line += self.tx('Rimedio: {remedy}').format(remedy=self.tx(result.remedy)) + '\n'
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
        missing = self.missing_count()
        passed = sum(result.status == 'OK' for result in self.results)
        first = (self.tx('Per l’uso finale non manca nulla') if missing == 0 else
                 self.tx('Per l’uso finale manca 1 cosa') if missing == 1 else
                 self.tx('Per l’uso finale mancano {count} cose').format(count=missing))
        second = (self.tx('nessun controllo superato') if passed == 0 else
                  self.tx('1 controllo superato') if passed == 1 else
                  self.tx('{count} controlli superati').format(count=passed))
        return f'{first} · {second}'

    def replace_checks(self, code, checks):
        """@brief Put new checks of one code in place of the old ones and show the verdict again.

        @param code `Check.code` of the rows to replace (e.g. «kicad_library»).
        @param checks The new checks of that code.
        @note For a part of the check that can be redone alone without
            kicad-cli (the installed libraries after an installation): the
            rest of the outcome still describes the same files.
        """
        if self._running or self.verdict_key is None:
            return
        self.results = [result for result in self.results if result.code != code] + list(checks)
        self._set_verdict()
        if self.aborted:
            self._set_aborted_verdict()
        self.report.setPlainText(self._format_report())

    def missing_count(self) -> int:
        """@brief Rows of the last finished run that are something missing for the final use.

        @return The count of the headline's first half (errors, before
            production, final reviews, checks not run); 0 before a run.
        """
        return sum(item.rank in MISSING_RANKS and not any(
            result.code in self.verdict_excludes for result in item.results) for item in self.items)

    @classmethod
    def verdict_of(cls, results):
        """@brief The verdict of a set of checks: (source title, colour, blocking).

        @param results Checks (`Check`) of one run, or a part of them.
        @return The Italian source of the verdict (a key of
            `VERDICT_EXPLANATIONS`), its colour and whether a check forbids the use.
        @note The rule of `_set_verdict`, usable on a SUBSET: the KiCad tab
            decides step 3 without the rows about the KiCad library, which
            step 3 itself resolves (review 2026-11-15, M1).
        """
        counts = {status: sum(result.status == status for result in results)
                  for status in ('OK', 'ERROR', 'WARN', 'SKIP')}
        blocking = any(cls._blocks_use(r) for r in results)
        review = any(cls._before_production(r) for r in results)
        manual_only_warning = all(
            result.status != 'WARN' or result.category == 'manual' for result in results)
        if blocking:
            return 'NON USARE', '#f38ba8', True
        if review:
            return 'DA RIVEDERE PRIMA DELL’USO', '#fab387', False
        if counts['SKIP']:
            return 'ESITO NON CONCLUSIVO', '#f9e2af', False
        if counts['WARN'] and not manual_only_warning:
            return 'UTILIZZABILE CON RISERVE', '#f9e2af', False
        if counts['WARN']:
            return 'PRONTO PER LE PROVE', '#f9e2af', False
        return 'PRONTO PER L’USO', '#a6e3a1', False

    def _set_verdict(self):
        """Verdict, headline and rows of a finished run.

        @note The verdict reads only `Check.status`, `Check.category` and
            `Check.importance`, never a translated title: any ERROR (or an
            importance-1 warning) -> «NON USARE»; an importance-2 warning ->
            «DA RIVEDERE PRIMA DELL’USO»; a skipped check -> «ESITO NON
            CONCLUSIVO»; other warnings -> «UTILIZZABILE CON RISERVE», or
            «PRONTO PER LE PROVE» when the manual checks are the only ones.
        """
        counted = [result for result in self.results if result.code not in self.verdict_excludes]
        counts = {status: sum(result.status == status for result in counted)
                  for status in ('OK', 'ERROR', 'WARN', 'SKIP')}
        title, color, blocking = self.verdict_of(counted)
        self.verdict_key, self.blocking = title, blocking
        self.verdict.setText(self.tx(title))
        self.verdict.setStyleSheet(f'font-size: 15px; font-weight: bold; color: {color};')
        self.explanation.setText(self.tx(VERDICT_EXPLANATIONS[title]))
        icon_name = ('check-status-red.svg' if blocking else
                     'check-status-yellow.svg' if counts['WARN'] or counts['SKIP'] else
                     'check-status-green.svg')
        self.verdict_icon.setPixmap(QIcon(str(Path(__file__).parent / 'assets' / icon_name))
                                    .pixmap(QSize(34, 34)))
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
        self._fit_items_height()

    def _fit_items_height(self):
        """Give the rows' area the rows' own height, up to ITEMS_MAX_HEIGHT (then it scrolls).

        Measured at the width the rows really get: a wrapped row's size hint
        alone is taller than the row and left an empty band below the rows.
        """
        layout = self.items_layout
        rows = [layout.itemAt(index).widget() for index in range(layout.count())]
        rows = [row for row in rows if row is not None]
        if not rows:
            self.items_scroll.setFixedHeight(0)
            return
        width = self.items_scroll.viewport().width() or self.width()
        # Row by row: the whole layout answers only after an event-loop turn
        # (rows just added), while each row's own height for a width is known now.
        height = sum(row.heightForWidth(width) if row.hasHeightForWidth() else row.sizeHint().height()
                     for row in rows)
        height += layout.spacing() * (len(rows) - 1)
        self.items_scroll.setFixedHeight(max(0, min(height, self.ITEMS_MAX_HEIGHT)))

    def resizeEvent(self, event):
        """@brief Qt override: the rows wrap again at the new width, so their area is measured again.

        @param event The resize event, passed on to the base class.
        """
        super().resizeEvent(event)
        self._fit_items_height()

    def _action_available(self, action):
        """True for an action the panel can run: a hand-off it offers, or one the window provides."""
        if action in self.unavailable_actions:
            return False
        return action in HANDOFF_ACTIONS or callable(self.actions.get(action))

    def _show_items(self):
        """One row per item: icon, sentence (full detail as tooltip), its own buttons.

        @note The highlighted button (object name «primary») is the first
            button of the FIRST row when that row is an error, or -- without
            any error row -- an importance-2 row; a first row without a
            button highlights nothing below it. It is never the default
            button and no row button is auto-default: Enter must not
            re-export or launch KiCad.
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
            text.setProperty('i18n_skip', True)
            if item.rank == RANK_PASSED:
                # The theme's OK green, explicitly: the application stylesheet
                # gives every QLabel its text colour, and the tick glyph comes from
                # a fallback font that ignores it -- the green status icon is drawn.
                text.setStyleSheet(f'QLabel {{ color: {GREEN}; }}')
                icon.setPixmap(QIcon(str(Path(__file__).parent / 'assets' / 'check-status-green.svg'))
                               .pixmap(QSize(14, 14)))
            tips = [f'{self.tx(r.title)}: {self.tx(r.detail)}'
                    + (('\n' + self.tx('Rimedio: {remedy}').format(remedy=self.tx(r.remedy)))
                       if r.remedy else '')
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
                button.setProperty('i18n_skip', True)
                button.setAutoDefault(False)
                button.setDefault(False)
                button.clicked.connect(lambda _checked=False, a=action: self._run_action(a))
                if item is target and first:
                    button.setObjectName('primary')
                first = False
                line.addWidget(button, alignment=Qt.AlignmentFlag.AlignVCenter)
                self.action_buttons.setdefault(action, button)
            self.items_layout.addWidget(row)
        self._fit_items_height()

    def highlighted_button(self):
        """@brief The highlighted button of the rows.

        @return The button named «primary» (the next thing to do), or None.
        """
        return next((button for button in self.items_box.findChildren(QPushButton)
                     if button.objectName() == 'primary'), None)

    def _run_action(self, action):
        """Run one action of a row.

        @param action Action id.
        @note A hand-off is emitted for the window (`handoff_requested`). A
            view runs and says how it went, nothing more. A fix that succeeds
            runs the whole verification again by itself («Verifica ripetuta
            automaticamente»); a failing one is reported and nothing re-runs.
        """
        if self._running:
            return
        reason = self.action_guard() if callable(self.action_guard) else ''
        if reason:
            self._show_action_status(lambda: reason)
            return
        if action in HANDOFF_ACTIONS:
            self.handoff = action
            self.ai_review_requested = action == ACTION_SUGGEST_PIN_TYPES
            self.handoff_requested.emit(action)
            return
        try:
            outcome = self.actions[action]()
        except Exception as exc:  # noqa: BLE001 - shown to the user, the panel stays usable
            template = ('Non riuscito: {error}' if action in VIEW_ACTIONS
                        else 'Correzione non riuscita: {error}')
            self._show_action_status(lambda: self.tx(template).format(error=exc))
            return
        message = outcome.strip() if isinstance(outcome, str) and outcome.strip() else ''
        if action in FIX_ACTIONS:
            self._show_action_status(lambda: (message + '\n' if message else '')
                                     + self.tx('Verifica ripetuta automaticamente.'))
            started = self.rerun_handler() if callable(self.rerun_handler) else self._rerun(keep_status=True)
            if started is False:
                # The window refused the re-run (it said why): the line must
                # not claim one (final verification 2026-11-15, 9).
                self._show_action_status(lambda: message or self.tx('Correzione eseguita.'))
            return
        if message:
            self._show_action_status(lambda: message)

    def _show_action_status(self, compose):
        """Show the line under the rows, composed again on a language switch."""
        self._action_status_source = compose
        self.action_status.setText(compose())
        self.action_status.show()

    def _set_aborted_verdict(self):
        """Verdict of a run stopped by «Interrompi»: never a readiness claim.

        The checks emitted before the stop stay in the rows and the details;
        what was not run is said, and nothing is called ready.
        """
        self.verdict_key = 'VERIFICA INTERROTTA'
        self.verdict.setText(self.tx('VERIFICA INTERROTTA'))
        self.verdict.setStyleSheet('font-size: 15px; font-weight: bold; color: #f9e2af;')
        self.verdict_icon.setPixmap(
            QIcon(str(Path(__file__).parent / 'assets' / 'check-status-yellow.svg'))
            .pixmap(QSize(34, 34)))
        self.explanation.setText(self.tx(
            'Verifica interrotta su richiesta: i controlli restanti non sono stati eseguiti. '
            'Usa «Controlla di nuovo» prima di usare il componente.'))
        self.headline.setText(self.tx('Verifica interrotta: {count} controlli eseguiti.').format(
            count=len(self.results)))
        # No ✓ row: a partial run cannot say what is free of problems.
        self.items = [item for item in self.items if item.rank != RANK_PASSED]
        self._show_items()

    def _stop_requested(self):
        """Ask the worker to stop after the KiCad command in flight and say so."""
        if not self._running:
            return
        self._stop_pending = True
        self.worker.stop()
        self.stop_button.setEnabled(False)
        self.status.setText(self._stop_text())

    def _stop_text(self):
        """The status line between «Interrompi» and the end of the run."""
        return self.tx('Interruzione richiesta: il controllo si ferma.')

    def _finished(self):
        self._running = False
        self._stop_pending = False
        self.stop_button.setEnabled(False)
        self.stop_button.hide()
        self.progress.hide()
        if self._forgetting:
            # Thrown away (a project switch): idle, never «interrupted».
            self._forgetting = False
            self.status.hide()
            self.rerun_button.setEnabled(True)
            self.rerun_button.show()
            self.show_idle(self.tx('Controllo non ancora eseguito'))
            self.run_finished.emit()
            return
        self.aborted = bool(self.worker.aborted)
        self._set_verdict()
        if self.aborted:
            self._set_aborted_verdict()
        self.report.setPlainText(self._format_report())
        self.status.hide()
        self.rerun_button.setEnabled(True)
        self.rerun_button.show()
        self.copy_button.setEnabled(True)
        self._label_rerun()
        self.run_finished.emit()
