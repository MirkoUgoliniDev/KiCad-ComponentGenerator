"""@brief «Scegli la rete SMD»: the local SMD network checkpoints, with what each declares.

@details User request (2026-10-02): the «Modello rete SMD» button opens a
window with the model's information instead of a bare file dialog, like
«Scegli il modello YOLO». On the left, the ``.pt`` files of models/smd, plus
the one in use and any file added with «Altro file...»; on the right, the
selected file: size, date, SHA-256 and what the checkpoint declares about
itself (architecture, training date and epochs, classes, kinds of rotation,
acceptance threshold, validation metrics).

@note A checkpoint is loaded with ``weights_only=True``
    (smd_component_classifier.describe_smd_checkpoint): no code from the file
    runs, so the details are read on their own, in the thread pool, and kept
    for the session by path, size and modification time.
"""
from __future__ import annotations

import os
from html import escape
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QPushButton, QScrollArea,
                             QVBoxLayout, QWidget)

from ..services import yolo_models
from ..services.messages import tx
from .theme import SUBTEXT, YELLOW
from .yolo_model_dialog import _breakable, _remember, _remembered, _size_text


class _Signals(QObject):
    """Carrier of a worker result back to the GUI thread."""

    ready = pyqtSignal(str, str, object)


class _Job(QRunnable):
    """Hash a checkpoint, or read what it declares, off the GUI thread."""

    def __init__(self, kind: str, path: str):
        super().__init__()
        self.kind, self.path = kind, path
        self.signals = _Signals()

    def run(self):
        """@brief QRunnable override: compute and emit (kind, path, result)."""
        try:
            if self.kind == "sha256":
                result = yolo_models.sha256_of(self.path)
            else:
                from ..services.smd_component_classifier import describe_smd_checkpoint
                result = dict(describe_smd_checkpoint(self.path), ok=True)
        except Exception as exc:  # noqa: BLE001 - shown in the window
            result = ({"ok": False, "error": str(exc)} if self.kind == "smd_check" else None)
        except BaseException as exc:  # noqa: BLE001 - nothing may leave a QRunnable
            result = ({"ok": False, "error": type(exc).__name__} if self.kind == "smd_check"
                      else None)
        self.signals.ready.emit(self.kind, self.path, result)


