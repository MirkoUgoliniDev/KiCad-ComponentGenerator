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
same body. A height that is an estimate (`component_state.height_is_estimated`)
is marked «stimata» in its cell; changing it by hand records it as the
user's reading (`component_state.mark_height_measured`).
"""
from copy import deepcopy

from PyQt6.QtCore import pyqtSignal

from .component_state import height_is_estimated, mark_height_measured, mark_height_placeholder
from ..services.smd_component_classifier import drop_classifier_identity
from .theme import spin_box_width_for_values
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
                      "Nessuna richiesta AI: dopo «Applica» il modello STEP si rigenera con FreeCAD.")
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
        self.btn_add.clicked.connect(self._add_new_row)
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
            # Six decimals: the photographic corrections of small packages
            # are below a hundredth of a millimetre.
            spin.setDecimals(6)
            spin.setRange(min(minimum, value), max(300.0, value))
            spin.setSingleStep(0.1)
            spin.setValue(value)
            if key == "height_mm" and height_is_estimated(component):
                # Same cell, but it reads as an estimate and not a measurement.
                spin.setSuffix(" ~")
                tip = "Altezza stimata, non misurata: correggila se la conosci."
                i18n = getattr(self.parent(), "i18n", None)
                spin.setToolTip(i18n.text(tip) if i18n is not None else tip)
                spin.setStyleSheet("color: #f9e2af;")
            self.table.setCellWidget(row, column, spin)
            self._fit_column_to(column, spin)
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
        # Data not shown (detection metadata) travel with the row.
        self._rows_data.append(component)

    def _fit_column_to(self, column: int, spin: QDoubleSpinBox) -> None:
        """Widen a numeric column so the cell shows its widest value whole.

        The default section width (about 100 px) left the six-decimal values
        clipped once the style sheet's button column was drawn («1,600000»
        read «1,6000», HANDOFF 138, C3). The width is measured on the spin box
        itself (`spin_box_width_for_values`: range, decimals, suffix and the
        chrome of the current style), so another font, style or range needs no
        change here. The column only grows: a later row with a narrower range
        must not clip an earlier one, and a column the user widened stays so.
        """
        # The grid line is drawn inside the section: the cell is 1 px narrower.
        needed = spin_box_width_for_values(spin) + (1 if self.table.showGrid() else 0)
        if self.table.columnWidth(column) < needed:
            self.table.setColumnWidth(column, needed)

    def _add_new_row(self) -> None:
        """Add a body by hand; its height is a placeholder until typed."""
        component = dict(NEW_COMPONENT)
        mark_height_placeholder(component)
        self._add_row(component, original=False)

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
            # The cell showed the component height, or the new component's
            # one if missing: a different value was typed by hand.
            before = (original if original is not None else {}).get(
                "height_mm", NEW_COMPONENT["height_mm"])
            if abs(float(entry["height_mm"]) - float(before or 0.0)) > 1e-9:
                mark_height_measured(entry)
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
                # The body the SMD network saw is no longer this one: type,
                # size, position or rotation changed by hand remove its
                # identity and its pose, which would otherwise choose model
                # and orientation in place of the correction (audit 2026-10-02).
                if resized or any(
                        entry[key] != original.get(key, entry[key])
                        for key in ("x_mm", "y_mm")) or (
                        entry["kind"] != (original.get("kind") or "generic")) or (
                        entry["rotation_deg"] != float(original.get("rotation_deg") or 0.0) % 360):
                    drop_classifier_identity(entry)
            result.append(entry)
        return result

    def _apply(self) -> None:
        self.components_applied.emit(self.components())
        self.accept()
