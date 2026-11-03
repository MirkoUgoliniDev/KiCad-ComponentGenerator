"""@brief «Scegli il modello YOLO»: the local weights files, with what is known about each.

@details Replaces the bare file dialog (user's request, 2026-09-26). On the
left, the weights files of the models folder, plus the model in use when it
lives elsewhere and any file added with «Altro file...». On the right, the
selected file: format, size, date; origin, family, license and training
data when the project knows the file (`services.yolo_models`, by SHA-256);
the classes it detects and whether the detector can use it.

@note Safety: a .pt checkpoint is a Python pickle, and loading it runs code
    from the file (`photo_yolo.resolve_yolo_model`). An ONNX file is checked
    as soon as it is selected; a .pt file only when the user presses
    «Verifica compatibilità», i.e. never just because it sits in a folder.
@note Hashing a ~50 MB checkpoint and loading a model take from a fraction
    of a second to a few seconds: both run in the thread pool, and results
    are kept for the session, keyed by path, size and modification time.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QPushButton, QScrollArea,
                             QVBoxLayout, QWidget)

from ..services import yolo_models
from ..services.messages import tx
from .theme import SUBTEXT, YELLOW

#: Session results, by file identity: {"sha256": str} and {"check": dict}.
_RESULTS: dict[tuple, dict] = {}
_RESULTS_LOCK = threading.Lock()


def _identity(path) -> tuple | None:
    """Path, size and modification time: a replaced file is examined again."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (os.path.normcase(os.path.abspath(str(path))), info.st_size, info.st_mtime_ns)


def _remembered(path, key):
    identity = _identity(path)
    with _RESULTS_LOCK:
        return (_RESULTS.get(identity) or {}).get(key) if identity else None


def _remember(path, key, value) -> None:
    _remember_identity(_identity(path), key, value)


def _remember_identity(identity, key, value) -> None:
    """Keep `value` for the file content `identity` describes (None keeps nothing)."""
    if identity is None:
        return
    with _RESULTS_LOCK:
        _RESULTS.setdefault(identity, {})[key] = value


#: Marker of "the caller did not say which identity its job examined".
_NOT_GIVEN = object()


class _JobResults:
    """Results of one window's jobs, including those the session cache refuses.

    A file that vanished or became unreadable has no identity, so `_remember`
    cannot keep its result; without a record of it, every redraw of the
    details would start the same job again, whose result redraws the details
    (review finding 2026-11-03: thousands of starts in two seconds). The
    refused result is kept here, by path, until the selection really changes.

    A file that changes WHILE its job runs (a model still being copied) has
    the same loop with another cause: its identity at the end is not the one
    examined, so the session cache never answers for it. Its result is
    remembered under the identity the job examined (it describes that
    content, not the current one) and kept here too, by path, until the
    selection really changes: one job per selection, not one per redraw.
    """

    def __init__(self) -> None:
        """@brief Start with no result kept outside the session cache."""
        self._unstored: dict[tuple, object] = {}
        #: Results of files that changed while their job ran, by (path, kind).
        self._settled: dict[tuple, object] = {}

    @staticmethod
    def _key(path, kind) -> tuple:
        return (os.path.normcase(os.path.abspath(str(path))), kind)

    def store(self, path, kind, value, examined=_NOT_GIVEN) -> None:
        """@brief Keep `value` in the session cache, or here when it cannot answer for the file.

        @param path File the job examined.
        @param kind Job kind ("sha256", "check", "smd_check").
        @param value Job result, already normalised by the caller.
        @param examined Identity (`_identity`) of the file when the job read
            it; omitted, the file is assumed unchanged since then.
        """
        key = self._key(path, kind)
        current = _identity(path)
        if examined is _NOT_GIVEN:
            examined = current
        if current is None:
            self._unstored[key] = value
            return
        self._unstored.pop(key, None)
        if examined is not None:
            _remember_identity(examined, kind, value)
        if examined != current:
            # The file changed under the job: its result answers for this
            # selection only, or every redraw would start the job again.
            self._settled[key] = value
        else:
            self._settled.pop(key, None)

    def get(self, path, kind):
        """@brief The known result of `kind` on `path`, or None when still unknown.

        @param path File path.
        @param kind Job kind.
        @return The cached result for the file as it is now, else the one kept
            for a vanished file or for a file that changed during its job,
            else None.
        """
        found = _remembered(path, kind)
        if found is not None:
            return found
        key = self._key(path, kind)
        found = self._unstored.get(key)
        return found if found is not None else self._settled.get(key)

    def forget(self, path) -> None:
        """@brief Drop the results kept for `path`: a new selection examines it again.

        @param path File path ("" or None forgets nothing).
        """
        if path:
            name = self._key(path, "")[0]
            for kept in (self._unstored, self._settled):
                for key in [key for key in kept if key[0] == name]:
                    del kept[key]


