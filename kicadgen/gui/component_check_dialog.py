"""@brief Background component verification with a concise verdict and details."""
import re
import threading
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt, QSize
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
                             QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
                             QToolButton, QHBoxLayout, QGroupBox)
from ..services.component_check import verify_component, Check


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

    def __init__(self, args, parent=None):
        """@brief Prepare one verification run owned by QApplication.

        @param args Keyword arguments of `verify_component`.
        @param parent Ignored for ownership: QApplication.instance() is the
            owner when there is one, so that no dialog can destroy the
            running thread (see the class note). Kept for signature
            compatibility.
        """
        application = QApplication.instance()
        super().__init__(application if application is not None else parent)
        self.args = args
        #: Set by «Interrompi» (and at application exit): no further KiCad
        #: command is started once the running one has returned.
        self.cancel = threading.Event()
        #: True when the run ended because of `cancel`, not by completion.
        self.aborted = False
        if application is not None:
            # Quitting while a KiCad command is in flight would destroy the
            # running thread: stop it after the current command and wait.
            # The wait is bounded by the 60 s limit of that ONE command.
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
        """Stop after the current command, then wait for the thread to leave."""
        self.stop()
        self.wait()

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


class ComponentCheckDialog(QDialog):
    """@brief Run artifact checks in a worker and present a copyable report."""
    def __init__(self, args, parent=None, tx=lambda s: s):
        """@brief Build the report window and start the verification worker.

        @param args Keyword arguments of `verify_component` (directory, name,
            rows...).
        @param parent Owner window.
        @param tx Translation function (Italian source -> shown text).
        @note The worker starts on the next event-loop turn, so the window is
            laid out before the first result arrives.
        """
        super().__init__(parent)
        self.tx = tx
        self.results = []
        self.setWindowTitle(tx('Verifica componente per KiCad'))
        self.resize(820, 470)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
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
        self.recommendation = QLabel()
        self.recommendation.setWordWrap(True)
        layout.addWidget(self.recommendation)
        self.actions_group = QGroupBox(tx('Cosa completare per l’uso finale'))
        actions_layout = QVBoxLayout(self.actions_group)
        self.actions_label = QLabel()
        self.actions_label.setWordWrap(True)
        actions_layout.addWidget(self.actions_label)
        self.ai_review_requested = False
        self.ai_review_button = QPushButton(tx('Suggerisci tipi pin con AI'))
        self.ai_review_button.setObjectName('primary')
        self.ai_review_button.clicked.connect(self._request_ai_pin_review)
        self.ai_review_button.hide()
        actions_layout.addWidget(self.ai_review_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.actions_group)
        self.actions_group.hide()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setEnabled(False)
        buttons.rejected.connect(self.reject)
        copy = QPushButton(tx('Copia rapporto'))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report.toPlainText()))
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        # «Interrompi» (Stop): no further KiCad command after the running one.
        self.stop_button = QPushButton(tx('Interrompi'))
        self.stop_button.clicked.connect(self._stop_requested)
        buttons.addButton(self.stop_button, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)
        # Owned by QApplication, not by this window: see _CheckWorker.
        self.worker = _CheckWorker(args)
        self.worker.result.connect(self._result)
        self.worker.finished.connect(self._finished)
        self._running = True
        QTimer.singleShot(0, self.worker.start)

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
        numeric = sorted({int(value) for value in values if value.isdigit()})
        other = sorted({value for value in values if not value.isdigit()})
        parts = []
        if numeric:
            start = previous = numeric[0]
            for value in numeric[1:]:
                if value == previous + 1:
                    previous = value
                    continue
                parts.append(str(start) if start == previous else f'{start}–{previous}')
                start = previous = value
            parts.append(str(start) if start == previous else f'{start}–{previous}')
        return ', '.join(parts + other)

    def _format_report(self):
        labels = {'OK': 'Superato', 'ERROR': 'Errore', 'WARN': 'Avviso', 'SKIP': 'Non verificato'}
        lines = []
        electrical_pins = []
        review_title = self.tx('Revisione simbolo')
        type_template = self.tx('Tipo elettrico da verificare: {number}')
        type_prefix = type_template.partition('{number}')[0]
        type_suffix = type_template.partition('{number}')[2]
        for result in self.results:
            match = re.fullmatch(re.escape(type_prefix) + r'(.+?)' + re.escape(type_suffix), result.detail)
            if result.title == review_title and match:
                electrical_pins.append(match.group(1).strip())
                continue
            lines.append(f'[{self.tx(labels[result.status])}] {self.tx(result.title)}\n'
                         f'{self.tx(result.detail)}\n')
        if electrical_pins:
            detail = self.tx('Tipi elettrici da revisionare: {count} pin ({pins}).').format(
                count=len(set(electrical_pins)), pins=self._pin_ranges(electrical_pins))
            lines.append(f'[{self.tx(labels["WARN"])}] {review_title}\n{detail}\n')
        report = '\n'.join(lines)
        if not getattr(self, '_running', True):
            report = (f'{self.verdict.text()}\n{self.recommendation.text()}\n'
                      f'{self.actions_group.title()}\n{self.actions_label.text()}\n\n'
                      f'{self.summary.text()}\n\n{report}')
        return report

    def _set_verdict(self):
        counts = {status: sum(result.status == status for result in self.results)
                  for status in ('OK', 'ERROR', 'WARN', 'SKIP')}
        manual_only_warning = all(
            result.status != 'WARN' or result.title == self.tx('Verifiche manuali')
            for result in self.results)
        if counts['ERROR']:
            title = 'NON USARE'
            recommendation = 'No. Correggi gli errori tecnici e ripeti la verifica prima di usarlo.'
            color = '#f38ba8'
        elif counts['SKIP']:
            title = 'ESITO NON CONCLUSIVO'
            recommendation = 'Non posso confermare che sia pronto: alcuni controlli non sono stati eseguiti.'
            color = '#f9e2af'
        elif counts['WARN'] and not manual_only_warning:
            title = 'UTILIZZABILE CON RISERVE'
            recommendation = ('Sì, per prove e prototipi. Prima dell’uso finale, completa le revisioni '
                              'indicate ed esegui ERC e DRC in KiCad.')
            color = '#f9e2af'
        elif counts['WARN']:
            title = 'PRONTO PER LE PROVE'
            recommendation = ('Sì. I controlli automatici sono superati; prima del rilascio finale '
                              'esegui comunque ERC e DRC in KiCad.')
            color = '#f9e2af'
        else:
            title = 'PRONTO PER L’USO'
            recommendation = 'Sì. I controlli automatici sono superati.'
            color = '#a6e3a1'
        self.verdict.setText(self.tx(title))
        self.verdict.setStyleSheet(f'font-size: 16px; font-weight: bold; color: {color};')
        icon_name = ('check-status-red.svg' if counts['ERROR'] else
                     'check-status-yellow.svg' if counts['WARN'] or counts['SKIP'] else
                     'check-status-green.svg')
        self.verdict_icon.setPixmap(QIcon(str(Path(__file__).parent / 'assets' / icon_name))
                                    .pixmap(QSize(44, 44)))
        self.recommendation.setText(self.tx(recommendation))
        parts = [f'{counts[key]} {self.tx(label)}' for key, label in
                 [('OK', 'controlli superati'), ('ERROR', 'errori'),
                  ('WARN', 'avvisi'), ('SKIP', 'non verificati')]]
        notes = []
        passed = {r.title for r in self.results if r.status == 'OK'}
        loaded = [self.tx(source) for source in (
            'Lettura footprint in KiCad', 'Simbolo applicato: lettura in KiCad',
            'Integrità STEP', 'Importazione 3D in KiCad') if self.tx(source) in passed]
        if loaded:
            notes.append(self.tx('KiCad ha letto correttamente: {items}.').format(
                items=', '.join(loaded)))
        pads = next((r.detail for r in self.results
                     if r.title == self.tx('Corrispondenza pin-pad') and r.status == 'OK'), None)
        if pads:
            notes.append(pads)
        review_title = self.tx('Revisione simbolo')
        type_template = self.tx('Tipo elettrico da verificare: {number}')
        type_pattern = re.escape(type_template.partition('{number}')[0]) + r'(.+?)' + re.escape(type_template.partition('{number}')[2])
        electrical_pins = [match.group(1).strip() for r in self.results
                           if r.title == review_title
                           for match in [re.fullmatch(type_pattern, r.detail)]
                           if match]
        if electrical_pins:
            notes.append(self.tx('{count} tipi elettrici da confermare (pin {pins}).').format(
                count=len(set(electrical_pins)), pins=self._pin_ranges(electrical_pins)))
        if counts['ERROR']:
            notes.append(self.tx('Correggi gli errori indicati nei dettagli prima di usare il componente.'))
        elif counts['WARN'] or counts['SKIP']:
            notes.append(self.tx('Controlla gli avvisi; ERC dello schema e DRC della scheda non sono inclusi.'))
        self.summary.setText(' · '.join(parts) + ('\n' + '\n'.join(notes) if notes else ''))
        actions = []
        if electrical_pins:
            actions.append(self.tx(
                'Conferma i tipi elettrici dei pin {pins} consultando il datasheet.').format(
                    pins=self._pin_ranges(electrical_pins)))
        missing_symbol = self.tx(
            'Nessun .kicad_sym nella cartella componente; verificata la versione applicata in memoria.')
        missing_3d = self.tx(
            'Il footprint su disco non contiene un riferimento 3D; il test di importazione lo aggiunge soltanto nella copia temporanea.')
        for result in self.results:
            if result.status == 'WARN' and result.title == self.tx('Simbolo esportato') \
                    and result.detail == missing_symbol:
                actions.append(self.tx(
                    'Esporta il simbolo .kicad_sym nella cartella del componente dal tab KiCad.'))
            elif result.status == 'WARN' and result.title == self.tx('Collegamento 3D originale') \
                    and result.detail == missing_3d:
                actions.append(self.tx(
                    'Installa la libreria dal tab KiCad e verifica il modello STEP nella libreria installata.'))
            elif result.status == 'ERROR':
                actions.append(self.tx('Apri i dettagli tecnici e correggi: {item}.').format(
                    item=result.title))
            elif result.status == 'SKIP':
                actions.append(self.tx('Completa il controllo non eseguito: {item}.').format(
                    item=result.title))
            elif result.status == 'WARN' and result.title not in {
                    self.tx('Revisione simbolo'), self.tx('Verifiche manuali'),
                    self.tx('Simbolo esportato'), self.tx('Collegamento 3D originale')}:
                actions.append(self.tx('Rivedi l’avviso: {item}.').format(item=result.title))
        if any(result.title == self.tx('Verifiche manuali') for result in self.results):
            actions.append(self.tx(
                'In KiCad esegui ERC sullo schema e DRC sulla scheda prima della produzione.'))
        self.actions_label.setText('\n'.join(f'• {item}' for item in dict.fromkeys(actions)))
        self.ai_review_button.setVisible(bool(electrical_pins))
        self.actions_group.setVisible(bool(actions))

    def _set_aborted_verdict(self):
        """Verdict of a run stopped by «Interrompi»: never a readiness claim.

        The checks emitted before the stop stay in the report and the
        counts; what was not run is said, and nothing is called ready.
        """
        self.verdict.setText(self.tx('VERIFICA INTERROTTA'))
        self.verdict.setStyleSheet('font-size: 16px; font-weight: bold; color: #f9e2af;')
        self.verdict_icon.setPixmap(
            QIcon(str(Path(__file__).parent / 'assets' / 'check-status-yellow.svg'))
            .pixmap(QSize(44, 44)))
        self.recommendation.setText(self.tx(
            'Verifica interrotta su richiesta: i controlli restanti non sono stati eseguiti. '
            'Ripeti la verifica prima di usare il componente.'))

    def _request_ai_pin_review(self):
        """Close the finished report and hand off pin review to the parent window."""
        if self._running:
            return
        self.ai_review_requested = True
        self.accept()

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
        self.progress.hide()
        self._set_verdict()
        if self.worker.aborted:
            self._set_aborted_verdict()
        self.report.setPlainText(self._format_report())
        self.status.hide()
        if not self.details_toggle.isChecked():
            self.resize(self.width(), 470)
        self.close_button.setEnabled(True)

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
