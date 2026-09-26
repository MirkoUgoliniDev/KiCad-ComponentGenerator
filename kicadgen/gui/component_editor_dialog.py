"""@brief Hand editor of the bodies mounted on a module, in its own window.

@details The «Componenti sul modulo» table lived under the 3D view until the
parameter panels were removed (2026-09-26). The photo review needs the
calibrated photo in memory; this editor does not, so a body can always be
corrected by hand: name, type, position, size, rotation, color and marking.

The dialog works on copies of the stored photo components and returns the
edited list only on «Applica». Evidence tied to what was changed is dropped,
with the same rules the old table applied:

- a new length or width means the photo rectangle no longer describes the
  body: its geometric evidence (bbox basis, package evidence, terminal
  geometry) and the model match built on it are removed;
- a new type or a new rotation makes the chosen KiCad model a guess about
  another part or another orientation: the model match and selection are
  removed (the old table did the same on every combo change).

Position, height, color and marking keep the evidence: they move or paint the
same body.
"""
from copy import deepcopy

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QTableWidget, QVBoxLayout)

#: Column titles, Italian sources translated by the window's i18n pass.
COLUMNS = ("Nome", "Tipo", "X", "Y", "Lung.", "Larg.", "Alt.", "Rot.", "Colore", "Marcatura")
#: (column, key, minimum) of the numeric cells, in mm.
_NUMBERS = ((2, "x_mm", -300.0), (3, "y_mm", -300.0), (4, "length_mm", 0.05),
            (5, "width_mm", 0.05), (6, "height_mm", 0.05))
#: Keys that describe the photo rectangle; they lose meaning when it is resized.
_GEOMETRY_EVIDENCE = ("package_evidence", "terminal_geometry", "model_match", "model_selection")
#: Keys that describe the KiCad model chosen for the body type.
_MODEL_EVIDENCE = ("model_match", "model_selection")
#: A new body, as the old «Aggiungi componente» created it.
NEW_COMPONENT = dict(name="Componente", kind="generic", x_mm=0.0, y_mm=0.0, length_mm=2.0,
                     width_mm=1.2, height_mm=0.6, rotation_deg=0.0, color_hex="#303030",
                     marking="")


class ComponentEditorDialog(QDialog):
    """@brief Edit the mounted bodies by hand; emits the edited list on «Applica»."""

    #: The edited components (photo component dicts), emitted once on «Applica».
    components_applied = pyqtSignal(list)

    def __init__(self, components, kinds, parent=None, *, select_row=None):
        """@brief Build the table from copies of the components.

        @param components Stored photo components (dicts with x_mm, y_mm, ...).
        @param kinds Component types offered in the «Tipo» column.
        @param parent Owner window.
        @param select_row Optional row to select and scroll to (3D right click).
        """
        super().__init__(parent)
        self.setWindowTitle("Modifica componenti")
        self.setModal(True)
        self.resize(1100, 520)
        self._kinds = tuple(kinds)
        #: Copies of the input, one per row, in row order; None for added rows.
        self._originals = []
        #: Full dict of each row (detection metadata travels with the row).
        self._rows_data = []
        layout = QVBoxLayout(self)
        note = QLabel("Origine al centro del modulo, X a destra, Y in basso come nel footprint; "
                      "Lung. = asse lungo del componente, Rot. = come sta sul modulo. "
                      "Nessuna richiesta AI: dopo «Applica» il modello 3D si rigenera con FreeCAD.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        for component in components:
            self._add_row(deepcopy(component), original=True)
        buttons_row = QHBoxLayout()
        self.btn_add = QPushButton("Aggiungi componente")
        self.btn_add.clicked.connect(lambda: self._add_row(dict(NEW_COMPONENT), original=False))
        self.btn_remove = QPushButton("Rimuovi componente selezionato")
        self.btn_remove.clicked.connect(self._remove_selected)
        buttons_row.addWidget(self.btn_add)
        buttons_row.addWidget(self.btn_remove)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Applica")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if select_row is not None and 0 <= select_row < self.table.rowCount():
            self.table.selectRow(select_row)

    def _add_row(self, component: dict, *, original: bool) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._originals.append(deepcopy(component) if original else None)
        self.table.setCellWidget(row, 0, QLineEdit(str(component.get("name") or "")))
        kind = QComboBox()
        for key in self._kinds:
            kind.addItem(key, key)
        current = component.get("kind") or "generic"
        if kind.findData(current) < 0:
            kind.addItem(current, current)
        kind.setCurrentIndex(max(kind.findData(current), 0))
        self.table.setCellWidget(row, 1, kind)
        for column, key, minimum in _NUMBERS:
            value = float(component.get(key, NEW_COMPONENT[key]) or 0.0)
            spin = QDoubleSpinBox()
            # Sei decimali: le correzioni fotografiche dei piccoli package
            # stanno sotto il centesimo di millimetro.
            spin.setDecimals(6)
            spin.setRange(min(minimum, value), max(300.0, value))
            spin.setSingleStep(0.1)
            spin.setValue(value)
            self.table.setCellWidget(row, column, spin)
        rotation = QComboBox()
        for degrees in (0, 90, 180, 270):
            rotation.addItem(f"{degrees}°", float(degrees))
        angle = float(component.get("rotation_deg") or 0.0) % 360
        if rotation.findData(angle) < 0:
            rotation.addItem(f"{angle:g}°", angle)
        rotation.setCurrentIndex(max(rotation.findData(angle), 0))
        self.table.setCellWidget(row, 7, rotation)
        color = QLineEdit(str(component.get("color_hex") or "#303030"))
        color.setMaxLength(7)
        self.table.setCellWidget(row, 8, color)
        self.table.setCellWidget(row, 9, QLineEdit(str(component.get("marking") or "")))
        # I dati non mostrati (metadati del rilevamento) viaggiano con la riga.
        self._rows_data.append(component)

    def _remove_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.table.removeRow(row)
        del self._originals[row]
        del self._rows_data[row]

    def components(self) -> list:
        """@brief The components as the table shows them now, evidence adjusted.

        @return New dicts: detection metadata of each row plus its edited values.
        """
        result = []
        for row in range(self.table.rowCount()):
            entry = deepcopy(self._rows_data[row])
            original = self._originals[row]
            entry["name"] = self.table.cellWidget(row, 0).text().strip() or "Componente"
            entry["kind"] = self.table.cellWidget(row, 1).currentData() or "generic"
            for column, key, _minimum in _NUMBERS:
                entry[key] = self.table.cellWidget(row, column).value()
            entry["rotation_deg"] = float(self.table.cellWidget(row, 7).currentData() or 0.0)
            entry["color_hex"] = self.table.cellWidget(row, 8).text().strip() or "#303030"
            entry["marking"] = self.table.cellWidget(row, 9).text().strip()
            if original is not None:
                resized = any(abs(float(entry[key]) - float(original.get(key) or 0.0)) > 1e-9
                              for key in ("length_mm", "width_mm"))
                if resized:
                    entry["bbox_basis"] = "unknown"
                    for key in _GEOMETRY_EVIDENCE:
                        entry.pop(key, None)
                elif (entry["kind"] != (original.get("kind") or "generic")
                      or entry["rotation_deg"] != float(original.get("rotation_deg") or 0.0) % 360):
                    for key in _MODEL_EVIDENCE:
                        entry.pop(key, None)
            result.append(entry)
        return result

    def _apply(self) -> None:
        self.components_applied.emit(self.components())
        self.accept()