class SmdModelDialog(QDialog):
    """@brief Choose the local SMD network checkpoint, seeing what each file declares."""

    def __init__(self, current: str, models_dir, default: str = "", tr=None, parent=None):
        """@brief Build the list and select the network in use.

        @param current Path in the «Modello rete SMD» field ("" = the default).
        @param models_dir Folder whose ``.pt`` files are listed (models/smd).
        @param default Path used when the field is empty (shown as default).
        @param tr Translation function (Italian source -> shown text).
        @param parent Owner window.
        """
        super().__init__(parent)
        self._tr = tr or (lambda text: text)
        self._models_dir = Path(models_dir)
        self._default = str(default or "")
        self._current = str(current or self._default)
        self._extra = [p for p in (self._current, self._default) if p]
        self._jobs = []
        self._closed = False
        self.setWindowTitle(self._tr("Scegli la rete SMD"))
        self.setMinimumSize(760, 460)

        intro = QLabel(tx("Reti SMD nella cartella {folder}: classificano i corpi trovati da "
                          "YOLO. La scelta vale per questa sessione.", folder=self._models_dir))
        intro.setWordWrap(True)
        intro.setProperty("i18n_skip", True)

        self.model_list = QListWidget()
        self.model_list.setMinimumWidth(260)
        self.model_list.currentItemChanged.connect(self._show_selected)

        self.details = QLabel()
        self.details.setProperty("i18n_skip", True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setWordWrap(True)
        self.details.setMinimumWidth(1)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.addWidget(self.details)
        holder_layout.addStretch(1)
        scroll.setWidget(holder)

        body = QHBoxLayout()
        body.addWidget(self.model_list, 2)
        body.addWidget(scroll, 3)

        self.other_button = QPushButton(self._tr("Altro file..."))
        self.other_button.setToolTip(self._tr("Sceglie una rete .pt fuori da questa cartella."))
        self.other_button.clicked.connect(self._choose_other)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            self._tr("Usa questa rete"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self._tr("Annulla"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.other_button)
        bottom.addStretch(1)
        bottom.addWidget(self.buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(body, 1)
        layout.addLayout(bottom)
        self._fill()

    # ── list ──────────────────────────────────────────────────────────

    def _files(self):
        """The .pt files of the folder plus the extra ones, without duplicates."""
        seen, files = set(), []
        folder = sorted(self._models_dir.glob("*.pt")) if self._models_dir.is_dir() else []
        for path in [*folder, *(Path(p) for p in self._extra)]:
            key = os.path.normcase(os.path.abspath(str(path)))
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            files.append(path)
        return files

    def _fill(self, select: str = "") -> None:
        """Rebuild the list and select `select`, else the network in use."""
        wanted = select or self._current
        self.model_list.blockSignals(True)
        self.model_list.clear()
        chosen = None
        for path in self._files():
            marks = []
            if self._same(path, self._current):
                marks.append(self._tr("in uso"))
            if self._default and self._same(path, self._default):
                marks.append(self._tr("predefinito"))
            text = path.name + (f"  ({', '.join(marks)})" if marks else "")
            text += f"\nPyTorch · {_size_text(path.stat().st_size)}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.model_list.addItem(item)
            if wanted and self._same(path, wanted):
                chosen = item
        self.model_list.blockSignals(False)
        if chosen is None and self.model_list.count():
            chosen = self.model_list.item(0)
        if chosen is not None:
            self.model_list.setCurrentItem(chosen)
        else:
            self._show_selected()

    @staticmethod
    def _same(first, second) -> bool:
        return bool(first and second) and (
            os.path.normcase(os.path.abspath(str(first)))
            == os.path.normcase(os.path.abspath(str(second))))

    def selected_path(self) -> str:
        """@brief Path of the selected file, or "" when the list is empty.

        @return The checkpoint path stored in the current list item, or "".
        """
        item = self.model_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def _choose_other(self) -> None:
        """«Altro file...»: add a checkpoint from anywhere and select it."""
        selected, _ = QFileDialog.getOpenFileName(
            self, self._tr("Altro file..."), str(self._models_dir), "PyTorch (*.pt)")
        if selected:
            self._extra.append(selected)
            self._fill(select=selected)

    # ── details ───────────────────────────────────────────────────────

    def _start(self, kind: str, path: str) -> None:
        if self._closed:
            return
        job = _Job(kind, path)
        job.signals.ready.connect(self._job_ready)
        self._jobs.append(job)
        QThreadPool.globalInstance().start(job)

    def _running(self, path, kind) -> bool:
        return any(job.kind == kind and self._same(job.path, path) for job in self._jobs)

    def _job_ready(self, kind, path, result) -> None:
        self._jobs = [job for job in self._jobs if not (job.kind == kind and job.path == path)]
        _remember(path, kind, result if kind == "smd_check" else (result or ""))
        if self._same(path, self.selected_path()):
            self._show_selected()

    def _show_selected(self, *_):
        """Draw the details of the selected file, starting what is still unknown."""
        if self._closed:
            return
        path = self.selected_path()
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if not path:
            self.details.setText(self._tr("Nessuna rete .pt in questa cartella: usa "
                                          "«Altro file...»."))
            ok_button.setEnabled(False)
            return
        for kind in ("sha256", "smd_check"):
            if _remembered(path, kind) is None and not self._running(path, kind):
                self._start(kind, path)
        check = _remembered(path, "smd_check")
        # A network that the classifier rejects cannot be chosen: it would
        # fail at analysis time, after everything else has been prepared.
        ok_button.setEnabled(not (check and not check.get("ok")))
        self.details.setText(self.details_html(path, _remembered(path, "sha256"), check))

    def details_html(self, path, digest, check) -> str:
        """@brief The details table of `path`: file facts, then what the checkpoint declares.

        @param path Checkpoint path.
        @param digest SHA-256 of the file, "" when unreadable, None while computing.
        @param check Result of describe_smd_checkpoint plus ``ok``, or
            ``{"ok": False, "error": ...}``, or None while reading.
        @return HTML table.
        """
        rows = []

        def row(label, value, note=""):
            extra = f"<br><span style='color:{YELLOW}'>{escape(note)}</span>" if note else ""
            rows.append(f"<tr><td style='padding:2px 14px 2px 0; color:{SUBTEXT}' "
                        f"valign='top'>{escape(label)}</td>"
                        f"<td>{escape(_breakable(str(value)))}{extra}</td></tr>")

        file = Path(path)
        try:
            info = file.stat()
            from datetime import datetime
            row(self._tr("Dimensione"), _size_text(info.st_size))
            row(self._tr("Data"), datetime.fromtimestamp(info.st_mtime).strftime("%d/%m/%Y %H:%M"))
        except OSError:
            pass
        row("SHA-256", self._tr("Calcolo in corso…") if digest is None else (digest or "—"))
        if check is None:
            row(self._tr("Contenuto"), self._tr("Lettura in corso…"))
        elif not check.get("ok"):
            row(self._tr("Contenuto"), self._tr("Non utilizzabile dalla rete SMD"),
                check.get("error", ""))
        else:
            row(self._tr("Formato"), check["format"] or "—")
            row(self._tr("Architettura"), f"{check['arch'] or '—'} · {check['img_size']} px")
            trained = check["trained"]
            if len(trained) >= 15 and trained[8] == "-":
                trained = f"{trained[6:8]}/{trained[4:6]}/{trained[:4]} {trained[9:11]}:{trained[11:13]}"
            row(self._tr("Addestrata"), tx("{date} · {epochs} epoche", date=trained or "—",
                                          epochs=check["epochs"] if check["epochs"] is not None
                                          else "?"))
            classes = [name for name in check["classes"] if name != "_other"]
            row(self._tr("Classi"), tx("{count}: {names}", count=len(classes),
                                       names=", ".join(classes)))
            kinds = check["orientations"]
            full = sorted(name for name, kind in kinds.items() if kind == "full")
            axis = sorted(name for name, kind in kinds.items() if kind == "axis")
            row(self._tr("Verso (pin 1)"), ", ".join(full) or "—")
            row(self._tr("Solo asse"), ", ".join(axis) or "—")
            row(self._tr("Soglia di accettazione"), f"{check['threshold']:.2f}")
            validation = check.get("validation")
            if validation and validation.get("samples") is not None:
                from ..services.smd_component_classifier import FEW_VALIDATION_SAMPLES
                samples = validation["samples"]
                accuracy = validation.get("accuracy")
                value = tx("{samples} campioni · accuratezza {accuracy}", samples=samples,
                           accuracy=f"{accuracy:.0%}" if isinstance(accuracy, (int, float))
                           else "?")
                note = (self._tr("Pochi campioni: la cifra dice poco su schede diverse da "
                                 "quelle usate per addestrarla.")
                        if isinstance(samples, int) and samples < FEW_VALIDATION_SAMPLES else "")
                row(self._tr("Validazione"), value, note)
            else:
                row(self._tr("Validazione"), self._tr("Non dichiarata nel file"))
        row(self._tr("Percorso"), path)
        return "<table>" + "".join(rows) + "</table>"

    def shutdown(self) -> None:
        """@brief Stop this window for good: no new jobs, no more job results.

        @note Same reason as YoloModelDialog.shutdown (HANDOFF 131): a window
            destroyed later by the garbage collector must not start a job.
        """
        self._closed = True
        self.model_list.blockSignals(True)
        for job in self._jobs:
            try:
                job.signals.ready.disconnect(self._job_ready)
            except TypeError:
                pass
        self._jobs = []

    def done(self, result) -> None:  # noqa: D401 - Qt override
        """@brief Qt override: shut the window down (see shutdown), then close.

        @param result Dialog result code.
        """
        self.shutdown()
        super().done(result)
