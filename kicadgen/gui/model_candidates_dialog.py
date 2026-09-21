"""@brief Isolated, offline STEP candidate review with optional CAD thumbnails."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from PyQt6.QtCore import (
    QObject, QPointF, QRunnable, QSignalBlocker, Qt, QThreadPool, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QPushButton, QVBoxLayout,
)

from ..services.model_library_search import filter_models, library_models

#: Pause after the last keystroke before the library filter runs, so typing a
#: word does not filter thousands of rows once per letter.
FILTER_DEBOUNCE_MS = 250


def _number(value, default=0.0):
    try:
        result = float(value) if not isinstance(value, bool) else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _display_dimensions(value):
    """Round displayed mm to three decimals without altering stored evidence."""
    if isinstance(value, dict):
        return '; '.join(f'{key}: {_display_dimensions(item)}' for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return ' × '.join(_display_dimensions(item) for item in value)
    number = _number(value, None)
    if number is None:
        return '?'
    return f'{number:.3f}'.rstrip('0').rstrip('.') if round(number, 3) else '0'


def _step_file(path):
    try:
        return Path(path).suffix.lower() in ('.step', '.stp') and Path(path).is_file()
    except (TypeError, ValueError, OSError):
        return False


def _thumbnail(path, rotation, dimensions=None):
    """Tessellate STEP; automatic orientation uses the production CAD importer."""
    from ..cad3d.step_generator import find_freecadcmd

    executable = find_freecadcmd()
    if rotation is None:
        length, width = dimensions or (None, None)
        length, width = _number(length, None), _number(width, None)
        if length is None or width is None or min(length, width) <= 0:
            raise ValueError('Automatic orientation requires positive component dimensions')
        component = dict(model_path=str(path), length_mm=length, width_mm=width)
        # Use the actual placement helper so body-section / total-envelope
        # comparisons and tie-breaking cannot diverge from generated CAD.
        cad_directory = str(Path(__file__).resolve().parents[1] / 'cad3d')
        geometry_source = (
            f'sys.path.insert(0, {cad_directory!r})\n'
            'from freecad_parts import _library_model\n'
            f'parts = _library_model({component!r}, 0, 0, 0, "#b0c7db")\n'
            'if not parts:\n'
            '    raise ValueError("Automatic model placement unavailable")\n'
            'shape = Part.makeCompound([shape for shape, _ in parts])\n')
        rotation = 0.0  # _library_model already applied inferred native rotation.
    else:
        geometry_source = f'shape = Part.read({str(path)!r})\n'
    with tempfile.TemporaryDirectory(prefix='kicad-candidate-') as directory:
        output = Path(directory) / 'mesh.json'
        script = Path(directory) / 'preview.py'
        # Paths are encoded as Python literals, never interpolated into shell code.
        script.write_text(
            'import Part, json, sys\n' + geometry_source +
            'vertices, triangles = shape.tessellate(0.15)\n'
            f'with open({str(output)!r}, "w") as stream:\n'
            '    json.dump({"vertices": [[v.x,v.y,v.z] for v in vertices], '
            '"triangles": triangles}, stream)\n', encoding='utf-8')
        subprocess.run([executable, str(script)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=25, check=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if output.stat().st_size > 20_000_000:
            raise ValueError('Preview mesh too large')
        mesh = json.loads(output.read_text(encoding='utf-8'))
    vertices, triangles = mesh['vertices'], mesh['triangles']
    if not vertices or not triangles or len(triangles) > 100_000:
        raise ValueError('Preview geometry unavailable or too complex')
    angle = math.radians(rotation)
    c, s = math.cos(angle), math.sin(angle)
    points = []
    for x, y, z in vertices:
        x, y = c*x-s*y, s*x+c*y
        point = ((x-y)*0.7071, (x+y)*0.4082-z*0.8165, (x+y+z)*0.5774)
        if not all(math.isfinite(v) for v in point):
            raise ValueError('Invalid mesh coordinates')
        points.append(point)
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    scale = 280 / max(xmax-xmin, ymax-ymin, 0.001)
    image = QImage(340, 340, QImage.Format.Format_RGB32)
    image.fill(QColor('#202431'))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor('#60758b'))
        for face in sorted(triangles, key=lambda f: sum(points[i][2] for i in f)):
            if len(face) != 3:
                raise ValueError('Invalid mesh triangle')
            painter.setBrush(QColor('#b0c7db'))
            painter.drawPolygon(QPolygonF([
                QPointF(170+(points[i][0]-(xmin+xmax)/2)*scale,
                        170+(points[i][1]-(ymin+ymax)/2)*scale) for i in face]))
    finally:
        painter.end()
    return image


class _Signals(QObject):
    ready = pyqtSignal(int, object)


class _Preview(QRunnable):
    def __init__(self, token, path, rotation, dimensions=None):
        super().__init__()
        self.signals = _Signals()
        self.token, self.path, self.rotation = token, path, rotation
        self.dimensions = dimensions

    def run(self):
        """@brief Generate an optional thumbnail without blocking the Qt event loop."""
        try:
            result = (_thumbnail(self.path, None, self.dimensions) if self.rotation is None
                      else _thumbnail(self.path, self.rotation))
        except Exception:
            result = None
        self.signals.ready.emit(self.token, result)


class _Index(QRunnable):
    """@brief Walk the installed KiCad 3D libraries off the GUI thread."""

    def __init__(self, token):
        """@brief Prepare one library walk identified by a dialog token.

        @param token Value the dialog compares against its current index token.
        """
        super().__init__()
        self.signals = _Signals()
        self.token = token

    def run(self):
        """@brief Build the model index; a failed walk yields an empty index."""
        try:
            result = list(library_models())
        except Exception:
            result = []
        self.signals.ready.emit(self.token, result)


class ModelCandidatesDialog(QDialog):
    """@brief Review at most five ranked suggestions and confirm one STEP model.

    @details Besides the ranked candidates, a KiCad-style filter over the whole
    installed 3D library lists ``library/stem`` rows for typed fragments; a row
    chosen there is confirmed exactly like a manually chosen file. The library
    index is built once per process, off the GUI thread, on the first query.
    @note Inputs are read-only. Apply selected_selection() only after Accepted;
    Accepted with rejected status means preserve the generic placeholder.
    Rotation is a local native-model Z override in degrees, or None to retain
    CAD bounding-box axis inference. Explicit zero disables that inference.
    """

    def __init__(self, component: dict, match_report: dict, photo_crop=None, parent=None):
        """@brief Build an independent candidate review dialog.

        @param component Component metadata, dimensions in mm and optional
        model_selection. Saved selection rotation takes precedence over
        model_rotation_deg, including None for automatic axis inference.
        @param match_report Ranked candidates with path, package, dimensions_mm,
        relative_xy_error and reasons; the first five distinct paths are shown.
        @param photo_crop Optional QImage, QPixmap, encoded image bytes or image path.
        @param parent Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle('Scelta del modello STEP')
        self.resize(820, 650)
        self._selection = dict(status='rejected', path='', rotation_deg=0.0)
        self._token = 0
        self._closed = False
        self._workers = {}
        self._dimensions = (component.get('length_mm'), component.get('width_mm'))
        saved = component.get('model_selection')
        saved = saved if isinstance(saved, dict) else {}
        saved_path = str(saved.get('path') or '')
        if not saved_path and match_report.get('status') in ('auto_matched', 'provisional_matched'):
            saved_path = str(match_report.get('selected_path') or '')
        initial_rotation = saved.get('rotation_deg', component.get('model_rotation_deg'))
        self._candidates = []
        seen = set()
        for candidate in match_report.get('candidates', []) or []:
            if not isinstance(candidate, dict):
                continue
            path = str(candidate.get('path') or '')
            if not path or path in seen:
                continue
            seen.add(path)
            self._candidates.append(dict(candidate, path=path))
            if len(self._candidates) == 5:
                break
        if saved_path and not any(c['path'] == saved_path for c in self._candidates):
            existing = next((dict(c) for c in match_report.get('candidates', []) or []
                             if isinstance(c, dict) and str(c.get('path')) == saved_path),
                            dict(path=saved_path, package=f'Modello attuale: {Path(saved_path).name}'))
            self._candidates = [existing] + self._candidates[:4]
        layout = QVBoxLayout(self)
        title = QLabel(f"{component.get('name', 'Componente')} — dimensioni stimate L × W × H (mm): "
                       f"{_display_dimensions([component.get('length_mm'), component.get('width_mm'), component.get('height_mm')])}")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        images = QHBoxLayout()
        self.photo_label = QLabel('Ritaglio fotografico non disponibile')
        self.preview_label = QLabel('Seleziona un candidato per visualizzare la geometria STEP reale')
        for label in (self.photo_label, self.preview_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumSize(340, 220)
            label.setWordWrap(True)
            images.addWidget(label)
        layout.addLayout(images)
        self._set_photo(photo_crop)
        # Library filter: the whole installed library, filtered KiCad-style by
        # typed fragments, so a model outside the five candidates needs no
        # folder browsing. The index is built lazily, on the first keystroke.
        self._library_models = None
        self._library_results = []
        self._index_token = 0
        self._index_worker = None
        filter_row = QHBoxLayout()
        self.library_filter = QLineEdit()
        self.library_filter.setObjectName('library_filter')
        self.library_filter.setPlaceholderText('Filtra la libreria KiCad: es. wroom usb-c sot23…')
        self.library_filter.setClearButtonEnabled(True)
        filter_row.addWidget(self.library_filter, 1)
        self.library_count_label = QLabel('')
        self.library_count_label.setTextFormat(Qt.TextFormat.PlainText)
        filter_row.addWidget(self.library_count_label)
        layout.addLayout(filter_row)
        self.library_list = QListWidget()
        self.library_list.setObjectName('library_list')
        self.library_list.setMaximumHeight(150)
        self.library_list.setVisible(False)
        layout.addWidget(self.library_list)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_library_filter)
        self.library_filter.textChanged.connect(self._filter_text_changed)
        self.library_list.currentRowChanged.connect(self._library_row_changed)
        self.candidate_list = QListWidget()
        for candidate in self._candidates:
            self.candidate_list.addItem(str(candidate.get('package') or Path(candidate['path']).name))
        layout.addWidget(self.candidate_list)
        self.details_label = QLabel('Scegli un candidato, filtra la libreria KiCad, scegli un file STEP oppure conserva il segnaposto.')
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details_label)
        controls = QHBoxLayout()
        self.auto_rotation = QCheckBox('Automatico: deduci gli assi CAD dall’ingombro')
        self.auto_rotation.setChecked(_number(initial_rotation, None) is None)
        layout.addWidget(self.auto_rotation)
        self.rotation_hint = QLabel()
        self.rotation_hint.setWordWrap(True)
        layout.addWidget(self.rotation_hint)
        controls.addWidget(QLabel('Rotazione Z locale del CAD nativo'))
        self.rotation = QDoubleSpinBox()
        self.rotation.setRange(-360, 360)
        self.rotation.setDecimals(2)
        self.rotation.setSingleStep(90)
        self.rotation.setSuffix(' °')
        self.rotation.setValue(_number(initial_rotation))
        self._update_rotation_controls()
        controls.addWidget(self.rotation)
        self.manual_button = QPushButton('Scegli file STEP…')
        self.manual_button.clicked.connect(self._choose_file)
        controls.addWidget(self.manual_button)
        self.none_button = QPushButton('Nessuno corrisponde — conserva il segnaposto')
        self.none_button.clicked.connect(self._none_match)
        controls.addWidget(self.none_button)
        layout.addLayout(controls)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Usa il modello selezionato')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Annulla')
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.candidate_list.currentRowChanged.connect(self._candidate_changed)
        self.rotation.valueChanged.connect(self._start_preview)
        self.auto_rotation.toggled.connect(self._rotation_mode_changed)
        if saved_path:
            row = next(i for i, c in enumerate(self._candidates) if c['path'] == saved_path)
            self.candidate_list.blockSignals(True)
            self.candidate_list.setCurrentRow(row)
            self.candidate_list.blockSignals(False)
            self._candidate_changed(row, preview=False)

    def showEvent(self, event):
        """@brief Start the restored selection preview only once the dialog is shown.

        @param event Qt show event.
        @note Construction never schedules CAD work; callers may replace preview
        hooks before showing the dialog, including in offscreen tests.
        """
        super().showEvent(event)
        if getattr(self, '_path', '') and not self._workers:
            self._start_preview()

    def _update_rotation_controls(self):
        automatic = self.auto_rotation.isChecked()
        self.rotation.setEnabled(not automatic)
        self.rotation_hint.setText(
            'L’anteprima usa lo stesso allineamento automatico della generazione CAD '
            '(ingombro totale e sezione del corpo). La rotazione nella foto è indipendente.' if automatic else
            'Override locale esplicito: anche 0° disattiva l’inferenza degli assi. '
            'La rotazione del componente nella foto resta indipendente.')

    def _rotation_mode_changed(self, *_):
        self._update_rotation_controls()
        self._start_preview()

    def _set_photo(self, crop):
        pixmap = QPixmap()
        if isinstance(crop, QPixmap):
            pixmap = crop
        elif isinstance(crop, QImage):
            pixmap = QPixmap.fromImage(crop)
        elif isinstance(crop, (bytes, bytearray)):
            pixmap.loadFromData(bytes(crop))
        elif isinstance(crop, (str, os.PathLike)):
            pixmap.load(str(crop))
        if not pixmap.isNull():
            self.photo_label.setPixmap(pixmap.scaled(340, 340, Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))

    def _clear_selection(self, widget):
        """Deselect a list without re-entering its row handler."""
        blocker = QSignalBlocker(widget)
        try:
            widget.setCurrentRow(-1)
        finally:
            blocker.unblock()

    def _filter_text_changed(self, *_):
        # Coalesce keystrokes; an emptied field clears immediately so the
        # candidate list is not hidden behind stale rows for a quarter second.
        if self.library_filter.text().strip():
            self._filter_timer.start()
        else:
            self._filter_timer.stop()
            self._apply_library_filter()

    def _apply_library_filter(self, *_):
        """Fill the library list for the current query, indexing on first use."""
        query = self.library_filter.text().strip()
        if self._closed:
            return
        if not query:
            self._show_library_results([], 0, '')
            return
        if self._library_models is None:
            self.library_count_label.setText('Indicizzazione della libreria…')
            self._start_index()
            return
        if not self._library_models:
            self._show_library_results([], 0, 'libreria KiCad non trovata')
            return
        result = filter_models(self._library_models, query)
        if result.truncated:
            text = f'primi {len(result)} di {result.total}'
        elif result.total == 0:
            text = 'nessun modello'
        elif result.total == 1:
            text = '1 modello'
        else:
            text = f'{result.total} modelli'
        self._show_library_results(list(result), result.total, text)

    def _show_library_results(self, models, total, text):
        """Replace the library rows; the current row is intentionally dropped.

        A path already chosen from the library stays selected in the dialog
        state, exactly like a manually chosen file survives a cancelled picker.
        """
        blocker = QSignalBlocker(self.library_list)
        try:
            self.library_list.clear()
            self._library_results = list(models)
            for model in self._library_results:
                self.library_list.addItem(model.label)
        finally:
            blocker.unblock()
        self.library_list.setVisible(bool(self._library_results))
        self.library_count_label.setText(text)

    def _start_index(self):
        """Walk the library at most once per dialog, never on the GUI thread."""
        if self._index_worker is not None or self._closed:
            return
        self._index_token += 1
        worker = _Index(self._index_token)
        self._index_worker = worker
        worker.signals.ready.connect(self._index_ready)
        QThreadPool.globalInstance().start(worker)

    def _index_ready(self, token, models):
        self._index_worker = None
        if self._closed or token != self._index_token:
            return
        self._library_models = list(models)
        self._apply_library_filter()

    def _library_row_changed(self, row):
        if row < 0 or row >= len(self._library_results):
            return
        self._clear_selection(self.candidate_list)
        model = self._library_results[row]
        self._path = str(model.path)
        self.details_label.setText(f'Dalla libreria KiCad: {self._path}\n'
                                   'Dimensioni e motivi di corrispondenza non disponibili.')
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(_step_file(self._path))
        self._start_preview()

    def _candidate_changed(self, row, preview=True):
        if row < 0:
            return
        self._clear_selection(self.library_list)
        candidate = self._candidates[row]
        self._path = candidate['path']
        reasons = candidate.get('reasons') or []
        if isinstance(reasons, (list, tuple)):
            reasons = '\n'.join(str(reason) for reason in reasons)
        error = _number(candidate.get('relative_xy_error'), float('nan'))
        error_text = f'{error:.1%}' if math.isfinite(error) else 'non disponibile'
        self.details_label.setText(f"{self._path}\nDimensioni del modello (mm): {_display_dimensions(candidate['dimensions_mm']) if candidate.get('dimensions_mm') else 'non disponibili'}"
                                   f"\nErrore relativo XY: {error_text}\nMotivi: {reasons or 'non disponibili'}")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(_step_file(self._path))
        if preview:
            self._start_preview()

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Scegli modello STEP', '', 'Modelli STEP (*.step *.stp *.STEP *.STP)')
        if not path:
            return
        self.candidate_list.setCurrentRow(-1)
        self._clear_selection(self.library_list)
        self._path = path
        self.details_label.setText(f'STEP manuale: {path}\nDimensioni e motivi di corrispondenza non disponibili.')
        self._start_preview()

    def _start_preview(self, *_):
        self._token += 1
        valid = _step_file(getattr(self, '_path', ''))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)
        self.preview_label.clear()
        self.preview_label.setText('Caricamento della geometria STEP reale…' if valid else 'File STEP non disponibile')
        # At most one process per dialog. Changes coalesce to the latest selection.
        if not valid or self._closed or self._workers:
            return
        worker = _Preview(self._token, self._path,
                          None if self.auto_rotation.isChecked() else self.rotation.value(),
                          self._dimensions)
        self._workers[self._token] = worker
        worker.signals.ready.connect(self._preview_ready)
        QThreadPool.globalInstance().start(worker)

    def _preview_ready(self, token, image):
        self._workers.pop(token, None)
        if self._closed:
            return
        if token != self._token:
            self._start_preview()
            return
        if isinstance(image, QImage) and not image.isNull():
            self.preview_label.setPixmap(QPixmap.fromImage(image))
        else:
            self.preview_label.setText('Anteprima della geometria reale non disponibile.\nPuoi scegliere il file STEP oppure conservare il segnaposto.')

    def _none_match(self):
        self._selection = dict(status='rejected', path='', rotation_deg=0.0)
        self.done(QDialog.DialogCode.Accepted)

    def accept(self):
        """@brief Confirm an existing STEP path and the selected native-model rotation."""
        if _step_file(getattr(self, '_path', '')):
            self._selection = dict(status='confirmed', path=self._path,
                                   rotation_deg=None if self.auto_rotation.isChecked() else float(self.rotation.value()))
            super().accept()

    def done(self, result):
        """@brief Close without waiting for CAD; discard late previews and cancelled choices.

        @param result Qt Accepted or Rejected dialog result.
        """
        self._closed = True
        self._token += 1
        self._index_token += 1
        self._filter_timer.stop()
        if result == QDialog.DialogCode.Rejected:
            self._selection = dict(status='rejected', path='', rotation_deg=0.0)
        super().done(result)

    def selected_selection(self) -> dict:
        """@brief Return a detached selection without changing either constructor input.

        @return Dict with status confirmed/rejected, path string and rotation_deg
        float for an explicit local override, or None for automatic CAD axis inference.
        @note Cancel returns rejected; callers must check exec() == Accepted before applying.
        """
        return dict(self._selection)