#: Zero-width space: lets a long path or identifier wrap after a separator
#: instead of widening the details beyond the window.
_BREAK = "​"


def _breakable(text: str) -> str:
    """Allow a line break after path and identifier separators."""
    for separator in ("\\", "/", "_", "-", ", "):
        text = text.replace(separator, separator + _BREAK)
    return text


def _size_text(size_bytes: int) -> str:
    """Size in MB with one decimal, or kB for small files."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, round(size_bytes / 1024))} kB" if size_bytes else "0 kB"


class _Signals(QObject):
    """Carrier of a worker result back to the GUI thread."""

    ready = pyqtSignal(str, str, object)


class _Job(QRunnable):
    """Hash a file, or check it with the detector, off the GUI thread."""

    def __init__(self, kind: str, path: str):
        """@brief Prepare one job of kind "sha256" or "check" on `path`.

        @param kind "sha256" to hash the file, anything else to load it with
            the detector and read its classes.
        @param path Model file.
        """
        super().__init__()
        self.kind, self.path = kind, path
        self.signals = _Signals()
        #: Identity of the file when the job read it (set by `run`).
        self.examined = None

    def run(self):
        """@brief QRunnable override: compute and emit (kind, path, result).

        @note The file identity is taken before reading it, so the result is
            remembered for the content examined even if the file changes meanwhile.
        """
        self.examined = _identity(self.path)
        try:
            if self.kind == "sha256":
                result = yolo_models.sha256_of(self.path)
            else:
                from ..services.photo_yolo import validate_yolo_setup
                info = validate_yolo_setup(self.path)
                result = {"ok": True, "classes": list(info.get("model_classes") or []),
                          "missing": list(info.get("missing_classes") or []),
                          "order": info.get("class_order", "")}
        except Exception as exc:  # noqa: BLE001 - shown in the window
            result = ({"ok": False, "error": str(exc)} if self.kind == "check" else None)
        except BaseException as exc:  # noqa: BLE001 - see below
            # An exception escaping a QRunnable terminates the process (Qt,
            # without a traceback): not even a BaseException must escape from
            # here. The result is a visible error, as for Exception.
            result = ({"ok": False, "error": type(exc).__name__} if self.kind == "check"
                      else None)
        self.signals.ready.emit(self.kind, self.path, result)


class YoloModelDialog(QDialog):
    """@brief Choose the local YOLO weights for this session, seeing what each file is."""

    def __init__(self, current: str, models_dir, default: str = "", tr=None, parent=None):
        """@brief Build the list and select the model in use.

        @param current Path in the model field ("" = the default).
        @param models_dir Folder whose weights files are listed.
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
        self._results = _JobResults()
        #: Set by shutdown(): a closed window starts no more jobs.
        self._closed = False
        self.setWindowTitle(self._tr("Scegli il modello YOLO"))
        self.setMinimumSize(760, 440)

        intro = QLabel(tx("Modelli nella cartella {folder}. La scelta vale per questa "
                          "sessione.", folder=self._models_dir))
        intro.setWordWrap(True)
        intro.setProperty("i18n_skip", True)

        self.model_list = QListWidget()
        self.model_list.setMinimumWidth(260)
        self.model_list.currentItemChanged.connect(self._selection_changed)

        self.details = QLabel()
        self.details.setProperty("i18n_skip", True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setWordWrap(True)
        self.details.setMinimumWidth(1)
        self.details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # Long texts wrap (`_breakable`) instead of scrolling horizontally.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.addWidget(self.details)
        self.check_button = QPushButton(self._tr("Verifica compatibilità"))
        self.check_button.setToolTip(self._tr(
            "Carica il modello per leggerne le classi e controllare che il rilevatore "
            "possa usarlo. Un file .pt esegue codice quando viene caricato: verificalo "
            "solo se ti fidi della sua provenienza."))
        self.check_button.clicked.connect(self._check_selected)
        holder_layout.addWidget(self.check_button, 0, Qt.AlignmentFlag.AlignLeft)
        holder_layout.addStretch(1)
        scroll.setWidget(holder)

        body = QHBoxLayout()
        body.addWidget(self.model_list, 2)
        body.addWidget(scroll, 3)

        self.other_button = QPushButton(self._tr("Altro file..."))
        self.other_button.setToolTip(self._tr("Sceglie un modello .onnx o .pt fuori da "
                                              "questa cartella."))
        self.other_button.clicked.connect(self._choose_other)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            self._tr("Usa questo modello"))
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

    def _fill(self, select: str = "") -> None:
        """Rebuild the list and select `select`, else the model in use."""
        wanted = os.path.normcase(os.path.abspath(select or self._current)) if (
            select or self._current) else ""
        self.model_list.blockSignals(True)
        self.model_list.clear()
        chosen = None
        for item_file in yolo_models.list_models(self._models_dir, extra=self._extra):
            path = str(item_file.path)
            marks = []
            if self._same(path, self._current):
                marks.append(self._tr("in uso"))
            if self._default and self._same(path, self._default):
                marks.append(self._tr("predefinito"))
            text = item_file.path.name + (f"  ({', '.join(marks)})" if marks else "")
            text += f"\n{item_file.format} · {_size_text(item_file.size_bytes)}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.model_list.addItem(item)
            if wanted and os.path.normcase(os.path.abspath(path)) == wanted:
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

        @return The weights path stored in the current list item, or "".
        """
        item = self.model_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def _choose_other(self) -> None:
        """«Altro file...»: add a weights file from anywhere and select it."""
        selected, _ = QFileDialog.getOpenFileName(
            self, self._tr("Altro file..."), str(self._models_dir),
            self._tr("Modelli YOLO (*.onnx *.pt)"))
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

    def _job_ready(self, kind, path, result) -> None:
        done = [job for job in self._jobs if job.kind == kind and job.path == path]
        self._jobs = [job for job in self._jobs if job not in done]
        examined = done[0].examined if done else _NOT_GIVEN
        self._results.store(path, kind, result if kind == "check" else (result or ""),
                            examined=examined)
        if self._same(path, self.selected_path()):
            self._show_selected()

    def _check_selected(self) -> None:
        """«Verifica compatibilità»: load the selected model on request."""
        path = self.selected_path()
        if path and self._results.get(path, "check") is None and not self._running(path, "check"):
            self._start("check", path)
            self._show_selected()

    def _running(self, path, kind) -> bool:
        return any(job.kind == kind and self._same(job.path, path) for job in self._jobs)

    def _selection_changed(self, *_):
        """A really new selection: a file that failed before is examined again."""
        self._results.forget(self.selected_path())
        self._show_selected()

    def _show_selected(self, *_):
        """Draw the details of the selected file, starting what is still unknown."""
        if self._closed:
            return
        path = self.selected_path()
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if not path:
            self.details.setText(self._tr("Nessun modello .onnx o .pt in questa cartella: "
                                          "usa «Altro file...»."))
            self.check_button.hide()
            ok_button.setEnabled(False)
            return
        facts = yolo_models.describe_file(path)
        digest = self._results.get(path, "sha256")
        if digest is None and not self._running(path, "sha256"):
            self._start("sha256", path)
        check = self._results.get(path, "check")
        if (check is None and not facts.runs_code_when_loaded
                and not self._running(path, "check")):
            self._start("check", path)
        self.check_button.setVisible(check is None and facts.runs_code_when_loaded
                                     and not self._running(path, "check"))
        # A model the detector rejected cannot be chosen: it would fail at
        # analysis time, after everything else has been prepared.
        ok_button.setEnabled(not (check and not check.get("ok")))
        self.details.setText(self._details_html(facts, digest, check, path))

    def _details_html(self, facts, digest, check, path) -> str:
        from html import escape
        rows = []

        def row(label, value, note=""):
            extra = f"<br><span style='color:{YELLOW}'>{escape(note)}</span>" if note else ""
            rows.append(f"<tr><td style='padding:2px 14px 2px 0; color:{SUBTEXT}' "
                        f"valign='top'>{escape(label)}</td>"
                        f"<td>{escape(_breakable(value))}{extra}</td></tr>")

        row(self._tr("Formato"), facts.format)
        row(self._tr("Dimensione"), _size_text(facts.size_bytes))
        row(self._tr("Data"), facts.modified.strftime("%d/%m/%Y %H:%M") if facts.modified else "—")
        info = yolo_models.provenance_of(digest) if digest else None
        if digest is None:
            row(self._tr("Origine"), self._tr("Riconoscimento in corso…"))
        elif info is None:
            row(self._tr("Origine"), self._tr("Non dichiarata"),
                self._tr("Questo file non è fra i modelli documentati in THIRD_PARTY_NOTICES.md, "
                  "oppure è stato modificato: origine e licenza sono da verificare."))
        else:
            row(self._tr("Origine"), f"{info.title} — {info.source}")
            row(self._tr("Famiglia"), info.family)
            license_notes = {
                yolo_models.STATUS_TO_VERIFY: self._tr("Da verificare prima di pubblicare."),
                yolo_models.STATUS_UNRESOLVED: self._tr(
                    "Non risolta: le fonti non concordano; va trattata come AGPL-3.0 "
                    "finché non è chiarita."),
            }
            row(self._tr("Licenza"), info.license, license_notes.get(info.license_status, ""))
            dataset_note = (self._tr("Origine e licenza dei dati da confermare.")
                            if info.dataset_status != yolo_models.STATUS_CONFIRMED else "")
            row(self._tr("Dati di addestramento"), info.dataset or self._tr("Non dichiarati"), dataset_note)
        if check is None:
            if self._running(path, "check"):
                row(self._tr("Compatibilità"), self._tr("Verifica in corso…"))
            elif facts.runs_code_when_loaded:
                row(self._tr("Compatibilità"), self._tr("Da verificare"),
                    self._tr("Un file .pt esegue codice quando viene caricato: si verifica solo "
                      "su tua richiesta."))
        elif check.get("ok"):
            order = (self._tr("lette per nome") if check.get("order") == "remapped by name"
                     else self._tr("nell'ordine del rilevatore"))
            row(self._tr("Compatibilità"), tx("Compatibile: {count} classi, {order}.",
                                       count=len(check["classes"]), order=order))
            row(self._tr("Classi"), ", ".join(check["classes"]))
            if check.get("missing"):
                row(self._tr("Non riconosce"), ", ".join(check["missing"]))
        else:
            row(self._tr("Compatibilità"), self._tr("Non utilizzabile dal rilevatore"), check.get("error", ""))
        row(self._tr("Percorso"), path)
        return "<table>" + "".join(rows) + "</table>"

    def shutdown(self) -> None:
        """@brief Stop this window for good: no new jobs, no more job results.

        @note Called by done() and by the owner after exec() returns. Without
            it the list, emptied when the window is destroyed later (by the
            garbage collector, at an arbitrary moment), reselected an item and
            started a model check in the pool long after the window was gone
            (HANDOFF 131: a test crash, and a pointless load of the weights).
            The jobs already running finish in the pool and their results are
            kept for the session.
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
